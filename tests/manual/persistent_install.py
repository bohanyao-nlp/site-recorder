"""TEST HELPER: persistently 'Load unpacked' the bundled extension into the
recording profile, the way a participant would (folder picker filled by UI
Automation). Usage: python persistent_install.py edge|chrome"""
import subprocess
import sys
from pathlib import Path
import threading
import time

from site_recorder import browsers, extinstall, recorder

browser = sys.argv[1]
UIA = str(Path(__file__).with_name("uia.ps1"))   # Windows only (UI Automation)
ext = recorder.ensure_extension_copy()
pid, pipe = extinstall._launch_debuggable(browser, print)
dm = extinstall._set_developer_mode(pipe, browser, print)
scheme = "edge" if browser == "edge" else "chrome"
tgt = pipe.send("Target.createTarget", {"url": f"{scheme}://extensions/"})
sess = pipe.send("Target.attachToTarget", {"targetId": tgt["targetId"], "flatten": True})["sessionId"]
time.sleep(2)
res = {}


def load():
    try:
        res["r"] = pipe.send("Runtime.evaluate", {
            "expression": "chrome.developerPrivate.loadUnpacked({failQuietly: false, populateError: true})"
                          ".then(r => 'OK ' + JSON.stringify(r)).catch(e => 'ERR ' + (e && e.message || e))",
            "awaitPromise": True, "returnByValue": True, "userGesture": True}, session=sess, timeout=60)
    except Exception as e:
        res["r"] = repr(e)


t = threading.Thread(target=load)
t.start()
time.sleep(2)
out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", UIA,
                      "-BrowserPid", str(pid), "-Mode", "fill", "-FolderPath", str(ext)],
                     capture_output=True, text=True)
print("uia:", out.stdout.strip()[-400:], out.stderr.strip()[-300:])
t.join(70)
print("loadUnpacked:", res.get("r"))
time.sleep(2)
pipe.send("Browser.close", timeout=10)
extinstall._wait_gone(browser, 20)
print("state:", browsers.extension_state(browser))
