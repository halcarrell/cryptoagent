import os, json
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8080))

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", "port": PORT}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

print(f"minimal server on :{PORT}", flush=True)
HTTPServer(("0.0.0.0", PORT), H).serve_forever()
