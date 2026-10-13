"""First-time setup, diagnostics, shortcuts and uninstall."""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import EXTENSION_ID, __version__, browsers, config, nativehost, paths, recorder

# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------

def open_path(p: Path | str) -> None:
    """Reveal a folder/file in the OS file manager."""
    p = str(p)
    try:
        if sys.platform == "win32":
            os.startfile(p)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log_exception(e)


def _app_logger() -> logging.Logger:
    lg = logging.getLogger("site_recorder.app")
    if not lg.handlers:
        paths.logs_dir().mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(paths.logs_dir() / "app.log", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
    return lg


def log_exception(e: BaseException) -> None:
    try:
        _app_logger().error("".join(traceback.format_exception(type(e), e, e.__traceback__)))
    except Exception:
        pass


def single_instance_lock(name: str):
    """A held lock, or None if another process already holds it."""
    from .host import HostLock
    paths.app_dir().mkdir(parents=True, exist_ok=True)
    lock = HostLock(paths.app_dir() / f"{name}.lock")
    return lock if lock.acquire(timeout=0) else None


# ---------------------------------------------------------------------------
# preparing the recording profile
# ---------------------------------------------------------------------------

def prepare(browser: str) -> None:
    """Everything setup can do without the participant (idempotent)."""
    paths.ensure_dirs(config.load().sessions_path)
    recorder.ensure_extension_copy()
    browsers.seed_profile(browser)
    nativehost.register(browser)


def extensions_page(browser: str) -> str:
    return "edge://extensions/" if browser == "edge" else "chrome://extensions/"


def store_instructions(browser: str) -> str:
    add = "'Get extension'" if browser == "edge" else "'Add to Chrome'"
    return (f"Click 'Install from Chrome Web Store'. In the recording browser window that opens, "
            f"click {add}, then 'Add extension'. Close that window — this tab updates by itself.")


def load_unpacked_howto() -> str:
    """How to pick the copied folder path in the OS folder dialog."""
    if sys.platform == "darwin":
        pick = ("in the folder dialog press ⌘⇧G, paste (⌘V), press Return, then click 'Select'")
    elif sys.platform == "win32":
        pick = "paste it (Ctrl+V) into the 'Folder' box of the dialog and click 'Select Folder'"
    else:
        pick = "in the folder dialog press Ctrl+L, paste (Ctrl+V), press Return, then 'Select'"
    return (f"Click 'Load unpacked' on the extensions page; the folder path is on your clipboard — {pick}.")


def bundled_instructions(browser: str) -> str:
    ext = paths.extension_dir()
    name = browsers.DISPLAY_NAME[browser]
    return (
        f"Alternative (if the Web Store is not available): click 'Use the bundled copy'. "
        f"Developer mode is switched on for you and the {name} extensions page opens. "
        f"{load_unpacked_howto()}\n    {ext}\n"
        + ("Edge then shows a 'Turn off developer mode?' notice at each start — close it with the X, "
           "do NOT turn it off (that disables the recorder)." if browser == "edge" else
           "Keep Developer mode on — turning it off disables the recorder.")
    )


def extension_instructions(browser: str) -> str:
    return store_instructions(browser) + "\n\n" + bundled_instructions(browser)


def _ensure_browser_closed_for_setup(browser: str) -> None:
    if recorder.active_session():
        raise recorder.RecorderError("Stop the current recording first.")


def open_webstore_install(browser: str) -> None:
    """Primary install path: the Web Store page, in the recording profile."""
    from . import WEBSTORE_URL
    prepare(browser)
    _ensure_browser_closed_for_setup(browser)
    browsers.launch(browser, [WEBSTORE_URL], log_file=paths.logs_dir() / f"browser-{browser}.log")


def open_bundled_install(browser: str) -> str:
    """Fallback: switch Developer mode on (one-time DevTools launch), then open
    the extensions page with the folder path on the clipboard."""
    from .extinstall import enable_developer_mode
    prepare(browser)
    _ensure_browser_closed_for_setup(browser)
    if browsers.is_running(browser):
        browsers.close(browser)
    ok, msg = enable_developer_mode(browser)
    _copy_to_clipboard(str(paths.extension_dir()))
    browsers.launch(browser, [extensions_page(browser)],
                    log_file=paths.logs_dir() / f"browser-{browser}.log")
    return msg if ok else (f"Could not switch Developer mode on automatically ({msg}) — "
                           "turn on the 'Developer mode' switch on the extensions page yourself.")


open_extension_install = open_bundled_install   # backwards-compatible name


def open_browser_unrecorded(browser: str) -> None:
    prepare(browser)
    browsers.launch(browser, None, log_file=paths.logs_dir() / f"browser-{browser}.log")


def _copy_to_clipboard(text: str) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["clip"], input=text.encode("utf-16-le"), check=False,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode(), check=False)
        else:
            for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, input=text.encode(), check=False)
                    break
    except Exception:
        pass


