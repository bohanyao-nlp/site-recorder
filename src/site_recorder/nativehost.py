"""Register the native-messaging host with Chrome / Edge.

The browser finds a host by NAME (``com.sitebench.collector``) through a
small JSON manifest that names the executable to start and which extension
ids may connect:

* Windows — a registry value under HKCU (one key per browser family) whose
  default value is the manifest's path. The manifest's ``path`` points at a
  ``.bat`` wrapper (Chrome starts hosts via ``cmd /c``).
* macOS / Linux — the manifest file itself, in ``<user-data-dir>/
  NativeMessagingHosts`` (Chromium derives DIR_USER_NATIVE_MESSAGING from
  the user-data dir and does NOT fall back to the default profile's folder).
  So the host is registered INSIDE the dedicated recording profile only — the
  participant's everyday browser never sees it.

The wrapper bakes in the absolute path of the Python interpreter of the
installed recorder (the uv tool venv) and the app dir, because the browser
starts the host with a minimal environment.

Registration is permanent (written at setup, refreshed before every
recording) — whether a connection is SERVED is decided per connection by the
host from ``active_session.json``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import ALLOWED_EXTENSION_IDS, NATIVE_HOST_NAME, paths

_WIN_REG_BASES = {
    "chrome": [r"Software\Google\Chrome\NativeMessagingHosts",
               r"Software\Chromium\NativeMessagingHosts"],
    # Edge reads its own key first; registering under Chrome's key too is
    # harmless and covers Edge builds that fall back to it.
    "edge": [r"Software\Microsoft\Edge\NativeMessagingHosts"],
}


def host_python() -> str:
    """The interpreter that runs the host: the one running us right now.

    On Windows a gui-script launcher runs ``pythonw.exe``; the host needs the
    console interpreter next to it (stdio pipes are what the protocol uses)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def wrapper_path() -> Path:
    return paths.host_dir() / ("site_recorder_host.bat" if sys.platform == "win32"
                               else "site_recorder_host.sh")


def manifest_path() -> Path:
    return paths.host_dir() / f"{NATIVE_HOST_NAME}.json"


def _manifest() -> dict:
    return {
        "name": NATIVE_HOST_NAME,
        "description": "SITE Recorder native messaging host",
        "path": str(wrapper_path()),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{i}/" for i in ALLOWED_EXTENSION_IDS],
    }


_ENV_PREFIXES = ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "ProgramFiles", "ProgramFiles(x86)")


def batch_path(p: str | Path) -> str:
    """A path for a .bat line, spelled through %ENV% variables where possible.

    cmd.exe decodes batch-file BYTES with the console code page, which varies
    (437, 850, 65001 …), so a literal non-ASCII user name in a path can break
    the script. Environment variables are expanded inside cmd in Unicode, so
    ``%LOCALAPPDATA%\\SiteRecorder`` works for any user name."""
    s = str(p)
    for var in _ENV_PREFIXES:
        val = os.environ.get(var)
        if val and s.lower().startswith(val.rstrip("\\").lower() + "\\"):
            return f"%{var}%" + s[len(val.rstrip("\\")):]
    return s


def write_batch(path: Path, body_lines: list[str]) -> None:
    """Write a Windows .bat/.cmd that survives non-ASCII paths.

    Lines should spell paths with batch_path(). If anything non-ASCII remains,
    switch the console to UTF-8 first (cmd re-decodes each later line with the
    current code page; output to nul — for the host, stdout is the protocol)."""
    text = "\r\n".join(body_lines) + "\r\n"
    if text.isascii():
        data = ("@echo off\r\n" + text).encode("ascii")
    else:
        data = ("@echo off\r\nchcp 65001 >nul 2>nul\r\n" + text).encode("utf-8")
    path.write_bytes(data)


