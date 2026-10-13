"""Small file helpers shared by the app, the CLI and the native host.

Several processes and threads read and write the same small JSON files
(active_session.json, control.json, status.json, live_stats.json …). Writes
are atomic (temp file + os.replace) so a reader never sees half a file. On
Windows, os.replace fails with a sharing violation while another process has
the destination open for reading, so writes retry briefly and reads retry
the same transient error instead of reporting "no file".
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path


def atomic_write_text(path: Path, text: str, retries: int = 40, delay: float = 0.025) -> None:
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for _ in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(delay)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj, **kw) -> None:
    atomic_write_text(path, json.dumps(obj, **kw))


def read_json(path: Path, retries: int = 6, delay: float = 0.025):
    """Parsed JSON, or None if the file is missing/unreadable/invalid."""
    for _ in range(retries):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except PermissionError:
            time.sleep(delay)          # Windows: momentarily locked by a writer
        except (OSError, ValueError):
            return None
    return None


class FileLock:
    """Exclusive, non-reentrant OS lock on a lock file (cross-process; also
    between two opens in the same process)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fh = None

    def acquire(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+b")
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.time() >= deadline:
                    self.fh.close()
                    self.fh = None
                    return False
                time.sleep(0.1)

    def release(self) -> None:
        if not self.fh:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
