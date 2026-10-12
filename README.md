# IntelliProxy – HTTP Proxy Server with Intelligent Caching

A multithreaded HTTP **forward proxy** written in pure Python (standard library only, no `pip install`).
It caches eligible `GET` responses, honours HTTP expiry rules, refreshes expired objects, enforces
configurable blocked-domain policies, tunnels HTTPS with `CONNECT`, and shows everything live on a web
dashboard fed by the proxy's real counters.

> **HTTPS note.** HTTPS requests are *tunnelled* (`CONNECT`): the proxy relays encrypted bytes and cannot
> read or cache them. Only plain-HTTP responses are cached. The dashboard counts HTTPS as "tunnels".

Requirements: Python 3.8+ (macOS: `python3 --version`). Optional: `openssl` (already on macOS) for the HTTPS demo.

---

## 1. Quick start (macOS + VS Code)

```bash
# 1. Open the unzipped folder in VS Code:  File ▸ Open Folder ▸ intelliproxy
#    Open the integrated terminal:  Terminal ▸ New Terminal   (Ctrl+`)

# 2. Start test origin + proxy + dashboard with ONE command
python3 run_demo.py
#    -> proxy on 127.0.0.1:8888, test origin on 127.0.0.1:9000, browser opens the dashboard

# 3. In a SECOND terminal (Terminal ▸ Split Terminal) run the scripted demo
python3 demo_client.py
```

`demo_client.py` sends real requests through the proxy and prints PASS/FAIL for each feature
(caching, expiry, revalidation, uncacheable content, blocking, concurrency, HTTPS tunnel).
Watch the dashboard update while it runs. Stop everything with **Ctrl-C** (a final report is printed).

Run the pieces separately instead (three terminals):

```bash
python3 origin_server.py --https-port 9443          # terminal 1: test origin
python3 proxy.py --port 8888 -v                      # terminal 2: proxy (+ dashboard at http://127.0.0.1:8888/)
curl -x http://127.0.0.1:8888 http://127.0.0.1:9000/hello -i    # terminal 3: a client
```

Run the tests (about 50 seconds):

```bash
python3 -m unittest test_proxy -v
```

If port 8888 is busy: `python3 run_demo.py --proxy-port 8899` (and use that port below), or find the old
process with `lsof -i :8888`.

---

## 2. Demonstration script (what to show the teacher)

Start `python3 run_demo.py`, keep the dashboard visible, and use a second terminal.
`P=http://127.0.0.1:8888` is used below; run `P=http://127.0.0.1:8888` once in that terminal.

| # | Show | Command | Expected |
|---|------|---------|----------|
| 1 | Cache miss then hit | `curl -s -x $P http://127.0.0.1:9000/hello -i` (twice) | `X-Cache: MISS`, then `X-Cache: HIT` with the **same** body ("request #1") |
| 2 | Proof the origin was not contacted | `curl -s http://127.0.0.1:9000/__hits` | `"/hello": 1` even after many hits |
| 3 | Expiry (TTL) | `curl -s -x $P http://127.0.0.1:9000/ttl/3` ; wait 4 s ; repeat | `HIT` while fresh, then `EXPIRED` with a **new** body (full refresh) |
| 4 | Revalidation (HTTP 304) | `curl -s -x $P http://127.0.0.1:9000/etag -i` ; wait 2 s ; repeat | second answer is `X-Cache: REVALIDATED` (origin sent 304, cached body reused) |
| 5 | Not cacheable | `curl -s -x $P http://127.0.0.1:9000/nostore -i` | `X-Cache: UNCACHEABLE` every time |
| 6 | Blocked domain | `curl -s -x $P http://doubleclick.net/ -i` | `403 Forbidden`, `X-Blocked-By: doubleclick.net`, no network access happened |
| 7 | Runtime blocklist edit | Dashboard ▸ **Blocklist** ▸ add `127.0.0.1` ▸ repeat step 1 → `403`; remove the rule → works again | rule is saved in `blocklist.txt` |
| 8 | Concurrency | `python3 demo_client.py` (step 6) or `for i in 1 2 3 4 5 6; do curl -s -x $P "http://127.0.0.1:9000/slow?ms=800" -o /dev/null & done; wait` | all finish in ≈0.8 s (not 6×0.8 s); Request log shows different `Thread-N` names; origin counter for the URL is `1` |
| 9 | HTTPS tunnel | `curl -sk -x $P https://localhost:9443/hello -i` | `200 Connection Established` then the page; dashboard shows a `TUNNEL` row; nothing enters the cache |
| 10 | Statistics report | `curl -s "$P/__proxy/analysis?format=text"` | requests, hits, misses, blocked, latency, hit ratio, recommendations |

