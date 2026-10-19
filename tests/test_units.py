"""Unit tests for state parsing, lifecycle bookkeeping and helpers (no browser)."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from site_recorder import EXTENSION_ID, browsers, config, nativehost, paths, recorder, sessions


def _prefs(home: Path, browser: str, entry: dict | None, file: str = "Secure Preferences"):
    d = paths.profile_dir(browser) / "Default"
    d.mkdir(parents=True, exist_ok=True)
    data = {"extensions": {"settings": {EXTENSION_ID: entry} if entry is not None else {}}}
    (d / file).write_text(json.dumps(data))


@pytest.mark.parametrize("entry,expect", [
    (None, dict(installed=False)),
    ({"location": 4, "creation_flags": 38, "path": "C:/x"}, dict(installed=True, enabled=True, source="unpacked")),
    ({"location": 1, "path": f"{EXTENSION_ID}/1.7.1_0"}, dict(installed=True, enabled=True, source="store")),
    ({"location": 4, "disable_reasons": 1 << 24}, dict(installed=True, enabled=False, problem="developer_mode_off")),
    ({"location": 4, "disable_reasons": [1 << 24]}, dict(installed=True, enabled=False, problem="developer_mode_off")),
    ({"location": 1, "disable_reasons": 1}, dict(installed=True, enabled=False, problem="disabled")),
    # loaded through DevTools for one launch: purged at next start -> not installed
    ({"location": 4, "creation_flags": 32806}, dict(installed=False)),
])
def test_extension_state(home, entry, expect):
    _prefs(home, "chrome", entry)
    st = browsers.extension_state("chrome")
    for k, v in expect.items():
        assert st[k] == v, (k, st)


def test_extension_state_store_files_before_prefs(home):
    d = paths.profile_dir("edge") / "Default" / "Extensions" / EXTENSION_ID / "1.7.1_0"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"version": "1.7.1"}))
    st = browsers.extension_state("edge")
    assert st["installed"] and st["source"] == "store" and st["version"] == "1.7.1"


def test_seed_profile_only_fresh(home):
    browsers.seed_profile("chrome")
    prefs = paths.profile_dir("chrome") / "Default" / "Preferences"
    assert json.loads(prefs.read_text())["profile"]["name"] == "SITE Recorder"
    prefs.write_text('{"browser-owned": true}')
    browsers.seed_profile("chrome")
    assert json.loads(prefs.read_text()) == {"browser-owned": True}
    ls = json.loads((paths.profile_dir("chrome") / "Local State").read_text())
    assert ls["background_mode"]["enabled"] is False


def test_config_roundtrip_and_corrupt(home):
    cfg = config.load()
    cfg.browser = "edge"
    config.save(cfg)
    assert config.load().is_configured
    paths.config_file().write_text("{not json")
    assert not config.load().is_configured             # corrupt -> defaults, no crash
    # unknown browser -> unset; keys of older versions (participant_id) are ignored
    paths.config_file().write_text(json.dumps({"browser": "safari", "participant_id": "P1"}))
    c = config.load()
    assert c.browser == "" and not hasattr(c, "participant_id")


def _session(home: Path, sid="20260101_101010", shots=2, warc=True, host=True) -> Path:
    sd = home / "sessions" / sid
    (sd / "browser_screenshots").mkdir(parents=True)
    for i in range(shots):
        (sd / "browser_screenshots" / f"{1767262210 + i * 30:.6f}_click_action.png").write_bytes(b"x")
    if warc:
        (sd / "archive").mkdir()
        (sd / "archive" / "session.warc.gz").write_bytes(b"\x1f\x8bxx")
    if host:
        (sd / "extension_host.log").write_text("ok")
    (sd / "metadata.json").write_text("{}")
    return sd


def test_finalize_statuses(home):
    full = _session(home, "20260101_000001")
    assert recorder.finalize(full, started_at=time.time() - 60)["status"] == "complete"
    noshots = _session(home, "20260101_000002", shots=0)
    info = recorder.finalize(noshots)
    assert info["status"] == "complete" and "No screenshots" in info["note"]
    nothing = _session(home, "20260101_000003", shots=0, warc=False)
    assert recorder.finalize(nothing)["status"] == "empty"
    never = _session(home, "20260101_000004", host=False)
    assert "never connected" in recorder.finalize(never)["note"]
    # idempotent + plumbing removed
    (full / "control.json").write_text("{}")
    recorder.finalize(full)
    assert not (full / "control.json").exists()
    assert json.loads((full / "metadata.json").read_text())["stopped_at"]


def test_recover_orphaned_session(home, monkeypatch):
    sd = _session(home)
    paths.app_dir().mkdir(parents=True, exist_ok=True)
    recorder._write_marker({"session_dir": str(sd), "session_id": sd.name, "state": "recording",
                            "recorder_pid": 999999, "recorder_started": 1.0, "browser": "chrome",
                            "profile_dir": str(paths.profile_dir("chrome")), "started_at": time.time() - 30})
    monkeypatch.setattr(browsers, "close", lambda b, timeout=15.0: True)
    monkeypatch.setattr(browsers, "is_running", lambda b: False)
    assert recorder.recover() == sd
    assert recorder.active_session() is None
    info = json.loads((sd / "session_info.json").read_text())
    assert info["end_reason"].startswith("recovered")


def test_recover_leaves_live_recorder_alone(home):
    sd = _session(home)
    me = psutil.Process(os.getpid())
    paths.app_dir().mkdir(parents=True, exist_ok=True)
    recorder._write_marker({"session_dir": str(sd), "session_id": sd.name, "state": "recording",
                            "recorder_pid": me.pid, "recorder_started": me.create_time(), "browser": "chrome"})
    assert recorder.recover() is None
    assert recorder.active_session()["session_id"] == sd.name


def test_adopt_orphan_with_running_browser(home, monkeypatch):
    sd = _session(home)
    paths.app_dir().mkdir(parents=True, exist_ok=True)
    recorder._write_marker({"session_dir": str(sd), "session_id": sd.name, "state": "recording",
                            "recorder_pid": 999999, "recorder_started": 1.0, "browser": "chrome"})
    monkeypatch.setattr(browsers, "is_running", lambda b: True)
    m = recorder.adopt_if_orphaned()
    assert m and m["recorder_pid"] == os.getpid()
    assert recorder.recover() is None             # now owned by a live recorder


def test_set_paused_writes_control(home):
    sd = _session(home)
    paths.app_dir().mkdir(parents=True, exist_ok=True)
    me = psutil.Process(os.getpid())
    recorder._write_marker({"session_dir": str(sd), "session_id": sd.name, "state": "recording",
                            "recorder_pid": me.pid, "recorder_started": me.create_time(), "cmd": 0})
    r = recorder.Recorder(config.Config(browser="chrome"))
    r.set_paused(True)
    r.set_paused(True)          # same value again must still be a NEW command
    c = json.loads((sd / "control.json").read_text())
    assert c == {"paused": True, "cmd": 2}


def test_sessions_listing(home):
    _session(home, "20260101_000001")
    (home / "sessions" / "not-a-session").mkdir()
    rows = sessions.list_sessions(home / "sessions", active_id="20260101_000001")
    assert [r.id for r in rows] == ["20260101_000001"] and rows[0].status == "recording"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch files")
@pytest.mark.parametrize("via_env", [True, False])
def test_write_batch_runs_with_non_ascii_path(tmp_path, monkeypatch, via_env):
    d = tmp_path / "Usér Ünïcode dir"
    d.mkdir()
    target = d / "out.txt"
    bat = d / "t.bat"
    if via_env:   # like a non-ASCII user name inside %LOCALAPPDATA%
        monkeypatch.setenv("LOCALAPPDATA", str(d))
    line = f'echo hello> "{nativehost.batch_path(target)}"'
    assert line.isascii() == via_env
    nativehost.write_batch(bat, [line])
    subprocess.run(["cmd", "/d", "/c", str(bat)], check=True, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    assert target.read_text().strip() == "hello"


def test_wrapper_and_manifest(home):
    nativehost.write_wrapper()
    w = nativehost.wrapper_path()
    text = w.read_bytes().decode("utf-8", errors="replace")
    assert "site_recorder.host" in text
    assert (nativehost.batch_path(paths.app_dir()) if sys.platform == "win32" else str(paths.app_dir())) in text
    m = nativehost._manifest()
    assert m["path"] == str(w)
    assert f"chrome-extension://{EXTENSION_ID}/" in m["allowed_origins"]


def test_extension_copy_updates_changed_files(home):
    d = recorder.ensure_extension_copy()
    bg = d / "background.js"
    bg.write_text("stale")
    recorder.ensure_extension_copy()
    assert bg.read_text(encoding="utf-8") != "stale"
    m = json.loads((d / "manifest.json").read_text())
    assert m["key"] and m["version"]


def test_is_registered_roundtrip(home):
    nativehost.write_wrapper()
    if sys.platform == "win32":
        pytest.skip("registry write is global; covered by the real-install test")
    nativehost.register("chrome")
    assert nativehost.is_registered("chrome") == (True, "registered")


def test_atomic_write_and_read_json(tmp_path):
    from site_recorder import fsutil
    p = tmp_path / "x.json"
    fsutil.atomic_write_json(p, {"a": 1})
    assert fsutil.read_json(p) == {"a": 1}
    assert fsutil.read_json(tmp_path / "missing.json") is None
    p.write_text("{broken")
    assert fsutil.read_json(p) is None
    assert not list(tmp_path.glob("*.tmp"))


def test_stop_is_serialized_and_expected_session(home, monkeypatch):
    """Two concurrent stops finalize once; a stop for another session is a no-op."""
    import threading
    sd = _session(home)
    me = psutil.Process(os.getpid())
    paths.app_dir().mkdir(parents=True, exist_ok=True)
    recorder._write_marker({"session_dir": str(sd), "session_id": sd.name, "state": "recording",
                            "recorder_pid": me.pid, "recorder_started": me.create_time(), "browser": "chrome"})
    monkeypatch.setattr(browsers, "close", lambda b, timeout=15.0: True)
    assert recorder.stop_active(expected_session="20000101_000000") is None
    assert recorder.active_session() is not None
    calls = []
    real_finalize = recorder.finalize
    monkeypatch.setattr(recorder, "finalize", lambda *a, **k: calls.append(1) or real_finalize(*a, **k))
    results = []
    ts = [threading.Thread(target=lambda: results.append(recorder.stop_active())) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert len(calls) == 1 and sorted(map(str, results)) == sorted([str(sd), "None"])
    assert recorder.active_session() is None


def test_host_finished_detects_stale_heartbeat(home):
    sd = _session(home)
    (sd / ".host_alive").write_text(json.dumps({"pid": os.getpid(), "ts": time.time() - 120}))
    assert recorder._host_finished_after(sd, time.time())          # heartbeat too old
    (sd / ".host_alive").write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    assert not recorder._host_finished_after(sd, time.time())      # live pid, fresh heartbeat
    (sd / ".host_done").write_text(json.dumps({"pid": os.getpid(), "ts": time.time() - 50}))
    assert recorder._host_finished_after(sd, time.time())          # that same host is done


def test_export_failure_leaves_no_partial(tmp_path, monkeypatch):
    from site_recorder import export
    sd = _session(tmp_path / "home")
    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(export, "_write_zip", boom)
    with pytest.raises(OSError):
        export.export_session(sd, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*"))


def test_stamped_extension_version_changes_with_code(home):
    d = recorder.ensure_extension_copy()
    v1 = json.loads((d / "manifest.json").read_text())["version"]
    assert v1.count(".") == 3
    src = Path(recorder.__file__).parent / "extension"
    files = sorted(f for f in src.iterdir() if f.is_file())
    m = (src / "manifest.json").read_bytes()
    v_again = json.loads(recorder._stamped_manifest(m, files))["version"]
    assert v_again == v1                                           # deterministic


def test_cdp_websocket_client():
    """extinstall._Cdp against a fake DevTools server: handshake, masked
    client frames, a fragmented reply with 64-bit lengths, errors, close."""
    import socket
    import struct
    import threading
    from site_recorder import extinstall

    srv = socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    seen = []

    def serve():
        c, _ = srv.accept()
        f = c.makefile("rb")
        assert f.readline().startswith(b"GET /devtools/browser/x ")
        while f.readline() != b"\r\n":
            pass
        c.sendall(b"HTTP/1.1 101 WebSocket Protocol Handshake\r\n\r\n")
        for _ in range(2):
            b0, b1 = f.read(2)
            assert b0 == 0x81 and b1 & 0x80              # final text frame, masked
            key = f.read(4)
            req = json.loads(extinstall._mask(f.read(b1 & 0x7F), key))
            seen.append(req)
            if req["method"] == "Big.echo":
                body = json.dumps({"id": req["id"], "result": {"v": "x" * 70000}}).encode()
                c.sendall(bytes([0x01, 127]) + struct.pack(">Q", 10) + body[:10]
                          + bytes([0x80, 127]) + struct.pack(">Q", len(body) - 10) + body[10:])
            else:
                body = json.dumps({"id": req["id"], "error": {"message": "nope"}}).encode()
                c.sendall(bytes([0x81, len(body)]) + body)
        c.close()
    threading.Thread(target=serve, daemon=True).start()

    cdp = extinstall._Cdp(port, "/devtools/browser/x")
    assert cdp.send("Big.echo", {"a": 1}, session="s1", timeout=5)["v"] == "x" * 70000
    with pytest.raises(RuntimeError, match="nope"):
        cdp.send("Bad.call", timeout=5)
    assert seen[0] == {"id": 1, "method": "Big.echo", "params": {"a": 1}, "sessionId": "s1"}
    with pytest.raises(TimeoutError):                     # server closed the socket
        cdp.send("After.close", timeout=5)
    cdp.close()
    srv.close()


def test_devtools_endpoint_parses_port_file(tmp_path):
    from site_recorder import extinstall
    f = tmp_path / "DevToolsActivePort"
    f.write_text("51234\n/devtools/browser/abc-123")
    assert extinstall._devtools_endpoint(f, timeout=1) == (51234, "/devtools/browser/abc-123")
    f.write_text("51234\n")                              # half-written: keep waiting
    with pytest.raises(TimeoutError):
        extinstall._devtools_endpoint(f, timeout=0.5)
