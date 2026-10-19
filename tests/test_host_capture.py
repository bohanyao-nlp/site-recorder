"""Host handling of the extension 1.9.0 capture upgrades: request/response
pairing, redirects, failures, redaction, screenshot blanking, layout
snapshots, socket/SSE records and the privacy skip list."""
import base64
import gzip
import io
import json
import time
from pathlib import Path

from test_host import _make_active_session


def _warc_records(sd: Path):
    from warcio.archiveiterator import ArchiveIterator
    out = []
    with open(sd / "archive" / "session.warc.gz", "rb") as f:
        for r in ArchiveIterator(f):
            out.append({
                "type": r.rec_type, "uri": r.rec_headers.get_header("WARC-Target-URI"),
                "id": r.rec_headers.get_header("WARC-Record-ID"),
                "concurrent": r.rec_headers.get_header("WARC-Concurrent-To"),
                "h": dict(r.rec_headers.headers),
                "http": dict(r.http_headers.headers) if r.http_headers else {},
                "status": r.http_headers.get_statuscode() if r.http_headers and r.rec_type == "response" else None,
                "body": r.content_stream().read(),
            })
    return out


def _stop(sd: Path, hp) -> None:
    time.sleep(0.4)
    (sd / "stop.request").write_text("x")
    hp.recv_until("stop", timeout=10)
    hp.send({"type": "stopped"})
    assert hp.p.wait(10) == 0


def _png(w: int, h: int, color=(255, 255, 255)) -> str:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_requests_pair_with_responses_redacted_and_timed(home, host_factory):
    sd = _make_active_session(home)
    hp = host_factory()
    hp.recv_until("connected")
    hp.send({"type": "network_request", "requestId": "a", "tabId": 1, "wallTime": 1700000000.25,
             "timestamp": 100.0, "typeStr": "XHR",
             "initiator": {"type": "script", "stack": [{"url": "https://e.com/app.js", "fn": "login"}]},
             "request": {"url": "https://e.com/login", "method": "POST",
                         "headers": {"Content-Type": "application/json", "Authorization": "Bearer SECRETTOKEN"}},
             "postData": json.dumps({"user": "ann", "password": "hunter2"})})
    hp.send({"type": "network_request", "requestId": "b", "tabId": 1, "wallTime": 1700000000.5,
             "timestamp": 100.25, "request": {"url": "https://e.com/other", "method": "GET", "headers": {}}})
    hp.send({"type": "network_response", "requestId": "a", "tabId": 1, "timestamp": 101.0,
             "encodedDataLength": 12,
             "response": {"url": "https://e.com/login", "status": 200, "statusText": "OK",
                          "headers": {"Set-Cookie": "sid=abc; Path=/"}},
             "body": base64.b64encode(b'{"ok": true}').decode(), "base64Encoded": True})
    _stop(sd, hp)

    recs = _warc_records(sd)
    i = next(i for i, r in enumerate(recs) if r["type"] == "request" and r["uri"] == "https://e.com/login")
    req, resp = recs[i], recs[i + 1]
    assert resp["type"] == "response" and resp["uri"] == "https://e.com/login"     # adjacent
    assert resp["concurrent"] == req["id"]
    body = json.loads(req["body"])
    assert body["user"] == "ann" and body["password"].startswith("REDACTED:")
    assert req["http"]["Authorization"].startswith("Bearer REDACTED:")
    assert "abc" not in resp["http"]["Set-Cookie"]
    assert req["h"]["WARC-SiteBench-Wall-Time"] == "1700000000.250000"
    assert resp["h"]["WARC-SiteBench-Wall-Time"] == "1700000001.250000"            # wall + monotonic delta
    assert req["h"]["WARC-Date"] == "2023-11-14T22:13:20Z"
    assert json.loads(req["h"]["WARC-SiteBench-Initiator"])["type"] == "script"
    assert req["h"]["WARC-SiteBench-Resource-Type"] == "XHR"
    assert resp["h"]["WARC-SiteBench-Encoded-Length"] == "12"
    assert resp["body"] == b'{"ok": true}'
    # the request that never got a response is flushed at session end
    assert any(r["type"] == "request" and r["uri"] == "https://e.com/other" for r in recs)


