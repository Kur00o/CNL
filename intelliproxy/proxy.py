#!/usr/bin/env python3
"""
IntelliProxy - a multithreaded HTTP forward proxy with intelligent caching.

Features
  * Thread-per-connection server (bounded), keep-alive, HTTPS via CONNECT tunnels
  * Shared cache: LRU + byte/entry limits, per-object size limit
  * Expiry: Cache-Control (s-maxage/max-age/no-cache/no-store/private),
    Expires, Age, heuristic freshness (10% of Last-Modified age), default TTL
  * Conditional revalidation (ETag / Last-Modified -> 304), Vary matching,
    client conditional requests answered from cache
  * Adaptive TTL for objects without explicit freshness (grows when content is
    unchanged, shrinks when it changes)
  * Request coalescing (N concurrent misses -> 1 origin fetch)
  * stale-if-error: serves expired content when the origin fails
  * Blocked-domain policy: exact/subdomain, globs, regex, allow-list overrides,
    hosts-file syntax, hot reload
  * Statistics and cache-hit analysis (JSON + text) with tuning recommendations

Web dashboard:  http://127.0.0.1:8888/        (served from dashboard.html, real live data)

Admin API (loopback clients only unless --admin-remote). Reads are GET, changes are POST:
  GET  /__proxy/live         everything the dashboard needs, in one call
  GET  /__proxy/stats        summary counters
  GET  /__proxy/analysis     full cache-hit analysis  (?format=text for a report)
  GET  /__proxy/requests     recent request log (?limit=200&since=<id>)
  GET  /__proxy/cache        cached objects (?limit=50&sort=hits|size|ttl)
  GET  /__proxy/blocklist    active rules (?check=host tells which rule matches)
  POST /__proxy/blocklist/add     {"rule": "ads.example.com"}
  POST /__proxy/blocklist/remove  {"rule": "ads.example.com"}
  POST /__proxy/reload       re-read the blocklist file
  POST /__proxy/purge        ?all=1 | ?expired=1 | ?url=... | ?domain=...
  POST /__proxy/stats/reset  zero the counters and the request log
  POST /__proxy/fetch        {"url": "...", "no_cache": false}  fetch through this proxy

Usage
  python3 proxy.py --port 8888 --blocklist blocklist.txt --cache-mb 64
  curl -x http://127.0.0.1:8888 http://example.com/
  curl http://127.0.0.1:8888/__proxy/analysis?format=text
"""
from __future__ import annotations

import argparse
import fnmatch
import html
import http.client
import json
import logging
import os
import re
import select
import signal
import socket
import sys
import threading
import time
from collections import Counter, OrderedDict, defaultdict, deque
from dataclasses import dataclass, field, replace
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("proxy")

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "trailers", "transfer-encoding", "upgrade",
}
CACHEABLE_STATUS = {200, 203, 204, 300, 301, 308, 404, 410}
ADAPTIVE_SOURCES = {"heuristic", "default"}
CONDITIONAL_HEADERS = {"if-none-match", "if-modified-since", "if-match",
                       "if-unmodified-since", "if-range", "range"}
CACHE_SERVED = {"HIT", "REVALIDATED", "STALE"}
CACHEABLE_OUTCOMES = {"HIT", "MISS", "EXPIRED", "REVALIDATED", "STALE", "UNCACHEABLE"}
VIA = "1.1 intelliproxy"
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_NAMES = {"127.0.0.1", "localhost", "::1"}
LAT_WINDOW = 2000                   # recent latencies kept for percentiles


# --------------------------------------------------------------------------- config
@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8888
    cache_bytes: int = 64 * 1024 * 1024
    max_entries: int = 10000
    max_object: int = 5 * 1024 * 1024
    default_ttl: float = 60.0       # when origin gives no freshness info
    min_ttl: float = 0.0            # floor for heuristic/default TTLs
    max_ttl: float = 86400.0        # ceiling for every TTL
    negative_ttl: float = 30.0      # cap for 404/410 without explicit freshness
    stale_if_error: float = 300.0   # serve expired content this long on origin errors
    retention: float = 600.0        # keep expired objects with validators this long
    upstream_timeout: float = 15.0
    client_timeout: float = 30.0
    max_threads: int = 200
    cache_cookies: bool = False     # cache responses that set cookies (unsafe)
    admin_remote: bool = False
    blocklist_file: str | None = None
    blocked: list = field(default_factory=list)
    report_interval: int = 0
    request_log_size: int = 1000


# --------------------------------------------------------------------------- helpers
def parse_cc(value):
    out = {}
    for part in (value or "").split(","):
        part = part.strip().lower()
        if part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip().strip('"')
    return out


def parse_http_date(value):
    try:
        return parsedate_to_datetime(value).timestamp()
    except Exception:
        return None


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def header_map(pairs):
    m = {}
    for k, v in pairs:
        lk = k.lower()
        m[lk] = f"{m[lk]}, {v}" if lk in m else v
    return m


class Target:
    __slots__ = ("scheme", "host", "port", "path_qs", "url", "host_header")

    def __init__(self, scheme, host, port, path_qs, url, host_header):
        self.scheme, self.host, self.port = scheme, host, port
        self.path_qs, self.url, self.host_header = path_qs, url, host_header


def parse_target(raw):
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("proxy requests need an absolute http(s):// URL")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("invalid port")
    default = 443 if parts.scheme == "https" else 80
    port = port or default
    host = parts.hostname.lower().rstrip(".")
    netloc = (f"[{host}]" if ":" in host else host) + ("" if port == default else f":{port}")
    path_qs = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return Target(parts.scheme, host, port, path_qs, f"{parts.scheme}://{netloc}{path_qs}", netloc)


