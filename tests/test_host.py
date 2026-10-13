"""The native host end to end: spawn it like a browser would and check the
session files it writes (formats the server pipeline depends on)."""
import base64
import json
import os
import time
from pathlib import Path

import psutil

from conftest import PNG_1x1


def _make_active_session(home: Path, sid: str = "20260101_120000", state: str = "recording") -> Path:
    app = home / "app"
    app.mkdir(parents=True, exist_ok=True)
    sd = home / "sessions" / sid
    sd.mkdir(parents=True)
    me = psutil.Process(os.getpid())
    (app / "active_session.json").write_text(json.dumps({
        "session_dir": str(sd), "session_id": sid, "state": state,
        "recorder_pid": me.pid, "recorder_started": me.create_time(),
        "browser": "chrome", "profile_dir": None,
    }))
    return sd


def test_refuses_without_active_session(home, host_factory):
    hp = host_factory()
    msg = hp.recv()
    assert msg == {"type": "stop"}
    assert hp.close() == 0
    log = home / "app" / "logs" / "host_refusals.log"
    assert "no active session" in log.read_text()


def test_refuses_when_recorder_dead(home, host_factory):
    sd = _make_active_session(home)
    m = json.loads((home / "app" / "active_session.json").read_text())
    m["recorder_pid"] = 999999          # no such process
    (home / "app" / "active_session.json").write_text(json.dumps(m))
    hp = host_factory()
    assert hp.recv() == {"type": "stop"}
    hp.close()
    assert not (sd / "extension_host.log").exists()   # no false "connected" signal


def test_refuses_while_stopping(home, host_factory):
    _make_active_session(home, state="stopping")
    hp = host_factory()
    assert hp.recv() == {"type": "stop"}
    hp.close()


def test_full_session(home, host_factory):
    sd = _make_active_session(home)
    hp = host_factory()
    hello = hp.recv_until("connected")
    assert hello["session_id"] == sd.name and hello["paused"] is False

    png = "data:image/png;base64," + base64.b64encode(PNG_1x1).decode()
    hp.send({"type": "tab_activated", "tabId": 7, "windowId": 1, "url": "https://example.com/",
             "title": "Example", "timestamp": time.time()})
    hp.send({"type": "network_request", "requestId": "r1", "tabId": 7, "frameId": "f",
             "site_url": "https://example.com/",
             "request": {"url": "https://example.com/api", "method": "GET", "headers": {"Accept": "*/*"}}})
    hp.send({"type": "network_response", "requestId": "r1",
             "response": {"url": "https://example.com/api", "status": 200, "statusText": "OK",
                          "headers": {"Content-Type": "application/json"}},
             "body": base64.b64encode(b'{"ok": true}').decode(), "base64Encoded": True})
    hp.send({"type": "browser_snapshot", "timestamp": 1, "event_type": "click_action",
             "details": {"x": 1, "y": 2}, "image_data": png, "url": "https://example.com/"})
    hp.send({"type": "dom_snapshot", "url": "https://example.com/", "title": "Ex",
             "reason": "load", "html": "<html>héllo \ud800</html>", "timestamp": 5})
    hp.send({"type": "file_upload", "url": "https://example.com/", "timestamp": 6,
             "file": {"name": "a b.txt", "size": 3, "mime": "text/plain",
                      "dataUrl": "data:text/plain;base64," + base64.b64encode(b"abc").decode()}})
    hp.send({"type": "websocket_frame", "requestId": "w", "timestamp": 7,
             "response": {"opcode": 1, "payloadData": "hi"}, "tabId": 7, "site_url": "https://example.com/"})
    hp.send({"type": "recording_paused", "timestamp": 8})
    hp.send({"type": "bogus_type", "x": 1})
    hp.send(["not", "a", "dict"])                      # must not kill the host

    # stop: the recorder drops stop.request; the host must relay "stop"
    time.sleep(0.5)
    (sd / "stop.request").write_text("x")
    assert hp.recv_until("stop", timeout=10)["type"] == "stop"
    assert hp.close() == 0

    done = json.loads((sd / ".host_done").read_text())
    assert done["reason"] == "stop"

    # screenshot <-> events.jsonl share the exact timestamp (pipeline join key)
    shots = list((sd / "browser_screenshots").glob("*.png"))
    assert len(shots) == 1 and shots[0].read_bytes() == PNG_1x1
    events = [json.loads(l) for l in (sd / "events.jsonl").read_text().splitlines()]
    snap = [e for e in events if e["type"] == "browser_snapshot"][0]
    assert shots[0].name == f"{snap['timestamp']:.6f}_click_action.png"
    assert "image_data" not in snap
    assert any(e["type"] == "recording_paused" for e in events)
    assert json.loads((sd / "status.json").read_text())["paused"] is True
    assert json.loads((sd / "live_stats.json").read_text())["clicks"] == 1

    # WARC: readable, request + response, SiteBench headers
    from warcio.archiveiterator import ArchiveIterator
    with open(sd / "archive" / "session.warc.gz", "rb") as f:
        recs = [(r.rec_type, r.rec_headers.get_header("WARC-Target-URI"),
                 r.rec_headers.get_header("WARC-SiteBench-Tab-Id"), r.content_stream().read())
                for r in ArchiveIterator(f)]
    assert ("request", "https://example.com/api", "7", b"") in recs
    resp = [r for r in recs if r[0] == "response"][0]
    assert resp[2] == "7" and resp[3] == b'{"ok": true}'   # tab id backfilled from request

    idx = [json.loads(l) for l in (sd / "dom_snapshots" / "index.jsonl").read_text().splitlines()]
    assert idx[0]["file"] == "00001.html"
    assert "héllo" in (sd / "dom_snapshots" / "00001.html").read_text(encoding="utf-8")
    ups = [json.loads(l) for l in (sd / "uploads" / "index.jsonl").read_text().splitlines()]
    assert ups[0]["file"] == "0001_a_b.txt" and (sd / "uploads" / "0001_a_b.txt").read_bytes() == b"abc"
    assert (sd / "tab_timeline.jsonl").read_text().count("tab_activated") == 1
    assert (sd / "websocket_frames.jsonl").exists()


