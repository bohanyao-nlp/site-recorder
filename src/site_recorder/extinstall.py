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

The DevTools pipe is file descriptors 3 (commands in) and 4 (responses out)
of the browser process. POSIX passes them with a shell redirect; Windows
passes C-runtime descriptors through ``os.spawnv`` (the CRT hands its fd
table to the child, the mechanism Puppeteer/Node use). That juggles fds 3/4
of the CALLING process, so the work runs in a fresh helper process:
``python -m site_recorder.extinstall devmode <browser>``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import browsers, paths

RESULT_PREFIX = "EXTINSTALL_RESULT "


class _Pipe:
    def __init__(self, wfd: int, rfd: int):
        self.w = os.fdopen(wfd, "wb", buffering=0)
        self.r = os.fdopen(rfd, "rb", buffering=0)
        self.pending: dict[int, dict] = {}
        self.cv = threading.Condition()
        self.next_id = 1
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        buf = b""
        while True:
            try:
                chunk = self.r.read(65536)
            except OSError:
                chunk = b""
            if not chunk:
                with self.cv:
                    self.closed = True
                    self.cv.notify_all()
                return
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "id" in msg:
                    with self.cv:
                        self.pending[msg["id"]] = msg
                        self.cv.notify_all()

    def send(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 20.0) -> dict:
        mid = self.next_id
        self.next_id += 1
        m = {"id": mid, "method": method, "params": params or {}}
        if session:
            m["sessionId"] = session
        self.w.write(json.dumps(m).encode() + b"\0")
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
        for f in (self.w, self.r):
            try:
                f.close()
            except OSError:
                pass


def _spawn_with_pipe(cmd: list[str]) -> tuple[int, _Pipe]:
    """Start the browser with DevTools pipe fds 3 (in) and 4 (out)."""
    if sys.platform == "win32":
        # Occupy every free descriptor up to 4 with placeholders FIRST, so the
        # pipes below can never be allocated at 3/4 themselves (aliasing them
        # would hand the browser our own ends). Single-threaded here.
        held = []
        while True:
            fd = os.open(os.devnull, os.O_RDWR)
            if fd > 4:
                os.close(fd)
                break
            held.append(fd)
        saved = {fd: os.dup(fd) for fd in (3, 4) if fd not in held}   # pre-existing files
        to_b_r, to_b_w = os.pipe()       # we write commands -> browser reads fd 3
        from_b_r, from_b_w = os.pipe()   # browser writes fd 4 -> we read
        os.dup2(to_b_r, 3, inheritable=True)
        os.dup2(from_b_w, 4, inheritable=True)
        # The browser's stdio -> null (it logs a lot), restored right after.
        sys.stdout.flush()
        sys.stderr.flush()
        null = os.open(os.devnull, os.O_RDWR)
        std_saved = {}
        for fd in (0, 1, 2):
            try:
                std_saved[fd] = os.dup(fd)
            except OSError:
                std_saved[fd] = None
            os.dup2(null, fd, inheritable=True)
        try:
            args = [subprocess.list2cmdline([a]) for a in cmd]
            handle = os.spawnv(os.P_NOWAIT, cmd[0], args)   # CRT passes fds 0-4
        finally:
            for fd, orig in std_saved.items():
                if orig is not None:
                    os.dup2(orig, fd, inheritable=True)
                    os.close(orig)
            os.close(null)
            for fd in (3, 4):
                if fd in saved:
                    orig = saved.pop(fd)
                    os.dup2(orig, fd, inheritable=False)
                    os.close(orig)
                else:
                    os.close(fd)
            for fd in held:
                if fd not in (3, 4):
                    os.close(fd)
        pid = _pid_from_handle(handle)
    else:
        to_b_r, to_b_w = os.pipe()       # we write commands -> browser reads fd 3
        from_b_r, from_b_w = os.pipe()   # browser writes fd 4 -> we read
        sh = ["/bin/sh", "-c", f'exec "$0" "$@" 3<&{to_b_r} 4>&{from_b_w}', *cmd]
        p = subprocess.Popen(sh, pass_fds=(to_b_r, from_b_w), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        pid = p.pid
    os.close(to_b_r)
    os.close(from_b_w)
    return pid, _Pipe(to_b_w, from_b_r)


def _pid_from_handle(handle: int) -> int:
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetProcessId(ctypes.c_void_p(handle)))
    except Exception:
        return 0


