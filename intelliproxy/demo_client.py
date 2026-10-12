#!/usr/bin/env python3
"""
Scripted demonstration for a running proxy (start it first with:  python3 run_demo.py).

Sends real requests through the proxy to the local test origin and checks every result, so you
can show the teacher each feature in about 15 seconds:  python3 demo_client.py
Exit code is 0 only if every check passes.
"""
import argparse
import http.client
import json
import socket
import ssl
import sys
import threading
import time

ap = argparse.ArgumentParser(description="Scripted IntelliProxy demonstration")
ap.add_argument("--proxy", default="127.0.0.1:8888")
ap.add_argument("--origin", default="127.0.0.1:9000")
ap.add_argument("--https-port", type=int, default=9443)
ap.add_argument("--blocked-host", default="doubleclick.net", help="a host that is in blocklist.txt")
args = ap.parse_args()
PH, PP = args.proxy.split(":"); PP = int(PP)
OH, OP = args.origin.split(":"); OP = int(OP)
ORIGIN = f"http://{args.origin}"
failures = 0


def call(host, port, method, url, body=None, headers=None):
    c = http.client.HTTPConnection(host, port, timeout=20)
    t0 = time.monotonic()
    c.request(method, url, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    ms = (time.monotonic() - t0) * 1000
    c.close()
    return r, data, ms


def through_proxy(method, url, **kw):
    return call(PH, PP, method, url, **kw)


def admin(method, path, payload=None):
    r, data, _ = call(PH, PP, method, path, body=json.dumps(payload) if payload is not None else None)
    return json.loads(data)


def check(label, ok, detail=""):
    global failures
    failures += (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<44} {detail}")


def step(title):
    print(f"\n== {title}")


def expect(path, outcome, method="GET", status=200, **kw):
    r, data, ms = through_proxy(method, ORIGIN + path, **kw)
    got = r.getheader("X-Cache")
    check(f"{method} {path}", got == outcome and r.status == status,
          f"-> {r.status} {got}  {ms:.1f} ms  (expected {outcome})")
    return r, data


try:
    admin("GET", "/__proxy/health")
    call(OH, OP, "GET", "/__hits")
except OSError as exc:
    sys.exit(f"Cannot reach the proxy ({args.proxy}) or origin ({args.origin}): {exc}\nStart them with: python3 run_demo.py")

admin("POST", "/__proxy/purge", {"all": 1})
admin("POST", "/__proxy/stats/reset", {})
call(OH, OP, "GET", "/__reset")

step("1. Caching: first request misses, repeats are served from the cache")
expect("/hello", "MISS"); expect("/hello", "HIT"); expect("/hello", "HIT")
hits = json.loads(call(OH, OP, "GET", "/__hits")[1])
check("origin contacted only once for /hello", hits.get("/hello") == 1, f"origin counter = {hits.get('/hello')}")

step("2. Expiry: a 2-second TTL runs out and the object is refreshed")
expect("/ttl/2", "MISS"); expect("/ttl/2", "HIT")
time.sleep(2.3)
expect("/ttl/2", "EXPIRED")

step("3. Revalidation: expired object with an ETag is confirmed with a 304 (no body re-sent)")
expect("/etag", "MISS")
time.sleep(1.3)
expect("/etag", "REVALIDATED")

step("4. Objects that must not be cached")
expect("/nostore", "UNCACHEABLE"); expect("/cookie", "UNCACHEABLE")
expect("/echo", "BYPASS", method="POST", body=b"data")

step("5. Blocked-domain policy (checked before any network access)")
r, _, _ = through_proxy("GET", f"http://{args.blocked_host}/")
check(f"GET http://{args.blocked_host}/", r.status == 403, f"-> {r.status}  rule: {r.getheader('X-Blocked-By')}")
admin("POST", "/__proxy/blocklist/add", {"rule": "live-demo.example"})
r, _, _ = through_proxy("GET", "http://www.live-demo.example/")
check("rule added at runtime blocks subdomains", r.status == 403, f"-> {r.status}")
admin("POST", "/__proxy/blocklist/remove", {"rule": "live-demo.example"})

step("6. Concurrency: 10 simultaneous clients ask for the same slow URL")
codes = []
ts = [threading.Thread(target=lambda: codes.append(through_proxy("GET", ORIGIN + "/slow?ms=600")[0].status)) for _ in range(10)]
t0 = time.monotonic(); [t.start() for t in ts]; [t.join() for t in ts]
took = time.monotonic() - t0
hits = json.loads(call(OH, OP, "GET", "/__hits")[1])
check("all 10 answered 200", codes == [200] * 10, f"in {took:.2f} s")
check("origin fetched only once (coalescing)", hits.get("/slow?ms=600") == 1, f"origin counter = {hits.get('/slow?ms=600')}")

step("7. HTTPS CONNECT tunnel (encrypted, passes through, never cached)")
try:
    raw = socket.create_connection((PH, PP), timeout=5)
    raw.sendall(f"CONNECT localhost:{args.https_port} HTTP/1.1\r\nHost: localhost:{args.https_port}\r\n\r\n".encode())
    status_line = raw.recv(4096).split(b"\r\n")[0]
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPConnection("localhost"); conn.sock = ctx.wrap_socket(raw, server_hostname="localhost")
    conn.request("GET", "/hello"); r = conn.getresponse(); body = r.read(); conn.close()
    check("CONNECT + TLS handshake + GET /hello", b" 200 " in status_line and r.status == 200 and r.getheader("X-Cache") is None,
          f"-> {r.status}, no X-Cache header (proxy cannot read encrypted data)")
except OSError as exc:
    print(f"  SKIP  HTTPS origin not reachable on port {args.https_port} ({exc})")

step("Result")
s = admin("GET", "/__proxy/stats")
print(f"  requests={s['requests_total']}  hits={s['hits']}  misses={s['misses']}  revalidated={s['revalidated']}  "
      f"uncacheable={s['uncacheable']}  bypass={s['bypass']}  blocked={s['blocked']}  tunnels={s['tunnels']}")
print(f"  cache hit ratio={s['hit_ratio']:.0%}   avg latency: cache {s['latency']['cache']['avg']} ms  vs  origin {s['latency']['origin']['avg']} ms")
print("  Open http://%s:%d/ to see the same data in the dashboard." % (PH, PP))
print("\nALL CHECKS PASSED" if not failures else f"\n{failures} CHECK(S) FAILED")
sys.exit(1 if failures else 0)