def test_reconnect_appends_and_continues_numbering(home, host_factory):
    sd = _make_active_session(home)
    png = "data:image/png;base64," + base64.b64encode(PNG_1x1).decode()
    for i in range(2):
        hp = host_factory()
        hello = hp.recv_until("connected")
        if i == 1:
            assert hello["paused"] is True       # reconnect keeps the pause
        hp.send({"type": "browser_snapshot", "event_type": "click_action", "details": {},
                 "image_data": png, "url": "https://e.com/"})
        hp.send({"type": "dom_snapshot", "url": "https://e.com/", "html": "<p>x</p>", "timestamp": 1})
        hp.send({"type": "file_upload", "url": "https://e.com/", "timestamp": 6,
                 "file": {"name": "same.txt", "size": 1, "dataUrl": "data:text/plain;base64,eA=="}})
        hp.send({"type": "network_request", "requestId": f"r{i}",
                 "request": {"url": f"https://e.com/{i}", "method": "GET", "headers": {}}})
        if i == 0:
            hp.send({"type": "recording_paused", "timestamp": 2})
        time.sleep(0.3)
        assert hp.close() == 0   # port dropped (service-worker restart)
    assert sorted(p.name for p in (sd / "dom_snapshots").glob("*.html")) == ["00001.html", "00002.html"]
    assert sorted(p.name for p in (sd / "uploads").iterdir() if p.suffix == ".txt") == \
        ["0001_same.txt", "0002_same.txt"]
    assert json.loads((sd / "live_stats.json").read_text())["clicks"] == 2
    from warcio.archiveiterator import ArchiveIterator
    with open(sd / "archive" / "session.warc.gz", "rb") as f:
        uris = [r.rec_headers.get_header("WARC-Target-URI") for r in ArchiveIterator(f)]
    assert uris == ["https://e.com/0", "https://e.com/1"]