def test_redirect_chain_and_failures(home, host_factory):
    sd = _make_active_session(home)
    hp = host_factory()
    hp.recv_until("connected")
    hp.send({"type": "network_request", "requestId": "r", "tabId": 2,
             "request": {"url": "https://e.com/a", "method": "GET", "headers": {}}})
    hp.send({"type": "network_request", "requestId": "r", "tabId": 2,
             "request": {"url": "https://e.com/b", "method": "GET", "headers": {}},
             "redirectResponse": {"url": "https://e.com/a", "status": 302, "statusText": "Found",
                                  "headers": {"Location": "https://e.com/b"}}})
    hp.send({"type": "network_response", "requestId": "r", "tabId": 2,
             "response": {"url": "https://e.com/b", "status": 200, "headers": {}}})
    hp.send({"type": "network_request", "requestId": "f", "tabId": 2,
             "request": {"url": "https://e.com/down", "method": "GET", "headers": {}}})
    hp.send({"type": "network_failed", "requestId": "f", "tabId": 2, "errorText": "net::ERR_NAME_NOT_RESOLVED"})
    _stop(sd, hp)
    recs = _warc_records(sd)
    assert [(r["type"], r["uri"], r["status"]) for r in recs[:4]] == [
        ("request", "https://e.com/a", None), ("response", "https://e.com/a", "302"),
        ("request", "https://e.com/b", None), ("response", "https://e.com/b", "200")]
    assert recs[1]["h"]["WARC-SiteBench-Redirect"] == "1"
    failed = [r for r in recs if r["uri"] == "https://e.com/down"][0]
    assert failed["h"]["WARC-SiteBench-Failed"] == "net::ERR_NAME_NOT_RESOLVED"


def test_same_request_id_in_two_targets_stays_apart(home, host_factory):
    sd = _make_active_session(home)
    hp = host_factory()
    hp.recv_until("connected")
    for sess, path in ((None, "/top"), ("S1", "/frame")):
        hp.send({"type": "network_request", "requestId": "1.1", "tabId": 3, "sessionId": sess,
                 "request": {"url": "https://e.com" + path, "method": "GET", "headers": {}}})
    hp.send({"type": "network_response", "requestId": "1.1", "tabId": 3, "sessionId": "S1",
             "response": {"url": "https://e.com/frame", "status": 200, "headers": {}}})
    _stop(sd, hp)
    recs = _warc_records(sd)
    assert [(r["type"], r["uri"]) for r in recs[:2]] == [("request", "https://e.com/frame"),
                                                        ("response", "https://e.com/frame")]
    assert recs[1]["h"]["WARC-SiteBench-Target-Session"] == "S1"
    assert ("request", "https://e.com/top") in [(r["type"], r["uri"]) for r in recs]


def test_screenshot_redaction_and_event_only(home, host_factory):
    sd = _make_active_session(home)
    hp = host_factory()
    hp.recv_until("connected")
    # 2x device pixels: a 20x10 CSS viewport captured as 40x20
    hp.send({"type": "browser_snapshot", "event_type": "click_action", "image_data": _png(40, 20),
             "details": {"x": 1, "y": 1, "vw": 20, "dpr": 2}, "redact_rects": [[10, 2, 4, 3]],
             "url": "https://e.com/"})
    time.sleep(0.05)
    hp.send({"type": "browser_snapshot", "event_type": "click_action", "image_data": _png(4, 4),
             "details": {}, "redact_unlocated": True, "url": "https://e.com/"})
    hp.send({"type": "browser_snapshot", "event_type": "keydown_action", "screenshot": False,
             "details": {"key": "j", "combo": "j"}, "url": "https://e.com/"})
    _stop(sd, hp)
    from PIL import Image
    shots = sorted((sd / "browser_screenshots").glob("*.png"))
    assert len(shots) == 1                                   # unlocated -> withheld, event-only -> none
    img = Image.open(shots[0]).convert("RGB")
    assert img.getpixel((24, 8)) == (128, 128, 128)          # inside the box (12,4 CSS -> 24,8 px)
    assert img.getpixel((2, 18)) == (255, 255, 255)          # outside
    ev = [json.loads(l) for l in (sd / "events.jsonl").read_text().splitlines()]
    snaps = [e for e in ev if e.get("type") == "browser_snapshot"]
    assert len(snaps) == 3 and all("image_data" not in e for e in snaps)
    assert snaps[0]["redacted_boxes"] == 1
    assert snaps[1]["screenshot"] is False and snaps[1]["screenshot_withheld"] == "sensitive_unlocated"
    assert snaps[2]["screenshot"] is False and snaps[2]["details"]["key"] == "j"
    assert json.loads((sd / "live_stats.json").read_text())["screenshots"] == 1


