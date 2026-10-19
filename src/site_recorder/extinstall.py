"""Extension-setup helpers that need a DevTools connection to the browser.

How the SITE Collector gets into the recording profile (findings, Chrome/Edge
155): ``--load-extension`` is ignored by branded Chrome, and an extension
loaded with the DevTools ``Extensions.loadUnpacked`` command is TEMPORARY
(purged at the next normal start). The persistent options are a Chrome Web
Store install (primary: two clicks, no developer mode) or a one-time "Load
unpacked" of the bundled copy, which only works while Developer mode is ON.

Developer mode lives in the integrity-protected Secure Preferences and cannot
be pre-seeded in a file. This module flips it ON through the extensions
page's own API over a one-time DevTools connection, so the participant only
has to click "Load unpacked". Recording launches never carry debugging
switches (they would expose ``navigator.webdriver`` to websites).

The connection is the browser's DevTools websocket on a random localhost
port (``--remote-debugging-port=0``; the browser writes the port to
``<profile>/DevToolsActivePort``). Not ``--remote-debugging-pipe``: Edge 155
relaunches itself at startup, and the relaunched browser never starts when
the launcher was given a pipe. The work runs in a helper process
(``python -m site_recorder.extinstall devmode <browser>``) so a hung browser
can be timed out without blocking the GUI.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import browsers, paths

RESULT_PREFIX = "EXTINSTALL_RESULT "


def _mask(data: bytes, key: bytes) -> bytes:
    n = len(data)
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


class _Cdp:
    """Minimal CDP client over the browser-level DevTools websocket (RFC 6455,
    stdlib only: text frames out, masked as a client must; frames in)."""

    def __init__(self, port: int, path: str, timeout: float = 10.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        # No Origin header: the browser only checks origins of clients that send one.
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.r = self.sock.makefile("rb")
        status = self.r.readline()
        while self.r.readline() not in (b"\r\n", b""):
            pass
        if b" 101 " not in status:
            self.close()
            raise RuntimeError(f"DevTools refused the connection: {status.decode(errors='replace').strip()}")
        self.sock.settimeout(None)
        self.wlock = threading.Lock()
        self.pending: dict[int, dict] = {}
        self.cv = threading.Condition()
        self.next_id = 1
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _read(self, n: int) -> bytes:
        data = self.r.read(n)
        if len(data) < n:
            raise OSError("connection closed")
        return data

    def _write(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        key = os.urandom(4)
        with self.wlock:
            self.sock.sendall(head + key + _mask(payload, key))

    def _reader(self) -> None:
        msg = b""
        try:
            while True:
                b0, b1 = self._read(2)
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                key = self._read(4) if b1 & 0x80 else b""
                data = self._read(n)
                if key:
                    data = _mask(data, key)
                op = b0 & 0x0F
                if op == 0x8:                       # close
                    break
                if op == 0x9:                       # ping
                    self._write(0xA, data)
                    continue
                if op not in (0x0, 0x1, 0x2):
                    continue
                msg += data
                if not b0 & 0x80:                   # more fragments follow
                    continue
                raw, msg = msg, b""
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if "id" in m:
                    with self.cv:
                        self.pending[m["id"]] = m
                        self.cv.notify_all()
        except (OSError, ValueError):
            pass
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def send(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 20.0) -> dict:
        mid = self.next_id
        self.next_id += 1
        m = {"id": mid, "method": method, "params": params or {}}
        if session:
            m["sessionId"] = session
        try:
            self._write(0x1, json.dumps(m).encode())
        except OSError:
            raise TimeoutError(f"{method}: the browser closed the connection") from None
        deadline = time.time() + timeout
        with self.cv:
            while mid not in self.pending:
                left = deadline - time.time()
                if left <= 0 or self.closed:
                    raise TimeoutError(f"{method}: no response from the browser")
                self.cv.wait(left)
            resp = self.pending.pop(mid)
        if "error" in resp:
            raise RuntimeError(f"{method}: {resp['error'].get('message')}")
        return resp.get("result", {})

    def close(self) -> None:
        for f in (self.r, self.sock):
            try:
                f.close()
            except OSError:
                pass


def _devtools_endpoint(port_file: Path, timeout: float) -> tuple[int, str]:
    """(port, browser websocket path) from the profile's DevToolsActivePort."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            lines = port_file.read_text(encoding="utf-8").splitlines()
            if len(lines) >= 2 and lines[1].startswith("/devtools/browser/"):
                return int(lines[0]), lines[1]
        except (OSError, ValueError):
            pass                              # not written (completely) yet
        time.sleep(0.25)
    raise TimeoutError("the browser did not open a DevTools port")


