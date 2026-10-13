"""Find, launch, inspect and close the dedicated recording browser.

The recording browser is Chrome or Edge started with
``--user-data-dir=<app_dir>/profiles/<browser>``. A separate user-data dir
makes it a separate browser INSTANCE: it runs side by side with the
participant's everyday browser (which is never touched), and everything we
need to know about it is visible on its command line.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import EXTENSION_ID, paths

DISPLAY_NAME = {"chrome": "Google Chrome", "edge": "Microsoft Edge"}

# Flags for every launch of the recording browser.
BASE_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    # The extension attaches the debugger (CDP) to capture network traffic;
    # this hides the "…started debugging this browser" infobar.
    "--silent-debugger-extension-api",
    # Edge implicitly signs a new profile into the Windows Microsoft account
    # and starts syncing — that would pull the participant's real bookmarks,
    # passwords and extensions into the recording profile (and push recorded
    # browsing out). Browser sync is never wanted here; website logins work.
    "--disable-sync",
]


# ---------------------------------------------------------------------------
# Executable discovery
# ---------------------------------------------------------------------------

def _candidates(browser: str) -> list[str]:
    if sys.platform == "win32":
        rel = {"chrome": [r"Google\Chrome\Application\chrome.exe"],
               "edge": [r"Microsoft\Edge\Application\msedge.exe"]}[browser]
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")]
        out = [str(Path(r) / x) for r in roots if r for x in rel]
        out += _windows_app_paths(browser)
        return out
    if sys.platform == "darwin":
        app = {"chrome": ["Google Chrome.app/Contents/MacOS/Google Chrome"],
               "edge": ["Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]}[browser]
        return [str(Path(root) / a) for root in ("/Applications", str(Path.home() / "Applications"))
                for a in app]
    names = {"chrome": ["google-chrome", "google-chrome-stable", "google-chrome-beta"],
             "edge": ["microsoft-edge", "microsoft-edge-stable", "microsoft-edge-beta"]}[browser]
    out = [shutil.which(n) or "" for n in names]
    out += {"chrome": ["/opt/google/chrome/chrome", "/usr/bin/google-chrome"],
            "edge": ["/opt/microsoft/msedge/msedge", "/usr/bin/microsoft-edge"]}[browser]
    return out


def _windows_app_paths(browser: str) -> list[str]:
    """Install location recorded by the installer (App Paths registry key)."""
    try:
        import winreg
    except ImportError:
        return []
    exe = {"chrome": "chrome.exe", "edge": "msedge.exe"}[browser]
    out = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                val, _ = winreg.QueryValueEx(k, "")
                out.append(val.strip('"'))
        except OSError:
            pass
    return out


def find_executable(browser: str) -> str | None:
    for c in _candidates(browser):
        if c and Path(c).is_file():
            return c
    return None


def installed_browsers() -> list[str]:
    return [b for b in ("chrome", "edge") if find_executable(b)]


def browser_version(browser: str) -> str:
    exe = find_executable(browser)
    if not exe:
        return ""
    try:
        if sys.platform == "win32":
            # chrome.exe sits next to a "<version>" folder.
            vers = [p.name for p in Path(exe).parent.iterdir()
                    if p.is_dir() and re.fullmatch(r"\d+(\.\d+){3}", p.name)]
            return max(vers, key=lambda v: tuple(int(x) for x in v.split("."))) if vers else ""
        if sys.platform == "darwin":
            import plistlib
            plist = Path(exe).parents[1] / "Info.plist"
            with open(plist, "rb") as f:
                return str(plistlib.load(f).get("CFBundleShortVersionString", ""))
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"(\d+(\.\d+)+)", out)
        return m.group(1) if m else out.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Running instances of the recording profile
# ---------------------------------------------------------------------------

def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def _arg_user_data_dir(cmdline: list[str]) -> str | None:
    for i, a in enumerate(cmdline):
        if a.startswith("--user-data-dir="):
            return a.split("=", 1)[1].strip('"')
        if a == "--user-data-dir" and i + 1 < len(cmdline):
            return cmdline[i + 1]
    return None


def running_instances(browser: str) -> list:
    """Main browser processes (psutil.Process) serving the recording profile."""
    import psutil
    want = _norm(str(paths.profile_dir(browser)))
    found = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = proc.info.get("cmdline") or []
            if not cmd or any(a.startswith("--type=") for a in cmd):
                continue  # renderer/GPU/utility children carry --type=
            udd = _arg_user_data_dir(cmd)
            if udd and _norm(udd) == want:
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return found


def is_running(browser: str) -> bool:
    return bool(running_instances(browser))


# ---------------------------------------------------------------------------
# Launch / close
# ---------------------------------------------------------------------------

def launch(browser: str, urls: list[str] | None = None, *, log_file: Path | None = None,
           extra_args: list[str] | None = None) -> subprocess.Popen:
    """Start (or, if already running, hand ``urls`` to) the recording browser."""
    exe = find_executable(browser)
    if not exe:
        raise FileNotFoundError(f"{DISPLAY_NAME[browser]} is not installed (looked in the usual places)")
    profile = paths.profile_dir(browser)
    profile.mkdir(parents=True, exist_ok=True)
    running = is_running(browser)
    if not running:
        _clear_stale_singleton(profile)
    flags = [f"--user-data-dir={profile}", *BASE_FLAGS, *(extra_args or [])]
    if sys.platform.startswith("linux"):
        # A fresh profile would otherwise prompt for the desktop keyring.
        flags.append("--password-store=basic")
    if sys.platform == "darwin":
        # Through LaunchServices (-n: a new instance even though the
        # participant's everyday Chrome is open) so macOS attributes the
        # browser's own permission prompts to the browser, not to us. When our
        # instance already runs, the new process hands the URLs to it and quits.
        app = str(Path(exe).parents[2])            # …/Google Chrome.app
        cmd = ["open", "-n", "-a", app, "--args", *flags, *(urls or [])]
    else:
        cmd = [exe, *flags, *(urls or [])]
    out = open(log_file, "ab") if log_file else subprocess.DEVNULL
    # cwd = home: the browser (and the native host it starts) must not keep
    # some unrelated folder — e.g. the unzipped installer — in use.
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT
                    if log_file else subprocess.DEVNULL, "env": _browser_env(), "cwd": str(Path.home())}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True   # don't die with the recorder's terminal
    try:
        return subprocess.Popen(cmd, **kwargs)
    finally:
        if log_file:
            out.close()


def _clear_stale_singleton(profile: Path) -> None:
    """Remove a dead instance's Singleton* files (POSIX).

    SingletonLock is a symlink "<hostname>-<pid>". If the hostname changed
    (Macs rename themselves across networks) Chrome refuses to start with
    "profile in use on another computer". Only called when no browser is
    running on this profile, so the lock is necessarily stale."""
    if sys.platform == "win32":
        return
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = profile / name
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
        except OSError:
            pass


def unsupported_reason(browser: str) -> str:
    """Why a found browser cannot host the recorder (Linux snap/flatpak)."""
    exe = find_executable(browser)
    if not exe or not sys.platform.startswith("linux"):
        return ""
    real = os.path.realpath(exe)
    if real.startswith("/snap/") or "/flatpak/" in real:
        return (f"{DISPLAY_NAME[browser]} at {real} is a snap/flatpak build — its sandbox cannot "
                "start the recorder. Install the .deb/.rpm from the official website.")
    return ""


def _browser_env() -> dict:
    env = dict(os.environ)
    # Our own interpreter's variables must not leak into the browser (and from
    # there into the native host it spawns).
    for k in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        env.pop(k, None)
    return env


def close(browser: str, timeout: float = 15.0) -> bool:
    """Close the recording browser gracefully; force only as a last resort.

    Graceful matters: a killed browser marks the profile as crashed (restore
    bubble next time) and can lose the last moments of the session."""
    import psutil
    procs = running_instances(browser)
    if not procs:
        return True
    for p in procs:
        try:
            if sys.platform == "win32":
                # WM_CLOSE to the browser's windows — like clicking the X.
                subprocess.run(["taskkill", "/PID", str(p.pid)], capture_output=True,
                               timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                p.send_signal(signal.SIGTERM)   # Chromium shuts down cleanly on SIGTERM
        except (psutil.NoSuchProcess, subprocess.TimeoutExpired, OSError):
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    if not alive:
        return True
    for p in alive:
        try:
            # The main process only (no /T): its renderers exit with it, and a
            # tree kill would also hit the native host if it is still flushing.
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(p.pid)], capture_output=True,
                               timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                p.kill()
        except (psutil.NoSuchProcess, subprocess.TimeoutExpired, OSError):
            pass
    psutil.wait_procs(alive, timeout=5)
    return False


# ---------------------------------------------------------------------------
# Profile inspection / seeding
# ---------------------------------------------------------------------------

def _profile_prefs_files(browser: str) -> list[Path]:
    d = paths.profile_dir(browser) / "Default"
    return [d / "Secure Preferences", d / "Preferences"]


def extension_state(browser: str) -> dict:
    """What the recording profile knows about the SITE Collector extension.

    {"installed": bool, "enabled": bool, "unpacked": bool, "path": str|None,
     "version": str|None}. Read from the profile's preference files, which the
    browser rewrites on exit and periodically while running."""
    state = {"installed": False, "enabled": False, "unpacked": False, "path": None,
             "version": None, "source": None, "problem": ""}
    for f in _profile_prefs_files(browser):
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        ext = (((data.get("extensions") or {}).get("settings")) or {}).get(EXTENSION_ID)
        if not isinstance(ext, dict):
            continue
        if int(ext.get("creation_flags") or 0) & 0x8000:
            # Loaded through DevTools for one launch only — purged at the next
            # start, so it does not count as installed.
            continue
        state["installed"] = True
        # location 1 = INTERNAL (Web Store), 4 = UNPACKED, 8 = COMMAND_LINE
        loc = ext.get("location")
        state["unpacked"] = loc in (4, 8)
        state["source"] = "unpacked" if loc in (4, 8) else "store"
        state["path"] = ext.get("path")
        reasons = ext.get("disable_reasons")
        if isinstance(reasons, list):
            bits = 0
            for r in reasons:
                try:
                    bits |= int(r)
                except (TypeError, ValueError):
                    bits |= 1
        else:
            bits = int(reasons or 0)
        disabled = bool(bits) or (reasons is None and ext.get("state") == 0)
        state["enabled"] = not disabled
        if bits & (1 << 24):
            state["problem"] = "developer_mode_off"
        elif disabled:
            state["problem"] = "disabled"
        break
    if not state["installed"]:
        # A Web Store install shows up as files before the preferences are
        # flushed (the browser writes them lazily while running).
        store_dir = paths.profile_dir(browser) / "Default" / "Extensions" / EXTENSION_ID
        if store_dir.is_dir() and any(p.is_dir() for p in store_dir.iterdir()):
            state.update(installed=True, enabled=True, source="store")
    state["version"] = _installed_version(browser, state)
    return state


def _installed_version(browser: str, state: dict) -> str | None:
    """Version from the installed copy's manifest.json."""
    cands: list[Path] = []
    if state.get("path"):
        p = Path(state["path"])
        if not p.is_absolute():   # store installs: "<id>/<version>_0" relative to Extensions/
            p = paths.profile_dir(browser) / "Default" / "Extensions" / p
        cands.append(p / "manifest.json")
    store_dir = paths.profile_dir(browser) / "Default" / "Extensions" / EXTENSION_ID
    if store_dir.is_dir():
        cands += sorted(store_dir.glob("*/manifest.json"), reverse=True)
    for c in cands:
        try:
            return json.loads(c.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError):
            continue
    return None


