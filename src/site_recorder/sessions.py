"""Read-only views over the sessions folder (listing, stats, sizes)."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import export

SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}$")


@dataclass
class SessionSummary:
    id: str
    path: Path
    started_at: float | None
    duration_s: float | None
    screenshots: int
    clicks: int
    inputs: int
    scrolls: int
    size_bytes: int
    status: str            # "recording" | "complete" | "empty" | "incomplete"
    exported_zip: str | None
    note: str = ""

    @property
    def started_label(self) -> str:
        if not self.started_at:
            return self.id
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started_at))

    @property
    def duration_label(self) -> str:
        if self.duration_s is None:
            return "–"
        m, s = divmod(int(self.duration_s), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _read_json(p: Path):
    from .fsutil import read_json
    return read_json(p)


def screenshot_times(session_dir: Path) -> list[float]:
    out = []
    d = session_dir / "browser_screenshots"
    if d.is_dir():
        for p in d.glob("*.png"):
            try:
                out.append(float(p.name.split("_", 1)[0]))
            except ValueError:
                pass
    return sorted(out)


def dir_size(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def summarize(session_dir: Path, active_id: str | None = None) -> SessionSummary:
    info = _read_json(session_dir / "session_info.json") or {}
    stats = _read_json(session_dir / "live_stats.json") or {}
    finalized = session_dir.name != active_id and "size_bytes" in info
    # Finalized sessions carry cached numbers: rescanning thousands of files
    # per session on every refresh made the window stutter.
    times = [] if finalized and "duration_s" in info else screenshot_times(session_dir)
    started = info.get("started_at")
    if started is None:
        try:
            started = time.mktime(time.strptime(session_dir.name, "%Y%m%d_%H%M%S"))
        except ValueError:
            started = times[0] if times else None
    duration = info.get("duration_s")
    if duration is None and len(times) >= 2:
        duration = times[-1] - times[0]
    if session_dir.name == active_id:
        status = "recording"
    elif info.get("status"):
        status = info["status"]
    elif not (session_dir / "extension_host.log").exists():
        status = "empty"
    else:
        status = "incomplete"
    ex = export.export_info(session_dir)
    return SessionSummary(
        id=session_dir.name, path=session_dir, started_at=started, duration_s=duration,
        screenshots=int(stats.get("screenshots", 0) or 0), clicks=int(stats.get("clicks", 0) or 0),
        inputs=int(stats.get("inputs", 0) or 0), scrolls=int(stats.get("scrolls", 0) or 0),
        size_bytes=int(info["size_bytes"]) if finalized else dir_size(session_dir), status=status,
        exported_zip=(ex or {}).get("zip"), note=info.get("note", ""),
    )


def list_sessions(sessions_dir: Path, active_id: str | None = None) -> list[SessionSummary]:
    if not sessions_dir.is_dir():
        return []
    out = []
    for d in sorted(sessions_dir.iterdir(), reverse=True):
        if d.is_dir() and SESSION_ID_RE.match(d.name):
            try:
                out.append(summarize(d, active_id))
            except OSError:
                continue
    return out
