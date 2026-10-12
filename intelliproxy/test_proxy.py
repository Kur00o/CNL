#!/usr/bin/env python3
"""End-to-end tests: local origin server <-> proxy <-> test client."""
import http.client
import json
import os
import socket
import ssl
import tempfile
import threading
import time
import unittest

import origin_server as og
import proxy

HITS, STATE = og.HITS, og.STATE      # origin-side counters (ground truth: did the origin get hit?)


def start(cfg_kwargs=None, blocked=()):
    kw = dict(port=0, blocked=list(blocked))
    kw.update(cfg_kwargs or {})
    server, core = proxy.build_server(proxy.Config(**kw))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, core


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.origin, _ = og.start_origin(port=0)
        cls.base = f"http://127.0.0.1:{cls.origin.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.origin.shutdown()
        cls.origin.server_close()

    def setUp(self):
        HITS.clear()
        STATE["flaky_fail"] = False
        self.server, self.core = start(self.cfg(), self.blocked())
        self.pport = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def cfg(self):
        return {}

    def blocked(self):
        return ()

    def admin(self, method, path, payload=None, headers=None):
        """Talk to the admin API directly (origin-form request). Returns (response, parsed_json_or_text)."""
        c = http.client.HTTPConnection("127.0.0.1", self.pport, timeout=10)
        body = json.dumps(payload) if payload is not None else None
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            return r, json.loads(data)
        except ValueError:
            return r, data.decode("utf-8", "replace")

    def settle(self, n, timeout=5):
        """Wait until the proxy has logged n requests (it records a request right AFTER sending
        the response, so a very fast client could otherwise read the statistics a moment early)."""
        end = time.time() + timeout
        while self.core.stats.seq < n and time.time() < end:
            time.sleep(0.002)

    def get(self, path, method="GET", headers=None, body=None, absolute=None, track=True):
        before = self.core.stats.seq
        c = http.client.HTTPConnection("127.0.0.1", self.pport, timeout=10)
        c.request(method, absolute or (self.base + path), body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        if track:
            self.settle(before + 1, timeout=0.5)
        return r, data


class CachingTests(Base):
    def test_miss_then_hit(self):
        r1, b1 = self.get("/long")
        r2, b2 = self.get("/long")
        self.assertEqual(r1.getheader("X-Cache"), "MISS")
        self.assertEqual(r2.getheader("X-Cache"), "HIT")
        self.assertEqual(b1, b2)
        self.assertEqual(HITS["/long"], 1)
        self.assertIsNotNone(r2.getheader("Age"))

    def test_expiry_refetch(self):
        _, b1 = self.get("/cacheable")
        self.assertEqual(self.get("/cacheable")[1], b1)
        time.sleep(1.3)
        r, b3 = self.get("/cacheable")
        self.assertEqual(r.getheader("X-Cache"), "EXPIRED")
        self.assertNotEqual(b3, b1)
        self.assertEqual(HITS["/cacheable"], 2)

    def test_revalidation_304(self):
        self.get("/etag")
        time.sleep(1.3)
        r, b = self.get("/etag")
        self.assertEqual(r.getheader("X-Cache"), "REVALIDATED")
        self.assertEqual(b, b"etag-body")
        self.assertEqual(HITS["/etag"], 2)
        r, _ = self.get("/etag")                     # fresh again
        self.assertEqual(r.getheader("X-Cache"), "HIT")

    def test_no_store_and_cookie_not_cached(self):
        for path in ("/nostore", "/cookie"):
            self.assertEqual(self.get(path)[0].getheader("X-Cache"), "UNCACHEABLE")
            self.get(path)
            self.assertEqual(HITS[path], 2)

    def test_default_ttl_and_heuristic(self):
        self.get("/default")
        self.assertEqual(self.get("/default")[0].getheader("X-Cache"), "HIT")
        self.get("/lm")
        r, _ = self.get("/lm")
        self.assertEqual(r.getheader("X-Cache"), "HIT")
        ent = self.core.cache.entries()
        src = {e["url"].split("/")[-1]: e["ttl_source"] for e in ent}
        self.assertEqual(src["default"], "default")
        self.assertEqual(src["lm"], "heuristic")

    def test_post_bypasses_cache(self):
        r, b = self.get("/echo", "POST", body=b"hello")
        self.assertEqual(b, b"echo:hello")
        self.assertEqual(r.getheader("X-Cache"), "BYPASS")

    def test_client_no_cache_forces_refresh(self):
        self.get("/long")
        r, _ = self.get("/long", headers={"Cache-Control": "no-cache"})
        self.assertNotEqual(r.getheader("X-Cache"), "HIT")
        self.assertEqual(HITS["/long"], 2)
        self.assertEqual(self.get("/long")[0].getheader("X-Cache"), "HIT")   # cache refreshed

    def test_client_conditional_answered_from_cache(self):
        self.get("/etag")
        r, b = self.get("/etag", headers={"If-None-Match": '"abc"'})
        self.assertEqual(r.status, 304)
        self.assertEqual(b, b"")
        self.assertEqual(HITS["/etag"], 1)

    def test_vary(self):
        _, en = self.get("/vary", headers={"Accept-Language": "en"})
        _, fr = self.get("/vary", headers={"Accept-Language": "fr"})
        self.assertEqual((en, fr), (b"en", b"fr"))
        r, _ = self.get("/vary", headers={"Accept-Language": "fr"})
        self.assertEqual(r.getheader("X-Cache"), "HIT")

    def test_head_served_from_cache(self):
        self.get("/long")
        r, b = self.get("/long", "HEAD")
        self.assertEqual(r.getheader("X-Cache"), "HIT")
        self.assertEqual(r.getheader("Content-Length"), "4")
        self.assertEqual(b, b"")

    def test_negative_caching(self):
        self.get("/missing")
        r, _ = self.get("/missing")
        self.assertEqual((r.status, r.getheader("X-Cache")), (404, "HIT"))

    def test_stale_if_error(self):
        self.get("/flaky")
        time.sleep(1.3)
        STATE["flaky_fail"] = True
        r, b = self.get("/flaky")
        self.assertEqual((r.status, r.getheader("X-Cache"), b), (200, "STALE", b"ok"))

    def test_coalescing(self):
        results, barrier = [], threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(self.get("/slow")[1])

        ts = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results, [b"slow"] * 8)
        self.assertEqual(HITS["/slow"], 1)
        self.assertGreaterEqual(self.core.stats.summary()["coalesced_requests"], 1)

    def test_keepalive_multiple_requests(self):
        c = http.client.HTTPConnection("127.0.0.1", self.pport, timeout=10)
        for _ in range(3):
            c.request("GET", self.base + "/long")
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 200)
        c.close()


