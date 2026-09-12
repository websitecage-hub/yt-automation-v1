"""Railway job agent: HTTPS remote-exec for heavy work (voice, renders).

Runs inside the Railway worker service. One endpoint, token-gated:
  POST /exec {"cmd": "...", "timeout": 300} -> {"rc": 0, "out": "..."}
  POST /write {"path": "...", "b64": "..."} -> {"ok": true, "bytes": n}
  GET  /read?path=... -> {"b64": "..."}  (small files: samples, logs)
  GET  /health -> {"ok": true}

Security: bearer token from AGENT_TOKEN env, compared in constant time.
Nothing here is reachable except through Railway's HTTPS domain.
"""
import base64
import hashlib
import hmac
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = (os.getenv("AGENT_TOKEN") or "").encode()
WORKDIR = os.getenv("AGENT_WORKDIR", "/data/kronvex")


def _authed(headers):
    auth = headers.get("authorization", "")
    if not (auth.startswith("Bearer ") and TOKEN):
        return False
    return hmac.compare_digest(auth[7:].encode(), TOKEN)


def _json_response(code, obj):
    body = json.dumps(obj).encode()
    return code, [("Content-Type", "application/json"),
                  ("Content-Length", str(len(body)))], body


def _handle(method, path, headers, raw):
    if method == "GET" and path == "/health":
        return _json_response(200, {"ok": True})
    if not _authed(headers):
        return _json_response(401, {"error": "unauthorized"})
    if method == "POST" and path == "/exec":
        try:
            req = json.loads(raw.decode() or "{}")
        except ValueError:
            return _json_response(400, {"error": "bad json"})
        cmd = str(req.get("cmd") or "")
        timeout = int(req.get("timeout") or 600)
        timeout = max(1, min(timeout, 3600))
        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=WORKDIR)
            return _json_response(200, {"rc": proc.returncode,
                                        "out": (proc.stdout or "")[-20000:],
                                        "err": (proc.stderr or "")[-5000:]})
        except subprocess.TimeoutExpired as e:
            return _json_response(200, {"rc": 124, "out": "",
                                        "err": "timeout after %ds" % timeout,
                                        "partial": str((e.stdout or ""))[-2000:]})
        except Exception as e:
            return _json_response(500, {"error": str(e)[:300]})
    if method == "POST" and path == "/write":
        try:
            req = json.loads(raw.decode() or "{}")
            rel = str(req.get("path") or "").lstrip("/")
            if not rel or ".." in rel:
                return _json_response(400, {"error": "bad path"})
            dest = os.path.join(WORKDIR, rel)
            os.makedirs(os.path.dirname(dest) or WORKDIR, exist_ok=True)
            data = base64.b64decode(req.get("b64") or "")
            with open(dest, "wb") as fh:
                fh.write(data)
            return _json_response(200, {"ok": True, "bytes": len(data)})
        except Exception as e:
            return _json_response(500, {"error": str(e)[:300]})
    if method == "POST" and path == "/read":
        try:
            req = json.loads(raw.decode() or "{}")
            rel = str(req.get("path") or "").lstrip("/")
            if not rel or ".." in rel:
                return _json_response(400, {"error": "bad path"})
            with open(os.path.join(WORKDIR, rel), "rb") as fh:
                data = fh.read(25 * 1024 * 1024)
            return _json_response(200, {"b64": base64.b64encode(data).decode(),
                                        "bytes": len(data)})
        except Exception as e:
            return _json_response(500, {"error": str(e)[:300]})
    return _json_response(404, {"error": "unknown endpoint"})


class Handler(BaseHTTPRequestHandler):
    server_version = "kronvex-agent/1"

    def _run(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        code, hs, body = _handle(self.command, self.path.split("?")[0], headers, raw)
        self.send_response(code)
        for k, v in hs:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _run

    def log_message(self, *a):
        pass


def main():
    os.makedirs(WORKDIR, exist_ok=True)
    port = int(os.getenv("PORT", "8080"))
    if not TOKEN:
        raise SystemExit("AGENT_TOKEN is required")
    server = HTTPServer(("0.0.0.0", port), Handler)
    print("agent on :%d workdir=%s" % (port, WORKDIR), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