def wait_for_extension(browser: str, timeout: float = 600.0, poll=None) -> bool:
    """Poll the recording profile until the extension shows up (CLI setup)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = browsers.extension_state(browser)
        if st["installed"] and st["enabled"]:
            return True
        if poll:
            poll()
        time.sleep(2)
    return False


def post_install(bin_dir: str | None = None) -> int:
    """Installer hook: everything that needs no answers from the participant."""
    paths.ensure_dirs(config.load().sessions_path)
    ext = recorder.ensure_extension_copy()
    nativehost.write_wrapper()
    cfg = config.load()
    if cfg.browser in config.BROWSERS:
        nativehost.register(cfg.browser)          # upgrade: point at the new install
    made = []
    if sys.platform == "win32" and bin_dir:
        made += install_windows_shims(Path(bin_dir))
    if not os.environ.get("SITE_RECORDER_NO_SHORTCUTS"):
        made += install_shortcuts(quiet=False)
    print(f"  extension folder: {ext}")
    print(f"  recordings:       {cfg.sessions_path}")
    for m in made:
        print(f"  shortcut:         {m}")
    found = browsers.installed_browsers()
    if not found:
        print("  ! Neither Google Chrome nor Microsoft Edge was found — install one before recording.")
    try:
        import tkinter  # noqa: F401
    except Exception:
        print("  ! Tk is not available — use the command line (site-recorder start/stop/export).")
    return 0


# ---------------------------------------------------------------------------
# interactive CLI setup
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str = "") -> str:
    try:
        v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        v = ""
    return v or default


def interactive_setup(participant: str | None = None, browser: str | None = None,
                      noninteractive: bool = False) -> int:
    cfg = config.load()
    print(f"SITE Recorder {__version__} setup\n")
    pid = participant or cfg.participant_id
    if not noninteractive:
        pid = _ask("Participant ID (from the study team)", pid)
    pid = config.clean_participant_id(pid or "")
    installed = browsers.installed_browsers()
    if not installed:
        print("Neither Google Chrome nor Microsoft Edge was found. Install one of them and re-run setup.")
        return 1
    b = browser or cfg.browser or installed[0]
    if not noninteractive and len(installed) > 1:
        b = _ask(f"Browser to record in ({' / '.join(installed)})", b)
    if b not in installed:
        print(f"{b!r} is not installed here (found: {', '.join(installed)}).")
        return 1
    cfg.participant_id, cfg.browser = pid, b
    config.save(cfg)
    prepare(b)
    install_shortcuts(quiet=True)
    print(f"✓ Settings saved (participant {pid or '—'}, {browsers.DISPLAY_NAME[b]})")
    print(f"✓ Native host registered; recordings go to {cfg.sessions_path}")

    st = browsers.extension_state(b)
    if st["installed"] and st["enabled"]:
        print(f"✓ Extension already installed (version {st['version']})")
    elif noninteractive:
        print("! Extension not installed yet — open the app (site-recorder) → Setup, or run "
              "`site-recorder setup` interactively.")
    else:
        print("\nOne-time step — install the SITE Collector extension into the recording browser.")
        print("  1) from the Chrome Web Store (recommended)\n  2) the bundled copy (Load unpacked)")
        choice = _ask("Choose", "1")
        if choice.strip() == "2":
            print("\n" + bundled_instructions(b))
            _ask("\nPress Enter to open the extensions page")
            print(open_bundled_install(b))
        else:
            print("\n" + store_instructions(b))
            _ask("\nPress Enter to open the Web Store page")
            open_webstore_install(b)
        print("Waiting for the extension to appear (close the recording browser window when done)…")
        if wait_for_extension(b):
            print("✓ Extension installed")
        else:
            print("✗ Did not detect the extension. Re-run `site-recorder setup` to try again.")
            return 1
    if not pid:
        print("! No participant ID set — exports need one. Re-run setup to set it.")
    print("\nSetup complete. Start the app with `site-recorder` (or the SITE Recorder shortcut).")
    return 0


# ---------------------------------------------------------------------------
# shortcuts
# ---------------------------------------------------------------------------

def _gui_launcher() -> list[str]:
    """Command that opens the app window.

    On Windows this is the tool's own pythonw.exe — NOT uv's generated
    ``site-recorder-gui.exe`` launcher: those are unsigned one-off binaries
    that Smart App Control blocks ("An Application Control policy has blocked
    this file"), while the interpreter itself runs fine."""
    bindir = Path(sys.executable).parent
    if sys.platform == "win32":
        pyw = bindir / "pythonw.exe"
        return [str(pyw if pyw.exists() else Path(sys.executable)), "-m", "site_recorder.gui"]
    c = bindir / "site-recorder-gui"
    return [str(c)] if c.exists() else [sys.executable, "-m", "site_recorder.gui"]


def install_windows_shims(bin_dir: Path) -> list[str]:
    """``site-recorder.cmd`` / ``site-recorder-gui.cmd`` in uv's bin dir.

    uv's ``.exe`` entry points are blocked under Smart App Control; when that
    happens they are removed so the ``.cmd`` shims (which run the tool's
    python.exe) take over on PATH (Windows prefers .exe over .cmd)."""
    py = Path(sys.executable).with_name("python.exe")
    pyw = Path(sys.executable).with_name("pythonw.exe")
    out = []
    shims = {
        "site-recorder.cmd": [f'"{nativehost.batch_path(py)}" -m site_recorder.cli %*'],
        "site-recorder-gui.cmd": [f'start "" "{nativehost.batch_path(pyw)}" -m site_recorder.gui %*'],
    }
    for name, lines in shims.items():
        nativehost.write_batch(bin_dir / name, lines)
        out.append(str(bin_dir / name))
    # Both launchers are uv trampolines with the same signing status, so probe
    # only the console one (the GUI one would open the app window). Remove
    # them when they are blocked — or when Smart App Control is on/evaluating,
    # since evaluation mode turns into "on" later and would then shadow the
    # .cmd shims (Windows prefers .exe on PATH).
    cli_exe = bin_dir / "site-recorder.exe"
    if cli_exe.exists() and (_smart_app_control_active() or _exe_blocked(cli_exe)):
        for exe in ("site-recorder.exe", "site-recorder-gui.exe"):
            try:
                (bin_dir / exe).unlink()
                out.append(f"removed {exe} (Smart App Control) — the .cmd shim is used instead")
            except OSError:
                pass
    return out


def _smart_app_control_active() -> bool:
    """Smart App Control on (1) or in evaluation (2)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\CI\Policy") as k:
            val, _ = winreg.QueryValueEx(k, "VerifiedAndReputablePolicyState")
        return int(val) in (1, 2)
    except (OSError, ValueError, ImportError):
        return False


def _exe_blocked(exe: Path) -> bool:
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode != 0 and b"policy" in (r.stderr + r.stdout).lower()
    except OSError as e:
        # WinError 4551: "An Application Control policy has blocked this file"
        return getattr(e, "winerror", None) in (4551, 4550, 1260) or "policy" in str(e).lower()
    except subprocess.TimeoutExpired:
        return False


def install_shortcuts(quiet: bool = False) -> list[str]:
    made = []
    icon_png = paths.extension_dir() / "icon_128.png"
    launcher = _gui_launcher()
    try:
        if sys.platform == "win32":
            made += _win_shortcuts(launcher)
        elif sys.platform == "darwin":
            made += _mac_app(launcher)
        else:
            made += _linux_desktop(launcher, icon_png)
    except Exception as e:
        log_exception(e)
        if not quiet:
            print(f"(could not create shortcuts: {e})")
    return made


def _ps(script: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run a PowerShell snippet. Values go in through environment variables
    (never interpolated into the script: an apostrophe in a user name would
    break the quoting), output comes back as UTF-8."""
    env = dict(os.environ)
    env.update(env_extra or {})
    script = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + script
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-Command", script], capture_output=True, text=True, timeout=60, env=env,
                          encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _win_shortcuts(launcher: list[str]) -> list[str]:
    icon = paths.app_dir() / "site-recorder.ico"
    _write_ico(icon)
    values = {
        "SR_TARGET": launcher[0],
        "SR_ARGS": " ".join(f'"{a}"' for a in launcher[1:]),
        "SR_WORKDIR": str(Path.home()),
        "SR_ICON": str(icon) if icon.exists() else "",
    }
    out = []
    for folder in ("Programs", "Desktop"):
        script = (
            f"$d = [Environment]::GetFolderPath('{folder}'); $p = Join-Path $d 'SITE Recorder.lnk'; "
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($p); "
            "$s.TargetPath = $env:SR_TARGET; $s.Arguments = $env:SR_ARGS; "
            "$s.WorkingDirectory = $env:SR_WORKDIR; "
            "if ($env:SR_ICON) { $s.IconLocation = $env:SR_ICON }; "
            "$s.Description = 'SITE Recorder'; $s.Save(); Write-Output $p")
        r = _ps(script, values)
        if r.returncode == 0 and r.stdout.strip():
            out.append(r.stdout.strip())
        else:
            print(f"  ! could not create the {folder} shortcut: {(r.stderr or r.stdout).strip()[-300:]}")
    return out


def _write_ico(dst: Path) -> None:
    try:
        from PIL import Image
        Image.open(paths.extension_dir() / "icon_128.png").save(dst, sizes=[(16, 16), (32, 32), (48, 48), (128, 128)])
    except Exception:
        pass


def _mac_app(launcher: list[str]) -> list[str]:
    app = Path.home() / "Applications" / "SITE Recorder.app"
    macos = app / "Contents" / "MacOS"
    res = app / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    res.mkdir(parents=True, exist_ok=True)
    q = lambda s: "'" + s.replace("'", "'\\''") + "'"
    exe = macos / "SITE Recorder"
    exe.write_text("#!/bin/sh\nexport PYTHONUTF8=1 LANG=${LANG:-en_US.UTF-8}\nexec "
                   + " ".join(q(a) for a in launcher) + "\n", encoding="utf-8")
    exe.chmod(0o755)
    plist = {
        "CFBundleName": "SITE Recorder", "CFBundleDisplayName": "SITE Recorder",
        "CFBundleIdentifier": "org.sitebench.recorder", "CFBundleExecutable": "SITE Recorder",
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": __version__,
        "LSMinimumSystemVersion": "10.13", "NSHighResolutionCapable": True,
    }
    icns = res / "AppIcon.icns"
    if _make_icns(icns):
        plist["CFBundleIconFile"] = "AppIcon"
    import plistlib
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(plist, f)
    return [str(app)]


def _make_icns(dst: Path) -> bool:
    src = paths.extension_dir() / "icon_128.png"
    if not src.exists() or not shutil.which("iconutil") or not shutil.which("sips"):
        return False
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        for sz in (16, 32, 64, 128, 256):
            subprocess.run(["sips", "-z", str(sz), str(sz), str(src), "--out",
                            str(iconset / f"icon_{sz}x{sz}.png")], capture_output=True)
        r = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(dst)], capture_output=True)
        return r.returncode == 0 and dst.exists()