# --------------------------------------------------------------------------- blocklist
class BlockList:
    """Rule syntax (one per line, '#' comments):
         example.com        blocks example.com and all subdomains
         *.example.com      glob (subdomains only);   ? and * supported
         re:^ads\\d+\\.net$   regular expression on the hostname
         !good.example.com  allow-list override (wins over any block rule)
         0.0.0.0 evil.com   hosts-file format
    Rules can be edited at runtime with add()/remove(); changes are written back to the
    blocklist file (atomically) so they survive restarts.
    """

    def __init__(self, path=None, extra=()):
        self.path, self.extra = path, list(extra)
        self._lock = threading.Lock()           # guards self.rules
        self._edit = threading.Lock()           # serialises add()/remove()
        self._sig = None
        self.rules = []                         # (allow, kind, pattern, raw, source)
        self.load()

    @staticmethod
    def _parse(line):
        line = line.split("#", 1)[0].strip()
        if not line:
            return None
        toks = line.split()
        if len(toks) == 2 and re.match(r"^[0-9a-fA-F:.]+$", toks[0]):
            line = toks[1]                      # hosts-file syntax
        allow = line.startswith("!")
        if allow:
            line = line[1:].strip()
        line = line.lower() if not line.startswith("re:") else line
        if line.startswith("re:"):
            return allow, "regex", re.compile(line[3:], re.I), line
        if "*" in line or "?" in line:
            return allow, "glob", line, line
        return allow, "domain", line.lstrip(".").rstrip("."), line

    @staticmethod
    def canonical(allow, raw):
        return ("!" if allow else "") + raw

    def _normalise(self, text):
        """Validate user input and return (allow, kind, pat, raw). Raises ValueError."""
        text = (text or "").strip()
        if not text or "#" in text or "\n" in text or "\r" in text:
            raise ValueError("rule must be one non-empty line without '#'")
        body = text.lstrip("!").strip()
        if "://" in body and not body.startswith("re:"):        # user pasted a URL
            host = urlsplit(body).hostname
            if not host:
                raise ValueError("cannot read a hostname from that URL")
            text = ("!" if text.startswith("!") else "") + host
        try:
            r = self._parse(text)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}")
        if r is None:
            raise ValueError("empty rule")
        allow, kind, pat, raw = r
        if kind != "regex" and not re.fullmatch(r"[a-z0-9_.*?-]+", raw):
            raise ValueError("invalid domain rule (letters, digits, - . * ? only)")
        return r

    @staticmethod
    def _sig_of(path):
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)

    def _read_file_lines(self):
        if self.path and os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                return fh.read().splitlines()
        return []

    def load(self):
        sig = self._sig_of(self.path) if self.path and os.path.exists(self.path) else None
        rules = []
        for source, lines in (("cli", self.extra), ("file", self._read_file_lines())):
            for ln in lines:
                try:
                    r = self._parse(ln)
                except re.error as exc:
                    log.warning("bad blocklist regex %r: %s", ln, exc)
                    continue
                if r:
                    rules.append((*r, source))
        with self._lock:
            self.rules, self._sig = rules, sig
        log.info("blocklist loaded: %d rules", len(rules))
        return len(rules)

    def maybe_reload(self):
        if self.path and os.path.exists(self.path) and self._sig_of(self.path) != self._sig:
            self.load()

    def _write_file(self, lines):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, self.path)              # atomic: readers never see a half-written file

    def add(self, text):
        """Add a rule. Returns (canonical_rule, added). Raises ValueError for bad input."""
        allow, kind, pat, raw = self._normalise(text)
        canon = self.canonical(allow, raw)
        with self._edit:
            if any(self.canonical(r[0], r[3]) == canon for r in self.rules):
                return canon, False
            if self.path:
                self._write_file(self._read_file_lines() + [canon])
            else:
                self.extra.append(canon)
            self.load()
        return canon, True

    def remove(self, text):
        """Remove a rule from the file. Returns True if something was removed."""
        allow, kind, pat, raw = self._normalise(text)
        canon = self.canonical(allow, raw)
        with self._edit:
            removed = False
            if self.path and os.path.exists(self.path):
                keep = []
                for ln in self._read_file_lines():
                    try:
                        r = self._parse(ln)
                    except re.error:
                        r = None
                    if r and self.canonical(r[0], r[3]) == canon:
                        removed = True
                    else:
                        keep.append(ln)
                if removed:
                    self._write_file(keep)
            if not removed and not self.path and canon in self.extra:   # in-memory mode
                self.extra.remove(canon)
                removed = True
            if removed:
                self.load()
                return True
        if any(self.canonical(r[0], r[3]) == canon and r[4] == "cli" for r in self.rules):
            raise ValueError("rule comes from --block on the command line; restart to change it")
        return False

    @staticmethod
    def _match(kind, pat, host):
        if kind == "domain":
            return host == pat or host.endswith("." + pat)
        if kind == "glob":
            return fnmatch.fnmatchcase(host, pat)
        return pat.search(host) is not None

    def check(self, host):
        """Return the matching block rule text, or None if allowed."""
        with self._lock:
            rules = self.rules
        host = host.lower().rstrip(".")
        for allow, kind, pat, _, _src in rules:
            if allow and self._match(kind, pat, host):
                return None
        for allow, kind, pat, raw, _src in rules:
            if not allow and self._match(kind, pat, host):
                return raw
        return None

    def describe(self):
        with self._lock:
            return [{"rule": self.canonical(a, raw), "raw": raw, "type": "allow" if a else "block",
                     "kind": k, "source": src} for a, k, _, raw, src in self.rules]


# --------------------------------------------------------------------------- cache
@dataclass(eq=False)
class Entry:
    key: str
    url: str
    host: str
    status: int
    reason: str
    headers: list
    body: bytes
    stored_at: float
    ttl: float
    ttl_source: str
    etag: str | None
    last_modified: str | None
    vary: dict
    no_stale: bool
    origin_ms: float
    size: int
    hits: int = 0
    created: float = 0.0

    @property
    def expires_at(self):
        return self.stored_at + self.ttl

    def fresh(self, now):
        return now < self.expires_at

    @property
    def has_validator(self):
        return bool(self.etag or self.last_modified)


@dataclass
class Policy:
    ttl: float
    source: str
    no_stale: bool
    initial_age: float


class Cache:
    def __init__(self, max_bytes, max_entries, retention, stale_window, stats):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.retention, self.stale_window = retention, stale_window
        self.stats = stats
        self._d = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    def get(self, key):
        with self._lock:
            e = self._d.get(key)
            if e is not None:
                self._d.move_to_end(key)
            return e

    def record_hit(self, entry):
        with self._lock:
            entry.hits += 1

    def _dead(self, e, now):
        grace = 0.0
        if not e.no_stale:
            grace = self.stale_window
        if e.has_validator:
            grace = max(grace, self.retention)
        return now > e.expires_at + grace

    def _remove(self, key, reason):
        e = self._d.pop(key, None)
        if e is not None:
            self._bytes -= e.size
            self.stats.on_eviction(reason, unused=(e.hits == 0))

    def _over(self):
        return self._bytes > self.max_bytes or len(self._d) > self.max_entries

    def put(self, e):
        with self._lock:
            old = self._d.pop(e.key, None)
            if old is not None:
                self._bytes -= old.size
            if e.size > self.max_bytes:
                return False
            self._d[e.key] = e
            self._bytes += e.size
            self.stats.on_store()
            if self._over():
                now = time.time()
                for k in [k for k, v in self._d.items() if self._dead(v, now)]:
                    self._remove(k, "expired")
                    if not self._over():
                        break
            while self._over() and self._d:
                self._remove(next(iter(self._d)), "lru")
            return True

    def remove(self, key, reason="purge"):
        with self._lock:
            self._remove(key, reason)

    def sweep(self):
        now = time.time()
        with self._lock:
            dead = [k for k, v in self._d.items() if self._dead(v, now)]
            for k in dead:
                self._remove(k, "expired")
        return len(dead)

    def purge(self, predicate):
        with self._lock:
            keys = [k for k, v in self._d.items() if predicate(v)]
            for k in keys:
                self._remove(k, "purge")
        return len(keys)

    def entries(self, limit=50, sort="hits"):
        now = time.time()
        with self._lock:
            items = list(self._d.values())
        keyf = {"hits": lambda e: -e.hits, "size": lambda e: -e.size,
                "ttl": lambda e: e.expires_at - now}.get(sort, lambda e: -e.hits)
        items.sort(key=keyf)
        def ctype(e):
            return next((v.split(";")[0].strip() for k, v in e.headers if k.lower() == "content-type"), "")
        return [{
            "url": e.url, "host": e.host, "status": e.status, "size": e.size, "hits": e.hits,
            "ttl_remaining": round(e.expires_at - now, 1), "ttl": round(e.ttl, 1),
            "ttl_source": e.ttl_source, "age": round(now - e.stored_at, 1),
            "state": "fresh" if e.fresh(now) else "expired",
            "validators": e.has_validator, "content_type": ctype(e),
        } for e in items[:limit]]

    def snapshot(self):
        now = time.time()
        with self._lock:
            items = list(self._d.values())
            nbytes = self._bytes
        return {
            "entries": len(items), "bytes": nbytes,
            "max_bytes": self.max_bytes, "max_entries": self.max_entries,
            "fresh": sum(1 for e in items if e.fresh(now)),
            "stale": sum(1 for e in items if not e.fresh(now)),
            "never_hit": sum(1 for e in items if e.hits == 0),
            "avg_entry_bytes": int(nbytes / len(items)) if items else 0,
        }


