"""Where the recorder keeps its files, per OS.

Two roots:

* ``app_dir`` — internal state the participant never needs to touch: the
  dedicated browser profiles, the unpacked extension, the native-messaging
  host wrapper + manifest, the active-session marker, logs, settings.
    Windows  %LOCALAPPDATA%\\SiteRecorder
    macOS    ~/Library/Application Support/SiteRecorder
    Linux    $XDG_DATA_HOME/site-recorder  (~/.local/share/site-recorder)

* ``user_dir`` — what the participant works with: ``sessions/`` (raw
  recordings) and ``exports/`` (the .zip files they send back). Defaults to
  ``~/SiteRecorder`` on every OS. Deliberately NOT under Documents/Desktop/
  Downloads: on macOS those are privacy-protected, and the native host that
  writes recordings is spawned by the browser, which usually has no access
  there — the recording would silently come out empty.

``SITE_RECORDER_HOME`` overrides both (everything goes under that one
folder) — used by tests and for side-by-side installs.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _home_override() -> Path | None:
    v = os.environ.get("SITE_RECORDER_HOME", "").strip()
    return Path(v).expanduser().resolve() if v else None


def app_dir() -> Path:
    o = _home_override()
    if o:
        return o / "app"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "SiteRecorder"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SiteRecorder"
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "site-recorder"


def user_dir() -> Path:
    o = _home_override()
    if o:
        return o
    return Path.home() / "SiteRecorder"


def config_file() -> Path:
    return app_dir() / "config.json"


def default_sessions_dir() -> Path:
    return user_dir() / "sessions"


def exports_dir() -> Path:
    return user_dir() / "exports"


def profile_dir(browser: str) -> Path:
    """The dedicated, persistent recording profile (a --user-data-dir)."""
    return app_dir() / "profiles" / browser


def extension_dir() -> Path:
    """Stable on-disk copy of the bundled extension, for 'Load unpacked'.

    Copied out of the installed package (whose location changes on every
    reinstall/upgrade) so the browser always finds the extension at the same
    path."""
    return app_dir() / "extension"


def host_dir() -> Path:
    """Native-messaging host wrapper script + manifest live here."""
    return app_dir() / "host"


def logs_dir() -> Path:
    return app_dir() / "logs"


def active_session_file() -> Path:
    """Marker for the session currently being recorded (see session.py)."""
    return app_dir() / "active_session.json"


def ensure_dirs(sessions_dir: Path | None = None) -> None:
    for d in (app_dir(), host_dir(), logs_dir(), exports_dir(),
              sessions_dir or default_sessions_dir()):
        d.mkdir(parents=True, exist_ok=True)
