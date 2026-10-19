"""Native-messaging host: the browser-side half of a recording.

The SITE Collector extension calls ``chrome.runtime.connectNative(
"com.sitebench.collector")``; the browser then starts THIS process (via the
small wrapper script ``setup`` registers) and speaks the native-messaging
protocol over stdin/stdout: each message is a 4-byte native-endian length
followed by that many bytes of UTF-8 JSON.

Everything the extension captures arrives here and is written into the
ACTIVE session's folder — the same files and formats as the in-repo
``site_bench.record.extension_host`` (so the server pipeline consumes the
sessions unchanged):

    archive/session.warc.gz       request/response records (+ SiteBench headers)
    browser_screenshots/*.png     one per captured event, ``<ts>_<event>.png``
    events.jsonl                  event metadata (screenshot payload stripped)
    tab_timeline.jsonl            tab activations / window focus
    dom_snapshots/                rendered-DOM HTML + index.jsonl
    layout_snapshots/             CDP DOMSnapshot (boxes + styles), .json.gz + index.jsonl
    websocket_frames.jsonl        WebSocket frames (both directions), socket events, SSE messages
    downloads/ uploads/ local_files/   user files + manifests
    live_stats.json  status.json  extension_host.log

Which session? The extension may connect at any time (it retries while idle,
and the participant's normal browser may even have the store build of the
extension). The host serves a connection only when ``active_session.json``
names a live session whose recorder process is alive and the connecting
browser is the dedicated recording profile; otherwise it tells the extension
to stand down and exits within milliseconds.

Differences from the in-repo host (deliberate): no live captioning (done
server-side now), PIL instead of OpenCV for the rare JPEG screenshot, a
per-session host lock so two host processes never append to the same files,
counters/numbering that survive host restarts, and stdout reserved strictly
for the protocol.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import re
import shutil
import struct
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter
    WARCIO_AVAILABLE = True
except ImportError:  # pragma: no cover - warcio is a hard dependency
    WARCIO_AVAILABLE = False

from collections import OrderedDict

from . import __version__
from .fsutil import FileLock, atomic_write_text
from .fsutil import read_json as _read_json
from .redact import Redactor, load_or_create_key

# Frames on the wire: 4-byte native-endian length + UTF-8 JSON. Chrome caps
# extension->host messages at 64 MiB; anything bigger is a framing error.
_MAX_MESSAGE = 64 * 1024 * 1024

# The recorder's old live dashboard ran on these origins; the extension still
# filters them, and so do we (harmless when nothing runs there).
DASHBOARD_ORIGINS = ("http://localhost:8501", "http://127.0.0.1:8501")


def _is_dashboard(url: str) -> bool:
    if not url:
        return False
    for origin in DASHBOARD_ORIGINS:
        if url == origin or url.startswith((origin + "/", origin + "?", origin + "#")):
            return True
    return False


# ---------------------------------------------------------------------------
# Protocol I/O
# ---------------------------------------------------------------------------

class Channel:
    """Length-prefixed JSON over the browser's pipes.

    The protocol stream is a PRIVATE duplicate of fd 1; ``sys.stdout`` is then
    pointed at stderr, so a stray ``print`` (ours or a library's) can never
    inject bytes into the protocol and desynchronize the extension."""

    def __init__(self) -> None:
        if sys.platform == "win32":
            # Windows opens inherited pipes in text mode (\n -> \r\n, ^Z = EOF),
            # fatal for the binary length prefix and payloads.
            import msvcrt
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        self._in = sys.stdin.buffer
        self._out = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
        sys.stdout = sys.stderr
        self._lock = threading.Lock()

    def read(self):
        """Next message, or None at EOF. Raises on a broken stream."""
        raw = self._in.read(4)
        if not raw:
            return None
        if len(raw) < 4:
            raise EOFError("truncated length prefix")
        (n,) = struct.unpack("@I", raw)
        if n > _MAX_MESSAGE:
            raise ValueError(f"message length {n} exceeds limit (stream desynchronized)")
        body = self._in.read(n)
        if len(body) < n:
            raise EOFError("truncated message body")
        return json.loads(body.decode("utf-8", errors="replace"))

    def send(self, message: dict) -> None:
        data = json.dumps(message).encode("utf-8")
        with self._lock:
            self._out.write(struct.pack("@I", len(data)) + data)
            self._out.flush()


# ---------------------------------------------------------------------------
# Session gating
# ---------------------------------------------------------------------------

def _pid_alive(pid: int, create_time: float | None = None) -> bool:
    if not pid:
        return False
    try:
        import psutil
        p = psutil.Process(pid)
        if create_time and abs(p.create_time() - float(create_time)) > 2.0:
            return False  # pid was reused by an unrelated process
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def _connecting_browser_profile() -> str | None:
    """The --user-data-dir of the browser that spawned us, if determinable.

    Returns "" for a browser running its DEFAULT profile (no flag), None when
    the parent chain could not be inspected (then we do not block)."""
    try:
        import psutil
        p = psutil.Process(os.getpid())
        for _ in range(6):
            p = p.parent()
            if p is None:
                return None
            name = (p.name() or "").lower()
            if any(b in name for b in ("chrome", "msedge", "microsoft edge", "chromium")):
                cmd = p.cmdline()
                for a in cmd:
                    if a.startswith("--user-data-dir="):
                        return a.split("=", 1)[1].strip('"')
                return ""
    except Exception:
        return None
    return None


def resolve_session(app_dir: Path) -> tuple[Path | None, str]:
    """(session_dir, "") if this spawn should record, else (None, reason)."""
    marker = _read_json(app_dir / "active_session.json")
    if not marker:
        return None, "no active session"
    session_dir = Path(marker.get("session_dir", ""))
    if not session_dir.is_dir():
        return None, f"active session folder missing: {session_dir}"
    if marker.get("state") not in ("starting", "recording"):
        return None, f"session state is {marker.get('state')!r}"
    if (session_dir / ".host_done").exists() and marker.get("state") != "recording":
        return None, "session already finished"
    for stop_file in ("stop.request",):
        if (session_dir / stop_file).exists():
            return None, "session is stopping"
    if not _pid_alive(int(marker.get("recorder_pid") or 0), marker.get("recorder_started")):
        return None, "recorder process is not running"
    want = marker.get("profile_dir")
    got = _connecting_browser_profile()
    if want and got is not None and _norm(got or "<default>") != _norm(want):
        return None, f"connection from a different browser profile ({got or 'default profile'})"
    # Fail-open when the parent chain cannot be inspected (logged by main).
    return session_dir, "" if got is not None else "browser profile not determinable"


# ---------------------------------------------------------------------------
# Per-session host lock (one writer per session folder)
# ---------------------------------------------------------------------------

class HostLock(FileLock):
    """Exclusive OS-level lock on ``<session>/.host.lock``.

    When the extension's service worker restarts it reconnects and the
    browser spawns a NEW host while the previous one may still be flushing.
    Two writers appending to the same gzip WARC would corrupt it, so the new
    host waits for the old one to let go."""


# ---------------------------------------------------------------------------
# The host
# ---------------------------------------------------------------------------

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class ExtensionHost:
    def __init__(self, session_dir: Path, channel: Channel, app_dir: Path | None = None):
        self.session_dir = Path(session_dir)
        self.ch = channel
        self.app_dir = Path(app_dir) if app_dir else None
        # Credential headers / secret-named body fields / token URL params are
        # replaced by keyed hashes before anything is written (see redact.py).
        self.redactor = Redactor(load_or_create_key(self.app_dir))
        self.archive_dir = self.session_dir / "archive"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.warc_path = self.archive_dir / "session.warc.gz"
        self._warned_stale_jpeg = False
        # Requests are HELD until their response arrives (the extension sends
        # a response only at loadingFinished) so the WARC gets each request
        # immediately followed by its response — the pipeline pairs a
        # response with the request record right before it. Keyed by
        # tab|session|requestId; bounded (a long session sees millions).
        self._pending_requests: OrderedDict = OrderedDict()
        # Same key -> tagging/timing of the request, kept after it is written
        # so a late response can still be tagged and timed.
        self._req_meta: OrderedDict = OrderedDict()
        self._stop = threading.Event()
        self._io_lock = threading.Lock()   # status.json written from two threads
        # Serializes session writes between the reader loop and a forced
        # shutdown from the control thread (see _stop_and_exit_soon).
        self._write_lock = threading.Lock()
        self._exit_reason = "eof"
        self._done = False
        self.last_message_at = 0.0
        self.messages = 0

        # Counters CONTINUE across host restarts (a service-worker restart
        # spawns a new host mid-session; starting from zero would overwrite
        # the live stats with smaller numbers).
        self.stats = {"screenshots": 0, "clicks": 0, "inputs": 0, "scrolls": 0}
        prev = _read_json(self.session_dir / "live_stats.json")
        if isinstance(prev, dict):
            for k in self.stats:
                try:
                    self.stats[k] = int(prev.get(k, 0))
                except (TypeError, ValueError):
                    pass

        self.warc_file = None
        self.warc_writer = None
        self._committed_path = self.session_dir / ".warc_committed"
        self._committed = 0
        self._committed_written_at = 0.0
        if WARCIO_AVAILABLE:
            # A previous host may have been killed mid-record (the browser
            # terminates its host ~2s after a port drop): cut any partial
            # gzip member off the end before appending, or every record after
            # it would be unreadable.
            _repair_warc_tail(self.warc_path, _read_int(self._committed_path))
            # APPEND, never truncate: each host adds its own gzip members and
            # warcio reads the concatenation as one archive. Unbuffered, so
            # bytes of a record whose write failed can be cut off exactly.
            self.warc_file = open(self.warc_path, "ab", buffering=0)
            self.warc_writer = WARCWriter(self.warc_file, gzip=True)
            self._committed = self.warc_file.tell()
            logging.info(f"WARC writer appending to {self.warc_path}")
        else:
            logging.error("warcio not importable — network capture disabled")

    # -- lifecycle -------------------------------------------------------------
    def run(self) -> None:
        logging.info(f"Native messaging host started (site-recorder {__version__}, pid {os.getpid()}).")
        status = _read_json(self.session_dir / "status.json") or {}
        paused = bool(status.get("paused"))
        # session_id + paused let the extension tell a RECONNECT (same session:
        # keep the participant's pause) from a new session.
        hello = {"type": "connected", "session_id": self.session_dir.name,
                 "paused": paused, "host_version": __version__}
        marker = _read_json(self.app_dir / "active_session.json") if self.app_dir else None
        if isinstance(marker, dict) and isinstance(marker.get("skip_domains"), list):
            hello["skip_domains"] = [str(d) for d in marker["skip_domains"]][:200]
        self.ch.send(hello)
        if paused:
            # Re-assert explicitly: extension builds before 1.8.0 (the Web
            # Store 1.7.1) ignore "paused" in the handshake and would silently
            # resume capture after a reconnect.
            self.ch.send({"type": "set_paused", "paused": True})
        self._touch_heartbeat()
        threading.Thread(target=self._keepalive_loop, daemon=True).start()
        threading.Thread(target=self._control_poll_loop, daemon=True).start()
        errors = 0
        try:
            while True:
                try:
                    msg = self.ch.read()
                except (EOFError, OSError, ValueError, struct.error) as e:
                    logging.warning(f"protocol stream ended: {e}")
                    break
                if msg is None:
                    logging.info("Stdin closed, exiting.")
                    break
                if isinstance(msg, dict) and msg.get("type") == "stopped":
                    # The extension acknowledged "stop": everything it sent
                    # before this ack has been handled (the port is FIFO).
                    logging.info("extension acknowledged stop")
                    break
                self.messages += 1
                self.last_message_at = time.time()
                try:
                    with self._write_lock:
                        if self._done:
                            break
                        self.handle_message(msg)
                    errors = 0
                except Exception as e:  # one bad message must not end the session
                    errors += 1
                    logging.exception(f"error handling {msg.get('type') if isinstance(msg, dict) else '?'}: {e}")
                    if errors > 200:
                        logging.error("too many consecutive handler errors — stopping host")
                        break
        finally:
            self._finish()

    def _finish(self) -> None:
        """Flush + close everything once; write .host_done. Idempotent."""
        with self._write_lock:
            if self._done:
                return
            self._done = True
            self._stop.set()
            # Requests that never got a response (failed, aborted, session end).
            try:
                while self._pending_requests:
                    _, req = self._pending_requests.popitem(last=False)
                    self._write_warc_request(req)
            except Exception as e:
                logging.warning(f"flushing pending requests failed: {e}")
            if self.warc_file:
                try:
                    self.warc_file.flush()
                    self._committed = self.warc_file.tell()
                    self.warc_file.close()
                except Exception:
                    pass
                self._write_committed(force=True)
            try:
                (self.session_dir / ".host_done").write_text(json.dumps(
                    {"pid": os.getpid(), "reason": self._exit_reason, "ts": time.time()}))
            except Exception:
                pass
            logging.info(f"Host exiting cleanly ({self._exit_reason}).")

    def _write_committed(self, force: bool = False) -> None:
        """Remember the WARC length that ends on a complete record (at most
        once a second; always on finish)."""
        now = time.time()
        if not force and now - self._committed_written_at < 1.0:
            return
        self._committed_written_at = now
        try:
            atomic_write_text(self._committed_path, str(self._committed))
        except OSError:
            pass

    def _stop_and_exit_soon(self, grace: float = 8.0) -> None:
        """Ask the extension to end the session. A current extension detaches,
        then sends "stopped" after everything it had queued (run() handles the
        queue, then exits); an older build closes the port instead. If neither
        happens within ``grace`` seconds, flush and exit anyway rather than
        lingering as a zombie that holds the extension's only port."""
        self._exit_reason = "stop"
        try:
            self.ch.send({"type": "stop"})
        except Exception:
            pass

        def _later():
            time.sleep(grace)
            if not self._done:
                logging.warning("extension did not close the port after stop — exiting")
                self._finish()
                logging.shutdown()
                os._exit(0)
        threading.Thread(target=_later, daemon=True).start()

    def _touch_heartbeat(self) -> None:
        try:
            (self.session_dir / ".host_alive").write_text(json.dumps({
                "pid": os.getpid(), "ts": time.time(),
                "last_message_at": self.last_message_at, "messages": self.messages}))
        except Exception:
            pass

    def _keepalive_loop(self) -> None:
        # Receiving a native message resets the MV3 service worker's ~30s idle
        # timer; 10s keeps it (and this port) alive through idle stretches.
        while not self._stop.wait(10):
            self._touch_heartbeat()
            try:
                self.ch.send({"type": "keepalive"})
            except Exception as e:
                logging.info(f"keepalive write stopped (port closing): {e}")
                break

    def _control_poll_loop(self) -> None:
        """Relay recorder commands to the extension.

        The recorder writes ``control.json`` = {"paused": bool, "cmd": n} for
        pause/resume (forwarded on every NEW cmd id) and {"stop": true} — or
        drops ``stop.request`` — to end the session: the extension then tears
        down (detaches debuggers) and closes our stdin, and run() flushes."""
        control_path = self.session_dir / "control.json"
        stop_request = self.session_dir / "stop.request"
        # Start from the command the PREVIOUS host already applied (kept in
        # status.json): re-sending an old command after a reconnect would
        # override a pause/resume made since then from the extension popup.
        st = _read_json(self.session_dir / "status.json") or {}
        last_cmd = st.get("cmd")
        last_paused = st.get("paused") if "paused" in st else None
        while not self._stop.wait(0.1):
            try:
                if stop_request.exists():
                    logging.info("stop.request present -> stop")
                    self._stop_and_exit_soon()
                    break
                if not control_path.is_file():
                    continue
                data = json.loads(control_path.read_text(encoding="utf-8"))
                if data.get("stop"):
                    logging.info("control.json -> stop")
                    self._stop_and_exit_soon()
                    break
                paused = bool(data.get("paused", False))
                cmd = data.get("cmd")
                changed = (cmd != last_cmd) if cmd is not None else (paused != last_paused)
                if changed:
                    last_cmd, last_paused = cmd, paused
                    self.ch.send({"type": "set_paused", "paused": paused})
                    self._write_status(paused, cmd=cmd)
                    logging.info(f"control.json -> set_paused {paused} (cmd={cmd})")
            except (ValueError, OSError) as e:
                logging.debug(f"control poll: {e}")
            except Exception as e:
                logging.info(f"control poll stopped: {e}")
                break

    def _write_status(self, paused: bool, ts: float | None = None, cmd=None) -> None:
        """status.json = {paused, ts, cmd}: the authoritative pause state plus
        the last app command applied (kept across writes from markers)."""
        with self._io_lock:
            p = self.session_dir / "status.json"
            try:
                prev = _read_json(p) or {}
                data = {"paused": bool(paused), "ts": ts if ts is not None else time.time()}
                c = cmd if cmd is not None else prev.get("cmd")
                if c is not None:
                    data["cmd"] = c
                atomic_write_text(p, json.dumps(data))
            except Exception as e:
                logging.warning(f"failed to write status.json: {e}")

    # -- dispatch ----------------------------------------------------------------
    def handle_message(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            return
        msg_type = msg.get("type")
        urls = [msg.get("site_url") or "", msg.get("url") or ""]
        for k in ("request", "response"):
            if isinstance(msg.get(k), dict):
                urls.append(msg[k].get("url") or "")
        if any(_is_dashboard(u) for u in urls):
            return
        # Credential query parameters (OAuth callbacks…) out of every URL we
        # store — consistently, so tab timeline and WARC tags still match.
        for k in ("url", "site_url"):
            if isinstance(msg.get(k), str):
                msg[k] = self.redactor.url(msg[k])

        if msg_type == "network_request":
            self._handle_network_request(msg)
        elif msg_type == "network_response":
            key = self._resolve_key(msg)
            req = self._pending_requests.pop(key, None)
            req_id = self._write_warc_request(req) if req else None
            self._write_warc_response(msg, self._req_meta.get(key), concurrent_to=req_id)
        elif msg_type == "network_failed":
            req = self._pending_requests.pop(self._resolve_key(msg), None)
            if req:
                self._write_warc_request(req, failed=str(msg.get("errorText") or "failed"))
        elif msg_type == "layout_snapshot":
            self._handle_layout_snapshot(msg)
        elif msg_type in ("zoom_change", "privacy_skip"):
            self._append_jsonl("events.jsonl", msg)
        elif msg_type in ("websocket_event", "eventsource_message"):
            if isinstance(msg.get("headers"), dict):
                msg["headers"] = self.redactor.headers(msg["headers"])
            self._append_jsonl("websocket_frames.jsonl", msg)
        elif msg_type == "browser_snapshot":
            self._handle_browser_snapshot(msg)
        elif msg_type == "dom_snapshot":
            self._handle_dom_snapshot(msg)
        elif msg_type == "websocket_frame":
            self._append_jsonl("websocket_frames.jsonl", msg)
        elif msg_type in ("tab_activated", "window_focus"):
            self._append_jsonl("tab_timeline.jsonl", msg)
        elif msg_type == "download_started":
            self._handle_download_started(msg)
        elif msg_type == "download_complete":
            self._handle_download_complete(msg)
        elif msg_type == "file_upload":
            self._handle_file_upload(msg)
        elif msg_type == "local_file_active":
            self._handle_local_file_active(msg)
        elif msg_type in ("recording_paused", "recording_resumed"):
            self._handle_recording_marker(msg)
        elif msg_type == "debugger_canceled":
            logging.warning(
                "USER DISMISSED THE DEBUGGER BANNER — network capture stopped for "
                "the rest of this session (screenshots/events continue).")
            self._append_jsonl("events.jsonl", {"type": "debugger_canceled",
                                                "timestamp": msg.get("timestamp")})
        elif msg_type == "all_windows_closed":
            # The participant closed the last recording window. On macOS the
            # browser keeps running without windows, so the app cannot see it
            # exit; this marker means the same thing ("browser closed").
            logging.info("all recording-browser windows closed")
            try:
                (self.session_dir / ".browser_windows_closed").write_text(str(time.time()))
            except OSError:
                pass
        elif msg_type == "debug_log":
            logging.info(f"[ext] {msg.get('msg')}")
        else:
            logging.debug(f"Unhandled message type: {msg_type}")

    def _append_jsonl(self, name: str, obj: dict) -> None:
        with open(self.session_dir / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj) + "\n")

    # -- screenshots + events ------------------------------------------------------
    def _handle_browser_snapshot(self, msg: dict) -> None:
        # Host-side timestamp: monotonic per process and unique, so filenames
        # never collide across tabs (and stay sortable for the video).
        timestamp = time.time()
        msg["timestamp"] = timestamp
        event_type = _SAFE_NAME.sub("_", str(msg.get("event_type") or "unknown"))[:60]
        image_data = msg.pop("image_data", None)

        img_bytes = self._decode_image(image_data) if image_data else None
        if img_bytes is not None:
            img_bytes = self._redact_screenshot(img_bytes, msg)
        if img_bytes is None and image_data:
            msg["screenshot"] = False

        if img_bytes is not None:
            self.stats["screenshots"] += 1
        if "click" in event_type:
            self.stats["clicks"] += 1
        elif "input" in event_type:
            self.stats["inputs"] += 1
        elif "scroll" in event_type:
            self.stats["scrolls"] += 1
        try:
            atomic_write_text(self.session_dir / "live_stats.json", json.dumps(self.stats))
        except OSError:
            pass

        self._append_jsonl("events.jsonl", msg)

        if img_bytes is not None:
            ss_dir = self.session_dir / "browser_screenshots"
            ss_dir.mkdir(exist_ok=True)
            out_path = ss_dir / f"{timestamp:.6f}_{event_type}.png"
            with open(out_path, "wb") as f:
                f.write(img_bytes)

    def _redact_screenshot(self, png: bytes, msg: dict) -> bytes | None:
        """Blank the boxes of sensitive fields (card numbers, one-time codes,
        unmasked passwords) before the screenshot touches disk. Boxes come in
        top-viewport CSS px; the image is in device px. A screenshot showing a
        sensitive field whose position is unknown is not kept at all."""
        if msg.get("redact_unlocated"):
            logging.info("screenshot dropped: sensitive field at an unknown position")
            msg["screenshot_withheld"] = "sensitive_unlocated"
            return None
        rects = msg.get("redact_rects")
        if not rects:
            return png
        try:
            from PIL import Image, ImageDraw
            img = Image.open(io.BytesIO(png))
            img.load()
            d = msg.get("details") if isinstance(msg.get("details"), dict) else {}
            try:
                vw = float(d.get("vw") or 0)
                scale = img.width / vw if vw else float(d.get("dpr") or 1.0)
            except (TypeError, ValueError):
                scale = 1.0
            draw = ImageDraw.Draw(img)
            pad = 4
            for r in rects:
                x, y, w, h = (float(v) for v in r[:4])
                draw.rectangle([(x - pad) * scale, (y - pad) * scale,
                                (x + w + pad) * scale, (y + h + pad) * scale], fill=(128, 128, 128))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            msg["redacted_boxes"] = len(rects)
            return buf.getvalue()
        except Exception as e:
            logging.error(f"screenshot redaction failed ({e}) — dropping the image")
            msg["screenshot_withheld"] = "redaction_failed"
            return None

    def _decode_image(self, image_data: str) -> bytes | None:
        try:
            header, b64 = image_data.split(",", 1)
        except (ValueError, AttributeError):
            return None
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return None
        if header.startswith("data:image/png"):
            return raw
        if header.startswith("data:image/jpeg"):
            if not self._warned_stale_jpeg:
                self._warned_stale_jpeg = True
                logging.warning("received a JPEG screenshot (captureVisibleTab fallback) — re-encoding to PNG")
            try:
                from PIL import Image
                buf = io.BytesIO()
                Image.open(io.BytesIO(raw)).save(buf, format="PNG")
                return buf.getvalue()
            except Exception as e:
                logging.error(f"JPEG->PNG re-encode failed: {e}")
                return None
        return None

    def _handle_recording_marker(self, msg: dict) -> None:
        ts = msg.get("timestamp")
        paused = msg.get("type") == "recording_paused"
        logging.info(f"Recording {msg.get('type')} at ts={ts}")
        self._append_jsonl("events.jsonl", {"type": msg.get("type"), "timestamp": ts})
        self._write_status(paused, ts=ts)

    # -- DOM snapshots ---------------------------------------------------------------
    def _handle_dom_snapshot(self, msg: dict) -> None:
        url = msg.get("url") or ""
        html = msg.get("html") or ""
        if not url or not html:
            return
        snap_dir = self.session_dir / "dom_snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        if not hasattr(self, "_dom_snapshot_n"):
            existing = [int(p.stem) for p in snap_dir.glob("[0-9]*.html") if p.stem.isdigit()]
            self._dom_snapshot_n = max(existing, default=0)
        self._dom_snapshot_n += 1
        fname = f"{self._dom_snapshot_n:05d}.html"
        # errors="replace": JS strings may carry lone surrogates.
        (snap_dir / fname).write_text(html, encoding="utf-8", errors="replace")
        row = {"url": url, "title": msg.get("title") or "", "reason": msg.get("reason") or "",
               "ts": msg.get("timestamp"), "file": fname, "bytes": len(html)}
        for k in ("shadow_roots", "cssom_bytes", "env"):
            if msg.get(k) is not None:
                row[k] = msg[k]
        with open(snap_dir / "index.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def _handle_layout_snapshot(self, msg: dict) -> None:
        """CDP DOMSnapshot.captureSnapshot result, stored as the gzip the
        extension produced: layout_snapshots/<n>.json.gz + index.jsonl."""
        data = msg.get("data_gz_b64") or ""
        if not data:
            return
        try:
            raw = base64.b64decode(data)
        except Exception:
            return
        out_dir = self.session_dir / "layout_snapshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        if not hasattr(self, "_layout_n"):
            existing = [int(p.name.split(".")[0]) for p in out_dir.glob("[0-9]*.json.gz")
                        if p.name.split(".")[0].isdigit()]
            self._layout_n = max(existing, default=0)
        self._layout_n += 1
        fname = f"{self._layout_n:05d}.json.gz"
        (out_dir / fname).write_bytes(raw)
        with open(out_dir / "index.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"url": msg.get("url"), "ts": msg.get("timestamp"), "tabId": msg.get("tabId"),
                                "file": fname, "json_bytes": msg.get("bytes"),
                                "styles": msg.get("styles")}) + "\n")

    # -- uploads / downloads / local files ----------------------------------------------
    def _next_upload_n(self, up_dir: Path) -> int:
        if not hasattr(self, "_upload_n"):
            # Continue numbering across host restarts — restarting at 1 would
            # overwrite earlier uploads that share a name.
            nums = []
            for p in up_dir.glob("[0-9][0-9][0-9][0-9]_*"):
                try:
                    nums.append(int(p.name[:4]))
                except ValueError:
                    pass
            self._upload_n = max(nums, default=0)
        self._upload_n += 1
        return self._upload_n

    def _handle_file_upload(self, msg: dict) -> None:
        f = msg.get("file") or {}
        name = str(f.get("name") or "upload")
        data_url = f.get("dataUrl") or ""
        up_dir = self.session_dir / "uploads"
        up_dir.mkdir(parents=True, exist_ok=True)
        stored = None
        if data_url and "," in data_url:
            raw = base64.b64decode(data_url.split(",", 1)[1])
            safe = f"{self._next_upload_n(up_dir):04d}_" + _SAFE_NAME.sub("_", name)[:80]
            (up_dir / safe).write_bytes(raw)
            stored = safe
        with open(up_dir / "index.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "name": name, "size": f.get("size"), "mime": f.get("mime"),
                "url": msg.get("url"), "ts": msg.get("timestamp"),
                "file": stored, "truncated": bool(f.get("truncated")),
            }) + "\n")
        logging.info(f"Upload stored: {name} ({f.get('size')} B) -> {stored or 'metadata-only'}")

    def _handle_download_started(self, msg: dict) -> None:
        downloads_dir = self.session_dir / "downloads"
        downloads_dir.mkdir(exist_ok=True)
        with open(downloads_dir / "index.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "event": "started", "id": msg.get("id"), "via": msg.get("via", "chrome"),
                "url": msg.get("url"), "filename": msg.get("filename"), "mime": msg.get("mime"),
                "ts": msg.get("timestamp") or msg.get("startTime"),
            }) + "\n")

    def _handle_download_complete(self, msg: dict) -> None:
        src_path = msg.get("filename") or ""
        if not src_path:
            return
        src = Path(src_path)
        if not src.is_file():
            logging.warning(f"download_complete: source file gone: {src}")
            return
        downloads_dir = self.session_dir / "downloads"
        downloads_dir.mkdir(exist_ok=True)
        dst = _unique_path(downloads_dir / src.name)
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            logging.error(f"download_complete: copy {src} -> {dst} failed: {e}")
            return
        with open(downloads_dir / "downloads.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": msg.get("id"), "url": msg.get("url"), "final_url": msg.get("finalUrl"),
                "referrer": msg.get("referrer"), "mime": msg.get("mime"),
                "size_bytes": msg.get("fileSize") or msg.get("totalBytes"),
                "start_time": msg.get("startTime"), "end_time": msg.get("endTime"),
                "source_path": str(src), "saved_path": dst.relative_to(self.session_dir).as_posix(),
            }) + "\n")
        logging.info(f"Saved download to {dst}")

    def _handle_local_file_active(self, msg: dict) -> None:
        from urllib.parse import urlparse
        from urllib.request import url2pathname
        url = msg.get("url") or ""
        if not url.startswith("file://"):
            return
        parsed = urlparse(url)
        # url2pathname decodes %XX and maps "/C:/x" -> "C:\x" on Windows.
        path = url2pathname(parsed.path)
        if parsed.netloc and parsed.netloc != "localhost":
            # file://server/share/x — a UNC path on Windows
            path = (f"\\\\{parsed.netloc}{path}" if sys.platform == "win32"
                    else f"//{parsed.netloc}{path}")
        src = Path(path)
        if not src.is_file():
            logging.warning(f"local_file_active: not a readable file: {src}")
            return
        local_dir = self.session_dir / "local_files"
        local_dir.mkdir(exist_ok=True)
        dst = local_dir / src.name
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            pass  # same file re-activated; don't duplicate
        else:
            dst = _unique_path(dst)
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                logging.error(f"local_file_active: copy {src} -> {dst} failed: {e}")
                return
        with open(self.session_dir / "local_files.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "tabId": msg.get("tabId"), "windowId": msg.get("windowId"), "url": url,
                "timestamp": msg.get("timestamp"), "source_path": str(src),
                "saved_path": dst.relative_to(self.session_dir).as_posix(),
                "size_bytes": dst.stat().st_size if dst.exists() else None,
            }) + "\n")

    # -- WARC --------------------------------------------------------------------------
    def _handle_network_request(self, msg: dict) -> None:
        key = _req_key(msg)
        prev = self._pending_requests.pop(key, None)
        if prev is not None:
            # The same requestId again = the next hop of a redirect chain: the
            # previous hop's request is complete, with this 3xx as its response.
            prev_id = self._write_warc_request(prev)
            redirect = msg.get("redirectResponse")
            if isinstance(redirect, dict) and redirect.get("url"):
                hop = {"type": "network_response", "response": redirect, "redirect": True,
                       "requestId": msg.get("requestId"), "tabId": msg.get("tabId"),
                       "sessionId": msg.get("sessionId"), "timestamp": msg.get("timestamp")}
                self._write_warc_response(hop, self._req_meta.get(key), concurrent_to=prev_id)
        self._pending_requests[key] = msg
        self._req_meta[key] = {k: msg.get(k) for k in
                               ("site_url", "tabId", "frameId", "sessionId", "wallTime", "timestamp")}
        self._req_meta.move_to_end(key)
        while len(self._req_meta) > 5000:
            self._req_meta.popitem(last=False)
        while len(self._pending_requests) > 5000:
            _, old = self._pending_requests.popitem(last=False)
            self._write_warc_request(old)

    def _resolve_key(self, msg: dict) -> str:
        """The request key a response/failure belongs to. A message without a
        tab id (older extension builds) falls back to the latest request with
        the same requestId."""
        key = _req_key(msg)
        if key in self._pending_requests or key in self._req_meta or msg.get("tabId") is not None:
            return key
        suffix = f"|{msg.get('requestId')}"
        for table in (self._pending_requests, self._req_meta):
            for k in reversed(table):
                if k.endswith(suffix):
                    return k
        return key

    @staticmethod
    def _wall_time(msg: dict, meta: dict | None) -> float | None:
        """Wall-clock seconds of a CDP event: the request's own wallTime, or
        the request's wallTime shifted by the monotonic delta to this event."""
        try:
            if msg.get("wallTime"):
                return float(msg["wallTime"])
            if meta and meta.get("wallTime") and meta.get("timestamp") and msg.get("timestamp"):
                return float(meta["wallTime"]) + float(msg["timestamp"]) - float(meta["timestamp"])
        except (TypeError, ValueError):
            pass
        return None

    def _sitebench_warc_headers(self, msg: dict, wall: float | None = None) -> dict:
        when = datetime.fromtimestamp(wall, timezone.utc) if wall else datetime.now(timezone.utc)
        headers = {"WARC-Date": when.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if wall:
            # WARC-Date is second-resolution; this is the exact event time.
            headers["WARC-SiteBench-Wall-Time"] = f"{wall:.6f}"
        if msg.get("site_url"):
            headers["WARC-SiteBench-Site-URL"] = msg["site_url"]
        if msg.get("tabId") is not None:
            headers["WARC-SiteBench-Tab-Id"] = str(msg["tabId"])
        if msg.get("frameId") is not None:
            headers["WARC-SiteBench-Frame-Id"] = str(msg["frameId"])
        if msg.get("sessionId"):
            # Traffic of an auto-attached child target (cross-origin iframe, worker).
            headers["WARC-SiteBench-Target-Session"] = str(msg["sessionId"])
        if msg.get("typeStr"):
            headers["WARC-SiteBench-Resource-Type"] = str(msg["typeStr"])
        return headers

    def _write_warc_request(self, req_msg: dict, failed: str | None = None) -> str | None:
        """Write one request record; returns its WARC-Record-ID."""
        if not self.warc_writer:
            return None
        request = req_msg.get("request") or {}
        url = self.redactor.url(request.get("url"))
        if not url:
            return None
        headers = self.redactor.headers(request.get("headers") or {})
        payload = b""
        if req_msg.get("postData") is not None:
            ctype = next((str(v) for k, v in headers.items() if str(k).lower() == "content-type"), "")
            text = self.redactor.body(str(req_msg["postData"]), ctype)
            payload = text.encode("utf-8", errors="surrogatepass")
        elif req_msg.get("postDataBase64"):
            try:
                payload = base64.b64decode(req_msg["postDataBase64"])
            except Exception:
                payload = b""
        headers_list = [(str(k), str(v)) for k, v in headers.items()]
        status_headers = StatusAndHeaders(f"{request.get('method') or 'GET'} {url} HTTP/1.1",
                                          headers_list, protocol="HTTP/1.1")
        warc_headers = self._sitebench_warc_headers(req_msg, self._wall_time(req_msg, None))
        if req_msg.get("initiator"):
            warc_headers["WARC-SiteBench-Initiator"] = json.dumps(req_msg["initiator"], separators=(",", ":"))[:4000]
        if req_msg.get("postDataOmitted"):
            warc_headers["WARC-SiteBench-Post-Omitted"] = str(req_msg["postDataOmitted"])
        if failed:
            warc_headers["WARC-SiteBench-Failed"] = failed[:200]
        record = self.warc_writer.create_warc_record(
            url, "request", payload=io.BytesIO(payload), http_headers=status_headers,
            warc_headers_dict=warc_headers)
        self._append_record(record)
        return record.rec_headers.get_header("WARC-Record-ID")

    def _write_warc_response(self, resp_msg: dict, req_meta: dict | None,
                             concurrent_to: str | None = None) -> None:
        if not self.warc_writer:
            return
        response = resp_msg.get("response") or {}
        url = self.redactor.url(response.get("url"))
        if not url:
            return
        headers = self.redactor.headers(response.get("headers") or {})
        headers_list = [(str(k), str(v)) for k, v in headers.items()]
        status_headers = StatusAndHeaders(f"{response.get('status')} {response.get('statusText') or 'OK'}",
                                          headers_list, protocol="HTTP/1.1")
        body_content = b""
        body_str = resp_msg.get("body")
        if body_str:
            if resp_msg.get("base64Encoded"):
                body_content = base64.b64decode(body_str)
            else:
                body_content = body_str.encode("utf-8", errors="surrogatepass")
        merged = dict(resp_msg)
        if req_meta:
            for k in ("site_url", "tabId", "frameId", "sessionId"):
                if merged.get(k) is None and req_meta.get(k) is not None:
                    merged[k] = req_meta.get(k)
        warc_headers = self._sitebench_warc_headers(merged, self._wall_time(resp_msg, req_meta))
        if concurrent_to:
            warc_headers["WARC-Concurrent-To"] = concurrent_to
        if resp_msg.get("bodyOmitted"):
            warc_headers["WARC-SiteBench-Body-Omitted"] = str(resp_msg["bodyOmitted"])
        if resp_msg.get("redirect"):
            warc_headers["WARC-SiteBench-Redirect"] = "1"
        if resp_msg.get("failed"):
            warc_headers["WARC-SiteBench-Failed"] = str(resp_msg["failed"])[:200]
        if resp_msg.get("encodedDataLength") is not None:
            warc_headers["WARC-SiteBench-Encoded-Length"] = str(resp_msg["encodedDataLength"])
        record = self.warc_writer.create_warc_record(
            url, "response", payload=io.BytesIO(body_content), http_headers=status_headers,
            warc_headers_dict=warc_headers)
        self._append_record(record)

    def _append_record(self, record) -> None:
        """Write one record (= one gzip member). If the write fails part-way
        (disk full, I/O error), cut the file back to the last complete record
        and rebuild the writer — a torn member in the MIDDLE of the WARC would
        make every later record unreadable."""
        pos = self._committed
        try:
            self.warc_writer.write_record(record)
            self.warc_file.flush()
        except Exception as e:
            logging.error(f"WARC write failed ({e}); cutting back to the last complete record")
            try:
                self.warc_file.close()
            except Exception:
                pass
            try:
                with open(self.warc_path, "r+b") as f:
                    f.truncate(pos)
            except OSError as e2:
                logging.error(f"WARC cut-back failed: {e2}")
            self.warc_file = open(self.warc_path, "ab", buffering=0)
            self.warc_writer = WARCWriter(self.warc_file, gzip=True)
            self._committed = self.warc_file.tell()
            raise
        self._committed = self.warc_file.tell()
        self._write_committed()


def _req_key(msg: dict) -> str:
    """Request identity across the extension's targets: CDP request ids are
    only unique per target, and child targets (iframes, workers) have their
    own sessions."""
    return f"{msg.get('tabId')}|{msg.get('sessionId') or ''}|{msg.get('requestId')}"


def _read_int(p: Path) -> int:
    try:
        return int(p.read_text().strip())
    except (OSError, ValueError):
        return 0


def _repair_warc_tail(warc: Path, committed: int) -> None:
    """Truncate a partial gzip member left at the end of the WARC.

    Scans only the bytes after ``committed`` (a known record boundary; 0 =
    scan everything) and keeps every COMPLETE gzip member found there."""
    import zlib
    try:
        size = warc.stat().st_size
    except OSError:
        return
    start = committed if 0 < committed <= size else 0
    if size == start:
        return
    good = start
    corrupt = False
    try:
        with open(warc, "rb") as f:
            f.seek(start)
            pos = start
            buf = b""
            while True:
                if not buf:
                    buf = f.read(1 << 20)
                    if not buf:
                        break                  # clean end after a complete member
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                complete = False
                while True:
                    d.decompress(buf)
                    if d.eof:
                        used = len(buf) - len(d.unused_data)
                        pos += used
                        buf = d.unused_data
                        complete = True
                        break
                    pos += len(buf)
                    buf = f.read(1 << 20)
                    if not buf:
                        break                  # EOF inside a member: partial
                if not complete:
                    break
                good = pos
    except zlib.error:
        corrupt = True          # damaged data, not a cut-off tail: keep the file
    except OSError:
        return
    if corrupt:
        logging.warning("WARC: damaged data after the last known-good record; left untouched")
        return
    if good < size:
        try:
            with open(warc, "r+b") as f:
                f.truncate(good)
            logging.warning(f"WARC: removed {size - good} trailing bytes of an incomplete record "
                            f"(a previous host was cut off)")
        except OSError as e:
            logging.warning(f"WARC tail repair failed: {e}")


def _unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        cand = p.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


# ---------------------------------------------------------------------------
# Entry point (invoked by the browser through the registered wrapper)
# ---------------------------------------------------------------------------

def _log_refusal(app_dir: Path, reason: str) -> None:
    try:
        logs = app_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / "host_refusals.log"
        if log.exists() and log.stat().st_size > 1_000_000:
            log.unlink()
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} pid={os.getpid()} {reason}\n")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SITE Recorder native-messaging host")
    parser.add_argument("--app-dir", required=True)
    # The browser appends the caller's origin (and on Windows a --parent-window
    # handle); ignore whatever we don't know.
    args, _unknown = parser.parse_known_args(argv)
    app_dir = Path(args.app_dir)

    channel = Channel()
    session_dir, reason = resolve_session(app_dir)
    if session_dir is None:
        # Not ours to record: tell the extension to stand down (it detaches
        # its debuggers and keeps polling quietly for the next real session).
        _log_refusal(app_dir, reason)
        try:
            channel.send({"type": "stop"})
        except Exception:
            pass
        return 0

    logging.basicConfig(filename=str(session_dir / "extension_host.log"), level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s", encoding="utf-8")
    if reason:
        logging.warning(f"accepting connection: {reason}")
    lock = HostLock(session_dir / ".host.lock")
    if not lock.acquire(timeout=15):
        logging.warning("another host still holds this session after 15s — refusing this connection")
        try:
            channel.send({"type": "stop"})
        except Exception:
            pass
        return 0
    try:
        # A previous host's ".host_done" belongs to the previous connection.
        try:
            (session_dir / ".host_done").unlink()
        except FileNotFoundError:
            pass
        ExtensionHost(session_dir, channel, app_dir=app_dir).run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