def _linux_desktop(launcher: list[str], icon: Path) -> list[str]:
    d = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "site-recorder.desktop"
    exec_line = " ".join('"' + a.replace('"', '\\"') + '"' for a in launcher)
    f.write_text(
        "[Desktop Entry]\nType=Application\nName=SITE Recorder\n"
        "Comment=Record browsing sessions for SITE-Bench\n"
        f"Exec={exec_line}\nIcon={icon}\nTerminal=false\nCategories=Utility;\n",
        encoding="utf-8")
    f.chmod(0o755)
    return [str(f)]


def remove_shortcuts() -> None:
    if sys.platform == "win32":
        _ps("foreach ($d in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) "
            "{ $p = Join-Path $d 'SITE Recorder.lnk'; if (Test-Path $p) { Remove-Item $p -Force } }")
    elif sys.platform == "darwin":
        shutil.rmtree(Path.home() / "Applications" / "SITE Recorder.app", ignore_errors=True)
    else:
        d = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"
        try:
            (d / "site-recorder.desktop").unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

def doctor_report() -> str:
    lines: list[str] = []
    ok_all = True

    def check(ok: bool, msg: str, fix: str = "") -> None:
        nonlocal ok_all
        ok_all &= ok
        lines.append(f"{'✓' if ok else '✗'} {msg}" + (f"\n    → {fix}" if (fix and not ok) else ""))

    cfg = config.load()
    lines.append(f"SITE Recorder {__version__} · Python {sys.version.split()[0]} · "
                 f"{platform.system()} {platform.release()} ({platform.machine()})")
    lines.append(f"app dir: {paths.app_dir()}")
    lines.append(f"sessions: {cfg.sessions_path}\n")
    check(bool(cfg.participant_id), f"participant ID: {cfg.participant_id or '(not set)'}", "Setup → step 1")
    check(cfg.browser in config.BROWSERS, f"browser: {cfg.browser or '(not chosen)'}", "Setup → step 2")
    try:
        import warcio  # noqa: F401
        check(True, "warcio importable")
    except ImportError:
        check(False, "warcio NOT importable — network capture would be lost", "reinstall the recorder")
    for b in browsers.installed_browsers():
        lines.append(f"  found {browsers.DISPLAY_NAME[b]} {browsers.browser_version(b)} at {browsers.find_executable(b)}")
    if cfg.browser in config.BROWSERS:
        b = cfg.browser
        check(bool(browsers.find_executable(b)), f"{browsers.DISPLAY_NAME[b]} installed", "install the browser")
        reg_ok, reg_msg = nativehost.is_registered(b)
        check(reg_ok, f"native host: {reg_msg}", "Setup → Save (re-registers)")
        st = browsers.extension_state(b)
        check(st["installed"], f"extension in recording profile: "
                               f"{'installed' if st['installed'] else 'missing'}"
                               + (f" (v{st['version'] or '?'}, {st.get('source')})" if st["installed"] else ""),
              "Setup → step 3")
        if st["installed"]:
            fix = ("Setup → 'Use the bundled copy' switches Developer mode back on"
                   if st.get("problem") == "developer_mode_off"
                   else "enable SITE Collector on the extensions page")
            check(st["enabled"], f"extension enabled{' (Developer mode is off)' if st.get('problem') == 'developer_mode_off' else ''}", fix)
            bundled = json.loads((Path(__file__).parent / "extension" / "manifest.json").read_text())["version"]
            # (the bundled copy is installed as <version>.<code stamp>)
            if st["version"] and ".".join(st["version"].split(".")[:3]) != bundled:
                lines.append(f"  note: profile has extension v{st['version']}, this recorder bundles v{bundled}"
                             + (" (the browser picks up the new files on its next start)" if st["unpacked"]
                                else " (Web Store build; it updates itself when a new version is published)"))
        if sys.platform != "darwin":   # on macOS the browser may stay open without windows
            check(not browsers.is_running(b) or bool(recorder.active_session()),
                  "recording browser not left open", "close the recording browser window")
    try:
        wrapper = nativehost.wrapper_path()
        check(wrapper.exists(), f"host wrapper: {wrapper}")
        r = _probe_host()
        check(r[0], f"host self-test: {r[1]}", "reinstall the recorder")
    except Exception as e:
        check(False, f"host self-test failed: {e}")
    try:
        probe = cfg.sessions_path / ".write_test"
        cfg.sessions_path.mkdir(parents=True, exist_ok=True)
        probe.write_text("x")
        probe.unlink()
        check(True, "sessions folder writable")
    except OSError as e:
        check(False, f"sessions folder not writable: {e}", "choose another folder")
    m = recorder.active_session()
    lines.append(f"\nactive session: {m['session_id'] + ' (' + m.get('state', '') + ')' if m else 'none'}")
    ref = paths.logs_dir() / "host_refusals.log"
    if ref.exists():
        tail = ref.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
        lines.append("recent host refusals (normal when the browser connects outside a recording):")
        lines += [f"  {t}" for t in tail]
    lines.append("\nALL GOOD" if ok_all else "\nSOME CHECKS FAILED — see → hints above")
    return "\n".join(lines)