def test_layout_dom_extras_sockets_and_misc_events(home, host_factory):
    sd = _make_active_session(home)
    hp = host_factory()
    hp.recv_until("connected")
    payload = gzip.compress(b'{"documents": [], "strings": []}')
    hp.send({"type": "layout_snapshot", "url": "https://e.com/", "timestamp": 3, "tabId": 1, "bytes": 30,
             "styles": ["display"], "data_gz_b64": base64.b64encode(payload).decode()})
    hp.send({"type": "dom_snapshot", "url": "https://e.com/", "html": "<p>x</p>", "timestamp": 4,
             "shadow_roots": 2, "cssom_bytes": 99, "env": {"dpr": 2, "tz": "UTC"}})
    hp.send({"type": "websocket_event", "event": "handshake", "requestId": "w", "site_url": "https://e.com/",
             "headers": {"Set-Cookie": "sid=zzz"}})
    hp.send({"type": "websocket_frame", "direction": "sent", "requestId": "w", "site_url": "https://e.com/",
             "response": {"opcode": 1, "payloadData": "hi"}})
    hp.send({"type": "eventsource_message", "requestId": "s", "data": "tok", "site_url": "https://e.com/"})
    hp.send({"type": "zoom_change", "tabId": 1, "zoom": 1.25, "timestamp": 5})
    hp.send({"type": "privacy_skip", "host": "bitwarden.com", "tabId": 1, "timestamp": 6})
    hp.send({"type": "tab_activated", "tabId": 1, "url": "https://e.com/cb?access_token=SECRET", "timestamp": 7})
    _stop(sd, hp)
    idx = [json.loads(l) for l in (sd / "layout_snapshots" / "index.jsonl").read_text().splitlines()]
    assert idx[0]["file"] == "00001.json.gz"
    assert gzip.decompress((sd / "layout_snapshots" / "00001.json.gz").read_bytes()).startswith(b'{"documents"')
    dom = json.loads((sd / "dom_snapshots" / "index.jsonl").read_text().splitlines()[0])
    assert dom["shadow_roots"] == 2 and dom["cssom_bytes"] == 99 and dom["env"]["tz"] == "UTC"
    ws = [json.loads(l) for l in (sd / "websocket_frames.jsonl").read_text().splitlines()]
    assert [w["type"] for w in ws] == ["websocket_event", "websocket_frame", "eventsource_message"]
    assert "zzz" not in ws[0]["headers"]["Set-Cookie"]
    ev = (sd / "events.jsonl").read_text()
    assert '"zoom_change"' in ev and '"privacy_skip"' in ev
    assert "SECRET" not in (sd / "tab_timeline.jsonl").read_text()


def test_handshake_carries_skip_domains(home, host_factory):
    _make_active_session(home)
    m = json.loads((home / "app" / "active_session.json").read_text())
    m["skip_domains"] = ["mybank.com"]
    (home / "app" / "active_session.json").write_text(json.dumps(m))
    hp = host_factory()
    assert hp.recv_until("connected")["skip_domains"] == ["mybank.com"]
    hp.close()
    assert (home / "app" / "redaction.key").exists()