def _launch_debuggable(browser: str, log):
    """Start the recording browser with a DevTools pipe (setup/test only)."""
    exe = browsers.find_executable(browser)
    if not exe:
        raise RuntimeError(f"{browsers.DISPLAY_NAME[browser]} is not installed")
    if browsers.is_running(browser):
        raise RuntimeError("close the recording browser window first")
    browsers.seed_profile(browser)
    profile = paths.profile_dir(browser)
    browsers._clear_stale_singleton(profile)
    cmd = [exe, f"--user-data-dir={profile}", *browsers.BASE_FLAGS,
           "--remote-debugging-pipe", "--enable-unsafe-extension-debugging", "about:blank"]
    if sys.platform.startswith("linux"):
        cmd.insert(-1, "--password-store=basic")
    log(f"launching {browsers.DISPLAY_NAME[browser]} with a DevTools pipe")
    pid, pipe = _spawn_with_pipe(cmd)
    ver = pipe.send("Browser.getVersion", timeout=40)
    log(f"connected to {ver.get('product')}")
    return pid, pipe


def _set_developer_mode(pipe: _Pipe, browser: str, log) -> str:
    """developerPrivate.updateProfileConfiguration on the extensions page —
    exactly what clicking the page's Developer-mode toggle does."""
    scheme = "edge" if browser == "edge" else "chrome"
    tgt = pipe.send("Target.createTarget", {"url": f"{scheme}://extensions/"})
    sess = pipe.send("Target.attachToTarget", {"targetId": tgt["targetId"], "flatten": True})["sessionId"]
    dm = "?"
    for _ in range(30):
        r = pipe.send("Runtime.evaluate", {
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
        pipe.send("Target.closeTarget", {"targetId": tgt["targetId"]})
    except Exception:
        pass
    return dm


def _devmode(browser: str, log) -> tuple[bool, str]:
    pid, pipe = _launch_debuggable(browser, log)
    try:
        dm = _set_developer_mode(pipe, browser, log)
        time.sleep(1.5)                       # let the profile persist the setting
        try:
            pipe.send("Browser.close", timeout=10)
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
    pid, pipe = _launch_debuggable(browser, log)
    try:
        scheme = "edge" if browser == "edge" else "chrome"
        tgt = pipe.send("Target.createTarget", {"url": f"{scheme}://extensions/"})
        sess = pipe.send("Target.attachToTarget", {"targetId": tgt["targetId"], "flatten": True})["sessionId"]
        res = "?"
        for _ in range(30):
            r = pipe.send("Runtime.evaluate", {
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
            pipe.send("Browser.close", timeout=10)
        except Exception:
            pass
        return (res == "ok"), ("reloaded" if res == "ok" else f"reload failed ({res})")
    finally:
        _wait_gone(browser, timeout=20)


def launch_with_temporary_extension(browser: str, log=print):
    """TEST ONLY: start the recording browser with the bundled extension loaded
    for this launch only. ``Extensions.loadUnpacked`` installs are purged at
    the next normal start, and the pipe flags make ``navigator.webdriver``
    true — never use this for real recordings. Must run in a process that owns
    nothing at fds 3/4. Returns (pid, pipe); closing the pipe ends the browser."""
    from .recorder import ensure_extension_copy
    ext = ensure_extension_copy()
    pid, pipe = _launch_debuggable(browser, log)
    res = pipe.send("Extensions.loadUnpacked", {"path": str(ext)}, timeout=40)
    log(f"temporarily loaded extension id {res.get('id')}")
    return pid, pipe


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
