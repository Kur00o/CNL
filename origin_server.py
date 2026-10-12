#!/usr/bin/env python3
"""
Local test origin server for IntelliProxy demonstrations and tests.

It plays the part of "the internet": every endpoint sends different caching headers so each
proxy behaviour can be shown on demand, and it counts how often it is really contacted, so a
cache HIT can be proven by watching /__hits NOT increase.

    python3 origin_server.py                       # http://127.0.0.1:9000
    python3 origin_server.py --https-port 9443     # also https://localhost:9443 (self-signed)

Endpoint                 What it sends                              Demonstrates
  /hello                  Cache-Control: max-age=60                  MISS then HIT
  /time                   max-age=5, body contains the time          expiry after 5 s
  /ttl/<n>                max-age=<n> seconds, unique body each time expiry -> EXPIRED (full refetch)
  /etag                   ETag + max-age=1, answers 304 to If-None-Match   REVALIDATED
  /expires                Expires: <now+4s> (no Cache-Control)       HTTP Expires header
  /nostore                Cache-Control: no-store                    UNCACHEABLE
  /cookie                 Set-Cookie + max-age=60                    UNCACHEABLE (private data)
  /default                no freshness headers at all                proxy default TTL
  /lm                     Last-Modified only                         heuristic TTL
  /slow?ms=800            max-age=60, waits <ms> first               latency + request coalescing
  /vary                   Vary: Accept-Language                      separate cache entries
  /page.html              small HTML page, max-age=30                something to open in a browser
  /big?kb=100             N kilobytes, max-age=60                    byte hit ratio
  /obj/<n>                1 KB object, max-age=60                    bulk traffic / LRU eviction
  POST /echo              echoes the body                            BYPASS (POST is never cached)
  /__hits                 JSON: how many times each path reached the origin
  /__reset                clear the counters
  /__control/flaky?fail=1 make /flaky return 500 (fail=0 to recover)   stale-if-error
  anything else           404 with max-age=60                        negative caching
"""
import argparse
import json
import os
import ssl
import subprocess
import sys
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
HITS = {}
STATE = {"flaky_fail": False}
LOCK = threading.Lock()
LM = "Wed, 01 Jan 2020 00:00:00 GMT"
PAGE = """<!doctype html><meta charset=utf-8><title>IntelliProxy test page</title>
<body style="font:16px system-ui;max-width:40rem;margin:3rem auto">
<h1>Test origin page</h1><p>Served at %s by the local origin server (request #%d for this URL).</p>
<p>Reload: if this number does not change, the proxy answered from its cache.</p></body>"""


class OriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TestOrigin/1.0"

    def log_message(self, *a):
        pass

    def _reply(self, status=200, body=b"", headers=(), ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        if status != 304:
            self.send_header("Content-Type", ctype)
        for k, v in headers:
            self.send_header(k, v)
        if status != 304:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if status != 304 and self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlsplit(self.path)
        p, q = u.path, parse_qs(u.query)
        if p == "/__hits":
            with LOCK:
                return self._reply(body=json.dumps(HITS, indent=2), ctype="application/json")
        if p == "/__reset":
            with LOCK:
                HITS.clear()
            return self._reply(body="reset\n")
        if p == "/__control/flaky":
            STATE["flaky_fail"] = q.get("fail", ["0"])[0] == "1"
            return self._reply(body=f"flaky_fail={STATE['flaky_fail']}\n")
        with LOCK:                              # count by full path incl. query string
            HITS[self.path] = n = HITS.get(self.path, 0) + 1
        stamp = time.strftime("%H:%M:%S")
        cc = lambda v: [("Cache-Control", v)]
        if p == "/hello":
            self._reply(body=f"hello from origin (request #{n})\n", headers=cc("max-age=60"))
        elif p == "/time":
            self._reply(body=f"origin time {stamp} (request #{n})\n", headers=cc("max-age=5"))
        elif p == "/cacheable":
            self._reply(body=f"v{n}", headers=cc("max-age=1"))
        elif p == "/long":
            self._reply(body="long", headers=cc("max-age=300"))
        elif p.startswith("/ttl/"):
            try:
                secs = int(p.split("/")[2])
            except ValueError:
                return self._reply(400, "bad ttl\n")
            self._reply(body=f"ttl={secs}s generated {stamp} (request #{n})\n", headers=cc(f"max-age={secs}"))
        elif p == "/etag":
            h = [("ETag", '"abc"'), ("Cache-Control", "max-age=1")]
            if self.headers.get("If-None-Match") == '"abc"':
                self._reply(304, headers=h)
            else:
                self._reply(body="etag-body", headers=h)
        elif p == "/expires":
            secs = int(q.get("s", ["4"])[0]) if q.get("s", ["4"])[0].isdigit() else 4
            self._reply(body=f"expires-demo #{n}\n", headers=[("Expires", formatdate(time.time() + secs, usegmt=True))])
        elif p == "/nostore":
            self._reply(body="no-store\n", headers=cc("no-store"))
        elif p == "/cookie":
            self._reply(body="session page\n", headers=[("Set-Cookie", "sid=1; HttpOnly"), ("Cache-Control", "max-age=60")])
        elif p == "/default":
            self._reply(body="default")
        elif p == "/lm":
            self._reply(body="lm", headers=[("Last-Modified", LM)])
        elif p == "/slow":
            ms = int(q.get("ms", ["400"])[0]) if q.get("ms", ["400"])[0].isdigit() else 400
            time.sleep(min(ms, 5000) / 1000)
            self._reply(body="slow", headers=cc("max-age=60"))
        elif p == "/flaky":
            if STATE["flaky_fail"]:
                self._reply(500, "boom")
            else:
                self._reply(body="ok", headers=cc("max-age=1"))
        elif p == "/big":
            kb = q.get("kb", [""])[0]
            size = min(int(kb), 4096) * 1024 if kb.isdigit() else 5000
            self._reply(body=b"B" * size, headers=cc("max-age=60"), ctype="application/octet-stream")
        elif p == "/vary":
            lang = self.headers.get("Accept-Language", "en")
            self._reply(body=lang, headers=[("Cache-Control", "max-age=60"), ("Vary", "Accept-Language")])
        elif p == "/page.html":
            self._reply(body=PAGE % (stamp, n), headers=cc("max-age=30"), ctype="text/html; charset=utf-8")
        elif p.startswith("/obj/"):
            self._reply(body=b"o" * 1000, headers=cc("max-age=60"))
        else:
            self._reply(404, "not found\n", headers=cc("max-age=60"))

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with LOCK:
            HITS["POST"] = HITS.get("POST", 0) + 1
        self._reply(body=b"echo:" + data)


def make_cert(directory):
    """Create a throw-away self-signed certificate with the openssl CLI (None if unavailable)."""
    os.makedirs(directory, exist_ok=True)
    cert, key = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
                            "-keyout", key, "-out", cert, "-subj", "/CN=localhost"],
                           check=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
    return cert, key


def start_origin(port=9000, https_port=0, host="127.0.0.1", certdir=os.path.join(HERE, ".certs")):
    """Start the origin on background threads. Returns (http_server, https_server_or_None)."""
    http_srv = ThreadingHTTPServer((host, port), OriginHandler)
    http_srv.daemon_threads = True
    threading.Thread(target=http_srv.serve_forever, daemon=True, name="origin-http").start()
    https_srv = None
    if https_port:
        pair = make_cert(certdir)
        if pair:
            https_srv = ThreadingHTTPServer((host, https_port), OriginHandler)
            https_srv.daemon_threads = True
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*pair)
            https_srv.socket = ctx.wrap_socket(https_srv.socket, server_side=True)
            threading.Thread(target=https_srv.serve_forever, daemon=True, name="origin-https").start()
        else:
            print("openssl not available: HTTPS origin disabled", file=sys.stderr)
    return http_srv, https_srv


def main():
    ap = argparse.ArgumentParser(description="Local test origin server")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--https-port", type=int, default=0, help="also serve HTTPS (self-signed) on this port")
    a = ap.parse_args()
    http_srv, https_srv = start_origin(a.port, a.https_port)
    print(f"Test origin:  http://127.0.0.1:{a.port}/   (counters: /__hits)")
    if https_srv:
        print(f"              https://localhost:{a.https_port}/  (self-signed certificate)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