# --------------------------------------------------------------------------- stats
class Stats:
    URL_LIMIT = 10000

    def __init__(self, log_size=1000):
        self.lock = threading.Lock()
        self.log_size = log_size
        self._reset()

    def _reset(self):
        """(Re)initialise every counter. Caller must hold self.lock, or be __init__."""
        self.started = time.time()
        self.outcomes, self.methods, self.statuses = Counter(), Counter(), Counter()
        self.miss_reasons = Counter()
        self.bytes_cache = self.bytes_origin = 0
        self.lat_sum, self.lat_n = defaultdict(float), Counter()
        self.saved_ms = 0.0
        self.domains = defaultdict(lambda: {"requests": 0, "lookups": 0, "served": 0,
                                            "bytes_cache": 0, "bytes_origin": 0,
                                            "saved_ms": 0.0})
        self.urls = {}                      # url -> [served_from_cache, origin_fetches]
        self.blocked_domains, self.blocked_rules = Counter(), Counter()
        self.tunnel_bytes = 0
        self.coalesced = 0
        self.evictions = Counter()
        self.stores = 0
        self.evicted_unused = 0
        self.revalidated_unchanged = 0
        self.timeline = OrderedDict()       # minute -> [lookups, served]
        self.requests = deque(maxlen=self.log_size)     # recent request log (newest last)
        self.seq = 0
        self.lat_cache = deque(maxlen=LAT_WINDOW)       # recent latencies: served from cache
        self.lat_origin = deque(maxlen=LAT_WINDOW)      # recent latencies: needed the origin

    def reset(self):
        with self.lock:
            self._reset()

    # -- recording
    def _log(self, client, method, url, host, status, outcome, nbytes, ms, reason):
        """Append to the request log. Caller holds self.lock."""
        self.seq += 1
        now = time.time()
        self.requests.append({
            "id": self.seq, "ts": round(now, 3), "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "client": client, "method": method, "url": url, "host": host, "status": status,
            "outcome": outcome, "bytes": nbytes, "ms": round(ms, 2), "reason": reason or "",
            "thread": threading.current_thread().name.split(" ")[0],
        })

    def recent(self, limit=200, since=0):
        with self.lock:
            rows = [r for r in self.requests if r["id"] > since]
            return {"latest_id": self.seq, "requests": rows[-limit:]}

    def rule_hits(self):
        with self.lock:
            return dict(self.blocked_rules)

    def on_store(self):
        with self.lock:
            self.stores += 1

    def on_eviction(self, reason, unused):
        with self.lock:
            self.evictions[reason] += 1
            if unused:
                self.evicted_unused += 1

    def on_coalesced(self):
        with self.lock:
            self.coalesced += 1

    def on_revalidated(self):
        with self.lock:
            self.revalidated_unchanged += 1

    def record_block(self, host, rule, client="", method="", url="", status=403):
        with self.lock:
            self.outcomes["BLOCKED"] += 1
            self.blocked_domains[host] += 1
            self.blocked_rules[rule] += 1
            self._log(client, method, url or host, host, status, "BLOCKED", 0, 0.0, f"rule: {rule}")

    def record_tunnel(self, host, nbytes, client="", url="", connect_ms=0.0):
        """HTTPS CONNECT tunnel closed. ms in the log is the time to open the tunnel."""
        with self.lock:
            self.outcomes["TUNNEL"] += 1
            self.tunnel_bytes += nbytes
            self.domains[host]["requests"] += 1
            self._log(client, "CONNECT", url or host, host, 200, "TUNNEL", nbytes, connect_ms,
                      "encrypted tunnel, not cacheable")

    def record(self, outcome, host, url, status, nbytes, ms, method, reason=None, saved_ms=0.0,
               client="", coalesced=False):
        served = outcome in CACHE_SERVED
        lookup = outcome in CACHEABLE_OUTCOMES
        with self.lock:
            self.outcomes[outcome] += 1
            self.methods[method] += 1
            self.statuses[status] += 1
            if reason:
                self.miss_reasons[reason] += 1
            if served:
                self.bytes_cache += nbytes
            else:
                self.bytes_origin += nbytes
            # A coalesced request is a HIT (the proxy made one origin fetch for many clients), but it
            # waited for that fetch, so its latency belongs with the origin, not with true cache hits.
            key = "COALESCED" if coalesced else outcome
            self.lat_sum[key] += ms
            self.lat_n[key] += 1
            if served and not coalesced:
                self.lat_cache.append(ms)
            elif coalesced or outcome in ("MISS", "EXPIRED", "UNCACHEABLE", "BYPASS"):
                self.lat_origin.append(ms)
            self._log(client, method, url, host, status, outcome, nbytes, ms,
                      reason or ("coalesced: shared another client's in-flight fetch" if coalesced else None))
            self.saved_ms += saved_ms
            d = self.domains[host]
            d["requests"] += 1
            d["saved_ms"] += saved_ms
            d["bytes_cache" if served else "bytes_origin"] += nbytes
            if lookup:
                d["lookups"] += 1
                d["served"] += served
                u = self.urls.setdefault(url, [0, 0])
                u[0 if served else 1] += 1
                if len(self.urls) > self.URL_LIMIT:
                    victims = sorted(self.urls, key=lambda k: sum(self.urls[k]))
                    for k in victims[: self.URL_LIMIT // 5]:
                        del self.urls[k]
                minute = int(time.time() // 60)
                b = self.timeline.setdefault(minute, [0, 0])
                b[0] += 1
                b[1] += served
                while len(self.timeline) > 120:
                    self.timeline.popitem(last=False)

    # -- reporting
    def _avg(self, names):
        n = sum(self.lat_n[k] for k in names)
        return round(sum(self.lat_sum[k] for k in names) / n, 2) if n else 0.0

    @staticmethod
    def _lat(vals):
        v = sorted(vals)
        if not v:
            return {"n": 0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
        pick = lambda p: round(v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))], 2)
        return {"n": len(v), "avg": round(sum(v) / len(v), 2), "p50": pick(50), "p95": pick(95),
                "max": round(v[-1], 2)}

    def summary(self):
        with self.lock:
            o = dict(self.outcomes)
            cacheable = sum(o.get(k, 0) for k in CACHEABLE_OUTCOMES)
            served = sum(o.get(k, 0) for k in CACHE_SERVED)
            total_bytes = self.bytes_cache + self.bytes_origin
            return {
                "uptime_s": round(time.time() - self.started),
                "requests_total": sum(o.values()),
                "outcomes": o,
                "methods": dict(self.methods),
                "cacheable_requests": cacheable,
                "served_from_cache": served,
                "hit_ratio": round(served / cacheable, 4) if cacheable else 0.0,
                "fresh_hit_ratio": round(o.get("HIT", 0) / cacheable, 4) if cacheable else 0.0,
                "bytes_from_cache": self.bytes_cache,
                "bytes_from_origin": self.bytes_origin,
                "byte_hit_ratio": round(self.bytes_cache / total_bytes, 4) if total_bytes else 0.0,
                "avg_hit_ms": self._avg(["HIT"]),
                "avg_origin_ms": self._avg(["MISS", "EXPIRED", "UNCACHEABLE", "BYPASS"]),
                "time_saved_ms": round(self.saved_ms),
                "coalesced_requests": self.coalesced,
                "hits": o.get("HIT", 0),
                "misses": o.get("MISS", 0) + o.get("EXPIRED", 0),
                "revalidated": o.get("REVALIDATED", 0),
                "stale_served": o.get("STALE", 0),
                "uncacheable": o.get("UNCACHEABLE", 0),
                "bypass": o.get("BYPASS", 0),
                "errors": o.get("ERROR", 0),
                "avg_ms_all": self._avg(list(self.lat_n)),
                "latency": {"cache": self._lat(self.lat_cache), "origin": self._lat(self.lat_origin)},
                "blocked": o.get("BLOCKED", 0),
                "tunnels": o.get("TUNNEL", 0),
                "tunnel_bytes": self.tunnel_bytes,
                "stores": self.stores,
                "revalidated_unchanged": self.revalidated_unchanged,
                "evictions": dict(self.evictions),
            }

    def analysis(self, cache, top=10):
        snap = cache.snapshot()                  # taken before our lock (lock ordering)
        s = self.summary()
        with self.lock:
            domains = []
            for host, d in self.domains.items():
                domains.append({
                    "domain": host, "requests": d["requests"], "lookups": d["lookups"],
                    "hit_ratio": round(d["served"] / d["lookups"], 3) if d["lookups"] else None,
                    "bytes_from_cache": d["bytes_cache"], "bytes_from_origin": d["bytes_origin"],
                    "time_saved_ms": round(d["saved_ms"]),
                })
            domains.sort(key=lambda d: -d["requests"])
            top_hit = sorted(((u, v) for u, v in self.urls.items() if v[0]), key=lambda x: -x[1][0])[:top]
            top_miss = sorted(((u, v) for u, v in self.urls.items() if v[1] >= 2),
                              key=lambda x: -x[1][1])[:top]
            miss_reasons = self.miss_reasons.most_common()
            timeline = [{"minute": time.strftime("%H:%M", time.localtime(m * 60)),
                         "lookups": v[0], "hit_ratio": round(v[1] / v[0], 3)}
                        for m, v in list(self.timeline.items())[-30:]]
            blocked = {"total": s["blocked"],
                       "top_domains": self.blocked_domains.most_common(top),
                       "top_rules": self.blocked_rules.most_common(top)}
            evicted_unused, evictions = self.evicted_unused, dict(self.evictions)
            stores = self.stores
        poor = [d["domain"] for d in domains
                if d["lookups"] >= 20 and d["hit_ratio"] is not None and d["hit_ratio"] < 0.1]
        recs = self._recommend(s, snap, miss_reasons, evictions, stores, evicted_unused, poor)
        return {
            "summary": s, "cache": snap,
            "miss_reasons": [{"reason": r, "count": c} for r, c in miss_reasons],
            "top_domains": domains[:top],
            "top_cached_urls": [{"url": u, "cache_hits": v[0], "origin_fetches": v[1]} for u, v in top_hit],
            "most_refetched_urls": [{"url": u, "cache_hits": v[0], "origin_fetches": v[1]} for u, v in top_miss],
            "low_hit_domains": poor,
            "evicted_without_reuse": evicted_unused,
            "timeline": timeline, "blocked": blocked, "recommendations": recs,
        }

    @staticmethod
    def _recommend(s, snap, miss_reasons, evictions, stores, evicted_unused, poor):
        recs = []
        non_served = max(1, s["cacheable_requests"] - s["served_from_cache"])
        uncache = sum(c for r, c in miss_reasons if r.startswith("uncacheable:"))
        if s["cacheable_requests"] >= 20:
            if uncache / non_served > 0.3:
                top = [r for r, c in miss_reasons if r.startswith("uncacheable:")][:3]
                recs.append(f"{uncache / non_served:.0%} of cache misses are uncacheable by origin policy "
                            f"({', '.join(top)}). Little can be gained without overriding headers.")
            expired = dict(miss_reasons).get("expired", 0)
            if expired / non_served > 0.3 and s["revalidated_unchanged"] < expired * 0.2:
                recs.append("Many objects expire and are refetched in full, but few revalidate with 304. "
                            "Origins lack ETag/Last-Modified; raise --default-ttl or --min-ttl.")
            if s["hit_ratio"] < 0.2:
                recs.append("Overall hit ratio is low; check the 'low_hit_domains' and 'most_refetched_urls' lists.")
        if stores and evictions.get("lru", 0) > 0.1 * stores:
            recs.append("Frequent LRU evictions: the cache is undersized. Increase --cache-mb / --max-entries.")
        if snap["entries"] >= 20 and snap["never_hit"] / snap["entries"] > 0.6:
            recs.append("Most cached objects have never been reused; the traffic is mostly one-shot. "
                        "A lower --max-object or --max-ttl would save memory.")
        if evicted_unused > 20 and stores and evicted_unused / stores > 0.5:
            recs.append("Over half of stored objects were evicted unused; caching them gave no benefit.")
        if poor:
            recs.append("Domains with <10% hit ratio: " + ", ".join(poor[:5]) + ".")
        if not recs:
            recs.append("No issues detected.")
        return recs