def seed_profile(browser: str) -> None:
    """Pre-set a FRESH recording profile so first launch needs no clicking.

    Only touches a profile the browser has never written; once Preferences
    exists the browser owns it (it rewrites the file on exit and checks
    integrity), so we leave it alone."""
    prof = paths.profile_dir(browser)
    default = prof / "Default"
    prefs = default / "Preferences"
    if prefs.exists():
        return
    default.mkdir(parents=True, exist_ok=True)
    downloads = paths.user_dir() / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    seed = {
        # Recording-profile downloads go to ~/SiteRecorder/downloads: kept out
        # of the participant's own Downloads, and readable by the recorder
        # (on macOS ~/Downloads is privacy-protected — the native host could
        # not copy a download from there into the session).
        "download": {"default_directory": str(downloads), "prompt_for_download": False,
                     "directory_upgrade": True},
        "savefile": {"default_directory": str(downloads)},
        # (Developer mode is a protected setting the browser ignores here; the
        # bundled-copy setup step switches it on through the extensions page.)
        "extensions": {"ui": {"developer_mode": True}},
        # Reopen the previous tabs when the recording browser starts again.
        "session": {"restore_on_startup": 1},
        "browser": {"has_seen_welcome_page": True, "check_default_browser": False},
        "profile": {"name": "SITE Recorder"},
    }
    prefs.write_text(json.dumps(seed), encoding="utf-8")
    local_state = prof / "Local State"
    if not local_state.exists():
        # Don't keep the browser alive in the background after its last
        # window closes (Edge enables this by default): closing the recording
        # window must end the browser process, which is how the app notices
        # that the participant finished.
        local_state.write_text(json.dumps({"background_mode": {"enabled": False}}), encoding="utf-8")
