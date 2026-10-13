import base64
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

# 1x1 transparent PNG
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated SITE_RECORDER_HOME for every test."""
    h = tmp_path / "home"
    monkeypatch.setenv("SITE_RECORDER_HOME", str(h))
    return h


class HostProc:
    """Drive the native host like a browser would: length-prefixed JSON."""

    def __init__(self, app_dir: Path, env: dict):
        self.p = subprocess.Popen(
            [sys.executable, "-m", "site_recorder.host", f"--app-dir={app_dir}",
             "chrome-extension://gbhaoocbgjdighlaaflhgfbmaahgmjaa/"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    def send(self, msg: dict) -> None:
        data = json.dumps(msg).encode()
        self.p.stdin.write(struct.pack("@I", len(data)) + data)
        self.p.stdin.flush()

    def recv(self, timeout: float = 10.0) -> dict | None:
        import threading
        out: dict = {}

        def rd():
            raw = self.p.stdout.read(4)
            if len(raw) < 4:
                out["msg"] = None
                return
            (n,) = struct.unpack("@I", raw)
            out["msg"] = json.loads(self.p.stdout.read(n))
        t = threading.Thread(target=rd, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError("no message from host")
        return out["msg"]

    def recv_until(self, mtype: str, timeout: float = 10.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = self.recv(deadline - time.time())
            if m is None:
                raise EOFError("host closed stdout")
            if m.get("type") == mtype:
                return m
        raise TimeoutError(mtype)

    def close(self, timeout: float = 10.0) -> int:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        return self.p.wait(timeout)


@pytest.fixture
def host_factory(home):
    procs = []

    def make() -> HostProc:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        hp = HostProc(home / "app", env)
        procs.append(hp)
        return hp
    yield make
    for hp in procs:
        if hp.p.poll() is None:
            hp.p.kill()