def render_report(a):
    s, c = a["summary"], a["cache"]
    mb = lambda n: f"{n / 1048576:.2f} MB"
    L = ["=" * 64, "CACHE ANALYSIS REPORT", "=" * 64,
         f"Uptime: {s['uptime_s']}s   Requests: {s['requests_total']}   "
         f"Blocked: {s['blocked']}   Tunnels: {s['tunnels']}",
         f"Hit ratio: {s['hit_ratio']:.1%} ({s['served_from_cache']}/{s['cacheable_requests']} cacheable)   "
         f"fresh hits: {s['fresh_hit_ratio']:.1%}",
         f"Byte hit ratio: {s['byte_hit_ratio']:.1%}   from cache {mb(s['bytes_from_cache'])}, "
         f"from origin {mb(s['bytes_from_origin'])}",
         f"Latency: hit {s['avg_hit_ms']} ms vs origin {s['avg_origin_ms']} ms   "
         f"est. time saved {s['time_saved_ms'] / 1000:.1f}s   coalesced: {s['coalesced_requests']}",
         f"Cache: {c['entries']} objects, {mb(c['bytes'])}/{mb(c['max_bytes'])}, "
         f"fresh {c['fresh']}, stale {c['stale']}, never reused {c['never_hit']}",
         f"Outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(s["outcomes"].items())),
         f"Evictions: {s['evictions'] or 'none'}"]
    if a["miss_reasons"]:
        L += ["", "Why requests went to the origin:"]
        L += [f"  {r['count']:>6}  {r['reason']}" for r in a["miss_reasons"][:10]]
    if a["top_domains"]:
        L += ["", "Top domains:"]
        for d in a["top_domains"][:8]:
            hr = "n/a" if d["hit_ratio"] is None else f"{d['hit_ratio']:.0%}"
            L.append(f"  {d['domain'][:40]:<40} req={d['requests']:<5} hit={hr}")
    if a["top_cached_urls"]:
        L += ["", "Most reused cached URLs:"]
        L += [f"  {u['cache_hits']:>5} hits  {u['url'][:70]}" for u in a["top_cached_urls"][:5]]
    if a["most_refetched_urls"]:
        L += ["", "Most refetched URLs (tuning candidates):"]
        L += [f"  {u['origin_fetches']:>5} fetches  {u['url'][:66]}" for u in a["most_refetched_urls"][:5]]
    L += ["", "Recommendations:"] + [f"  - {r}" for r in a["recommendations"]]
    return "\n".join(L)