class SmallCacheTests(Base):
    def cfg(self):
        return {"cache_bytes": 4000, "max_object": 3000}

    def test_lru_eviction_and_size_limit(self):
        for i in range(6):
            self.get(f"/obj/{i}")
        snap = self.core.cache.snapshot()
        self.assertLessEqual(snap["bytes"], 4000)
        self.assertLess(snap["entries"], 6)
        self.assertGreater(self.core.stats.summary()["evictions"].get("lru", 0), 0)
        self.assertEqual(self.get("/obj/0")[0].getheader("X-Cache"), "MISS")   # evicted
        r, _ = self.get("/big")                                                # 5000 > max_object
        self.assertEqual(r.getheader("X-Cache"), "UNCACHEABLE")
        r, b = self.get("/big")
        self.assertEqual(len(b), 5000)
        self.assertEqual(HITS["/big"], 2)


class BlockingTests(Base):
    def blocked(self):
        return ["blocked.test", "*.glob.test", r"re:^ads\d+\.test$", "!ok.blocked.test"]

    def test_block_rules(self):
        bl = self.core.blocklist
        for h in ("blocked.test", "a.b.blocked.test", "x.glob.test", "ads12.test"):
            self.assertIsNotNone(bl.check(h), h)
        for h in ("glob.test", "ads.test", "ok.blocked.test", "example.com"):
            self.assertIsNone(bl.check(h), h)

    def test_http_blocked_403(self):
        r, b = self.get("", absolute="http://sub.blocked.test/page")
        self.assertEqual(r.status, 403)
        self.assertEqual(r.getheader("X-Blocked-By"), "blocked.test")
        self.assertIn(b"blocked", b.lower())
        self.assertEqual(self.core.stats.summary()["blocked"], 1)

    def test_connect_blocked(self):
        s = socket.create_connection(("127.0.0.1", self.pport), timeout=5)
        s.sendall(b"CONNECT blocked.test:443 HTTP/1.1\r\nHost: blocked.test:443\r\n\r\n")
        self.assertIn(b" 403 ", s.recv(4096).split(b"\r\n")[0])
        s.close()

    def test_connect_tunnel_works(self):
        port = self.origin.server_address[1]
        s = socket.create_connection(("127.0.0.1", self.pport), timeout=5)
        s.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.assertIn(b" 200 ", s.recv(4096).split(b"\r\n")[0])
        s.sendall(b"GET /long HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        data = b""
        while chunk := s.recv(4096):
            data += chunk
        self.assertTrue(data.endswith(b"long"))
        s.close()

    def test_hot_reload_from_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("first.test\n")
        bl = proxy.BlockList(fh.name)
        self.assertIsNotNone(bl.check("first.test"))
        with open(fh.name, "w") as f:
            f.write("0.0.0.0 second.test\n")
        os.utime(fh.name, (time.time() + 5, time.time() + 5))
        bl.maybe_reload()
        self.assertIsNone(bl.check("first.test"))
        self.assertIsNotNone(bl.check("second.test"))
        os.unlink(fh.name)


class AnalysisTests(Base):
    def test_stats_and_admin(self):
        for _ in range(5):
            self.get("/long")
        self.get("/nostore")
        self.get("", absolute="http://blocked.test/")
        r, b = self.get("/__proxy/analysis", absolute=f"http://127.0.0.1:{self.pport}/__proxy/analysis", track=False)
        a = json.loads(b)
        s = a["summary"]
        self.assertEqual(s["outcomes"]["HIT"], 4)
        self.assertEqual(s["outcomes"]["MISS"], 1)
        self.assertAlmostEqual(s["hit_ratio"], 4 / 6, places=3)
        self.assertGreater(s["byte_hit_ratio"], 0.5)
        self.assertEqual(a["cache"]["entries"], 1)
        self.assertTrue(any(m["reason"] == "cold" for m in a["miss_reasons"]))
        self.assertTrue(any(m["reason"] == "uncacheable:no-store" for m in a["miss_reasons"]))
        self.assertEqual(a["top_cached_urls"][0]["cache_hits"], 4)
        self.assertTrue(a["recommendations"])
        r, b = self.get("", absolute=f"http://127.0.0.1:{self.pport}/__proxy/analysis?format=text", track=False)
        self.assertIn(b"CACHE ANALYSIS REPORT", b)

    def test_purge(self):
        self.get("/long")
        _, b = self.admin("POST", "/__proxy/purge?all=1")
        self.assertEqual(b["purged"], 1)
        self.assertEqual(self.get("/long")[0].getheader("X-Cache"), "MISS")

    def test_adaptive_ttl_grows_when_unchanged(self):
        self.core.cfg.default_ttl = 1
        self.get("/default")
        time.sleep(1.2)
        r, _ = self.get("/default")           # same body -> ttl doubles
        self.assertEqual(r.getheader("X-Cache"), "EXPIRED")
        self.assertGreaterEqual(self.core.cache.entries()[0]["ttl"], 2)


# --------------------------------------------------------------------------- expiry
class ExpiryTests(Base):
    def test_expires_header_controls_lifetime(self):
        self.get("/expires?s=1")
        self.assertEqual(self.get("/expires?s=1")[0].getheader("X-Cache"), "HIT")
        time.sleep(1.4)
        self.assertEqual(self.get("/expires?s=1")[0].getheader("X-Cache"), "EXPIRED")
        self.assertEqual(HITS["/expires?s=1"], 2)

    def test_ttl_remaining_counts_down_and_listing_marks_state(self):
        self.get("/cacheable")                              # max-age=1
        r, _ = self.get("/cacheable")
        self.assertLessEqual(int(r.getheader("X-Cache-TTL-Remaining")), 1)
        self.assertEqual(self.core.cache.entries()[0]["state"], "fresh")
        time.sleep(1.3)
        self.assertEqual(self.core.cache.entries()[0]["state"], "expired")

    def test_expired_entry_is_refreshed_and_new_body_served(self):
        _, first = self.get("/cacheable")
        time.sleep(1.3)
        r, second = self.get("/cacheable")
        self.assertEqual(r.getheader("X-Cache"), "EXPIRED")
        self.assertNotEqual(first, second)                  # origin generated a new version
        r, third = self.get("/cacheable")
        self.assertEqual((r.getheader("X-Cache"), third), ("HIT", second))   # refreshed copy is cached

    def test_purge_expired_only_removes_expired(self):
        self.get("/cacheable")                              # 1 s
        self.get("/long")                                   # 300 s
        time.sleep(1.3)
        _, out = self.admin("POST", "/__proxy/purge?expired=1")
        self.assertEqual(out["purged"], 1)
        self.assertEqual([e["url"].split("/")[-1] for e in self.core.cache.entries()], ["long"])

    def test_policy_rules(self):
        pol = lambda status=200, **h: self.core.policy(status, {k.replace("_", "-"): v for k, v in h.items()}, time.time())
        p, _ = pol(cache_control="s-maxage=100, max-age=5")
        self.assertEqual((p.ttl, p.source), (100, "explicit"))          # shared-cache directive wins
        p, _ = pol(cache_control="max-age=30", age="10")
        self.assertEqual((p.ttl, p.initial_age), (30, 10))              # Age header respected
        p, why = pol(cache_control="no-store")
        self.assertEqual((p, why), (None, "no-store"))
        self.assertEqual(pol(cache_control="private, max-age=60")[1], "private")
        self.assertEqual(pol(set_cookie="a=b", cache_control="max-age=60")[1], "set-cookie")
        self.assertEqual(pol(500, cache_control="max-age=60")[1], "status-500")
        self.assertEqual(pol(cache_control="max-age=0")[1], "zero-ttl")
        p, _ = pol(cache_control="no-cache", etag='"x"')
        self.assertEqual((p.ttl, p.no_stale), (0.0, True))              # store but always revalidate
        p, _ = pol(cache_control="max-age=60, must-revalidate")
        self.assertTrue(p.no_stale)                                     # never served stale on error

    def test_sweeper_drops_expired_entries(self):
        self.core.cache.stale_window = 0
        self.core.cache.retention = 0
        self.get("/cacheable")
        time.sleep(1.2)
        self.assertEqual(self.core.cache.sweep(), 1)
        self.assertEqual(self.core.cache.snapshot()["entries"], 0)
        self.assertEqual(self.core.stats.summary()["evictions"]["expired"], 1)


# --------------------------------------------------------------------------- concurrency
class ConcurrencyTests(Base):
    def run_threads(self, n, fn):
        errors, barrier = [], threading.Barrier(n)

        def wrap(i):
            try:
                barrier.wait()
                fn(i)
            except Exception as exc:                        # surfaced in the assertion below
                errors.append(repr(exc))
        ts = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(errors, [])

    def test_many_clients_same_urls_one_origin_fetch_each(self):
        """24 clients x 10 URLs: exactly 10 origin fetches, all other requests are hits."""
        def worker(_):
            for k in range(10):
                r, b = self.get(f"/obj/{k}")
                assert r.status == 200 and len(b) == 1000
        self.run_threads(24, worker)
        self.settle(240)
        self.assertEqual({p: n for p, n in HITS.items()}, {f"/obj/{k}": 1 for k in range(10)})
        s = self.core.stats.summary()
        self.assertEqual((s["requests_total"], s["misses"], s["hits"]), (240, 10, 230))
        self.assertEqual(self.core.cache.snapshot()["entries"], 10)

    def test_slow_requests_are_served_in_parallel(self):
        t0 = time.monotonic()
        self.run_threads(6, lambda i: self.get(f"/slow?ms=600&i={i}"))
        self.assertLess(time.monotonic() - t0, 2.5)
        self.settle(6)         # sequential would take >= 3.6 s
        threads = {r["thread"] for r in self.core.stats.recent(50)["requests"]}
        self.assertGreater(len(threads), 1)                 # served by different worker threads
        self.assertGreaterEqual(self.server.connection_stats()["peak"], 2)

    def test_coalesced_waiters_are_hits_but_not_cache_latency(self):
        self.run_threads(6, lambda i: self.get("/slow?ms=400"))
        self.settle(6)
        s = self.core.stats.summary()
        self.assertEqual((s["misses"], s["hits"], HITS["/slow?ms=400"]), (1, 5, 1))   # one origin fetch
        self.assertEqual(s["latency"]["cache"]["n"], 0)       # nobody got an instant cache hit...
        self.assertEqual(s["latency"]["origin"]["n"], 6)      # ...all six waited for the origin
        self.assertGreaterEqual(s["latency"]["origin"]["p50"], 350)
        rows = self.core.stats.recent()["requests"]
        self.assertEqual(sum("coalesced" in r["reason"] for r in rows), 5)
        self.assertEqual(s["avg_hit_ms"], 0.0)

    def test_purge_while_requests_in_flight(self):
        stop = threading.Event()

        def purger():
            while not stop.is_set():
                self.core.cache.purge(lambda e: True)
                time.sleep(0.002)
        p = threading.Thread(target=purger)
        p.start()
        try:
            def worker(i):
                for k in range(15):
                    r, b = self.get(f"/obj/{(i + k) % 5}")
                    assert r.status == 200 and len(b) == 1000
            self.run_threads(10, worker)
        finally:
            stop.set()
            p.join()
        c = self.core.cache
        self.assertEqual(c._bytes, sum(e.size for e in c._d.values()))   # byte accounting never drifted

    def test_cache_object_is_thread_safe(self):
        stats = proxy.Stats()
        c = proxy.Cache(50_000, 1000, 10, 10, stats)

        def mk(i):
            body = b"x" * 500
            return proxy.Entry(key=f"k{i}", url=f"k{i}", host="h", status=200, reason="OK", headers=[],
                               body=body, stored_at=time.time(), ttl=60, ttl_source="explicit", etag=None,
                               last_modified=None, vary={}, no_stale=False, origin_ms=1, size=600)

        def worker(i):
            for k in range(400):
                key = (i * 7 + k) % 200
                c.put(mk(key))
                c.get(f"k{(key + 1) % 200}")
                if k % 50 == 0:
                    c.remove(f"k{key}")
                if k % 97 == 0:
                    c.purge(lambda e: e.key.endswith("3"))
        self.run_threads(12, worker)
        self.assertLessEqual(c._bytes, 50_000)
        self.assertEqual(c._bytes, sum(e.size for e in c._d.values()))
        self.assertEqual(len(c._d), len(set(c._d)))

    def test_connection_limit_returns_503(self):
        self.server.shutdown(); self.server.server_close()
        self.server, self.core = start({"max_threads": 2})
        self.pport = self.server.server_address[1]
        idle = [socket.create_connection(("127.0.0.1", self.pport)) for _ in range(2)]
        time.sleep(0.3)                                     # both workers are now occupied
        s = socket.create_connection(("127.0.0.1", self.pport), timeout=5)
        s.sendall(b"GET /__proxy/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        self.assertIn(b" 503 ", s.recv(4096).split(b"\r\n")[0])
        s.close()
        [i.close() for i in idle]
        self.assertEqual(self.server.connection_stats()["rejected"], 1)


# --------------------------------------------------------------------------- blocklist API
class BlocklistApiTests(Base):
    def cfg(self):
        fh = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        fh.write("# my rules\nstatic.rule.test\n")
        fh.close()
        self.path = fh.name
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        return {"blocklist_file": self.path}

    def blocked(self):
        return ["cli.test"]

    def file_lines(self):
        with open(self.path) as fh:
            return fh.read().splitlines()

    def test_add_rule_blocks_immediately_and_persists(self):
        r, out = self.admin("POST", "/__proxy/blocklist/add", {"rule": "127.0.0.1"})
        self.assertEqual((r.status, out["changed"]), (200, True))
        self.assertIn("127.0.0.1", self.file_lines())
        self.assertIn("# my rules", self.file_lines())      # comments in the file survive
        r, _ = self.get("/long")                            # origin lives on 127.0.0.1
        self.assertEqual(r.status, 403)
        self.assertNotIn("/long", HITS)                     # blocked BEFORE any origin contact
        self.assertEqual(self.core.stats.summary()["blocked"], 1)

    def test_remove_rule_unblocks(self):
        self.admin("POST", "/__proxy/blocklist/add", {"rule": "127.0.0.1"})
        _, out = self.admin("POST", "/__proxy/blocklist/remove", {"rule": "127.0.0.1"})
        self.assertTrue(out["changed"])
        self.assertNotIn("127.0.0.1", self.file_lines())
        self.assertEqual(self.get("/long")[0].status, 200)

    def test_duplicate_invalid_and_url_input(self):
        self.admin("POST", "/__proxy/blocklist/add", {"rule": "dup.test"})
        _, out = self.admin("POST", "/__proxy/blocklist/add", {"rule": "DUP.test"})
        self.assertFalse(out["changed"])                    # case-insensitive duplicate
        self.assertEqual(self.admin("POST", "/__proxy/blocklist/add", {"rule": "re:(("})[0].status, 400)
        self.assertEqual(self.admin("POST", "/__proxy/blocklist/add", {"rule": "bad domain!"})[0].status, 400)
        self.assertEqual(self.admin("POST", "/__proxy/blocklist/add", {"rule": ""})[0].status, 400)
        _, out = self.admin("POST", "/__proxy/blocklist/add", {"rule": "http://Ads.Example.com/x?y=1"})
        self.assertEqual(out["rule"], "ads.example.com")    # URL reduced to hostname

    def test_allow_rule_overrides_block(self):
        self.admin("POST", "/__proxy/blocklist/add", {"rule": "example.org"})
        self.admin("POST", "/__proxy/blocklist/add", {"rule": "!ok.example.org"})
        bl = self.core.blocklist
        self.assertIsNotNone(bl.check("a.example.org"))
        self.assertIsNone(bl.check("ok.example.org"))

    def test_command_line_rules_are_protected(self):
        r, out = self.admin("POST", "/__proxy/blocklist/remove", {"rule": "cli.test"})
        self.assertEqual(r.status, 400)
        self.assertIn("command line", out["error"])
        self.assertIsNotNone(self.core.blocklist.check("cli.test"))

    def test_listing_shows_per_rule_block_counts_and_check(self):
        self.admin("POST", "/__proxy/blocklist/add", {"rule": "counted.test"})
        for _ in range(3):
            self.get("", absolute="http://counted.test/")
        _, view = self.admin("GET", "/__proxy/blocklist?check=x.counted.test")
        rule = next(r for r in view["rules"] if r["rule"] == "counted.test")
        self.assertEqual((rule["blocked_requests"], rule["source"]), (3, "file"))
        self.assertEqual(view["check"]["blocked_by"], "counted.test")

    def test_manual_edit_of_file_is_picked_up(self):
        with open(self.path, "a") as fh:
            fh.write("manual.test\n")
        os.utime(self.path, (time.time() + 5, time.time() + 5))
        self.core.blocklist.maybe_reload()
        self.assertIsNotNone(self.core.blocklist.check("manual.test"))


# --------------------------------------------------------------------------- HTTPS tunnel
class HttpsTunnelTests(Base):
    def test_tls_through_connect_works_and_is_not_cached(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with tempfile.TemporaryDirectory() as d:
            http_srv, https_srv = og.start_origin(0, port, certdir=d)
            if https_srv is None:
                http_srv.shutdown()
                self.skipTest("openssl CLI not available")
            try:
                raw = socket.create_connection(("127.0.0.1", self.pport), timeout=5)
                raw.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
                self.assertIn(b" 200 ", raw.recv(4096).split(b"\r\n")[0])
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE   # self-signed test cert
                tls = ctx.wrap_socket(raw, server_hostname="localhost")      # TLS end-to-end with origin
                conn = http.client.HTTPConnection("localhost")
                conn.sock = tls                                              # speak HTTP inside the tunnel
                for _ in range(2):                                           # same URL twice
                    conn.request("GET", "/hello")
                    r = conn.getresponse()
                    self.assertEqual((r.status, r.read()[:5]), (200, b"hello"))
                    self.assertIsNone(r.getheader("X-Cache"))                # proxy never saw the plaintext
                tls.close()
                time.sleep(0.3)
            finally:
                for srv in (http_srv, https_srv):
                    srv.shutdown(); srv.server_close()
        s = self.core.stats.summary()
        self.assertEqual(s["tunnels"], 1)
        self.assertEqual(s["cacheable_requests"], 0)
        self.assertEqual(self.core.cache.snapshot()["entries"], 0)           # HTTPS content is NOT cached
        self.assertEqual(HITS.get("/hello"), 2)                              # both reached the origin

    def test_connect_to_dead_port_is_502(self):
        raw = socket.create_connection(("127.0.0.1", self.pport), timeout=5)
        raw.sendall(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertIn(b" 502 ", raw.recv(4096).split(b"\r\n")[0])
        raw.close()


# --------------------------------------------------------------------------- statistics + dashboard
class StatisticsTests(Base):
    def test_counters_match_known_traffic(self):
        for _ in range(3):
            self.get("/long")                               # MISS, HIT, HIT
        self.get("/nostore")                                # UNCACHEABLE
        s0 = self.core.stats.summary()
        self.assertEqual((s0["hits"], s0["misses"], s0["uncacheable"]), (2, 1, 1))
        self.core.blocklist.add("gone.test")
        self.get("", absolute="http://gone.test/a")
        self.get("", absolute="http://www.gone.test/b")
        s = self.core.stats.summary()
        self.assertEqual(s["blocked"], 2)
        self.assertEqual(s["hits"], 2)
        self.assertAlmostEqual(s["hit_ratio"], 2 / s["cacheable_requests"], places=4)
        self.assertEqual(s["requests_total"], sum(s["outcomes"].values()))

    def test_latency_hits_faster_than_origin(self):
        self.get("/slow?ms=300")                            # origin takes >= 300 ms
        for _ in range(3):
            self.get("/slow?ms=300")
        lat = self.core.stats.summary()["latency"]
        self.assertEqual((lat["origin"]["n"], lat["cache"]["n"]), (1, 3))
        self.assertGreaterEqual(lat["origin"]["avg"], 300)
        self.assertLess(lat["cache"]["p95"], lat["origin"]["avg"] / 3)
        self.assertGreater(self.core.stats.summary()["time_saved_ms"], 0)


class DashboardTests(Base):
    def test_dashboard_page_served(self):
        r, html = self.admin("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn("text/html", r.getheader("Content-Type"))
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertIn("IntelliProxy", html)

    def test_dashboard_only_calls_real_endpoints_and_has_no_fake_data(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(proxy.__file__)), "dashboard.html")) as fh:
            page = fh.read()
        import re
        paths = set(re.findall(r"/__proxy/[a-z/]+", page))
        self.assertTrue(paths)
        for p in paths:
            r, _ = self.admin("POST" if p.rstrip("/").split("/")[-1] in ("add", "remove", "purge", "reset", "fetch", "reload") else "GET", p)
            self.assertNotEqual(r.status, 404, p)
        self.assertNotIn("Math.random", page)
        self.assertNotIn("mock", page.lower())

    def test_live_feed_reflects_real_traffic(self):
        self.get("/long"); self.get("/long"); self.get("/long")
        r, live = self.admin("GET", "/__proxy/live")
        self.assertEqual(r.status, 200)
        rows = live["requests"]
        self.assertEqual([x["outcome"] for x in rows], ["MISS", "HIT", "HIT"])
        for key in ("id", "time", "client", "method", "url", "status", "bytes", "ms", "thread", "host"):
            self.assertIn(key, rows[0])
        self.assertTrue(rows[0]["url"].endswith("/long"))
        self.assertEqual(live["analysis"]["summary"]["hits"], 2)
        self.assertEqual(live["cache_entries"][0]["hits"], 2)
        self.assertIn("rules", live["blocklist"])
        self.assertEqual(live["server"]["max"], 200)
        _, newer = self.admin("GET", f"/__proxy/requests?since={rows[1]['id']}")
        self.assertEqual(len(newer["requests"]), 1)         # only the request after id #2

    def test_admin_calls_are_not_counted_as_proxy_traffic(self):
        self.admin("GET", "/__proxy/live"); self.admin("GET", "/")
        self.get("", absolute=f"http://127.0.0.1:{self.pport}/__proxy/stats", track=False)   # absolute-form to ourselves
        self.assertEqual(self.core.stats.summary()["requests_total"], 0)

    def test_changes_require_post(self):
        for path in ("/__proxy/purge?all=1", "/__proxy/reload", "/__proxy/stats/reset"):
            r, _ = self.admin("GET", path)
            self.assertEqual(r.status, 405, path)
            self.assertEqual(r.getheader("Allow"), "POST")
        self.get("/long")
        self.assertEqual(self.core.cache.snapshot()["entries"], 1)

    def test_cross_site_and_rebinding_requests_refused(self):
        r, _ = self.admin("POST", "/__proxy/purge?all=1", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.admin("POST", "/__proxy/purge?all=1", headers={"Origin": f"http://127.0.0.1:{self.pport}",
                                                                   "Host": f"127.0.0.1:{self.pport}"})
        self.assertEqual(r.status, 200)
        r, _ = self.admin("GET", "/__proxy/live", headers={"Host": "evil.example"})
        self.assertEqual(r.status, 403)

    def test_reset_stats_clears_counters_and_log_but_not_cache(self):
        self.get("/long"); self.get("/long")
        self.admin("POST", "/__proxy/stats/reset")
        s = self.core.stats.summary()
        self.assertEqual((s["requests_total"], s["hits"]), (0, 0))
        self.assertEqual(self.core.stats.recent()["requests"], [])
        self.assertEqual(self.core.cache.snapshot()["entries"], 1)

    def test_purge_by_url_and_domain(self):
        self.get("/long"); self.get("/hello")
        _, out = self.admin("POST", "/__proxy/purge", {"url": self.base + "/long"})
        self.assertEqual(out["purged"], 1)
        _, out = self.admin("POST", "/__proxy/purge", {"domain": "127.0.0.1"})
        self.assertEqual(out["purged"], 1)
        self.assertEqual(self.admin("POST", "/__proxy/purge")[0].status, 400)

    def test_request_tester_goes_through_the_real_cache(self):
        _, a = self.admin("POST", "/__proxy/fetch", {"url": self.base + "/hello"})
        _, b = self.admin("POST", "/__proxy/fetch", {"url": self.base + "/hello"})
        _, c = self.admin("POST", "/__proxy/fetch", {"url": self.base + "/hello", "no_cache": True})
        self.assertEqual([x["headers"]["X-Cache"] for x in (a, b, c)], ["MISS", "HIT", "EXPIRED"])
        self.assertIn("hello from origin", a["body_preview"])
        self.assertEqual(HITS["/hello"], 2)
        self.assertEqual(self.admin("POST", "/__proxy/fetch", {"url": "https://example.com/"})[0].status, 400)
        self.assertEqual(self.admin("POST", "/__proxy/fetch", {"url": f"http://127.0.0.1:{self.pport}/"})[0].status, 400)
        self.assertEqual(self.admin("POST", "/__proxy/fetch", {"url": "nonsense"})[0].status, 400)

    def test_bad_query_values_do_not_crash(self):
        for q in ("top=abc", "limit=-5", "limit=zzz"):
            self.assertEqual(self.admin("GET", f"/__proxy/analysis?{q}")[0].status, 200)
            self.assertEqual(self.admin("GET", f"/__proxy/cache?{q}")[0].status, 200)

    def test_blocked_page_escapes_hostname(self):
        self.core.blocklist.add("re:.*")
        r, b = self.get("", absolute="http://<b>x.test/")
        self.assertEqual(r.status, 403)
        self.assertIn(b"&lt;b&gt;x.test", b)
        self.assertNotIn(b"<b>x.test", b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
