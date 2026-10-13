"""Session lifecycle: start / pause / resume / stop / recover.

State lives on disk, never only in memory, so a crash of the app (or of the
terminal running ``site-recorder start``) cannot lose or wedge a recording:

* ``<app_dir>/active_session.json`` — the ONE session currently being
  recorded: its folder, state ("starting" | "recording" | "stopping"), the
  recorder process (pid + start time), the browser and its profile dir. The
  native host serves a browser connection only while this names a live
  recorder (see host.resolve_session).
* ``<session>/control.json`` — pause/resume commands relayed to the extension.
* ``<session>/stop.request`` — end of session; the host relays it, the
  extension detaches and acknowledges, the host flushes and writes
  ``.host_done``.
* ``<session>/session_info.json`` — written at finalize: who/when/what.

Start, stop, recover and adopt are serialized across threads AND processes
(the app and ``site-recorder`` in a terminal) by ``<app_dir>/lifecycle.lock``.
Any process can stop or finalize a session left behind by another (crashed)
recorder: ``recover()`` runs at every app/CLI start.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__, browsers, nativehost, paths
from .config import Config
from .fsutil import FileLock, atomic_write_json, read_json
from .sessions import dir_size, screenshot_times

# How long to wait for the extension to connect after start, and for the
# host to flush after a stop request.
CONNECT_TIMEOUT_S = 45.0
FLUSH_TIMEOUT_S = 20.0
# A host heartbeat (written every 10 s) older than this means no live host.
HEARTBEAT_STALE_S = 35.0
# Plumbing removed from a session folder once it is finalized.
_PLUMBING = ("control.json", "stop.request", ".host_alive", ".host.lock", "status.json",
             ".warc_committed", ".browser_windows_closed")


class RecorderError(RuntimeError):
    """A user-facing problem (message is shown as-is)."""


# ---------------------------------------------------------------------------
# active_session.json + the lifecycle lock
# ---------------------------------------------------------------------------

def _read_marker() -> dict | None:
    m = read_json(paths.active_session_file())
    return m if isinstance(m, dict) else None


def _write_marker(data: dict) -> None:
    f = paths.active_session_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(f, data, indent=2)


def _clear_marker(session_dir: Path | None = None) -> None:
    m = _read_marker()
    if m and session_dir is not None and Path(m.get("session_dir", "")) != Path(session_dir):
        return  # belongs to another session; not ours to clear
    try:
        paths.active_session_file().unlink()
    except FileNotFoundError:
        pass


class _Lifecycle:
    """Context manager holding <app_dir>/lifecycle.lock (raises RecorderError
    when another start/stop does not finish within ``timeout``)."""

    def __init__(self, timeout: float, what: str):
        self.lock = FileLock(paths.app_dir() / "lifecycle.lock")
        self.timeout, self.what = timeout, what

    def __enter__(self):
        if not self.lock.acquire(self.timeout):
            raise RecorderError(f"Could not {self.what}: another start/stop is still in progress.")
        return self

    def __exit__(self, *exc):
        self.lock.release()


def _self_identity() -> tuple[int, float]:
    import psutil
    p = psutil.Process(os.getpid())
    return p.pid, p.create_time()


def _recorder_alive(marker: dict) -> bool:
    import psutil
    pid = int(marker.get("recorder_pid") or 0)
    if not pid:
        return False
    try:
        p = psutil.Process(pid)
        ct = marker.get("recorder_started")
        if ct and abs(p.create_time() - float(ct)) > 2.0:
            return False
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def active_session() -> dict | None:
    """The marker of the session being recorded right now (by anyone), if any."""
    m = _read_marker()
    if not m:
        return None
    if not Path(m.get("session_dir", "")).is_dir():
        return None
    return m


def owned_by_me(m: dict | None) -> bool:
    return bool(m) and int(m.get("recorder_pid") or 0) == os.getpid()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _new_session_dir(sessions_dir: Path) -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    while True:
        sid = datetime.now().strftime("%Y%m%d_%H%M%S")
        d = sessions_dir / sid
        try:
            d.mkdir()
            return d
        except FileExistsError:
            time.sleep(1.0)   # ids are per-second; wait for the next one


_read_json = read_json   # (kept for callers in gui.py)


def trace(session_dir: Path, msg: str) -> None:
    """Append a lifecycle line to <session>/recorder_trace.log — makes every
    session folder self-describing when a recording comes back empty."""
    try:
        with open(Path(session_dir) / "recorder_trace.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [pid {os.getpid()}] {msg}\n")
    except OSError:
        pass


def _host_finished_after(session_dir: Path, since: float) -> bool:
    """True once no host is serving the session any more (after ``since``)."""
    sd = Path(session_dir)
    done = read_json(sd / ".host_done")
    hb = read_json(sd / ".host_alive")
    if isinstance(done, dict):
        if float(done.get("ts", 0)) >= since - 1:
            return True
        # .host_done from the LAST host that ran (a new host deletes it at
        # start): that host is gone, even though it finished before ``since``.
        if isinstance(hb, dict) and done.get("pid") == hb.get("pid"):
            return True
    if not isinstance(hb, dict):
        return True                      # no host ever ran
    if time.time() - float(hb.get("ts", 0)) > HEARTBEAT_STALE_S:
        return True                      # heartbeat stopped: the host is dead
    try:
        import psutil
        return not psutil.pid_exists(int(hb.get("pid", 0)))
    except Exception:
        return False


def host_connected(session_dir: Path) -> bool:
    return (Path(session_dir) / "extension_host.log").exists()


def host_heartbeat_age(session_dir: Path) -> float | None:
    hb = read_json(Path(session_dir) / ".host_alive")
    if not isinstance(hb, dict):
        return None
    return max(0.0, time.time() - float(hb.get("ts", 0)))


def browser_closed(m: dict) -> bool:
    """Has the participant closed the recording browser? (On macOS the browser
    keeps running without windows; the extension reports that instead.)"""
    b = m.get("browser") or ""
    if b not in browsers.DISPLAY_NAME:
        return False
    if (Path(m.get("session_dir", "")) / ".browser_windows_closed").exists():
        return True
    return not browsers.is_running(b)


# ---------------------------------------------------------------------------
# the extension copy
# ---------------------------------------------------------------------------

def ensure_extension_copy() -> Path:
    """Copy the bundled extension to its stable location (idempotent).

    Refreshes changed files so an upgrade of the recorder upgrades the
    extension (see reload_unpacked_if_changed for making the browser pick
    the new code up)."""
    src = Path(__file__).parent / "extension"
    dst = paths.extension_dir()
    # Update IN PLACE, only files whose bytes differ (never remove the folder:
    # a browser silently uninstalls an unpacked extension whose folder
    # disappears).
    dst.mkdir(parents=True, exist_ok=True)
    files = sorted(f for f in src.iterdir() if f.is_file())
    for f in files:
        data = f.read_bytes()
        if f.name == "manifest.json":
            data = _stamped_manifest(data, files)
        target = dst / f.name
        try:
            if target.read_bytes() == data:
                continue
        except OSError:
            pass
        tmp = dst / (f.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return dst


def _stamped_manifest(manifest_bytes: bytes, files: list[Path]) -> bytes:
    """The manifest with a 4th version component derived from the code, so
    every code change is also a version change."""
    import hashlib
    h = hashlib.sha256()
    for f in files:
        if f.name != "manifest.json":
            h.update(f.name.encode() + b"\0" + f.read_bytes())
    stamp = int(h.hexdigest()[:8], 16) % 65535 + 1
    m = json.loads(manifest_bytes)
    base = ".".join(str(m.get("version", "1.0.0")).split(".")[:3])
    m["version"] = f"{base}.{stamp}"
    return (json.dumps(m, indent=2) + "\n").encode("utf-8")


def _copy_version() -> str:
    m = read_json(paths.extension_dir() / "manifest.json") or {}
    return str(m.get("version", ""))


def reload_unpacked_if_changed(browser: str, log=lambda m: None) -> None:
    """Make the browser run the CURRENT bundled code.

    An unpacked extension keeps running the service worker it registered
    when it was loaded — new files on disk are NOT picked up at the next
    start (verified with Chrome/Edge 155). After an upgrade, reload it once
    through the extensions page (the same as clicking its Reload button).
    Only for the bundled (unpacked) copy; a Web Store install updates itself.
    The browser must not be running."""
    st = browsers.extension_state(browser)
    if not (st["installed"] and st.get("source") == "unpacked"):
        return
    try:
        same = Path(st.get("path") or "").resolve() == paths.extension_dir().resolve()
    except OSError:
        same = False
    if not same:
        return
    stamp_file = paths.app_dir() / f"extension_loaded_{browser}.txt"
    want = _copy_version()
    try:
        have = stamp_file.read_text(encoding="utf-8").strip()
    except OSError:
        have = ""
    if have == want or browsers.is_running(browser):
        return
    from .extinstall import reload_extension
    ok, msg = reload_extension(browser)
    log(f"reloaded the extension ({msg})" if ok else f"extension reload failed: {msg}")
    if ok:
        stamp_file.write_text(want, encoding="utf-8")


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class Recorder:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # -- preflight ---------------------------------------------------------------
    def preflight(self) -> list[str]:
        """Problems that would make a recording come out empty (user-facing)."""
        problems = []
        if not self.cfg.is_configured:
            problems.append("Setup is not finished (participant ID and browser).")
            return problems
        b = self.cfg.browser
        if not browsers.find_executable(b):
            problems.append(f"{browsers.DISPLAY_NAME[b]} is not installed.")
        elif browsers.unsupported_reason(b):
            problems.append(browsers.unsupported_reason(b))
        try:
            probe = self.cfg.sessions_path
            probe.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(probe).free
            if free < 1 << 30:
                problems.append(f"Only {free / 2**30:.1f} GB free on the recordings disk — free up some "
                                "space first (a recording can take several GB).")
        except OSError as e:
            problems.append(f"The recordings folder {self.cfg.sessions_path} is not usable: {e}")
        st = browsers.extension_state(b)
        if not st["installed"]:
            problems.append("The SITE Collector extension is not installed in the recording browser "
                            "(Setup → step 3).")
        elif not st["enabled"]:
            problems.append("The SITE Collector extension is disabled in the recording browser"
                            + (" because Developer mode was turned off — Setup → 'Use the bundled "
                               "copy' switches it back on." if st.get("problem") == "developer_mode_off"
                               else " — enable it on the extensions page."))
        return problems

    # -- start ---------------------------------------------------------------------
    def start(self, *, wait_connect: bool = True, on_wait=None) -> Path:
        with _Lifecycle(timeout=5, what="start"):
            _recover_unlocked(self.cfg)
            if active_session():
                raise RecorderError("A recording is already in progress.")
            problems = self.preflight()
            if problems:
                raise RecorderError("\n".join(problems))
            b = self.cfg.browser
            ensure_extension_copy()
            nativehost.register(b)
            reload_unpacked_if_changed(b)

            session_dir = _new_session_dir(self.cfg.sessions_path)
            pid, created = _self_identity()
            marker = {
                "session_dir": str(session_dir),
                "session_id": session_dir.name,
                "state": "starting",
                "recorder_pid": pid,
                "recorder_started": created,
                "browser": b,
                "profile_dir": str(paths.profile_dir(b)),
                "started_at": time.time(),
            }
            _write_marker(marker)
        # From here on, ANY failure or interrupt (Ctrl+C during the connect
        # wait) must end this session instead of orphaning it.
        try:
            return self._start_session(session_dir, marker, wait_connect, on_wait)
        except BaseException as e:
            trace(session_dir, f"start aborted: {type(e).__name__}: {e}")
            try:
                stop_active(reason="start aborted", cfg=self.cfg, expected_session=session_dir.name)
            except Exception:
                pass
            raise

    def _start_session(self, session_dir: Path, marker: dict, wait_connect: bool, on_wait) -> Path:
        b = self.cfg.browser
        atomic_write_json(session_dir / "metadata.json", {
            "browser_pid": None, "start_url": self.cfg.start_url or "",
            "browser": b, "recorder": "site-recorder", "recorder_version": __version__,
        })
        # Launch the recording browser, or — when it is already open — hand it
        # a blank tab: the new tab wakes the extension, which connects right
        # away. The start page is opened only AFTER the extension connected:
        # a page that loads before the debugger attaches is missing from the
        # network archive.
        log = session_dir / "browser.log"
        try:
            if browsers.is_running(b):
                browsers.launch(b, ["about:blank"], log_file=log)
            else:
                browsers.launch(b, None, log_file=log)
        except Exception as e:
            write_session_info(session_dir, self.cfg, status="empty", note=f"browser failed to start: {e}")
            raise RecorderError(f"Could not start {browsers.DISPLAY_NAME[b]}: {e}") from e

        marker["state"] = "recording"
        _write_marker(marker)
        trace(session_dir, f"recording started ({b} {browsers.browser_version(b)}, "
                           f"recorder pid {marker['recorder_pid']})")
        if wait_connect:
            deadline = time.time() + CONNECT_TIMEOUT_S
            while time.time() < deadline and not host_connected(session_dir):
                if on_wait:
                    on_wait()
                time.sleep(0.3)
            trace(session_dir, "extension connected" if host_connected(session_dir)
                  else f"extension did not connect within {CONNECT_TIMEOUT_S:.0f}s")
            if self.cfg.start_url:
                time.sleep(0.5)          # let the extension attach to the open tabs
                try:
                    browsers.launch(b, [self.cfg.start_url], log_file=log)
                except Exception as e:
                    trace(session_dir, f"could not open start page: {e}")
            procs = browsers.running_instances(b)
            if procs:
                meta = read_json(session_dir / "metadata.json") or {}
                meta["browser_pid"] = procs[0].pid
                atomic_write_json(session_dir / "metadata.json", meta)
        return session_dir

    # -- pause / resume ------------------------------------------------------------
    def set_paused(self, paused: bool) -> None:
        """Queue a pause/resume command (relayed to the extension by the host).

        The command counter lives in control.json itself, so this never
        rewrites active_session.json (and cannot race a concurrent stop)."""
        m = active_session()
        if not m or m.get("state") != "recording":
            raise RecorderError("No recording in progress.")
        sd = Path(m["session_dir"])
        prev = read_json(sd / "control.json") or {}
        st = read_json(sd / "status.json") or {}
        cmd = max(int(prev.get("cmd") or 0), int(st.get("cmd") or 0)) + 1
        atomic_write_json(sd / "control.json", {"paused": bool(paused), "cmd": cmd})

    @staticmethod
    def is_paused() -> bool:
        m = active_session()
        if not m:
            return False
        st = read_json(Path(m["session_dir"]) / "status.json") or {}
        return bool(st.get("paused"))

    # -- stop ------------------------------------------------------------------------
    def stop(self, *, close_browser: bool = True, reason: str = "stopped by participant") -> Path | None:
        return stop_active(close_browser=close_browser, reason=reason, cfg=self.cfg)


def stop_active(*, close_browser: bool = True, reason: str = "stopped by participant",
                cfg: Config | None = None, expected_session: str | None = None,
                ended_at: float | None = None) -> Path | None:
    """Stop the active session (from any process) and finalize it.

    Serialized: a second caller waits for a stop already in progress and then
    finds nothing left to stop. With ``expected_session`` it only stops that
    session (never one started meanwhile by someone else)."""
    with _Lifecycle(timeout=FLUSH_TIMEOUT_S + 60, what="stop the recording"):
        return _stop_unlocked(close_browser=close_browser, reason=reason, cfg=cfg,
                              expected_session=expected_session, ended_at=ended_at)


def _stop_unlocked(*, close_browser: bool, reason: str, cfg: Config | None,
                   expected_session: str | None = None, ended_at: float | None = None) -> Path | None:
    m = active_session()
    if not m:
        _clear_marker()        # a marker whose folder vanished
        return None
    if expected_session and m.get("session_id") != expected_session:
        return None
    session_dir = Path(m["session_dir"])
    # 1) Mark "stopping" FIRST: any reconnect racing the stop is refused by
    #    the host from here on (no zombie host re-opening the session).
    m["state"] = "stopping"
    _write_marker(m)
    trace(session_dir, f"stop requested ({reason})")
    # 2) Ask the live host to end the session (it relays to the extension,
    #    which detaches its debuggers and acknowledges; the host flushes).
    stop_ts = time.time()
    try:
        (session_dir / "stop.request").write_text(str(stop_ts), encoding="utf-8")
    except OSError:
        pass
    if host_connected(session_dir):
        deadline = time.time() + FLUSH_TIMEOUT_S
        while time.time() < deadline and not _host_finished_after(session_dir, stop_ts):
            time.sleep(0.2)
        trace(session_dir, "host finished" if _host_finished_after(session_dir, stop_ts)
              else f"host did not confirm the flush within {FLUSH_TIMEOUT_S:.0f}s")
    # 3) Close the recording browser (it is ours — the participant's everyday
    #    browser is a different instance and is never touched).
    browser = m.get("browser") or ""
    if close_browser and browser in browsers.DISPLAY_NAME:
        browsers.close(browser)
    info = finalize(session_dir, cfg, reason=reason, started_at=m.get("started_at"),
                    browser=browser, ended_at=ended_at)
    _clear_marker(session_dir)
    trace(session_dir, f"finalized: {info.get('status')}")
    return session_dir


def write_session_info(session_dir: Path, cfg: Config | None, **kw) -> dict:
    info = read_json(session_dir / "session_info.json") or {}
    info.update({k: v for k, v in kw.items() if v is not None})
    info.setdefault("session_id", session_dir.name)
    info["recorder_version"] = __version__
    if cfg is not None:
        info.setdefault("participant_id", cfg.participant_id)
    atomic_write_json(session_dir / "session_info.json", info, indent=2)
    return info


def _last_activity(session_dir: Path) -> float | None:
    """When the session last captured anything (for sessions recovered later)."""
    times = screenshot_times(session_dir)
    cands = [times[-1]] if times else []
    for name in (".host_done", ".host_alive"):
        d = read_json(session_dir / name)
        if isinstance(d, dict) and d.get("ts"):
            cands.append(float(d["ts"]))
    return max(cands) if cands else None


def finalize(session_dir: Path, cfg: Config | None = None, *, reason: str = "",
             started_at: float | None = None, browser: str = "",
             ended_at: float | None = None) -> dict:
    """Write session_info.json and remove per-session plumbing. Idempotent."""
    times = screenshot_times(session_dir)
    stats = read_json(session_dir / "live_stats.json") or {}
    connected = host_connected(session_dir)
    warc = session_dir / "archive" / "session.warc.gz"
    has_network = warc.exists() and warc.stat().st_size > 0
    has_pages = (session_dir / "dom_snapshots" / "index.jsonl").exists()
    if not connected:
        status, note = "empty", ("The extension never connected — nothing was captured. "
                                 "Check Setup (extension installed and enabled).")
    elif not (times or has_network or has_pages):
        status, note = "empty", "The extension connected, but nothing was captured (no pages visited?)."
    elif not times:
        status, note = "complete", ("No screenshots: they are taken when you click, type, scroll or "
                                    "rest the mouse on a page.")
    else:
        status, note = "complete", ""
    ended_at = ended_at or time.time()
    started_at = started_at or (times[0] if times else None)
    for name in _PLUMBING:
        try:
            (session_dir / name).unlink()
        except OSError:
            pass
    info = write_session_info(
        session_dir, cfg,
        status=status, note=note, end_reason=reason,
        started_at=started_at,
        ended_at=ended_at,
        first_frame_at=times[0] if times else None,
        last_frame_at=times[-1] if times else None,
        # wall-clock length of the recording (pauses included)
        duration_s=round(max(0.0, ended_at - started_at), 1) if started_at else 0.0,
        counts=stats or None,
        n_screenshots=len(times),
        browser=browser or None,
        browser_version=browsers.browser_version(browser) if browser in browsers.DISPLAY_NAME else None,
        os=f"{platform.system()} {platform.release()} ({platform.machine()})",
        python=sys.version.split()[0],
    )
    meta = read_json(session_dir / "metadata.json") or {}
    meta["stopped_at"] = info.get("ended_at")
    try:
        atomic_write_json(session_dir / "metadata.json", meta)
    except OSError:
        pass
    # Cached for the session list (rescanning big folders is slow).
    info = write_session_info(session_dir, cfg, size_bytes=dir_size(session_dir))
    return info


def recover(cfg: Config | None = None) -> Path | None:
    """Finish a session whose recorder died (app crash, closed terminal, reboot)."""
    with _Lifecycle(timeout=FLUSH_TIMEOUT_S + 60, what="recover the previous recording"):
        return _recover_unlocked(cfg)


def _recover_unlocked(cfg: Config | None) -> Path | None:
    m = _read_marker()
    if not m:
        return None
    sd = Path(m.get("session_dir", ""))
    if not sd.is_dir():
        _clear_marker()
        return None
    if _recorder_alive(m) and m.get("state") in ("starting", "recording"):
        return None  # a live recorder owns it
    return _stop_unlocked(close_browser=True, reason="recovered after the recorder exited", cfg=cfg,
                          ended_at=_last_activity(sd))


def adopt_if_orphaned() -> dict | None:
    """Take ownership of an active session whose recorder died — the GUI calls
    this at startup so a crash of the app does not end a running recording."""
    with _Lifecycle(timeout=FLUSH_TIMEOUT_S + 60, what="resume the running recording"):
        m = _read_marker()
        if not m or m.get("state") != "recording" or _recorder_alive(m):
            return None
        browser = m.get("browser") or ""
        if browser in browsers.DISPLAY_NAME and browsers.is_running(browser):
            pid, created = _self_identity()
            m["recorder_pid"], m["recorder_started"] = pid, created
            _write_marker(m)
            return m
        return None