# --------------------------------------------------------------------------- core
class ProxyCore:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stats = Stats(cfg.request_log_size)
        self.blocklist = BlockList(cfg.blocklist_file, cfg.blocked)
        self.cache = Cache(cfg.cache_bytes, cfg.max_entries, cfg.retention,
                           cfg.stale_if_error, self.stats)
        self._flights = {}
        self._flight_lock = threading.Lock()
        self._stop = threading.Event()

    # request coalescing
    def begin_flight(self, key):
        with self._flight_lock:
            ev = self._flights.get(key)
            if ev is not None:
                return ev, False
            ev = self._flights[key] = threading.Event()
            return ev, True

    def end_flight(self, key, ev):
        with self._flight_lock:
            self._flights.pop(key, None)
        ev.set()

    # freshness policy
    def policy(self, status, h, now):
        cfg = self.cfg
        cc = parse_cc(h.get("cache-control"))
        if "no-store" in cc:
            return None, "no-store"
        if "private" in cc:
            return None, "private"
        if h.get("set-cookie") and not cfg.cache_cookies:
            return None, "set-cookie"
        if h.get("vary", "").strip() == "*":
            return None, "vary-star"
        if status not in CACHEABLE_STATUS:
            return None, f"status-{status}"
        date = (parse_http_date(h["date"]) if "date" in h else None) or now
        source = "explicit"
        if to_float(cc.get("s-maxage")) is not None:
            ttl = to_float(cc["s-maxage"])
        elif to_float(cc.get("max-age")) is not None:
            ttl = to_float(cc["max-age"])
        elif "expires" in h:
            exp = parse_http_date(h["expires"])
            ttl = (exp - date) if exp else 0.0
        elif parse_http_date(h.get("last-modified", "")) is not None:
            ttl = max(0.0, (date - parse_http_date(h["last-modified"])) * 0.1)
            source = "heuristic"
        else:
            ttl, source = cfg.default_ttl, "default"
        if source != "explicit" and status in (404, 410):
            ttl = min(ttl, cfg.negative_ttl)
        no_stale = "must-revalidate" in cc or "proxy-revalidate" in cc
        if "no-cache" in cc:
            ttl, no_stale = 0.0, True
        ttl = min(ttl, cfg.max_ttl)
        if source in ADAPTIVE_SOURCES:
            ttl = max(ttl, cfg.min_ttl)
        if ttl <= 0 and not ("etag" in h or "last-modified" in h):
            return None, "zero-ttl"
        age = max(0.0, to_float(h.get("age")) or 0.0)
        return Policy(max(ttl, 0.0), source, no_stale, age), None

    def build_entry(self, target, status, reason, pairs, body, policy, now,
                    origin_ms, req_headers, prev=None):
        h = header_map(pairs)
        ttl = policy.ttl
        if prev is not None and policy.source in ADAPTIVE_SOURCES and prev.ttl_source in ADAPTIVE_SOURCES:
            if prev.body == body:      # unchanged -> trust it longer
                ttl = min(self.cfg.max_ttl, max(prev.ttl * 2, ttl))
            else:                      # changed -> check more often
                ttl = max(max(self.cfg.min_ttl, 1.0), min(ttl, prev.ttl / 2))
        skip = HOP_BY_HOP | {"content-length", "age"}
        headers = [(k, v) for k, v in pairs if k.lower() not in skip]
        vary = {n.strip().lower(): req_headers.get(n.strip(), "")
                for n in h.get("vary", "").split(",") if n.strip()}
        size = len(body) + sum(len(k) + len(v) + 4 for k, v in headers) + 256
        return Entry(key=target.url, url=target.url, host=target.host, status=status,
                     reason=reason or "", headers=headers, body=body,
                     stored_at=now - policy.initial_age, ttl=ttl, ttl_source=policy.source,
                     etag=h.get("etag"), last_modified=h.get("last-modified"), vary=vary,
                     no_stale=policy.no_stale, origin_ms=origin_ms, size=size,
                     hits=prev.hits if prev else 0, created=now)

    def refresh_entry(self, entry, pairs, now):
        """Apply a 304 response: merge headers, recompute freshness, re-store."""
        skip = HOP_BY_HOP | {"content-length", "content-encoding", "content-range", "age"}
        new = [(k, v) for k, v in pairs if k.lower() not in skip]
        names = {k.lower() for k, _ in new}
        merged = [(k, v) for k, v in entry.headers if k.lower() not in names] + new
        policy, _ = self.policy(entry.status, header_map(merged), now)
        if policy is None:                      # 304 turned the object uncacheable
            self.cache.remove(entry.key)
            return replace(entry, headers=merged)
        ttl = policy.ttl
        if policy.source in ADAPTIVE_SOURCES:
            ttl = min(self.cfg.max_ttl, max(entry.ttl * 2, ttl))
        h = header_map(merged)
        fresh = replace(entry, headers=merged, stored_at=now - policy.initial_age, ttl=ttl,
                        ttl_source=policy.source, no_stale=policy.no_stale,
                        etag=h.get("etag", entry.etag),
                        last_modified=h.get("last-modified", entry.last_modified))
        self.cache.put(fresh)
        self.stats.on_revalidated()
        return fresh

    def open_upstream(self, target, method, headers, body):
        cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        conn = cls(target.host, target.port, timeout=self.cfg.upstream_timeout)
        try:
            conn.putrequest(method, target.path_qs, skip_host=True, skip_accept_encoding=True)
            for k, v in headers:
                conn.putheader(k, v)
            conn.endheaders(body)
            return conn, conn.getresponse()
        except BaseException:
            conn.close()
            raise

    # background maintenance
    def start_background(self):
        def sweeper():
            while not self._stop.wait(15):
                try:
                    self.cache.sweep()
                    self.blocklist.maybe_reload()
                except Exception:
                    log.exception("maintenance failed")
        threading.Thread(target=sweeper, daemon=True, name="sweeper").start()
        if self.cfg.report_interval > 0:
            def reporter():
                while not self._stop.wait(self.cfg.report_interval):
                    log.info("\n%s", render_report(self.stats.analysis(self.cache)))
            threading.Thread(target=reporter, daemon=True, name="reporter").start()

    def stop(self):
        self._stop.set()


