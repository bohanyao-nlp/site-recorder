"""Command-line interface: ``site-recorder <command>``.

Everything the window (``site-recorder gui``) does is also available here:

    site-recorder setup               first-time setup (participant id, browser, extension)
    site-recorder start               record until the recording browser is closed / Ctrl+C
    site-recorder stop                stop the recording started elsewhere (e.g. by the app)
    site-recorder pause | resume
    site-recorder status
    site-recorder list                recorded sessions
    site-recorder export [ID ...]     zip sessions for sending (default: all not yet exported)
    site-recorder delete ID
    site-recorder open [sessions|exports]
    site-recorder browser             open the recording browser without recording
    site-recorder doctor              check the installation
    site-recorder gui                 the app window
    site-recorder uninstall           remove browser registration + shortcuts
"""
from __future__ import annotations

import argparse
import shutil
import signal
import sys
import time
from pathlib import Path

from . import __version__, browsers, config, export, paths, recorder, sessions, setup


def _cfg() -> config.Config:
    return config.load()


def _print_problems(problems: list[str]) -> None:
    for p in problems:
        print(f"  ✗ {p}")


def cmd_setup(args) -> int:
    return setup.interactive_setup(participant=args.participant, browser=args.browser,
                                   noninteractive=args.yes)


def cmd_start(args) -> int:
    cfg = _cfg()
    rec = recorder.Recorder(cfg)
    if args.url:
        cfg.start_url = args.url
    print(f"Starting {browsers.DISPLAY_NAME.get(cfg.browser, cfg.browser)} (recording profile)…")
    dots = {"n": 0}

    def tick():
        dots["n"] += 1
        if dots["n"] % 10 == 0:
            print("  waiting for the extension to connect…", flush=True)

    # Interrupts (Ctrl+C, closing the terminal) only set a flag; the session is
    # then stopped and saved properly. Installed BEFORE start: an interrupt
    # during the connect wait must not orphan a half-started session.
    stop = {"flag": False}

    def _sig(_s, _f):
        stop["flag"] = True
        raise KeyboardInterrupt
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _sig)
            except (OSError, ValueError):
                pass
    try:
        sd = rec.start(on_wait=tick)       # cleans up after itself if interrupted
    except recorder.RecorderError as e:
        print(f"Cannot start recording:\n{e}")
        return 1
    except KeyboardInterrupt:
        print("\nStart cancelled.")
        return 130
    if recorder.host_connected(sd):
        print(f"● Recording  →  {sd}")
    else:
        print(f"! The extension has not connected yet ({sd}).\n"
              "  Recording continues; if this persists, run `site-recorder doctor`.")
    print("  Browse normally in the recording browser window.")
    print("  Stop: " + ("quit that browser (⌘Q)" if sys.platform == "darwin" else "close that browser window")
          + ", or press Ctrl+C here.")
    gone_since = None
    stopped_elsewhere = False
    reason = "stopped from terminal"
    try:
        while True:
            time.sleep(0.5)
            m = recorder.active_session()
            if not m or Path(m["session_dir"]) != sd or m.get("state") != "recording":
                stopped_elsewhere = True
                print("Recording was stopped elsewhere.")
                break
            if recorder.browser_closed(m):
                gone_since = gone_since or time.time()
                if time.time() - gone_since > 3:     # tolerate a relaunch blip
                    print("Recording browser closed.")
                    reason = "recording browser closed"
                    break
            else:
                gone_since = None
    except KeyboardInterrupt:
        pass
    finally:
        if not stopped_elsewhere:
            print("Stopping and saving…", flush=True)
            for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
                if hasattr(signal, name):          # finish saving even on a 2nd Ctrl+C
                    try:
                        signal.signal(getattr(signal, name), signal.SIG_IGN)
                    except (OSError, ValueError):
                        pass
            recorder.stop_active(reason=reason, cfg=cfg, expected_session=sd.name)
    if stopped_elsewhere:
        return 0
    s = sessions.summarize(sd)
    print(f"Saved session {s.id}: {s.duration_label}, {s.screenshots} screenshots, "
          f"{s.clicks} clicks, {sessions.human_size(s.size_bytes)} ({s.status})")
    print("Export it with:  site-recorder export")
    return 0


def cmd_stop(args) -> int:
    sd = recorder.stop_active(cfg=_cfg(), close_browser=not args.keep_browser)
    print(f"Stopped: {sd}" if sd else "No recording in progress.")
    return 0


def cmd_pause(args, paused: bool) -> int:
    try:
        recorder.Recorder(_cfg()).set_paused(paused)
    except recorder.RecorderError as e:
        print(e)
        return 1
    print("Paused." if paused else "Resumed.")
    return 0


def cmd_status(args) -> int:
    cfg = _cfg()
    print(f"site-recorder {__version__}")
    print(f"  participant: {cfg.participant_id or '(not set)'}   browser: {cfg.browser or '(not set)'}")
    m = recorder.active_session()
    if not m:
        print("  not recording")
    else:
        sd = Path(m["session_dir"])
        age = recorder.host_heartbeat_age(sd)
        print(f"  RECORDING {m['session_id']} ({m.get('state')})"
              f"{' — paused' if recorder.Recorder.is_paused() else ''}")
        print(f"    extension connected: {'yes' if recorder.host_connected(sd) else 'no'}"
              + (f", last heartbeat {age:.0f}s ago" if age is not None else ""))
    problems = recorder.Recorder(cfg).preflight()
    if problems:
        print("  setup problems:")
        _print_problems(problems)
    return 0


