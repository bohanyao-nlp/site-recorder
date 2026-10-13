"""Pack a recorded session into one .zip the participant sends back.

Layout of ``<participant>_<session_id>.zip``::

    <session_id>/                      the session folder, as recorded
        archive/session.warc.gz
        browser_screenshots/*.png
        events.jsonl  tab_timeline.jsonl  dom_snapshots/  ...
        export_manifest.json           written last: who/what/versions +
                                       sha256 of every file above

The server-side ingest (server/ingest.py) verifies every checksum
before accepting a zip, so a truncated upload/download is caught instead of
silently feeding a half session into reconstruction.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import time
import zipfile
from pathlib import Path
from typing import Callable, Iterable

from . import __version__

MANIFEST_NAME = "export_manifest.json"
EXPORT_FORMAT = "site-recorder-export/1"
EXPORT_MARKER = ".export_info.json"   # inside the session dir; not exported

# Per-session control/plumbing files that mean nothing off this machine.
_SKIP_NAMES = {
    "control.json", "status.json", "nm_host.bat", "nm_host.sh",
    "com.sitebench.collector.json", EXPORT_MARKER, MANIFEST_NAME,
    ".warc_committed", ".browser_windows_closed", ".host.lock", ".host_alive", "stop.request",
    ".DS_Store", "Thumbs.db", "desktop.ini",
}
_SKIP_SUFFIXES = (".tmp", ".partial", ".lock")
# Already-compressed formats: store as-is (deflating them only burns CPU).
_STORED_SUFFIXES = {".gz", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm",
                    ".zip", ".7z", ".xz", ".bz2", ".mp3", ".ogg", ".woff", ".woff2"}

_CHUNK = 1 << 20
SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}$")


def _iter_files(session_dir: Path) -> Iterable[Path]:
    for p in sorted(session_dir.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        if p.name in _SKIP_NAMES or p.name.endswith(_SKIP_SUFFIXES):
            continue
        yield p


def export_zip_name(participant_id: str, session_id: str) -> str:
    return f"{participant_id or 'anonymous'}_{session_id}.zip"


def export_session(session_dir: Path, dest_dir: Path, *, participant_id: str,
                   extra_info: dict | None = None,
                   progress: Callable[[int, int], None] | None = None) -> Path:
    """Write the session to ``dest_dir/<participant>_<session>.zip``.

    Written to a ``.partial`` file and renamed at the end, so an interrupted
    export never leaves a plausible-looking but broken zip behind.
    ``progress(done_bytes, total_bytes)`` is called as files are added."""
    session_dir = Path(session_dir)
    sid = session_dir.name
    files = list(_iter_files(session_dir))
    total = sum(f.stat().st_size for f in files)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / export_zip_name(participant_id, sid)
    tmp = out.with_name(out.name + ".partial")

    entries = []
    try:
        _write_zip(tmp, session_dir, sid, files, total, entries, progress, participant_id, extra_info)
    except BaseException:
        # Never leave a big half-written file behind (e.g. disk full).
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.replace(tmp, out)
    manifest_time = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        (session_dir / EXPORT_MARKER).write_text(json.dumps({
            "zip": str(out), "exported_at": manifest_time,
            "bytes": out.stat().st_size,
        }), encoding="utf-8")
    except OSError:
        pass
    return out


def _write_zip(tmp: Path, session_dir: Path, sid: str, files: list, total: int, entries: list,
               progress, participant_id: str, extra_info: dict | None) -> None:
    done = 0
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
        for f in files:
            rel = f.relative_to(session_dir).as_posix()
            size = f.stat().st_size
            stored = f.suffix.lower() in _STORED_SUFFIXES
            # strict_timestamps=False: files dated before 1980 (copied
            # downloads can be) would otherwise abort the export.
            zi = zipfile.ZipInfo.from_file(f, arcname=f"{sid}/{rel}", strict_timestamps=False)
            zi.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            h = hashlib.sha256()
            n_read = 0
            with open(f, "rb") as src, zf.open(zi, "w", force_zip64=size >= (1 << 31) - _CHUNK) as dst:
                while True:
                    buf = src.read(_CHUNK)
                    if not buf:
                        break
                    h.update(buf)
                    dst.write(buf)
                    done += len(buf)
                    n_read += len(buf)
                    if progress:
                        progress(done, total)
            # The size of what was actually hashed (a log still being
            # appended to would otherwise fail verification on the server).
            entries.append({"path": rel, "size": n_read, "sha256": h.hexdigest()})

        manifest = {
            "format": EXPORT_FORMAT,
            "participant_id": participant_id,
            "session_id": sid,
            "recorder_version": __version__,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "total_bytes": total,
            "files": entries,
        }
        if extra_info:
            manifest["session_info"] = extra_info
        zf.writestr(f"{sid}/{MANIFEST_NAME}", json.dumps(manifest, indent=2),
                    compress_type=zipfile.ZIP_DEFLATED)


def export_info(session_dir: Path) -> dict | None:
    try:
        return json.loads((Path(session_dir) / EXPORT_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def verify_zip(zip_path: Path) -> tuple[bool, str, dict | None]:
    """Check a session zip against its manifest. Returns (ok, message, manifest).

    Stdlib-only on purpose: the server-side ingest imports this module by path."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if n.endswith("/" + MANIFEST_NAME)]
            if len(names) != 1:
                return False, f"expected exactly one {MANIFEST_NAME}, found {len(names)}", None
            mname = names[0]
            root = mname[: -len(MANIFEST_NAME)]          # "<sid>/"
            manifest = json.loads(zf.read(mname))
            if manifest.get("format") != EXPORT_FORMAT:
                return False, f"unknown export format {manifest.get('format')!r}", manifest
            sid = str(manifest.get("session_id") or "")
            # The id becomes a folder name on the server: accept only the
            # recorder's own format, and only a zip rooted at that folder.
            if not SESSION_ID_RE.match(sid) or root != sid + "/":
                return False, f"bad session id / layout ({sid!r}, root {root!r})", manifest
            present = {n for n in zf.namelist() if not n.endswith("/")}
            listed = {root + e.get("path", "") for e in manifest.get("files", [])} | {mname}
            extra = sorted(present - listed)
            if extra:
                return False, f"zip contains files not in its manifest: {extra[:3]}", manifest
            for e in manifest.get("files", []):
                name = root + e["path"]
                if name not in present:
                    return False, f"missing file {e['path']}", manifest
                h = hashlib.sha256()
                n = 0
                with zf.open(name) as fh:
                    while True:
                        buf = fh.read(_CHUNK)
                        if not buf:
                            break
                        h.update(buf)
                        n += len(buf)
                if n != e["size"] or h.hexdigest() != e["sha256"]:
                    return False, f"checksum mismatch for {e['path']}", manifest
            return True, f"{len(manifest.get('files', []))} files verified", manifest
    except zipfile.BadZipFile as e:
        return False, f"not a valid zip ({e})", None
    except (OSError, ValueError, KeyError) as e:
        return False, f"unreadable ({e})", None