def _probe_host() -> tuple[bool, str]:
    """Run the registered wrapper like the browser would, with no active
    session: it must answer with a well-formed {"type":"stop"} message."""
    import struct
    w = nativehost.wrapper_path()
    if not w.exists():
        return False, "wrapper missing"
    cmd = ["cmd", "/d", "/c", str(w), f"chrome-extension://{EXTENSION_ID}/"] if sys.platform == "win32" \
        else [str(w), f"chrome-extension://{EXTENSION_ID}/"]
    saved = paths.active_session_file()
    if saved.exists():
        return True, "skipped (a recording is active)"
    try:
        # Run from the host folder (what the browser does): a stray json.py in
        # the caller's folder must not shadow the standard library.
        r = subprocess.run(cmd, input=b"", capture_output=True, timeout=60, cwd=str(paths.host_dir()),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, "host did not exit within 60s"
    out = r.stdout
    if len(out) < 4:
        return False, f"no protocol output (exit {r.returncode}): {r.stderr.decode(errors='replace')[-300:]}"
    (n,) = struct.unpack("@I", out[:4])
    try:
        msg = json.loads(out[4:4 + n])
    except ValueError:
        return False, "malformed protocol output"
    return (msg.get("type") == "stop"), "host starts and speaks the protocol"


def doctor(verbose: bool = True) -> bool:
    rep = doctor_report()
    if verbose:
        print(rep)
    return rep.rstrip().endswith("ALL GOOD")


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------

def uninstall(purge: bool = False, yes: bool = False) -> int:
    if recorder.active_session():
        recorder.stop_active(reason="uninstall")
    if not yes:
        try:
            if input("Remove SITE Recorder's browser registration and shortcuts? [y/N] ").strip().lower() != "y":
                return 1
        except EOFError:
            return 1
    nativehost.unregister()
    remove_shortcuts()
    if sys.platform == "win32":
        try:
            bin_dir = subprocess.run(["uv", "tool", "dir", "--bin"], capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=30,
                                     creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
            for n in ("site-recorder.cmd", "site-recorder-gui.cmd"):
                if bin_dir:
                    (Path(bin_dir) / n).unlink(missing_ok=True)
        except (OSError, subprocess.SubprocessError):
            pass
    if purge:
        for b in config.BROWSERS:
            if browsers.is_running(b):
                browsers.close(b)
        shutil.rmtree(paths.app_dir(), ignore_errors=True)
        print(f"Removed {paths.app_dir()} (recording browser profiles, settings, logs).")
    print(f"Your recordings are kept in {config.load().sessions_path.parent if not purge else paths.user_dir()}.")
    print("To remove the program itself:  uv tool uninstall site-recorder")
    return 0