def _launch_debuggable(browser: str, log) -> _Cdp:
    """Start the recording browser with a DevTools port (setup/test only)."""
    exe = browsers.find_executable(browser)
    if not exe:
        raise RuntimeError(f"{browsers.DISPLAY_NAME[browser]} is not installed")
    if browsers.is_running(browser):
        raise RuntimeError("close the recording browser window first")
    browsers.seed_profile(browser)
    profile = paths.profile_dir(browser)
    browsers._clear_stale_singleton(profile)
    port_file = profile / "DevToolsActivePort"
    port_file.unlink(missing_ok=True)         # a previous run's port
    # Port 0: the browser picks a free port and, outside headless mode,
    # listens on 127.0.0.1 only.
    cmd = [exe, f"--user-data-dir={profile}", *browsers.BASE_FLAGS,
           "--remote-debugging-port=0", "--enable-unsafe-extension-debugging", "about:blank"]
    if sys.platform.startswith("linux"):
        cmd.insert(-1, "--password-store=basic")
    log(f"launching {browsers.DISPLAY_NAME[browser]} with a DevTools port")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "env": browsers._browser_env(),
                    "cwd": str(Path.home())}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    # The launched process may only hand over to a relaunched browser (Edge),
    # so the browser is found by its profile afterwards, not by this pid.
    subprocess.Popen(cmd, **kwargs)
    try:
        port, path = _devtools_endpoint(port_file, timeout=40)
        cdp = _Cdp(port, path)
        ver = cdp.send("Browser.getVersion")
    except Exception:
        browsers.close(browser, timeout=10)
        raise
    log(f"connected to {ver.get('product')}")
    return cdp


def _set_developer_mode(cdp: _Cdp, browser: str, log) -> str:
    """developerPrivate.updateProfileConfiguration on the extensions page —
    exactly what clicking the page's Developer-mode toggle does."""
    scheme = "edge" if browser == "edge" else "chrome"
    tgt = cdp.send("Target.createTarget", {"url": f"{scheme}://extensions/"})
    sess = cdp.send("Target.attachToTarget", {"targetId": tgt["targetId"], "flatten": True})["sessionId"]
    dm = "?"
    for _ in range(30):
        r = cdp.send("Runtime.evaluate", {
            "expression": "(chrome.developerPrivate ? chrome.developerPrivate"
                          ".updateProfileConfiguration({inDeveloperMode: true})"
                          ".then(() => 'ok').catch(e => 'ERR ' + e.message) : 'notready')",
            "awaitPromise": True, "returnByValue": True, "userGesture": True}, session=sess)
        dm = ((r.get("result") or {}).get("value")) or "?"
        if dm != "notready":
            break
        time.sleep(0.5)
    log(f"developer mode: {dm}")
    try:
        cdp.send("Target.closeTarget", {"targetId": tgt["targetId"]})
    except Exception:
        pass
    return dm


def _devmode(browser: str, log) -> tuple[bool, str]:
    cdp = _launch_debuggable(browser, log)
    try:
        dm = _set_developer_mode(cdp, browser, log)
        time.sleep(1.5)                       # let the profile persist the setting
        try:
            cdp.send("Browser.close", timeout=10)
        except Exception:
            pass
        if dm == "ok":
            return True, "Developer mode is on"
        return False, f"could not enable Developer mode ({dm})"
    finally:
        _wait_gone(browser, timeout=20)


