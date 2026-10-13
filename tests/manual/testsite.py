"""Tiny local test site for e2e recording tests (runs in a thread)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE_A = b"""<!doctype html><html><head><title>Page A</title></head><body>
<h1>Page A</h1><p>Navigating to B in 3 s...</p>
<script>setTimeout(() => { location.href = '/b'; }, 3000);</script></body></html>"""

PAGE_B = b"""<!doctype html><html><head><title>Page B</title><link rel=stylesheet href=/style.css></head><body>
<h1>Page B</h1><img src=/img.svg><div id=out></div>
<script>
let n = 0;
async function tick() {
  const r = await fetch('/api/items?n=' + (n++));
  document.getElementById('out').textContent = JSON.stringify(await r.json());
  await fetch('/api/save', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({n})});
}
setInterval(tick, 1500); tick();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/a"):
            self._send(200, "text/html", PAGE_A)
        elif p == "/b":
            self._send(200, "text/html", PAGE_B)
        elif p == "/style.css":
            self._send(200, "text/css", b"body{font-family:sans-serif;background:#eef}")
        elif p == "/img.svg":
            self._send(200, "image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><circle cx="20" cy="20" r="18" fill="teal"/></svg>')
        elif p == "/api/items":
            self._send(200, "application/json", json.dumps({"items": ["alpha", "beta"], "q": self.path}).encode())
        else:
            self._send(404, "text/plain", b"nope")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        self._send(200, "application/json", b'{"saved": true}')


def start(port: int = 8765) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