**Dashboard tour** (`http://127.0.0.1:8888/`): *Overview* (hit ratio, outcome bar, latency table, request tester,
latency and hit-ratio charts) → *Request log* (every request with outcome, latency and worker thread, filterable) →
*Cache* (stored objects with TTL countdown, remove one / remove expired / clear) → *Blocklist* (add, remove,
check a hostname, per-rule block counts) → *Analysis* (why requests went to the origin, per-domain stats,
most reused / most refetched URLs, tuning recommendations). **Reset statistics** zeroes counters before a demo.
The **Try a request** box sends a real request through the proxy, so a browser is not required.

> **Browser tip.** Browsers deliberately skip the proxy for `localhost`/`127.0.0.1`, so use `curl -x` or the
> dashboard's request tester for the local origin. To use a browser through the proxy, set the macOS Wi-Fi
> HTTP proxy to `127.0.0.1:8888` and browse plain-`http://` sites (HTTPS sites will be tunnelled, not cached).

---

## 3. Architecture

```
                         ┌──────────────────────── IntelliProxy (proxy.py) ─────────────────────────┐
  Client ──TCP──▶        │  ProxyServer (ThreadingHTTPServer)                                       │
  curl / browser         │   accept() ─▶ new worker thread per connection (max 200, else 503)       │
  / dashboard            │        │                                                                 │
                         │        ▼                                                                 │
                         │  ProxyHandler (one per connection, keep-alive)                           │
                         │   ├─ path starts with "/"  ─▶ Admin API + Dashboard (loopback only)     │
                         │   ├─ CONNECT host:443      ─▶ BlockList? ─▶ TCP tunnel (select relay)    │
                         │   └─ GET http://host/path                                                │
                         │        1. BlockList.check(host) ──blocked──▶ 403 (never touches network) │
                         │        2. Cache.get(url)                                                 │
                         │             fresh ───────────────────────────────▶ HIT                  │
                         │             expired + validator ─▶ conditional GET ─▶ 304 ▶ REVALIDATED │
                         │             expired / absent ─▶ origin fetch ─▶ policy() ─▶ store        │
                         │                                                  EXPIRED / MISS         │
                         │             not cacheable (no-store, cookie, POST…) ─▶ stream, BYPASS /  │
                         │                                                        UNCACHEABLE      │
                         │        3. Stats.record() ─▶ counters + request log + latency window      │
                         │                                                                          │
                         │  Shared, lock-protected state:  Cache (LRU, RLock) · Stats (Lock) ·      │
                         │  BlockList (Lock) · in-flight fetch table (request coalescing)           │
                         └────────────────┬─────────────────────────────────────────────────────────┘
                                          │ new TCP connection per origin fetch (http.client)
                                          ▼
                                 Origin web server  (origin_server.py in the demo)
```

Lock order is always **cache → stats**, so threads cannot deadlock. Admin/dashboard calls are *not* counted as
proxy traffic.