# --------------------------------------------------------------------------- server
class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, addr, handler, core):
        self.core = core
        self.slots = threading.BoundedSemaphore(core.cfg.max_threads)
        self._conn_lock = threading.Lock()
        self.active = self.peak = self.rejected = 0
        super().__init__(addr, handler)

    def connection_stats(self):
        with self._conn_lock:
            return {"active": self.active, "peak": self.peak, "rejected": self.rejected,
                    "max": self.core.cfg.max_threads}

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            with self._conn_lock:
                self.rejected += 1
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        with self._conn_lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._conn_lock:
                self.active -= 1
            self.slots.release()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "IntelliProxy/1.0"

    def setup(self):
        self.timeout = self.server.core.cfg.client_timeout
        super().setup()

    def log_message(self, fmt, *args):      # silence http.server's own logging
        pass

    # ---- dispatch
    def _dispatch(self):
        self._headers_sent = False
        try:
            if self.path.startswith("/"):
                self._admin()
            else:
                self._proxy()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:
            log.exception("internal error handling %s %s", self.command, self.path)
            self.close_connection = True
            if not self._headers_sent:
                try:
                    self._send_text(500, "Internal proxy error")
                except Exception:
                    pass

    # ---- low-level senders
    def _send_text(self, status, text, ctype="text/plain; charset=utf-8", extra=()):
        body = text.encode() if isinstance(text, str) else text
        self._send_message(status, None, [("Content-Type", ctype), *extra], body,
                           head_only=(self.command == "HEAD"))

    def _send_message(self, status, reason, headers, body, head_only=False, length=None):
        self._headers_sent = True
        self.send_response_only(status, reason)
        names = set()
        for k, v in headers:
            lk = k.lower()
            if lk in HOP_BY_HOP or lk == "content-length":
                continue
            names.add(lk)
            self.send_header(k, v)
        if "date" not in names:
            self.send_header("Date", self.date_time_string())
        no_body = status in (204, 304) or 100 <= status < 200
        if not no_body:
            self.send_header("Content-Length", str(len(body) if length is None else length))
        self.send_header("Via", VIA)
        self.end_headers()
        if not head_only and not no_body:
            self.wfile.write(body)

    def _stream(self, resp, pairs, prefix, extra):
        """Relay an origin response without caching it."""
        status = resp.status
        no_body = self.command == "HEAD" or status in (204, 304) or 100 <= status < 200
        h = header_map(pairs)
        clen = h.get("content-length")
        self._headers_sent = True
        self.send_response_only(status, resp.reason)
        for k, v in pairs:
            if k.lower() not in HOP_BY_HOP and k.lower() != "content-length":
                self.send_header(k, v)
        for k, v in extra:
            self.send_header(k, v)
        if clen is not None and clen.isdigit() and "transfer-encoding" not in h:
            self.send_header("Content-Length", clen)
        elif not no_body:
            self.send_header("Connection", "close")     # close-delimited body
            self.close_connection = True
        self.send_header("Via", VIA)
        self.end_headers()
        total = 0
        if not no_body:
            if prefix:
                self.wfile.write(prefix)
                total += len(prefix)
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                total += len(chunk)
        return total

    # ---- request helpers
    def _read_body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            out = bytearray()
            while True:
                size = int(self.rfile.readline(65537).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(65537).strip():
                        pass
                    return bytes(out)
                out += self.rfile.read(size)
                self.rfile.read(2)
                if len(out) > 64 * 1024 * 1024:
                    raise ValueError("request body too large")
        cl = self.headers.get("Content-Length")
        if cl:
            return self.rfile.read(int(cl))
        return None

    def _upstream_headers(self, target, body, strip_conditional):
        conn_tokens = {t.strip().lower() for t in self.headers.get("Connection", "").split(",")}
        out = []
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk in conn_tokens or lk in ("host", "content-length", "expect"):
                continue
            if strip_conditional and lk in CONDITIONAL_HEADERS:
                continue
            out.append((k, v))
        out.append(("Host", target.host_header))
        if body is not None or self.command in ("POST", "PUT", "PATCH"):
            out.append(("Content-Length", str(len(body or b""))))
        out += [("Via", VIA), ("Connection", "close")]
        return out

    def _classify(self):
        """-> (cacheable_request, bypass_reason, force_refresh)"""
        if self.command not in ("GET", "HEAD"):
            return False, "method", False
        if self.headers.get("Range"):
            return False, "range", False
        if self.headers.get("Authorization"):
            return False, "authorization", False
        cc = parse_cc(self.headers.get("Cache-Control"))
        if "no-store" in cc:
            return False, "client-no-store", False
        force = ("no-cache" in cc or cc.get("max-age") == "0"
                 or "no-cache" in self.headers.get("Pragma", "").lower())
        return True, None, force

    def _vary_ok(self, entry):
        return all(self.headers.get(n, "") == v for n, v in entry.vary.items())

    def _client_validators_match(self, entry):
        if entry.status != 200:
            return False
        inm = self.headers.get("If-None-Match")
        if inm:
            if inm.strip() == "*":
                return True
            strip = lambda t: t[2:] if t.startswith("W/") else t
            return entry.etag is not None and strip(entry.etag) in {strip(t.strip()) for t in inm.split(",")}
        ims = self.headers.get("If-Modified-Since")
        if ims and entry.last_modified:
            a, b = parse_http_date(ims), parse_http_date(entry.last_modified)
            return a is not None and b is not None and b <= a
        return False

    def _finish(self, outcome, target, status, nbytes, t0, reason=None, saved_from=None, coalesced=False):
        ms = (time.monotonic() - t0) * 1000
        saved = max(0.0, saved_from - ms) if saved_from is not None else 0.0
        self.server.core.stats.record(outcome, target.host, target.url, status, nbytes,
                                      ms, self.command, reason, saved, client=self.client_address[0],
                                      coalesced=coalesced)
        log.info("[%s] %s %s %s -> %s %s %dB %.0fms%s", threading.current_thread().name.split(" ")[0],
                 self.client_address[0], self.command, target.url, status, outcome, nbytes, ms,
                 f" [{reason}]" if reason else "")

    def _is_self(self, target):
        """True if an absolute-form request points at this proxy's own admin port."""
        if target.port != self.server.server_address[1]:
            return False
        return target.host in LOCAL_NAMES | {self.server.core.cfg.host, self.connection.getsockname()[0]}

    # ---- main proxy path
    def _proxy(self):
        core = self.server.core
        t0 = time.monotonic()
        try:
            target = parse_target(self.path)
        except ValueError as exc:
            return self._send_text(400, f"Bad request: {exc}")

        if self._is_self(target):            # browser sent the dashboard URL through the proxy
            self.path = target.path_qs
            return self._admin()

        rule = core.blocklist.check(target.host)
        if rule:
            core.stats.record_block(target.host, rule, self.client_address[0], self.command, target.url)
            log.info("%s %s %s BLOCKED by %r", self.client_address[0], self.command, target.url, rule)
            body = (f"<html><body><h1>403 Blocked</h1><p>Access to <b>{html.escape(target.host)}</b> "
                    f"is blocked by proxy policy.</p></body></html>")
            return self._send_text(403, body, "text/html; charset=utf-8", [("X-Blocked-By", rule)])

        try:
            body = self._read_body()
        except (ValueError, OSError) as exc:
            self.close_connection = True
            return self._send_text(400, f"Bad request body: {exc}")

        cacheable, bypass_reason, force = self._classify()
        if not cacheable:
            return self._passthrough(target, body, t0, f"bypass:{bypass_reason}")

        entry = core.cache.get(target.url)
        vary_miss = bool(entry and not self._vary_ok(entry))
        if vary_miss:
            entry = None
        now = time.time()
        if entry and entry.fresh(now) and not force:
            core.cache.record_hit(entry)
            return self._serve_entry(entry, "HIT", target, t0)
        if self.command == "HEAD":                      # never populate the cache via HEAD
            return self._passthrough(target, body, t0, "bypass:head-miss")

        if force:
            reason = "client-no-cache"
        elif vary_miss:
            reason = "vary-mismatch"
        else:
            reason = "expired" if entry else "cold"

        leader = False
        ev = None
        if not force:
            ev, leader = core.begin_flight(target.url)
            if not leader:                              # someone is already fetching it
                ev.wait(core.cfg.upstream_timeout + 5)
                e2 = core.cache.get(target.url)
                if e2 and self._vary_ok(e2) and e2.fresh(time.time()):
                    core.stats.on_coalesced()
                    core.cache.record_hit(e2)
                    return self._serve_entry(e2, "HIT", target, t0, coalesced=True)
        try:
            if leader:
                # Re-check: the previous leader may have finished between our first cache
                # lookup and begin_flight(). Without this we would fetch the object twice.
                e3 = core.cache.get(target.url)
                if e3 and self._vary_ok(e3) and e3.fresh(time.time()):
                    core.cache.record_hit(e3)
                    return self._serve_entry(e3, "HIT", target, t0)
            self._fetch_store_serve(target, body, entry, reason, t0)
        finally:
            if leader:
                core.end_flight(target.url, ev)

    def _stale_usable(self, entry):
        return bool(entry and not entry.no_stale
                    and time.time() - entry.expires_at <= self.server.core.cfg.stale_if_error)

    def _origin_failed(self, target, stale, exc, t0):
        if self._stale_usable(stale):
            log.warning("origin failed for %s (%s); serving stale", target.url, exc)
            self.server.core.cache.record_hit(stale)
            return self._serve_entry(stale, "STALE", target, t0,
                                     extra=[("Warning", '111 intelliproxy "Revalidation failed"')])
        status = 504 if isinstance(exc, socket.timeout) else 502
        self._send_text(status, f"{status} Upstream error: {exc}")
        self._finish("ERROR", target, status, 0, t0, reason=type(exc).__name__)

    def _fetch_store_serve(self, target, body, stale, reason, t0):
        core, cfg = self.server.core, self.server.core.cfg
        hdrs = self._upstream_headers(target, body, strip_conditional=True)
        if stale is not None:
            if stale.etag:
                hdrs.append(("If-None-Match", stale.etag))
            if stale.last_modified:
                hdrs.append(("If-Modified-Since", stale.last_modified))
        ts = time.monotonic()
        try:
            conn, resp = core.open_upstream(target, "GET", hdrs, None)
        except Exception as exc:
            return self._origin_failed(target, stale, exc, t0)
        try:
            origin_ms = (time.monotonic() - ts) * 1000
            pairs = resp.getheaders()
            status = resp.status
            now = time.time()

            if status == 304 and stale is not None:
                resp.read()
                fresh = core.refresh_entry(stale, pairs, now)
                core.cache.record_hit(fresh)
                return self._serve_entry(fresh, "REVALIDATED", target, t0,
                                         saved_from=stale.origin_ms)
            if status >= 500 and self._stale_usable(stale):
                resp.read()
                return self._origin_failed(target, stale, f"origin returned {status}", t0)

            policy, why = core.policy(status, header_map(pairs), now)
            clen = to_float(resp.getheader("Content-Length"))
            if policy is not None and clen is not None and clen > cfg.max_object:
                policy, why = None, "too-large"
            buf, overflow = bytearray(), False
            if policy is not None:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > cfg.max_object:
                        overflow = True
                        break
                if overflow:
                    policy, why = None, "too-large"

            if policy is None:
                n = self._stream(resp, pairs, bytes(buf), [("X-Cache", "UNCACHEABLE")])
                return self._finish("UNCACHEABLE", target, status, n, t0, f"uncacheable:{why}")

            entry = core.build_entry(target, status, resp.reason, pairs, bytes(buf), policy,
                                     now, origin_ms, self.headers, prev=stale)
            core.cache.put(entry)
            outcome = "EXPIRED" if stale is not None else "MISS"
            return self._serve_entry(entry, outcome, target, t0, reason=reason)
        except (socket.timeout, http.client.HTTPException, OSError) as exc:
            if self._headers_sent:
                self.close_connection = True
                raise
            return self._origin_failed(target, stale, exc, t0)
        finally:
            conn.close()

    def _passthrough(self, target, body, t0, reason):
        core = self.server.core
        hdrs = self._upstream_headers(target, body, strip_conditional=False)
        try:
            conn, resp = core.open_upstream(target, self.command, hdrs, body)
        except Exception as exc:
            return self._origin_failed(target, None, exc, t0)
        try:
            n = self._stream(resp, resp.getheaders(), b"", [("X-Cache", "BYPASS")])
            self._finish("BYPASS", target, resp.status, n, t0, reason)
        except (socket.timeout, http.client.HTTPException, OSError):
            self.close_connection = True
            if not self._headers_sent:
                self._send_text(502, "502 Upstream error")
            else:
                raise
        finally:
            conn.close()

    def _serve_entry(self, entry, outcome, target, t0, reason=None, extra=(), saved_from=None,
                     coalesced=False):
        now = time.time()
        status, body = entry.status, entry.body
        if self._client_validators_match(entry):
            status, body = 304, b""
        elif outcome in CACHE_SERVED and saved_from is None:
            saved_from = entry.origin_ms
        reason_phrase = "Not Modified" if status == 304 else entry.reason
        headers = list(entry.headers) + [
            ("Age", str(int(max(0, now - entry.stored_at)))),
            ("X-Cache", outcome),
            ("X-Cache-TTL-Remaining", str(int(max(0, entry.expires_at - now)))),
            *extra,
        ]
        self._send_message(status, reason_phrase, headers, body,
                           head_only=(self.command == "HEAD"),
                           length=len(entry.body) if self.command == "HEAD" else None)
        self._finish(outcome, target, status, len(entry.body), t0,
                     reason if outcome in ("MISS", "EXPIRED") else None,
                     saved_from=saved_from if outcome in CACHE_SERVED else None, coalesced=coalesced)

    # ---- CONNECT tunnel (HTTPS)
    def do_CONNECT(self):
        core = self.server.core
        self._headers_sent = False
        host, _, port_s = self.path.rpartition(":")
        host = host.strip("[]").lower().rstrip(".")
        try:
            port = int(port_s)
            if not host:
                raise ValueError
        except ValueError:
            return self._send_text(400, "Bad CONNECT target")
        rule = core.blocklist.check(host)
        if rule:
            core.stats.record_block(host, rule, self.client_address[0], "CONNECT", self.path)
            log.info("%s CONNECT %s BLOCKED by %r", self.client_address[0], self.path, rule)
            return self._send_text(403, f"CONNECT to {host} blocked by proxy policy",
                                   extra=[("X-Blocked-By", rule)])
        t0 = time.monotonic()
        try:
            upstream = socket.create_connection((host, port), timeout=core.cfg.upstream_timeout)
        except OSError as exc:
            return self._send_text(502, f"Cannot reach {host}:{port}: {exc}")
        connect_ms = (time.monotonic() - t0) * 1000
        try:
            self.send_response_only(200, "Connection Established")
            self.end_headers()
            self.close_connection = True
            total = self._relay(upstream)
        finally:
            upstream.close()
        core.stats.record_tunnel(host, total, self.client_address[0], self.path, connect_ms)
        log.info("%s CONNECT %s tunnel closed, %dB", self.client_address[0], self.path, total)

    def _relay(self, upstream):
        client, total = self.connection, 0
        socks = [client, upstream]
        while True:
            try:
                r, _, x = select.select(socks, [], socks, 60)
            except (OSError, ValueError):
                break
            if x or not r:
                break
            for s in r:
                try:
                    data = s.recv(65536)
                    if not data:
                        return total
                    (upstream if s is client else client).sendall(data)
                except OSError:
                    return total
                total += len(data)
        return total

    # ---- admin API + dashboard
    @staticmethod
    def _int(value, default, lo=1, hi=5000):
        try:
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _truthy(value):
        return str(value).lower() in ("1", "true", "yes", "on")

    def _admin_allowed(self):
        """Loopback only (unless --admin-remote), and the Host header must be a local name
        (defeats DNS-rebinding: a hostile site resolving to 127.0.0.1)."""
        core = self.server.core
        ip = self.client_address[0]
        local = ip in ("127.0.0.1", "::1") or ip.startswith("::ffff:127.")
        if core.cfg.admin_remote:
            return True
        if not local:
            return False
        host = urlsplit("//" + self.headers.get("Host", "")).hostname or ""
        return host in LOCAL_NAMES | {core.cfg.host, self.connection.getsockname()[0]}

    def _origin_ok(self):
        """CSRF defence for POST: browsers always send Origin on cross-site posts."""
        origin = self.headers.get("Origin")
        return not origin or urlsplit(origin).netloc == self.headers.get("Host", "")

    def _blocklist_view(self):
        core = self.server.core
        hits = core.stats.rule_hits()
        rules = core.blocklist.describe()
        for r in rules:
            r["blocked_requests"] = hits.get(r["raw"], 0) if r["type"] == "block" else 0
        return {"file": core.blocklist.path and os.path.basename(core.blocklist.path), "rules": rules}

    def _live(self, limit):
        """Everything the dashboard shows, from one consistent call."""
        core, cfg = self.server.core, self.server.core.cfg
        a = core.stats.analysis(core.cache)
        rec = core.stats.recent(limit)
        return {
            "now": time.time(), "analysis": a,
            "requests": rec["requests"], "latest_id": rec["latest_id"],
            "cache_entries": core.cache.entries(300, "ttl"),
            "blocklist": self._blocklist_view(),
            "server": {**self.server.connection_stats(),
                       "listen": f"{cfg.host}:{self.server.server_address[1]}",
                       "cache_mb": round(cfg.cache_bytes / 1048576, 1), "max_entries": cfg.max_entries,
                       "max_object_mb": round(cfg.max_object / 1048576, 2),
                       "default_ttl": cfg.default_ttl, "min_ttl": cfg.min_ttl, "max_ttl": cfg.max_ttl,
                       "stale_if_error": cfg.stale_if_error},
        }

    def _admin_fetch(self, p):
        """Fetch a URL *through this proxy* exactly like any client would (dashboard tester)."""
        cfg = self.server.core.cfg
        try:
            t = parse_target(str(p.get("url", "")))
        except ValueError as exc:
            return 400, {"error": str(exc)}
        if t.scheme != "http":
            return 400, {"error": "only http:// can be fetched here (HTTPS is tunnelled, never cached)"}
        if self._is_self(t):
            return 400, {"error": "that URL points at the proxy itself"}
        method = "HEAD" if str(p.get("method", "GET")).upper() == "HEAD" else "GET"
        hdrs = {"Connection": "close", "User-Agent": "IntelliProxy-Dashboard"}
        if self._truthy(p.get("no_cache")):
            hdrs["Cache-Control"] = "no-cache"
        t0 = time.monotonic()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1],
                                          timeout=cfg.upstream_timeout + 5)
        try:
            conn.request(method, t.url, headers=hdrs)
            r = conn.getresponse()
            data = r.read(262144)
        except (OSError, http.client.HTTPException) as exc:
            return 502, {"error": f"request failed: {exc}"}
        finally:
            conn.close()
        ms = round((time.monotonic() - t0) * 1000, 2)
        ctype = r.getheader("Content-Type", "")
        textual = ctype.startswith("text/") or "json" in ctype or "xml" in ctype
        preview = data[:600].decode("utf-8", "replace") if textual else f"({len(data)} bytes, {ctype or 'binary'})"
        keep = ("X-Cache", "Age", "X-Cache-TTL-Remaining", "Cache-Control", "ETag", "Content-Type",
                "X-Blocked-By", "Warning")
        return 200, {"status": r.status, "reason": r.reason, "elapsed_ms": ms, "bytes": len(data),
                     "headers": {k: r.getheader(k) for k in keep if r.getheader(k)}, "body_preview": preview}

    def _admin(self):
        core = self.server.core
        send = lambda code, text, ctype="text/plain; charset=utf-8", extra=(): self._send_text(
            code, text, ctype, [("Cache-Control", "no-store"), *extra])
        js = lambda obj, code=200: send(code, json.dumps(obj, indent=2, default=str), "application/json")
        if self.command == "POST":
            # Always consume the body first so keep-alive connections never desynchronise.
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 1_000_000 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("body too large or chunked")
                raw = self.rfile.read(n) if n else b""
            except (ValueError, OSError):
                self.close_connection = True
                return send(400, "Bad request body")
        if not self._admin_allowed():
            return send(403, "Admin API is restricted to localhost")
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        route = u.path.rstrip("/") or "/"
        method = self.command

        gets = {"/__proxy/stats", "/__proxy/analysis", "/__proxy/cache", "/__proxy/requests",
                "/__proxy/blocklist", "/__proxy/live", "/__proxy", "/__proxy/health", "/", "/dashboard"}
        posts = {"/__proxy/reload", "/__proxy/purge", "/__proxy/stats/reset", "/__proxy/fetch",
                 "/__proxy/blocklist/add", "/__proxy/blocklist/remove"}
        if route not in gets | posts:
            return send(404, "Unknown admin endpoint. Open / for the dashboard or /__proxy/health")
        if method == "POST" and route in posts:
            if not self._origin_ok():
                return send(403, "Cross-origin POST refused")
            if raw:
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        q.update(obj)
                except ValueError:
                    return js({"error": "body must be JSON"}, 400)
        elif not (method in ("GET", "HEAD") and route in gets):
            allowed = "POST" if route in posts else "GET, HEAD"
            self.close_connection = True
            return send(405, f"Method not allowed here; use {allowed}", extra=[("Allow", allowed)])

        # ---- reads
        if route in ("/", "/dashboard"):
            try:
                with open(os.path.join(HERE, "dashboard.html"), "rb") as fh:
                    page = fh.read()
            except OSError:
                return send(404, "dashboard.html is missing (it must sit next to proxy.py)")
            return send(200, page, "text/html; charset=utf-8")
        if route == "/__proxy/live":
            return js(self._live(self._int(q.get("limit"), 200, 1, 1000)))
        if route == "/__proxy/stats":
            return js(core.stats.summary())
        if route == "/__proxy/analysis":
            a = core.stats.analysis(core.cache, top=self._int(q.get("top"), 10, 1, 100))
            if q.get("format") == "text":
                return send(200, render_report(a) + "\n")
            return js(a)
        if route == "/__proxy/requests":
            return js(core.stats.recent(self._int(q.get("limit"), 200, 1, 1000),
                                        self._int(q.get("since"), 0, 0, 10**12)))
        if route == "/__proxy/cache":
            return js({"snapshot": core.cache.snapshot(),
                       "entries": core.cache.entries(self._int(q.get("limit"), 50, 1, 5000),
                                                     q.get("sort", "hits"))})
        if route == "/__proxy/blocklist":
            view = self._blocklist_view()
            if q.get("check"):
                view["check"] = {"host": q["check"], "blocked_by": core.blocklist.check(q["check"])}
            return js(view)
        if route in ("/__proxy", "/__proxy/health"):
            return js({"status": "ok", "dashboard": "/", "endpoints": sorted(gets | posts)})

        # ---- changes (POST only)
        if route == "/__proxy/reload":
            return js({"rules_loaded": core.blocklist.load()})
        if route == "/__proxy/stats/reset":
            core.stats.reset()
            return js({"reset": True})
        if route == "/__proxy/fetch":
            code, obj = self._admin_fetch(q)
            return js(obj, code)
        if route in ("/__proxy/blocklist/add", "/__proxy/blocklist/remove"):
            try:
                if route.endswith("add"):
                    rule, changed = core.blocklist.add(q.get("rule"))
                else:
                    changed = core.blocklist.remove(q.get("rule"))
                    rule = q.get("rule")
            except ValueError as exc:
                return js({"error": str(exc)}, 400)
            except OSError as exc:
                return js({"error": f"cannot write blocklist file: {exc}"}, 500)
            return js({"rule": rule, "changed": changed, **self._blocklist_view()})
        # route == /__proxy/purge
        if self._truthy(q.get("all")):
            n = core.cache.purge(lambda e: True)
        elif self._truthy(q.get("expired")):
            now = time.time()
            n = core.cache.purge(lambda e: not e.fresh(now))
        elif q.get("url"):
            try:
                key = parse_target(str(q["url"])).url
            except ValueError as exc:
                return js({"error": str(exc)}, 400)
            n = core.cache.purge(lambda e: e.key == key)
        elif q.get("domain"):
            d = str(q["domain"]).lower().strip(".")
            n = core.cache.purge(lambda e: e.host == d or e.host.endswith("." + d))
        else:
            return js({"error": "use all=1, expired=1, url=... or domain=..."}, 400)
        return js({"purged": n})


