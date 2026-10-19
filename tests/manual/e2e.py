"""End-to-end recording test (real browser). Usage: python e2e.py edge|chrome [wait_s]

Loads the extension TEMPORARILY over a DevTools pipe (test-only; real installs
are via the Web Store / Load unpacked) and then drives a real recording
session through the recorder API: start (attach to the running browser), real
page loads, periodic screenshot, pause/resume, stop, finalize, export, verify.
"""
import json
import sys
import time

from site_recorder import browsers, config, export, extinstall, nativehost, paths, recorder, setup

browser = sys.argv[1]
wait_s = float(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith('-') else 70
cfg = config.load()
cfg.browser = browser
import testsite
SRV = testsite.start(8765)
cfg.start_url = "http://127.0.0.1:8765/a"
config.save(cfg)
setup.prepare(browser)
print("registered:", nativehost.is_registered(browser))

PERSISTENT = "--persistent" in sys.argv
pipe = None
if PERSISTENT:
    print("extension state:", browsers.extension_state(browser))
    print("preflight:", recorder.Recorder(cfg).preflight())
else:
    pid, pipe = extinstall.launch_with_temporary_extension(browser)
    time.sleep(3)
    recorder.Recorder.preflight = lambda self: []      # temp install may not be in prefs yet

t0 = time.time()
sd = recorder.Recorder(cfg).start()
print(f"started {sd.name}; connected={recorder.host_connected(sd)} after {time.time()-t0:.1f}s")
time.sleep(4)
browsers.launch(browser, ["https://www.wikipedia.org/"])   # a tab opened WITH a URL (pendingUrl path)
time.sleep(3)
browsers.launch(browser, ["http://127.0.0.1:8765/a"])        # second local tab
print(f"waiting {wait_s:.0f}s for the periodic screenshot…")
time.sleep(wait_s)
rec = recorder.Recorder(cfg)
rec.set_paused(True)
time.sleep(3)
print("paused:", recorder.Recorder.is_paused())
rec.set_paused(False)
time.sleep(3)
print("resumed:", not recorder.Recorder.is_paused())
out = recorder.stop_active(cfg=cfg, reason="e2e")
print("stopped:", out, "| browser still running:", browsers.is_running(browser))
if pipe:
    pipe.close()
info = json.loads((sd / "session_info.json").read_text())
print("session_info:", {k: info.get(k) for k in ("status", "note", "duration_s", "counts", "browser_version")})
files = sorted(str(p.relative_to(sd)).replace("\\", "/") for p in sd.rglob("*") if p.is_file())
print("files:", [f for f in files if not f.startswith(("browser_screenshots/", "dom_snapshots/0"))],
      f"+ {sum(f.startswith('browser_screenshots/') for f in files)} screenshots",
      f"+ {sum(f.startswith('dom_snapshots/0') for f in files)} dom snapshots")
if (sd / "archive" / "session.warc.gz").exists():
    from warcio.archiveiterator import ArchiveIterator
    with open(sd / "archive" / "session.warc.gz", "rb") as f:
        recs = [(r.rec_type, r.rec_headers.get_header("WARC-Target-URI"),
                 r.rec_headers.get_header("WARC-SiteBench-Tab-Id")) for r in ArchiveIterator(f)]
    print("warc records:", len(recs), "responses:", sorted(set(u for t, u, _ in recs if t == "response"))[:12],
          "untagged:", sum(1 for _, _, tab in recs if tab is None))
ev = [json.loads(l) for l in (sd / "events.jsonl").read_text().splitlines()] if (sd / "events.jsonl").exists() else []
print("events:", [e.get("event_type") or e.get("type") for e in ev])
tl = (sd / "tab_timeline.jsonl").read_text().splitlines() if (sd / "tab_timeline.jsonl").exists() else []
print("timeline:", [json.loads(l).get("url") for l in tl][:6])
print("trace:\n" + (sd / "recorder_trace.log").read_text())
print("host log tail:\n" + "\n".join((sd / "extension_host.log").read_text(encoding="utf-8").splitlines()[-12:]))
z = export.export_session(sd, paths.exports_dir())
print("export:", z.name, export.verify_zip(z)[:2])
print("active after stop:", recorder.active_session())