### How caching decisions are made
1. Only `GET`/`HEAD` without `Range`, `Authorization` or request `no-store` are looked up (`POST` etc. → `BYPASS`).
2. A response is stored unless it has `no-store`, `private`, `Set-Cookie`, `Vary: *`, an uncacheable status, a
   zero lifetime without validators, or exceeds `--max-object-mb`. Cacheable statuses: 200, 203, 204, 300, 301, 308, 404, 410.
3. Lifetime (TTL), first match wins: `s-maxage` → `max-age` → `Expires` → heuristic (10 % of time since
   `Last-Modified`) → `--default-ttl` (60 s). Capped by `--max-ttl`; `Age` header is subtracted; 404/410 without
   explicit freshness are capped at 30 s. `no-cache` / `must-revalidate` are respected.
4. Fresh entry → **HIT**. Expired entry with `ETag`/`Last-Modified` → conditional request; `304` → **REVALIDATED**;
   otherwise full refetch → **EXPIRED** (refreshed copy stored). Absent → **MISS**.
5. Extras: `Vary` matching, LRU eviction by bytes/entries, request coalescing (N simultaneous misses → 1 origin
   fetch), `stale-if-error` (expired copy served for up to 300 s if the origin fails), client `no-cache` forces a
   refresh, client conditional requests answered with `304` from cache.