for _m in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
    setattr(ProxyHandler, f"do_{_m}", ProxyHandler._dispatch)


# --------------------------------------------------------------------------- entry point
def build_server(cfg):
    core = ProxyCore(cfg)
    server = ProxyServer((cfg.host, cfg.port), ProxyHandler, core)
    return server, core


def main(argv=None):
    p = argparse.ArgumentParser(description="Multithreaded caching HTTP proxy")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8888)
    p.add_argument("--blocklist", default=os.path.join(HERE, "blocklist.txt"),
                   help="file with blocked-domain rules (default: blocklist.txt next to proxy.py; "
                        "the dashboard edits this file)")
    p.add_argument("--block", action="append", default=[], metavar="RULE",
                   help="extra block rule (repeatable)")
    p.add_argument("--cache-mb", type=float, default=64)
    p.add_argument("--max-entries", type=int, default=10000)
    p.add_argument("--max-object-mb", type=float, default=5)
    p.add_argument("--default-ttl", type=float, default=60)
    p.add_argument("--min-ttl", type=float, default=0)
    p.add_argument("--max-ttl", type=float, default=86400)
    p.add_argument("--negative-ttl", type=float, default=30)
    p.add_argument("--stale-if-error", type=float, default=300)
    p.add_argument("--retention", type=float, default=600)
    p.add_argument("--timeout", type=float, default=15, help="origin timeout (s)")
    p.add_argument("--max-threads", type=int, default=200)
    p.add_argument("--cache-cookies", action="store_true")
    p.add_argument("--admin-remote", action="store_true")
    p.add_argument("--report-interval", type=int, default=0, help="log a report every N seconds")
    p.add_argument("--log-file")
    p.add_argument("-v", "--verbose", action="store_true", help="log every request")
    a = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if a.verbose or a.report_interval else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(a.log_file)] if a.log_file else None)
    cfg = Config(host=a.host, port=a.port, cache_bytes=int(a.cache_mb * 1048576),
                 max_entries=a.max_entries, max_object=int(a.max_object_mb * 1048576),
                 default_ttl=a.default_ttl, min_ttl=a.min_ttl, max_ttl=a.max_ttl,
                 negative_ttl=a.negative_ttl, stale_if_error=a.stale_if_error,
                 retention=a.retention, upstream_timeout=a.timeout,
                 max_threads=a.max_threads, cache_cookies=a.cache_cookies,
                 admin_remote=a.admin_remote, blocklist_file=a.blocklist,
                 blocked=a.block, report_interval=a.report_interval)
    try:
        server, core = build_server(cfg)
    except OSError as exc:
        print(f"Cannot listen on {cfg.host}:{cfg.port}: {exc}\n"
              f"Is another proxy already running? Try a different --port, or find the old one with: "
              f"lsof -i :{cfg.port}", file=sys.stderr)
        return 1
    core.start_background()
    print(f"IntelliProxy listening on {cfg.host}:{server.server_address[1]}  "
          f"(cache {a.cache_mb:g} MB, {len(core.blocklist.rules)} block rules)", flush=True)
    print(f"Dashboard: http://{cfg.host}:{server.server_address[1]}/", flush=True)
    print(f"Report:    http://{cfg.host}:{server.server_address[1]}/__proxy/analysis?format=text", flush=True)
    def _graceful(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _graceful)      # works even if the shell set SIGINT to ignored
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _graceful)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        core.stop()
        server.server_close()
        print("\n" + render_report(core.stats.analysis(core.cache)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
