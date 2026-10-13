"""Participant settings, persisted as JSON in the app dir."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import paths

BROWSERS = ("chrome", "edge")


@dataclass
class Config:
    participant_id: str = ""
    browser: str = ""            # "chrome" | "edge"; "" = not chosen yet
    sessions_dir: str = ""       # "" = default (~/SiteRecorder/sessions)
    start_url: str = ""          # page opened when a recording starts ("" = new tab)

    # -- derived -------------------------------------------------------------
    @property
    def sessions_path(self) -> Path:
        return Path(self.sessions_dir).expanduser() if self.sessions_dir else paths.default_sessions_dir()

    @property
    def is_configured(self) -> bool:
        return bool(self.participant_id) and self.browser in BROWSERS


_PID_RE = re.compile(r"[^A-Za-z0-9_-]+")


def clean_participant_id(raw: str) -> str:
    """Participant ids end up in file names — keep them filesystem-safe."""
    return _PID_RE.sub("-", (raw or "").strip()).strip("-")[:40]


def load() -> Config:
    f = paths.config_file()
    data = {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except Exception:
        # A corrupt config must never stop the app from opening; fall back to
        # defaults (the setup screen lets the participant fill it in again).
        data = {}
    known = {fl.name for fl in fields(Config)}
    cfg = Config(**{k: v for k, v in data.items() if k in known and isinstance(v, str)})
    if cfg.browser not in BROWSERS:
        cfg.browser = ""
    return cfg


def save(cfg: Config) -> None:
    f = paths.config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    os.replace(tmp, f)
