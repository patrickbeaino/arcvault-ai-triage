#!/usr/bin/env python3
"""ArcVault triage board — bonus demo UI (not part of the assessed workflow).

Zero-dependency local server:
  GET  /            the board (ui/index.html)
  GET  /portal      the customer-facing intake page (ui/portal.html)
  GET  /api/tickets all processed records from output/, newest first
  POST /api/submit  forwards {source, message} to the live n8n webhook and
                    returns the processed record (server-side proxy, no CORS)
  POST /api/review  human-in-the-loop override: re-routes a ticket to a queue,
                    recorded in the JSON with an audit trail (rule: human_override)

Run:  python3 ui/server.py   → http://localhost:8090
"""
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
INDEX = Path(__file__).resolve().parent / "index.html"
PORTAL = Path(__file__).resolve().parent / "portal.html"
WEBHOOK = os.environ.get("ARCVAULT_WEBHOOK", "http://localhost:5678/webhook/arcvault-intake")
PORT = int(os.environ.get("ARCVAULT_UI_PORT", "8090"))
QUEUES = ["Engineering", "Product", "Billing", "IT/Security", "Human Review / Escalation"]


def load_tickets():
    tickets = []
    for f in sorted(OUTPUT_DIR.glob("*.json")):
        try:
            tickets.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            continue  # skip half-written or invalid files rather than break the board
    tickets.sort(key=lambda t: t.get("meta", {}).get("processed_at", ""), reverse=True)
    return tickets


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/portal"):
            self._send(200, PORTAL.read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/demo"):
            self._send(200, (Path(__file__).resolve().parent / "demo.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/tickets":
            self._send(200, {"tickets": load_tickets(), "webhook": WEBHOOK})
        elif self.path == "/favicon.ico":
            self.send_response(204); self.end_headers()
        else:
            self._send(404, {"error": "not found"})

    def _review(self, payload):
        rid = str(payload.get("request_id", ""))
        dest = payload.get("destination")
        path = OUTPUT_DIR / f"{rid}.json"
        if not rid or "/" in rid or not path.is_file():
            return self._send(404, {"error": f"No ticket named {rid or '(empty)'}."})
        if dest not in QUEUES:
            return self._send(400, {"error": f"Unknown queue: {dest}"})
        record = json.loads(path.read_text())
        record["review"] = {
            "previous_destination": record.get("routing", {}).get("destination"),
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "via": "triage_board",
        }
        record.setdefault("routing", {})["destination"] = dest
        record["routing"]["rule"] = "human_override"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2) + "\n")
        tmp.replace(path)
        return self._send(200, record)

    def do_POST(self):
        if self.path not in ("/api/submit", "/api/review"):
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
            if self.path == "/api/review":
                return self._review(payload)
            message = (payload.get("message") or "").strip()
            if not message:
                return self._send(400, {"error": "Enter a message before routing."})
            body = json.dumps({"source": payload.get("source", "web_form"), "message": message}).encode()
            req = urllib.request.Request(WEBHOOK, body, {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=240) as resp:
                return self._send(200, resp.read())
        except urllib.error.URLError as e:
            return self._send(502, {"error": f"Workflow unreachable at {WEBHOOK} — is n8n running? ({e.reason})"})
        except Exception as e:  # keep the demo alive whatever happens
            return self._send(500, {"error": str(e)})

    def log_message(self, fmt, *args):  # quiet: only log errors, not every poll
        if args and str(args[0]).startswith(("4", "5")):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    print(f"ArcVault triage board → http://localhost:{PORT}   (workflow: {WEBHOOK})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