def cmd_list(args) -> int:
    cfg = _cfg()
    m = recorder.active_session()
    rows = sessions.list_sessions(cfg.sessions_path, m.get("session_id") if m else None)
    if not rows:
        print(f"No sessions in {cfg.sessions_path}")
        return 0
    print(f"{'ID':<16} {'started':<17} {'length':>8} {'shots':>6} {'size':>9}  status     exported")
    for s in rows:
        print(f"{s.id:<16} {s.started_label:<17} {s.duration_label:>8} {s.screenshots:>6} "
              f"{sessions.human_size(s.size_bytes):>9}  {s.status:<10} {'yes' if s.exported_zip else '-'}")
    return 0


def cmd_export(args) -> int:
    cfg = _cfg()
    if not cfg.participant_id:
        print("Set your participant ID first: site-recorder setup")
        return 1
    m = recorder.active_session()
    active = m.get("session_id") if m else None
    rows = sessions.list_sessions(cfg.sessions_path, active)
    if args.ids:
        rows = [s for s in rows if s.id in args.ids]
        missing = set(args.ids) - {s.id for s in rows}
        for i in sorted(missing):
            print(f"  no such session: {i}")
    elif not args.all:
        rows = [s for s in rows if not s.exported_zip]
    rows = [s for s in rows if s.status != "recording"]
    if not rows:
        print("Nothing to export.")
        return 0
    dest = Path(args.dest).expanduser() if args.dest else paths.exports_dir()
    for s in rows:
        print(f"Exporting {s.id} ({sessions.human_size(s.size_bytes)})…", flush=True)
        out = export.export_session(s.path, dest, participant_id=cfg.participant_id)
        print(f"  → {out}")
    print(f"\nSend the .zip file(s) in {dest} to the study team.")
    return 0


def cmd_delete(args) -> int:
    cfg = _cfg()
    sd = cfg.sessions_path / args.id
    m = recorder.active_session()
    if m and m.get("session_id") == args.id:
        print("That session is being recorded — stop it first.")
        return 1
    if not sd.is_dir() or not sessions.SESSION_ID_RE.match(args.id):
        print(f"No such session: {args.id}")
        return 1
    if not args.yes:
        if input(f"Delete {sd} permanently? [y/N] ").strip().lower() != "y":
            return 1
    shutil.rmtree(sd)
    print("Deleted.")
    return 0


def cmd_open(args) -> int:
    cfg = _cfg()
    target = paths.exports_dir() if args.what == "exports" else cfg.sessions_path
    target.mkdir(parents=True, exist_ok=True)
    setup.open_path(target)
    print(target)
    return 0


def cmd_browser(args) -> int:
    cfg = _cfg()
    if not cfg.browser:
        print("Run `site-recorder setup` first.")
        return 1
    browsers.seed_profile(cfg.browser)
    browsers.launch(cfg.browser, args.urls or None)
    print(f"Opened the recording browser ({browsers.DISPLAY_NAME[cfg.browser]}). "
          "Nothing is recorded until you start a recording.")
    return 0


def cmd_doctor(args) -> int:
    ok = setup.doctor(verbose=True)
    return 0 if ok else 1


def cmd_gui(args) -> int:
    from . import gui
    return gui.main()


def cmd_post_install(args) -> int:
    return setup.post_install(bin_dir=args.bin_dir)


def cmd_uninstall(args) -> int:
    return setup.uninstall(purge=args.purge, yes=args.yes)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="site-recorder", description="SITE-Bench data-collection recorder")
    p.add_argument("--version", action="version", version=f"site-recorder {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("setup", help="first-time setup")
    s.add_argument("--participant", help="participant ID given to you by the study team")
    s.add_argument("--browser", choices=config.BROWSERS)
    s.add_argument("-y", "--yes", action="store_true", help="non-interactive (use given/default values)")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("start", help="start recording (until the browser is closed or Ctrl+C)")
    s.add_argument("--url", help="page to open when recording starts")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("stop", help="stop the current recording")
    s.add_argument("--keep-browser", action="store_true", help="leave the recording browser open")
    s.set_defaults(func=cmd_stop)

    sub.add_parser("pause", help="pause capture").set_defaults(func=lambda a: cmd_pause(a, True))
    sub.add_parser("resume", help="resume capture").set_defaults(func=lambda a: cmd_pause(a, False))
    sub.add_parser("status", help="show status").set_defaults(func=cmd_status)
    sub.add_parser("list", help="list sessions").set_defaults(func=cmd_list)

    s = sub.add_parser("export", help="zip sessions for sending")
    s.add_argument("ids", nargs="*", help="session IDs (default: all not yet exported)")
    s.add_argument("--all", action="store_true", help="re-export everything")
    s.add_argument("--dest", help="output folder (default ~/SiteRecorder/exports)")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("delete", help="delete a session")
    s.add_argument("id")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("open", help="open the sessions or exports folder")
    s.add_argument("what", nargs="?", choices=["sessions", "exports"], default="exports")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("browser", help="open the recording browser without recording")
    s.add_argument("urls", nargs="*")
    s.set_defaults(func=cmd_browser)

    sub.add_parser("doctor", help="check the installation").set_defaults(func=cmd_doctor)
    sub.add_parser("gui", help="open the app window").set_defaults(func=cmd_gui)

    # Run by install.sh / install.ps1 right after `uv tool install`.
    s = sub.add_parser("post-install", help=argparse.SUPPRESS)
    s.add_argument("--bin-dir", help="uv's tool bin dir (Windows: .cmd shims go there)")
    s.set_defaults(func=cmd_post_install)

    s = sub.add_parser("uninstall", help="remove browser registration and shortcuts")
    s.add_argument("--purge", action="store_true", help="also delete the recording browser profile")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_uninstall)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # ✓/● on legacy Windows consoles
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # No command: open the app window (what a double-click would do).
        return cmd_gui(args)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