def test_stop_without_cooperating_extension_exits(home, host_factory):
    """An old extension build ignores "stop"; the host must not linger."""
    sd = _make_active_session(home)
    hp = host_factory()
    hp.recv_until("connected")
    (sd / "stop.request").write_text("x")
    hp.recv_until("stop")
    # do NOT close stdin — the host has to exit on its own
    assert hp.p.wait(timeout=10) == 0
    assert json.loads((sd / ".host_done").read_text())["reason"] == "stop"


def test_large_garbage_length_does_not_hang(home, host_factory):
    _make_active_session(home)
    hp = host_factory()
    hp.recv_until("connected")
    import struct
    hp.p.stdin.write(struct.pack("@I", 0xFFFFFFF0))
    hp.p.stdin.flush()
    assert hp.p.wait(timeout=10) == 0


def test_reconnect_does_not_resend_old_command(home, host_factory):
    """App pause (cmd 1) + resume (cmd 2), then a popup pause, then a port
    drop: the NEW host must not replay cmd 2 (that would un-pause)."""
    sd = _make_active_session(home)
    hp = host_factory()
    hp.recv_until("connected")
    for cmd, paused in ((1, True), (2, False)):
        (sd / "control.json").write_text(json.dumps({"paused": paused, "cmd": cmd}))
        assert hp.recv_until("set_paused")["paused"] is paused
        hp.send({"type": "recording_paused" if paused else "recording_resumed", "timestamp": time.time()})
    hp.send({"type": "recording_paused", "timestamp": time.time()})      # popup pause
    time.sleep(0.4)
    assert hp.close() == 0
    hp2 = host_factory()
    hello = hp2.recv_until("connected")
    assert hello["paused"] is True
    assert hp2.recv(5) == {"type": "set_paused", "paused": True}       # re-asserted for old builds
    time.sleep(0.6)                                                     # several control polls
    hp2.send({"type": "debug_log", "msg": "ping"})
    (sd / "stop.request").write_text("x")
    msgs = []
    while True:
        m = hp2.recv(10)
        if m is None or m.get("type") == "stop":
            break
        msgs.append(m)
    assert not [m for m in msgs if m.get("type") == "set_paused" and m.get("paused") is False]
    hp2.send({"type": "stopped"})
    assert hp2.p.wait(10) == 0


def test_stopped_ack_drains_queue_then_exits(home, host_factory):
    sd = _make_active_session(home)
    hp = host_factory()
    hp.recv_until("connected")
    (sd / "stop.request").write_text("x")
    hp.recv_until("stop")
    for i in range(20):                         # messages still queued in the port
        hp.send({"type": "network_request", "requestId": f"q{i}",
                 "request": {"url": f"https://q.example/{i}", "method": "GET", "headers": {}}})
    hp.send({"type": "stopped"})
    assert hp.p.wait(10) == 0
    from warcio.archiveiterator import ArchiveIterator
    with open(sd / "archive" / "session.warc.gz", "rb") as f:
        assert len(list(ArchiveIterator(f))) == 20


def test_truncated_warc_tail_is_repaired(home, host_factory):
    sd = _make_active_session(home)
    hp = host_factory()
    hp.recv_until("connected")
    for i in range(3):
        hp.send({"type": "network_request", "requestId": f"r{i}",
                 "request": {"url": f"https://t.example/{i}", "method": "GET", "headers": {}}})
    time.sleep(0.5)
    hp.close()
    warc = sd / "archive" / "session.warc.gz"
    data = warc.read_bytes()
    warc.write_bytes(data + data[:40])            # a host killed mid-record
    hp2 = host_factory()
    hp2.recv_until("connected")
    hp2.send({"type": "network_request", "requestId": "r9",
              "request": {"url": "https://t.example/9", "method": "GET", "headers": {}}})
    time.sleep(0.5)
    hp2.close()
    from warcio.archiveiterator import ArchiveIterator
    with open(warc, "rb") as f:
        uris = [r.rec_headers.get_header("WARC-Target-URI") for r in ArchiveIterator(f)]
    assert uris == [f"https://t.example/{i}" for i in (0, 1, 2, 9)]