def _reload(browser: str, log) -> tuple[bool, str]:
    """developerPrivate.reload of the unpacked extension — what its Reload
    button does: re-reads the files and re-registers the service worker."""
    from . import EXTENSION_ID
    cdp = _launch_debuggable(browser, log)
    try:
        scheme = "edge" if browser == "edge" else "chrome"
        tgt = cdp.send("Target.createTarget", {"url": f"{scheme}://extensions/"})
        sess = cdp.send("Target.attachToTarget", {"targetId": tgt["targetId"], "flatten": True})["sessionId"]
        res = "?"
        for _ in range(30):
            r = cdp.send("Runtime.evaluate", {
                "expression": "(chrome.developerPrivate ? chrome.developerPrivate"
                              f".reload('{EXTENSION_ID}', {{failQuietly: true}})"
                              ".then(() => 'ok').catch(e => 'ERR ' + (e && e.message || e)) : 'notready')",
                "awaitPromise": True, "returnByValue": True, "userGesture": True}, session=sess)
            res = ((r.get("result") or {}).get("value")) or "?"
            if res != "notready":
                break
            time.sleep(0.5)
        log(f"reload: {res}")
        time.sleep(2.0)                       # let the new worker register + prefs persist
        try:
            cdp.send("Browser.close", timeout=10)
        except Exception:
            pass
        return (res == "ok"), ("reloaded" if res == "ok" else f"reload failed ({res})")
    finally:
        _wait_gone(browser, timeout=20)


def _wait_gone(browser: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and browsers.is_running(browser):
        time.sleep(0.3)
    if browsers.is_running(browser):
        browsers.close(browser, timeout=10)


def enable_developer_mode(browser: str, timeout: float = 120.0) -> tuple[bool, str]:
    """Switch Developer mode on in the recording profile (helper process)."""
    return _run_helper("devmode", browser, timeout)


def reload_extension(browser: str, timeout: float = 120.0) -> tuple[bool, str]:
    """Reload the unpacked extension so the browser runs its current files."""
    return _run_helper("reload", browser, timeout)


def _run_helper(action: str, browser: str, timeout: float) -> tuple[bool, str]:
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe" and py.with_name("python.exe").exists():
        py = py.with_name("python.exe")
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    try:
        # -P: don't let a stray json.py/signal.py in the caller's cwd shadow
        # the stdlib (and run from the app dir anyway).
        safe = ["-P"] if sys.version_info >= (3, 11) else []
        r = subprocess.run([str(py), *safe, "-m", "site_recorder.extinstall", action, browser],
                           capture_output=True, text=True, timeout=timeout, env=env,
                           cwd=str(paths.app_dir()),
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        # The helper was killed; do not leave its debuggable browser behind.
        if browsers.is_running(browser):
            browsers.close(browser, timeout=10)
        return False, "timed out"
    for line in r.stdout.splitlines()[::-1]:
        if line.startswith(RESULT_PREFIX):
            d = json.loads(line[len(RESULT_PREFIX):])
            return bool(d["ok"]), d["message"]
    return False, f"helper failed (exit {r.returncode}): {(r.stderr or r.stdout)[-400:]}"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] not in ("devmode", "reload") or argv[1] not in browsers.DISPLAY_NAME:
        print("usage: python -m site_recorder.extinstall devmode|reload chrome|edge", file=sys.stderr)
        return 2
    action, browser = argv
    lines: list[str] = []

    def log(m: str) -> None:
        lines.append(m)
        print(m, file=sys.stderr, flush=True)
    try:
        ok, msg = (_devmode if action == "devmode" else _reload)(browser, log)
    except Exception as e:  # report, never traceback into the GUI
        ok, msg = False, f"{type(e).__name__}: {e}"
    try:
        paths.logs_dir().mkdir(parents=True, exist_ok=True)
        with open(paths.logs_dir() / "extension_install.log", "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {action} {browser}\n"
                    + "\n".join(lines) + f"\nresult: ok={ok} {msg}\n")
    except OSError:
        pass
    print(RESULT_PREFIX + json.dumps({"ok": ok, "message": msg}), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
