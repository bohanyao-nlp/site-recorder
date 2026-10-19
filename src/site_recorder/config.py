"""Recorder settings, persisted as JSON in the app dir."""
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
    browser: str = ""            # "chrome" | "edge"; "" = not chosen yet
    sessions_dir: str = ""       # "" = default (~/SiteRecorder/sessions)
    start_url: str = ""          # page opened when a recording starts ("" = new tab)
    # Extra sites never recorded (no screenshots, DOM, events or network), on
    # top of the password-manager vaults the extension always skips.
    # Comma/space separated host names; subdomains are included.
    skip_domains: str = ""

    # -- derived -------------------------------------------------------------
    @property
    def sessions_path(self) -> Path:
        return Path(self.sessions_dir).expanduser() if self.sessions_dir else paths.default_sessions_dir()

    @property
    def is_configured(self) -> bool:
        return self.browser in BROWSERS


def skip_domain_list(raw: str) -> list[str]:
    """``skip_domains`` as a clean list of host names."""
    out = []
    for d in re.split(r"[,\s]+", raw or ""):
        d = d.strip().lower()
        d = re.sub(r"^[a-z]+://", "", d).split("/", 1)[0].lstrip("*.")
        if d and d not in out:
            out.append(d)
    return out


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
    # Unknown keys (settings of older versions) are dropped.
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