### Statistics definitions
| Field | Meaning |
|-------|---------|
| `requests_total` | every proxied request: cache outcomes + blocked + tunnels + errors |
| `hits` | `HIT`: fresh object served from cache (includes clients that waited on another client's in-flight fetch) |
| `misses` | `MISS` (not cached) + `EXPIRED` (cached but expired, refetched) |
| `revalidated` | stale object confirmed by origin `304` (body reused) |
| `blocked` | requests (HTTP and CONNECT) refused by the blocklist |
| `hit_ratio` | (`HIT` + `REVALIDATED` + `STALE`) ÷ cacheable requests (`HIT`, `MISS`, `EXPIRED`, `REVALIDATED`, `STALE`, `UNCACHEABLE`); `POST`, blocked and tunnels are excluded |
| latency "answered from cache" | time to serve true cache hits (average, median, 95th percentile over the last 2000) |
| latency "needed the origin" | `MISS`/`EXPIRED`/`UNCACHEABLE`/`BYPASS` **and** coalesced waiters, because they waited for the origin |

Statistics and the cache live in memory and reset when the proxy restarts.

---

## 4. Files

| File | Purpose |
|------|---------|
| `proxy.py` | The proxy: `Config`, `BlockList` (rules, runtime add/remove, atomic file save), `Cache` (thread-safe LRU), `Stats` (counters, latency percentiles, request log, analysis), `ProxyCore` (TTL policy, revalidation, coalescing), `ProxyServer`/`ProxyHandler` (threads, HTTP, CONNECT), admin API and dashboard serving, `main()` CLI |
| `dashboard.html` | Single-page dashboard (plain HTML/CSS/JS, no external libraries, works offline). Polls `/__proxy/live` every 1.5 s; contains no sample data |
| `origin_server.py` | Local test origin with endpoints for every caching behaviour, hit counters (`/__hits`), optional self-signed HTTPS |
| `run_demo.py` | One command: starts origin + proxy and opens the dashboard |
| `demo_client.py` | Scripted demonstration with PASS/FAIL checks against a running proxy |
| `test_proxy.py` | 57 automated tests (below) |
| `blocklist.txt` | Blocked-domain rules (edited by hand or from the dashboard; hot-reloaded) |

### Blocklist rule syntax (`blocklist.txt`)
```
doubleclick.net              # blocks the domain and every subdomain
*.adservice.example          # glob, subdomains only
re:^track\d+\.example\.org$  # regular expression on the hostname
0.0.0.0 telemetry.example.com  # hosts-file format
!static.doubleclick.net      # allow-list override, always wins
```

### Admin API (localhost only; reads are `GET`, changes are `POST`)
```
GET  /__proxy/live            all dashboard data in one call      GET  /__proxy/stats       summary counters
GET  /__proxy/analysis        full analysis (?format=text)        GET  /__proxy/requests    request log (?since=ID&limit=N)
GET  /__proxy/cache           cached objects (?limit&sort)        GET  /__proxy/blocklist   rules (?check=host)
POST /__proxy/blocklist/add   {"rule": "ads.example.com"}        POST /__proxy/blocklist/remove  {"rule": "..."}
POST /__proxy/purge           ?all=1 | ?expired=1 | ?url= | ?domain=    POST /__proxy/reload       re-read blocklist file
POST /__proxy/stats/reset     zero counters + log                 POST /__proxy/fetch  {"url": "http://…", "no_cache": false}
```
Example: `curl -X POST "http://127.0.0.1:8888/__proxy/purge?all=1"`.

### Command-line options
`--port 8888  --host 127.0.0.1  --blocklist FILE  --block RULE  --cache-mb 64  --max-entries 10000  --max-object-mb 5
--default-ttl 60  --min-ttl 0  --max-ttl 86400  --negative-ttl 30  --stale-if-error 300  --max-threads 200
--timeout 15  --report-interval N  --log-file FILE  --admin-remote  -v`

---

## 5. Tests (`python3 -m unittest test_proxy -v`)

All tests run end-to-end: a real origin server, a real proxy and real sockets on random local ports.

| Class (tests) | What is verified |
|---------------|------------------|
| `CachingTests` (14) | miss→hit, origin hit once, expiry refetch, 304 revalidation, no-store/cookie not cached, default & heuristic TTL, POST bypass, client `no-cache`, client conditional → 304, `Vary`, HEAD from cache, negative caching, stale-if-error, request coalescing, keep-alive |
| `ExpiryTests` (6) | `Expires` header lifetime, TTL countdown and fresh/expired state, expired entry refreshed with new body, purge-expired, TTL policy rules (`s-maxage`, `Age`, `no-store`, `private`, cookies, status, `no-cache`, `must-revalidate`), sweeper evicts expired entries |
| `ConcurrencyTests` (6) | 24 clients × 10 URLs ⇒ exactly 10 origin fetches and exact counters; slow requests run in parallel on different threads; coalesced waiters count as hits but not as cache latency; purge during load keeps byte accounting exact; `Cache` stress test from 12 threads; connection limit returns `503` |
| `BlockingTests` (5), `BlocklistApiTests` (7) | exact/subdomain/glob/regex/allow rules, HTTP `403`, CONNECT blocked, hot reload; runtime add/remove persisted to file (comments kept), blocked before any origin contact, duplicate/invalid input rejected, URL reduced to hostname, command-line rules protected, per-rule counts, manual file edits picked up |
| `HttpsTunnelTests` (2) | real TLS handshake and requests **through** `CONNECT` reach the origin, no `X-Cache`, nothing cached; dead target → `502` |
| `StatisticsTests` (2), `AnalysisTests` (3) | counters match known traffic, hit ratio, latency (cache faster than origin), JSON/text analysis, purge, adaptive TTL |
| `DashboardTests` (11) | page served, dashboard only calls endpoints that exist and has no fake data, live feed fields, admin calls not counted, changes require `POST`, cross-site `Origin` and DNS-rebinding `Host` refused, stats reset, purge by URL/domain, request tester goes through the real cache, bad query values don't crash, block page escapes the hostname |
| `SmallCacheTests` (1) | LRU eviction and per-object size limit |

---

## 6. Limitations (be upfront in the viva)

- HTTPS is tunnelled only: no TLS interception, so HTTPS content is never cached or inspected.
- The cache and statistics are in memory (lost on restart). One process; thread-per-connection (fine for a lab, not for 10 000 clients).
- `Range`, `Authorization` and non-`GET` requests bypass the cache; `Content-Encoding` variants rely on the origin sending `Vary`.
- Admin API and dashboard are for localhost; `--admin-remote` opens them to the network and should not be used on untrusted networks.

---

## 7. Viva questions (20)

1. **What is a forward proxy and how does it differ from a reverse proxy?** A forward proxy acts for clients: the client is configured to send absolute-URI requests (`GET http://host/path`) to it. A reverse proxy acts for servers and clients don't know it exists.
2. **Why TCP, and how does your server accept clients?** HTTP needs reliable ordered streams. `ThreadingHTTPServer` listens on a TCP socket, `accept()`s each connection and hands it to a new thread.
3. **How is multithreading implemented, and how do you stop thread explosion?** One worker thread per connection (keep-alive, so several requests per thread); a bounded semaphore caps it at 200 and extra connections get `503`.
4. **How is the cache thread-safe?** One `RLock` guards the `OrderedDict` and the byte counter; all reads, writes, evictions and purges go through it. A test hammers it from 12 threads and checks byte accounting.
5. **How do you avoid deadlock?** Fixed lock order (cache → stats), no lock is held during network I/O, and request coalescing uses events, not nested locks.
6. **What is request coalescing?** When many clients miss on the same URL, one thread fetches and the others wait for it and then read the cached copy; the origin is hit once (verified by the origin counter).
7. **What is the cache key?** The absolute URL, plus a match on the request headers named in the response's `Vary`.
8. **Which responses do you cache?** `GET`/`HEAD` with cacheable status codes, no `no-store`/`private`/`Set-Cookie`/`Vary: *`, non-zero lifetime, size ≤ 5 MB.
9. **How is TTL determined?** `s-maxage` → `max-age` → `Expires` → 10 % of `Last-Modified` age → default 60 s, capped by max TTL, minus the `Age` header.
10. **What happens when an entry expires?** If it has `ETag`/`Last-Modified` the proxy sends a conditional request: `304` → `REVALIDATED` (cheap), `200` → `EXPIRED` (new copy stored). Without validators it refetches fully.
11. **What do `no-cache` and `no-store` mean?** `no-store`: never keep it. `no-cache`: may keep it but must revalidate before every reuse.
12. **What is stale-if-error?** If the origin fails or returns 5xx, an expired copy (up to 300 s old, unless `must-revalidate`) is served with a `Warning` header.
13. **How does eviction work?** LRU: on each insert, expired entries are dropped first, then least-recently-used ones until under the byte and entry limits.
14. **How is the hit ratio computed?** (HIT + REVALIDATED + STALE) ÷ cacheable lookups. Blocked requests, tunnels and POSTs are excluded because they could never be hits.
15. **How do blocked-domain policies work?** Rules (domain+subdomains, glob, regex, `!allow`) are checked on the hostname before any DNS lookup or connection, for both HTTP and CONNECT; the rule file hot-reloads and the dashboard edits it atomically.
16. **How does HTTPS work through your proxy?** The client sends `CONNECT host:443`; the proxy opens a TCP connection to the host, replies `200`, and relays bytes both ways with `select()`. TLS is end-to-end, so the proxy can't read or cache it.
17. **Why not cache HTTPS?** It would require a man-in-the-middle certificate, which breaks the security model and needs the client to trust the proxy's CA. This project deliberately doesn't do that.
18. **Why must hop-by-hop headers be removed?** `Connection`, `Keep-Alive`, `Transfer-Encoding` etc. apply to one TCP hop only; forwarding them would corrupt framing. The proxy strips them and sets its own `Content-Length` and `Via`.
19. **How do you secure the admin API?** Loopback clients only, `Host` header must be local (anti DNS-rebinding), changes need `POST`, and a cross-site `Origin` is refused (anti CSRF).
20. **How would you scale or improve this?** Use an event loop (`asyncio`/`epoll`) or a worker pool instead of thread-per-connection, add a persistent/disk cache, shared cache across processes, and optional TLS interception with explicit client consent.