def write_wrapper() -> Path:
    paths.host_dir().mkdir(parents=True, exist_ok=True)
    py = host_python()
    app = str(paths.app_dir())
    w = wrapper_path()
    if sys.platform == "win32":
        # PYTHONUTF8: the browser's environment has no UTF-8 locale on Windows.
        write_batch(w, [
            "set PYTHONUTF8=1",
            "set PYTHONIOENCODING=utf-8",
            "set PYTHONSAFEPATH=1",
            f'"{batch_path(py)}" -m site_recorder.host --app-dir="{batch_path(app)}" %*',
        ])
    else:
        q = lambda s: "'" + s.replace("'", "'\\''") + "'"
        w.write_text(
            "#!/bin/sh\n"
            "export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONSAFEPATH=1\n"
            f"exec {q(py)} -m site_recorder.host --app-dir={q(app)} \"$@\"\n",
            encoding="utf-8")
        w.chmod(0o755)
    return w


def manifest_dirs(browser: str) -> list[Path]:
    """Where the manifest FILE goes on macOS/Linux: inside the recording profile."""
    return [paths.profile_dir(browser) / "NativeMessagingHosts"]


def register(browser: str) -> list[str]:
    """(Re)write wrapper + manifest and register for ``browser``.

    Idempotent; returns human-readable locations written."""
    write_wrapper()
    m = _manifest()
    mp = manifest_path()
    mp.write_text(json.dumps(m, indent=2), encoding="utf-8")
    written = [str(mp)]
    if sys.platform == "win32":
        import winreg
        for base in _WIN_REG_BASES[browser]:
            key_path = f"{base}\\{NATIVE_HOST_NAME}"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(mp))
            written.append(f"HKCU\\{key_path}")
    else:
        for d in manifest_dirs(browser):
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{NATIVE_HOST_NAME}.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
            written.append(str(d))
    return written


def is_registered(browser: str) -> tuple[bool, str]:
    """Does the browser resolve our host name to OUR manifest/wrapper?"""
    w = wrapper_path()
    if not w.exists():
        return False, "host wrapper missing"
    want = batch_path(host_python()) if sys.platform == "win32" else host_python()
    if want not in w.read_text(encoding="utf-8", errors="replace"):
        return False, "host wrapper points at a different Python (reinstalled?)"
    if sys.platform == "win32":
        import winreg
        for base in _WIN_REG_BASES[browser]:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\{NATIVE_HOST_NAME}") as key:
                    val, _ = winreg.QueryValueEx(key, "")
            except OSError:
                return False, f"registry key HKCU\\{base}\\{NATIVE_HOST_NAME} missing"
            if os.path.normcase(val) != os.path.normcase(str(manifest_path())):
                return False, f"registry points at another manifest ({val})"
        return True, "registered"
    f = manifest_dirs(browser)[0] / f"{NATIVE_HOST_NAME}.json"
    try:
        m = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, f"manifest missing in {f.parent}"
    if m.get("path") != str(w):
        return False, f"manifest in {f.parent} points elsewhere"
    return True, "registered"


def unregister() -> None:
    """Remove every registration this module may have written (uninstall)."""
    if sys.platform == "win32":
        import winreg
        for bases in _WIN_REG_BASES.values():
            for base in bases:
                key_path = f"{base}\\{NATIVE_HOST_NAME}"
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                        val, _ = winreg.QueryValueEx(key, "")
                    # Only remove keys that point at OUR manifest — the in-repo
                    # dev recorder registers the same host name.
                    if os.path.normcase(val) == os.path.normcase(str(manifest_path())):
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
                except OSError:
                    pass
    else:
        for browser in ("chrome", "edge"):
            for d in manifest_dirs(browser):
                f = d / f"{NATIVE_HOST_NAME}.json"
                try:
                    if json.loads(f.read_text(encoding="utf-8")).get("path") == str(wrapper_path()):
                        f.unlink()
                except (OSError, ValueError):
                    pass
    for f in (manifest_path(), wrapper_path()):
        try:
            f.unlink()
        except OSError:
            pass
