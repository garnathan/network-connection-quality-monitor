#!/usr/bin/env python3
"""
webmon.py — always-on broadband monitor with a web UI.

A long-running daemon (built for a Raspberry Pi) that continuously probes the
connection the same way the interactive CLI does, stores every sample in SQLite
so history survives reboots, and serves a self-contained modern dashboard on the
local network. Designed to run 24/7 without getting in the way of work or video
calls.

What it measures (identical probes to monitor.py, via probes):
  latency / jitter / packet loss   (ping)
  DNS resolve time                 (dig)
  page TTFB + total                (curl)
  download / upload throughput     (Cloudflare)  -- bandwidth-guarded
  bufferbloat                      (loaded latency during a transfer)
  link type                        (wired vs wireless egress interface)

Bandwidth safety: the cheap probes (latency/DNS/TTFB/link) run often and cost
almost nothing. The expensive throughput test runs infrequently AND is skipped
whenever the link is already busy, so it never fights an active call or stream.
Every test traverses whatever interface the host is routing over, so on the Pi
this shows the real state of the wired (or wireless) uplink.

Web UI:
  * a card per metric with current value + avg/p95/max + a history chart
  * a global window selector: 1h / 6h / 1d / 1w / 1mo
  * a wired-vs-wireless link timeline
  * verdict banner, recent-events feed, live data-usage counter
  * HTTP Basic Auth (credentials from ICCD_USER/ICCD_PASS; a random password is
    generated and logged if none is set — nothing is shipped in the repo)

Zero third-party dependencies — pure Python 3 stdlib.

Usage:
    ./webmon.py                           # 0.0.0.0:8080, db in ./data/
    ./webmon.py --port 8080 --db /path/to/webmon.db
    ICCD_USER=admin ICCD_PASS=... ./webmon.py
"""

import argparse
import base64
import hmac
import json
import os
import re
import signal
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Locate the shared backend `probes.py`: a sibling once deployed (the deploy
# script bundles it next to webmon.py on the Pi), or one level up in the repo
# layout (repo-root probes.py, this file in webui/).
_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, os.path.dirname(_here)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import probes as P  # noqa: E402

# --------------------------------------------------------------------------- #
# Metric catalogue (stable names shared by the store and the web API)
# --------------------------------------------------------------------------- #

# name, threshold-key, unit, label, decimals
METRICS = [
    ("latency", "latency_ms", "ms", "Latency", 0),
    ("jitter", "jitter_ms", "ms", "Jitter", 1),
    ("loss", "loss_pct", "%", "Packet loss", 1),
    ("dns", "dns_ms", "ms", "DNS resolve", 0),
    ("ttfb", "ttfb_ms", "ms", "Page load (TTFB)", 0),
    ("download", "download_mbps", "Mbps", "Download", 1),
    ("upload", "upload_mbps", "Mbps", "Upload", 1),
    ("bufferbloat", "bufferbloat_ms", "ms", "Bufferbloat", 0),
]
CHARTED = [m[0] for m in METRICS]           # metrics with a numeric chart
ALL_SAMPLE_METRICS = CHARTED + ["http_total", "link"]


# --------------------------------------------------------------------------- #
# systemd integration (sd_notify + watchdog) — pure stdlib, no python-systemd
# --------------------------------------------------------------------------- #

def sd_notify(state):
    """Send a datagram to systemd's notify socket. No-op when not run under
    systemd (NOTIFY_SOCKET unset), so the daemon still runs standalone."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr[0] == "@":                       # Linux abstract namespace
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode("utf-8"))
    except OSError:
        pass


def _http_self_ok(cfg):
    """Loopback GET so the watchdog also verifies the dashboard is actually
    serving — a wedged accept loop with live workers must NOT read as healthy."""
    import http.client
    try:
        auth = base64.b64encode(f"{cfg.user}:{cfg.password}".encode()).decode()
        conn = http.client.HTTPConnection("127.0.0.1", cfg.port, timeout=5)
        conn.request("GET", "/api/summary?window=300",
                     headers={"Authorization": "Basic " + auth})
        r = conn.getresponse()
        r.read()
        conn.close()
        return r.status == 200
    except OSError:
        return False


def watchdog_loop(daemon, live, cfg, stop):
    """Pet systemd's watchdog ONLY while the daemon is genuinely healthy, so a
    hung (not just crashed) process is restarted too. Health =
      * every worker thread alive,
      * a fresh sample was written recently (MONOTONIC clock, so a post-boot NTP
        step cannot fake staleness), AND
      * the HTTP dashboard actually answers a loopback request.
    If unhealthy we stop petting; systemd restarts us when WatchdogSec elapses.

    WATCHDOG_USEC / WATCHDOG_PID are set by systemd only when WatchdogSec= is
    configured; absent (or wrong PID) => watchdog disabled, this is a no-op."""
    usec = os.environ.get("WATCHDOG_USEC")
    wpid = os.environ.get("WATCHDOG_PID")
    if not usec or (wpid and wpid != str(os.getpid())):
        return
    period_s = int(usec) / 1e6
    # Stop petting well BEFORE WatchdogSec so a real hang is caught within ~one
    # WatchdogSec, not two; and pet more often than we go stale.
    stale_after = min(3 * daemon.c.latency_interval + 10, period_s * 0.5)
    ping_every = max(1.0, min(period_s / 3.0, stale_after * 0.6))
    started = time.monotonic()
    while not stop.wait(ping_every):
        now = time.monotonic()
        alive = all(t.is_alive() for t in daemon.threads)
        with live.lock:
            last_mono = live.last_write_mono
        warming = (now - started) < stale_after            # grace for first samples
        fresh = warming or (last_mono and (now - last_mono) < stale_after)
        if alive and fresh and _http_self_ok(cfg):
            sd_notify("WATCHDOG=1")


# --------------------------------------------------------------------------- #
# SQLite store
# --------------------------------------------------------------------------- #

class Store:
    """Thread-safe sample store. One serialised writer connection (WAL), plus
    short-lived read connections per request (WAL lets readers run lock-free)."""

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._wlock = threading.Lock()
        self._w = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._w.execute("PRAGMA journal_mode=WAL")
        self._w.execute("PRAGMA synchronous=NORMAL")
        self._w.executescript("""
            CREATE TABLE IF NOT EXISTS samples(
                ts REAL NOT NULL, metric TEXT NOT NULL, value REAL);
            CREATE INDEX IF NOT EXISTS idx_samples ON samples(metric, ts);
            CREATE TABLE IF NOT EXISTS events(
                ts REAL NOT NULL, severity TEXT, msg TEXT);
            CREATE INDEX IF NOT EXISTS idx_events ON events(ts);
            CREATE TABLE IF NOT EXISTS data_usage(
                day TEXT PRIMARY KEY, down_bytes INTEGER DEFAULT 0,
                up_bytes INTEGER DEFAULT 0);
        """)
        self._w.commit()

    def read(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def write_sample(self, metric, value, ts):
        with self._wlock:
            self._w.execute("INSERT INTO samples(ts, metric, value) VALUES(?,?,?)",
                            (ts, metric, value))
            self._w.commit()

    def write_event(self, severity, msg, ts):
        with self._wlock:
            self._w.execute("INSERT INTO events(ts, severity, msg) VALUES(?,?,?)",
                            (ts, severity, msg))
            self._w.commit()

    def add_usage(self, day, down, up):
        with self._wlock:
            self._w.execute(
                "INSERT INTO data_usage(day, down_bytes, up_bytes) VALUES(?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET "
                "down_bytes=down_bytes+?, up_bytes=up_bytes+?",
                (day, down, up, down, up))
            self._w.commit()

    def prune(self, older_than_ts):
        with self._wlock:
            self._w.execute("DELETE FROM samples WHERE ts < ?", (older_than_ts,))
            self._w.execute("DELETE FROM events WHERE ts < ?", (older_than_ts,))
            self._w.commit()


def query_series(conn, metric, since, until, target=240):
    span = max(1.0, until - since)
    bsize = max(1, int(span / target))
    rows = conn.execute(
        "SELECT CAST(ts/? AS INT)*? AS b, AVG(value), MIN(value), MAX(value) "
        "FROM samples WHERE metric=? AND ts>=? AND ts<=? AND value IS NOT NULL "
        "GROUP BY CAST(ts/? AS INT) ORDER BY b",
        (bsize, bsize, metric, since, until, bsize)).fetchall()
    return [[round(r[0], 1), r[1], r[2], r[3]] for r in rows]


def latest_values(conn, metrics):
    """Most recent non-null (value, ts) per metric — used to seed the live
    'current' values from persisted history after a restart."""
    out = {}
    for m in metrics:
        row = conn.execute(
            "SELECT value, ts FROM samples WHERE metric=? AND value IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1", (m,)).fetchone()
        if row:
            out[m] = (row[0], row[1])
    return out


def query_values(conn, metric, since, until=None):
    if until is None:
        until = time.time()
    rows = conn.execute(
        "SELECT value FROM samples WHERE metric=? AND ts>=? AND ts<=? "
        "AND value IS NOT NULL", (metric, since, until)).fetchall()
    return [r[0] for r in rows]


def stats_of(vals):
    if not vals:
        return None
    vals_sorted = sorted(vals)
    n = len(vals_sorted)
    mean = sum(vals_sorted) / n
    return {
        "n": n, "min": vals_sorted[0], "max": vals_sorted[-1],
        "avg": mean, "median": vals_sorted[n // 2],
        "p95": P.percentile(vals_sorted, 95),
    }


# --------------------------------------------------------------------------- #
# Live snapshot (current values, for a fast /api/summary)
# --------------------------------------------------------------------------- #

class Live:
    def __init__(self):
        self.lock = threading.Lock()
        self.start = time.time()
        self.last = {}                       # metric -> (value, ts)
        self.link = {"iface": None, "type": "unknown", "ts": 0}
        self.errors = {}
        self.tp_running = False
        self.last_tp = None
        # Monotonic stamp of the most recent write of any metric — used by the
        # watchdog so a post-boot NTP wall-clock step can't fake staleness.
        self.last_write_mono = 0.0

    def set(self, metric, value, ts):
        with self.lock:
            self.last[metric] = (value, ts)
            self.last_write_mono = time.monotonic()

    def set_link(self, iface, typ, ts):
        with self.lock:
            self.link = {"iface": iface, "type": typ, "ts": ts}

    def set_error(self, probe, err):
        with self.lock:
            if err:
                self.errors[probe] = err
            else:
                self.errors.pop(probe, None)


# --------------------------------------------------------------------------- #
# Monitor daemon
# --------------------------------------------------------------------------- #

class Daemon:
    def __init__(self, store, live, cfg, stop):
        self.s = store
        self.live = live
        self.c = cfg
        self.stop = stop
        self.dns_i = 0
        self.http_i = 0
        fd, self.payload = tempfile.mkstemp(prefix="iccd-up-", suffix=".bin")
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(int(cfg.upload_mb * 1_000_000)))

    def cleanup(self):
        if self.payload and os.path.exists(self.payload):
            os.unlink(self.payload)

    def _event(self, sev, msg):
        self.s.write_event(sev, msg, time.time())

    def _safe_event(self, sev, msg):
        try:
            self._event(sev, msg)
        except Exception:            # noqa: BLE001 - logging must never kill a worker
            pass

    def _record(self, metric, value, ts=None):
        ts = ts or time.time()
        self.s.write_sample(metric, value, ts)
        self.live.set(metric, value, ts)

    def _loop(self, name, interval, tick):
        """Run tick() forever, self-healing: a transient probe or SQLite error
        (e.g. a momentarily full/busy SD card) is logged and RETRIED on the next
        interval rather than killing the worker thread — a dead worker would
        otherwise trip the watchdog into a whole-process restart loop."""
        while not self.stop.is_set():
            try:
                tick()
            except Exception as e:   # noqa: BLE001 - a worker must survive blips
                self.live.set_error(name, str(e))
                self._safe_event("warn", f"{name} worker error: {e}")
            self.stop.wait(interval)

    # -- latency / jitter / loss --
    def latency_worker(self):
        def tick():
            r = P.probe_ping(self.c.anchor, count=5, interval=0.5)
            ts = time.time()
            if r["ok"]:
                self.live.set_error("latency", None)
                self._record("latency", r["avg"], ts)
                self._record("jitter", r["jitter"], ts)
                self._record("loss", r["loss"], ts)
                if r["loss"] >= P.THRESHOLDS["loss_pct"][1]:
                    self._event("bad", f"packet loss {r['loss']:.0f}% to {self.c.anchor}")
            else:
                self.live.set_error("latency", r["error"])
                self._record("latency", None, ts)
                self._record("jitter", None, ts)
                self._record("loss", 100.0, ts)
                self._event("bad", f"no ping reply from {self.c.anchor} ({r['error']})")
        self._loop("latency", self.c.latency_interval, tick)

    # -- DNS --
    def dns_worker(self):
        def tick():
            domain = P.DNS_DOMAINS[self.dns_i % len(P.DNS_DOMAINS)]
            self.dns_i += 1
            r = P.probe_dns(domain)
            self.live.set_error("dns", r["error"])
            self._record("dns", r["ms"])
            if r["ms"] and r["ms"] >= P.THRESHOLDS["dns_ms"][1]:
                self._event("warn", f"slow DNS {r['ms']:.0f} ms for {domain}")
        self._loop("dns", self.c.dns_interval, tick)

    # -- HTTP page load / TTFB --
    def http_worker(self):
        def tick():
            url = P.HTTP_TARGETS[self.http_i % len(P.HTTP_TARGETS)]
            self.http_i += 1
            host = re.sub(r"^https?://", "", url).split("/")[0]
            r = P.probe_http(url)
            self.live.set_error("http", r["error"])
            self._record("ttfb", r["ttfb_ms"])
            self._record("http_total", r["total_ms"])
            if r["ttfb_ms"] and r["ttfb_ms"] >= P.THRESHOLDS["ttfb_ms"][1]:
                self._event("warn", f"slow page load: TTFB {r['ttfb_ms']:.0f} ms for {host}")
        self._loop("http", self.c.http_interval, tick)

    # -- link type (wired vs wireless) --
    def link_worker(self):
        state = {"last": None}

        def tick():
            link = P.detect_link(self.c.anchor)
            ts = time.time()
            self.live.set_link(link["iface"], link["type"], ts)
            self._record("link", P.LINK_VALUE.get(link["type"], 0.0), ts)
            if state["last"] is not None and link["type"] != state["last"]:
                self._event("info", f"link changed: {state['last']} -> {link['type']}"
                            + (f" ({link['iface']})" if link["iface"] else ""))
            state["last"] = link["type"]
        self._loop("link", self.c.link_interval, tick)

    # -- throughput + bufferbloat (bandwidth-guarded) --
    def _link_busy(self):
        """Is the uplink already in use? If so, skip the saturating test so we
        never interfere with an active call/stream (and the reading would be
        contaminated anyway)."""
        with closing(self.s.read()) as conn:
            baseline = P.percentile(
                query_values(conn, "latency", time.time() - 1800), 50)
        r = P.probe_ping(self.c.anchor, count=3, interval=0.4)
        if not r["ok"]:
            return True, "no route"
        if r["loss"] >= 2:
            return True, f"loss {r['loss']:.0f}%"
        if baseline and r["avg"] > max(baseline * 1.8, baseline + 40):
            return True, f"latency {r['avg']:.0f}>{baseline:.0f} ms (in use)"
        return False, ""

    def _throughput_tick(self):
        busy, why = self._link_busy()
        if busy:
            # Skip WITHOUT recording None — the card keeps its last real reading
            # instead of blanking to "—" whenever the link happens to be in use.
            self._event("info", f"throughput test skipped — link busy ({why})")
            return

        with self.live.lock:
            self.live.tp_running = True
        try:
            with closing(self.s.read()) as conn:
                baseline = P.percentile(
                    query_values(conn, "latency", time.time() - 600), 50)
            dl = P.probe_transfer_loaded(self.c.anchor, "download",
                                         int(self.c.download_mb * 1_000_000),
                                         self.payload, self.c.throughput_max_time)
            ul = {"mbps": None, "loaded_avg": None, "bytes": 0}
            if not self.stop.is_set():
                ul = P.probe_transfer_loaded(self.c.anchor, "upload",
                                             int(self.c.upload_mb * 1_000_000),
                                             self.payload, self.c.throughput_max_time)
            loaded = [v for v in (dl["loaded_avg"], ul["loaded_avg"]) if v is not None]
            bloat = max(max(loaded) - baseline, 0.0) if loaded and baseline else None

            ts = time.time()
            self._record("download", dl["mbps"], ts)
            self._record("upload", ul["mbps"], ts)
            self._record("bufferbloat", bloat, ts)
            self.s.add_usage(datetime.now().strftime("%Y-%m-%d"),
                             dl["bytes"], ul["bytes"])

            exp = self.c.download_expected_mbps
            if dl["mbps"] is not None and P.rating(dl["mbps"], "download_mbps") == "bad":
                pct = (100 * dl["mbps"] / exp) if exp else 0
                self._event("bad", f"download {dl['mbps']:.0f} Mbps — only {pct:.0f}% "
                            f"of your {exp:.0f} Mbps plan")
            if bloat is not None:
                rt = P.rating(bloat, "bufferbloat_ms")
                if rt == "bad":
                    self._event("bad", f"bufferbloat +{bloat:.0f} ms under load (video calls will lag)")
                elif rt == "warn":
                    self._event("warn", f"bufferbloat +{bloat:.0f} ms under load")
        finally:
            with self.live.lock:
                self.live.tp_running = False
                self.live.last_tp = time.time()

    def throughput_worker(self):
        # Small initial delay so the cheap probes populate the dashboard first.
        if self.stop.wait(5):
            return
        self._loop("throughput", self.c.throughput_interval, self._throughput_tick)

    # -- retention --
    def prune_worker(self):
        self._loop("prune", 6 * 3600,
                   lambda: self.s.prune(time.time() - self.c.retention_days * 86400))

    def start(self):
        names = ["latency_worker", "dns_worker", "http_worker", "link_worker",
                 "throughput_worker", "prune_worker"]
        self.threads = [threading.Thread(target=getattr(self, n), daemon=True,
                                         name=n) for n in names]
        for t in self.threads:
            t.start()

    def join(self, timeout):
        for t in self.threads:
            t.join(timeout=timeout)


# --------------------------------------------------------------------------- #
# API payload builders
# --------------------------------------------------------------------------- #

def build_summary(store, live, cfg, window):
    now = time.time()
    with live.lock:
        last = dict(live.last)
        link = dict(live.link)
        start = live.start
        errors = dict(live.errors)
        tp_running = live.tp_running
        last_tp = live.last_tp
    with closing(store.read()) as conn:
        # verdict over the last hour (cheap, meaningful "recent" health)
        worst = 0
        since = now - 3600
        for name, key, *_ in METRICS:
            vals = query_values(conn, name, since)
            if not vals:
                continue
            hib = P.THRESHOLDS[key][2]
            v = (sum(vals) / len(vals)) if hib else P.percentile(vals, 95)
            worst = max(worst, {"good": 0, "warn": 1, "bad": 2}[P.rating(v, key)])
        events = [dict(ts=r["ts"], severity=r["severity"], msg=r["msg"])
                  for r in conn.execute(
                      "SELECT ts, severity, msg FROM events ORDER BY ts DESC LIMIT 50")]
        day = datetime.now().strftime("%Y-%m-%d")
        month = datetime.now().strftime("%Y-%m")
        du = conn.execute("SELECT down_bytes, up_bytes FROM data_usage WHERE day=?",
                          (day,)).fetchone()
        mu = conn.execute("SELECT COALESCE(SUM(down_bytes),0), COALESCE(SUM(up_bytes),0) "
                          "FROM data_usage WHERE day LIKE ?", (month + "%",)).fetchone()

    expected = {"download": cfg.download_expected_mbps,
                "upload": cfg.upload_expected_mbps}
    metrics = {}
    for name, key, unit, label, dec in METRICS:
        v, ts = last.get(name, (None, None))
        metrics[name] = {
            "label": label, "unit": unit, "decimals": dec, "key": key,
            "value": v, "ts": ts, "rating": P.rating(v, key),
            "good": P.THRESHOLDS[key][0], "warn": P.THRESHOLDS[key][1],
            "higher_better": P.THRESHOLDS[key][2],
            "expected": expected.get(name),
            "error": errors.get("latency" if name in ("latency", "jitter", "loss")
                                 else "http" if name in ("ttfb",) else name),
        }
    verdict = ["HEALTHY", "DEGRADED", "PROBLEMS"][worst]
    return {
        "generated": now, "uptime": now - start, "verdict": verdict,
        "window": window,
        "link": {"iface": link["iface"], "type": link["type"], "ts": link["ts"]},
        "throughput_running": tp_running, "last_throughput": last_tp,
        "data": {"today": {"down": (du["down_bytes"] if du else 0),
                           "up": (du["up_bytes"] if du else 0)},
                 "month": {"down": mu[0], "up": mu[1]}},
        "metrics": metrics, "events": events,
        "config": {"latency_interval": cfg.latency_interval,
                   "dns_interval": cfg.dns_interval,
                   "http_interval": cfg.http_interval,
                   "throughput_interval": cfg.throughput_interval,
                   "download_mb": cfg.download_mb, "upload_mb": cfg.upload_mb,
                   "anchor": cfg.anchor, "retention_days": cfg.retention_days},
    }


def build_history(store, window):
    now = time.time()
    since = now - window
    out = {"window": window, "generated": now, "series": {}, "stats": {}}
    with closing(store.read()) as conn:
        for name, key, unit, label, dec in METRICS:
            out["series"][name] = query_series(conn, name, since, now)
            out["stats"][name] = stats_of(query_values(conn, name, since))
        out["link"] = query_series(conn, "link", since, now)
    return out


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "webmon"
    store = None
    live = None
    cfg = None

    def log_message(self, *a):
        pass  # quiet

    def _auth_ok(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return (hmac.compare_digest(user, self.cfg.user)
                and hmac.compare_digest(pw, self.cfg.password))

    def _need_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Broadband Monitor"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Authentication required")

    def _json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _window(self):
        m = re.search(r"[?&]window=(\d+)", self.path)
        w = int(m.group(1)) if m else 3600
        return max(300, min(w, 40 * 86400))

    def do_GET(self):
        if not self._auth_ok():
            return self._need_auth()
        path = self.path.split("?", 1)[0]
        try:
            if path == "/" or path == "/index.html":
                return self._html(HTML_PAGE)
            if path == "/api/summary":
                return self._json(build_summary(self.store, self.live, self.cfg,
                                                self._window()))
            if path == "/api/history":
                return self._json(build_history(self.store, self._window()))
        except BrokenPipeError:
            return
        except Exception as e:  # noqa: BLE001
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"error: {e}".encode())
            return
        self.send_response(404)
        self.end_headers()


# --------------------------------------------------------------------------- #
# Config & main
# --------------------------------------------------------------------------- #

def parse_args(argv):
    def envd(k, d):
        return os.environ.get(k, d)
    p = argparse.ArgumentParser(
        description="Always-on broadband monitor with a web UI.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    default_db = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "webmon.db")
    p.add_argument("--host", default=envd("ICCD_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(envd("ICCD_PORT", "8080")))
    p.add_argument("--db", default=envd("ICCD_DB", default_db))
    p.add_argument("--user", default=envd("ICCD_USER", "admin"))
    p.add_argument("--password", default=envd("ICCD_PASS", ""),
                   help="dashboard password; if unset, a random one is "
                        "generated at startup and logged (set ICCD_PASS for a "
                        "stable login — see webui/webmon.env.example)")
    p.add_argument("--anchor", default=envd("ICCD_ANCHOR", P.DEFAULT_ANCHOR))
    p.add_argument("--latency-interval", type=float, default=30.0)
    p.add_argument("--dns-interval", type=float, default=60.0)
    p.add_argument("--http-interval", type=float, default=120.0)
    p.add_argument("--link-interval", type=float, default=20.0)
    p.add_argument("--throughput-interval", type=float,
                   default=float(envd("ICCD_TP_INTERVAL", "1800")),
                   help="seconds between (bandwidth-guarded) throughput tests")
    p.add_argument("--download-mb", type=float, default=10.0)
    p.add_argument("--upload-mb", type=float, default=4.0)
    p.add_argument("--download-expected-mbps", type=float,
                   default=float(envd("ICCD_DOWN_EXPECTED_MBPS",
                                      str(P.DEFAULT_DOWN_EXPECTED_MBPS))),
                   help="plan's download line rate; ratings are judged vs this")
    p.add_argument("--upload-expected-mbps", type=float,
                   default=float(envd("ICCD_UP_EXPECTED_MBPS",
                                      str(P.DEFAULT_UP_EXPECTED_MBPS))),
                   help="plan's upload line rate; ratings are judged vs this")
    p.add_argument("--throughput-max-time", type=int, default=10)
    p.add_argument("--retention-days", type=int, default=40)
    return p.parse_args(argv)


def main(argv):
    cfg = parse_args(argv)
    # No credential is shipped in the repo. If none is configured, generate a
    # random one at startup and log it (rather than fall back to a known default).
    if not cfg.password:
        import secrets
        cfg.password = secrets.token_urlsafe(9)
        print("  WARNING: no ICCD_PASS set — generated a temporary dashboard")
        print(f"           password: {cfg.password}")
        print("           set ICCD_USER/ICCD_PASS for a stable login "
              "(see webui/webmon.env.example)")
    # Judge throughput relative to the plan's line rate (set before any probe
    # thread reads THRESHOLDS).
    P.configure_expected(cfg.download_expected_mbps, cfg.upload_expected_mbps)

    store = Store(cfg.db)
    live = Live()
    # Seed 'current' values from persisted history so cards show the last real
    # reading immediately after a restart (not a blank "—" until the next probe,
    # which for throughput can be up to throughput_interval away).
    with closing(store.read()) as conn:
        for m, (v, ts) in latest_values(conn, CHARTED).items():
            live.last[m] = (v, ts)
    stop = threading.Event()

    daemon = Daemon(store, live, cfg, stop)
    daemon.start()

    Handler.store = store
    Handler.live = live
    Handler.cfg = cfg
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    httpd.daemon_threads = True

    def shutdown(*_):
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # systemd: announce readiness (socket is already bound) + start the
    # health-gated watchdog petter.
    sd_notify("READY=1")
    wd = threading.Thread(target=watchdog_loop, args=(daemon, live, cfg, stop),
                          daemon=True, name="watchdog")
    wd.start()

    print(f"webmon on http://{cfg.host}:{cfg.port}  (user={cfg.user})")
    print(f"  db={cfg.db}  anchor={cfg.anchor}")
    print(f"  plan: {cfg.download_expected_mbps:.0f} down / "
          f"{cfg.upload_expected_mbps:.0f} up Mbps  (good >= 70%, warn >= 40%)")
    print(f"  throughput every {cfg.throughput_interval:.0f}s "
          f"(<= {cfg.download_mb}+{cfg.upload_mb} MB, skipped when link busy)")
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        # Short join: workers are daemon threads and exit on `stop`; a probe
        # mid-flight is abandoned. Keep total shutdown well under TimeoutStopSec.
        daemon.join(timeout=4)
        daemon.cleanup()
    return 0


# --------------------------------------------------------------------------- #
# Web UI (self-contained: no external CSS/JS/CDN — works even with no WAN)
# --------------------------------------------------------------------------- #

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Internet Monitor</title>
<style>
  :root{
    --bg:#0b0f14; --panel:#141a22; --panel2:#0f151c; --border:#243040;
    --text:#e6edf3; --muted:#8b97a6; --accent:#58a6ff;
    --good:#3fb950; --warn:#e3b341; --bad:#f85149; --none:#6e7b8a;
  }
  *{box-sizing:border-box}
  body{margin:0;background:linear-gradient(180deg,#0b0f14,#0a0d12);
    color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased}
  .num{font-variant-numeric:tabular-nums}
  header{position:sticky;top:0;z-index:5;backdrop-filter:blur(8px);
    background:rgba(11,15,20,.82);border-bottom:1px solid var(--border);
    padding:14px 20px;display:flex;flex-wrap:wrap;gap:14px 20px;align-items:center}
  header h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}
  header .sub{color:var(--muted);font-size:12px}
  .spacer{flex:1}
  .pill{display:inline-flex;align-items:center;gap:7px;padding:6px 12px;border-radius:999px;
    font-weight:650;font-size:12.5px;border:1px solid var(--border);background:var(--panel)}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--none)}
  .pill.good{color:var(--good)} .pill.good .dot{background:var(--good);box-shadow:0 0 8px var(--good)}
  .pill.warn{color:var(--warn)} .pill.warn .dot{background:var(--warn);box-shadow:0 0 8px var(--warn)}
  .pill.bad{color:var(--bad)}   .pill.bad .dot{background:var(--bad);box-shadow:0 0 8px var(--bad);
    animation:pulse 1.6s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .chip{color:var(--muted);font-size:12px;display:inline-flex;gap:6px;align-items:center}
  .chip b{color:var(--text);font-weight:600}
  .seg{display:inline-flex;border:1px solid var(--border);border-radius:10px;overflow:hidden}
  .seg button{background:transparent;color:var(--muted);border:0;padding:7px 12px;
    font:inherit;font-size:12.5px;cursor:pointer;font-weight:600}
  .seg button:hover{background:var(--panel)}
  .seg button.on{background:var(--accent);color:#04121f}
  main{padding:20px;max-width:1500px;margin:0 auto}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel2));
    border:1px solid var(--border);border-left:4px solid var(--none);border-radius:14px;
    padding:15px 16px 10px;box-shadow:0 1px 0 rgba(255,255,255,.02) inset;
    transition:border-color .3s,box-shadow .3s,background .3s}
  /* status stripe on the card edge; the VALUE carries the colour, the graph stays neutral */
  .card.good{border-left-color:var(--good)}
  .card.warn{border-left-color:var(--warn)}
  .card.bad{border-left-color:var(--bad)}
  .card .top{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
  .card .name{color:var(--muted);font-size:12.5px;font-weight:600;text-transform:uppercase;
    letter-spacing:.5px}
  .card .rate{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;border:1px solid var(--border)}
  .rate.good{color:var(--good)} .rate.warn{color:var(--warn)} .rate.bad{color:var(--bad)} .rate.none{color:var(--none)}
  .card .val{font-size:34px;font-weight:700;margin:6px 0 2px;letter-spacing:-.5px}
  .card .val .u{font-size:15px;color:var(--muted);font-weight:600;margin-left:4px}
  .val.good{color:var(--good)} .val.warn{color:var(--warn)} .val.bad{color:var(--bad)} .val.none{color:var(--muted)}
  .card .mini{color:var(--muted);font-size:12px;display:flex;gap:14px;margin-bottom:8px}
  .card .mini b{color:var(--text);font-weight:600}
  .card svg{display:block;width:100%;height:88px}
  .card .xr{display:flex;justify-content:space-between;color:var(--muted);font-size:10.5px;margin-top:2px}
  .card.err{border-color:var(--bad)}
  .linkband{width:100%;height:40px;border-radius:8px;overflow:hidden;border:1px solid var(--border);margin-top:6px}
  .legend{display:flex;gap:14px;color:var(--muted);font-size:11.5px;margin-top:8px;flex-wrap:wrap}
  .legend i{width:10px;height:10px;border-radius:3px;display:inline-block;margin-right:5px;vertical-align:middle}
  .events{margin-top:18px;background:linear-gradient(180deg,var(--panel),var(--panel2));
    border:1px solid var(--border);border-radius:14px;padding:8px 4px}
  .events h2{font-size:12.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;
    margin:8px 14px}
  .ev{display:flex;gap:12px;padding:6px 14px;border-top:1px solid rgba(255,255,255,.03);font-size:13px}
  .ev time{color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
  .ev.bad .msg{color:var(--bad)} .ev.warn .msg{color:var(--warn)} .ev.info .msg{color:var(--muted)}
  footer{color:var(--muted);font-size:11.5px;text-align:center;padding:18px}
  .off{opacity:.45}
</style>
</head>
<body>
<header>
  <h1>Internet Monitor</h1>
  <span class="sub" id="host"></span>
  <span class="spacer"></span>
  <span class="pill" id="link"><span class="dot"></span><span id="linktext">link…</span></span>
  <span class="chip">up <b id="uptime">—</b></span>
  <span class="chip">data today <b id="dtoday">—</b></span>
  <span class="chip">this month <b id="dmonth">—</b></span>
  <span class="pill" id="verdict"><span class="dot"></span><span id="verdicttext">…</span></span>
</header>
<main>
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap">
    <span class="chip">History window</span>
    <div class="seg" id="win">
      <button data-w="3600" class="on">1H</button>
      <button data-w="21600">6H</button>
      <button data-w="86400">1D</button>
      <button data-w="604800">1W</button>
      <button data-w="2592000">1M</button>
    </div>
    <span class="spacer"></span>
    <span class="chip" id="updated"></span>
  </div>

  <div class="grid" id="cards"></div>

  <div class="card" style="margin-top:16px">
    <div class="top"><span class="name">Link type over time — wired vs wireless</span>
      <span class="rate" id="linkrate">—</span></div>
    <div class="val" id="linknow" style="font-size:22px">—</div>
    <div class="linkband"><svg id="linksvg" viewBox="0 0 1000 40" preserveAspectRatio="none"
      style="width:100%;height:40px"></svg></div>
    <div class="xr"><span id="linkx0"></span><span id="linkx1"></span></div>
    <div class="legend">
      <span><i style="background:var(--good)"></i>wired</span>
      <span><i style="background:var(--warn)"></i>wireless</span>
      <span><i style="background:var(--bad)"></i>down</span>
    </div>
  </div>

  <div class="events">
    <h2>Recent events</h2>
    <div id="events"></div>
  </div>
</main>
<footer id="foot"></footer>

<script>
"use strict";
const $ = s => document.querySelector(s);
let WINDOW = 3600;        // default to the last hour
let META = null;          // last summary metrics meta (labels/units/thresholds)

const RATE_CLASS = r => (["good","warn","bad"].includes(r) ? r : "none");
function fmt(v, dec){
  if(v===null||v===undefined||Number.isNaN(v)) return "—";
  return Number(v).toLocaleString(undefined,{minimumFractionDigits:dec,maximumFractionDigits:dec});
}
function dur(s){ s=Math.floor(s); const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),
  m=Math.floor(s%3600/60); if(d) return d+"d "+h+"h"; if(h) return h+"h "+m+"m";
  return m+"m "+(s%60)+"s"; }
function mb(n){ n=n||0; if(n>=1e9) return (n/1e9).toFixed(2)+" GB"; return (n/1e6).toFixed(1)+" MB"; }
function tlabel(ts){ const d=new Date(ts*1000);
  if(WINDOW<=86400) return d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
  return d.toLocaleDateString([], {month:"short",day:"numeric"})+" "+
    d.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"}); }

// ---- SVG line+band chart -------------------------------------------------
function chart(points, o){
  // points: [[t,avg,min,max]]
  const W=1000, H=200, padT=12, padB=10, padL=2, padR=2;
  if(!points || points.length===0)
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><text x="12" y="26"
      fill="var(--muted)" font-size="16">no data yet</text></svg>`;
  let lo=Infinity, hi=-Infinity;
  for(const p of points){ lo=Math.min(lo,p[2]??p[1]); hi=Math.max(hi,p[3]??p[1]); }
  if(o.redline!==null && o.redline!==undefined){ lo=Math.min(lo,o.redline); hi=Math.max(hi,o.redline); }
  if(lo===hi){ hi=lo+1; lo=Math.max(0,lo-1); }
  const pad=(hi-lo)*0.12; hi+=pad; lo-=pad; if(o.nonneg&&lo<0)lo=0;
  const t0=points[0][0], t1=points[points.length-1][0], tspan=Math.max(1,t1-t0);
  const x=t=>padL+((t-t0)/tspan)*(W-padL-padR);
  const y=v=>padT+(1-(v-lo)/(hi-lo))*(H-padT-padB);
  let top="",bot="";
  points.forEach((p,i)=>{ top+=(i?"L":"M")+x(p[0]).toFixed(1)+" "+y(p[3]??p[1]).toFixed(1)+" "; });
  for(let i=points.length-1;i>=0;i--){ const p=points[i];
    bot+="L"+x(p[0]).toFixed(1)+" "+y(p[2]??p[1]).toFixed(1)+" "; }
  const band=`<path d="${top}${bot}Z" fill="${o.color}" opacity="0.13"/>`;
  let line=""; points.forEach((p,i)=>{ line+=(i?"L":"M")+x(p[0]).toFixed(1)+" "+y(p[1]).toFixed(1)+" "; });
  const path=`<path d="${line}" fill="none" stroke="${o.color}" stroke-width="2"
     vector-effect="non-scaling-stroke" stroke-linejoin="round"/>`;
  // gridlines + labels
  let grid="";
  for(let g=0;g<=2;g++){ const val=lo+(hi-lo)*(g/2); const yy=y(val).toFixed(1);
    grid+=`<line x1="0" x2="${W}" y1="${yy}" y2="${yy}" stroke="#ffffff" opacity="0.05"
      vector-effect="non-scaling-stroke"/>`;
    grid+=`<text x="6" y="${(+yy-3)}" fill="var(--muted)" font-size="11"
      vector-effect="non-scaling-stroke">${fmt(val,o.dec)}</text>`; }
  // Red annotation line: the threshold that puts this metric into the bad zone.
  let thr="";
  if(o.redline!==null && o.redline!==undefined){ const yy=y(o.redline).toFixed(1);
    thr=`<line x1="0" x2="${W}" y1="${yy}" y2="${yy}" stroke="var(--bad)" opacity="0.85"
      stroke-width="1.5" stroke-dasharray="6 4" vector-effect="non-scaling-stroke"/>`; }
  const last=points[points.length-1];
  const dot=`<circle cx="${x(last[0]).toFixed(1)}" cy="${y(last[1]).toFixed(1)}" r="3.2"
     fill="${o.color}" vector-effect="non-scaling-stroke"/>`;
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${grid}${band}${thr}${path}${dot}</svg>`;
}

function linkColor(v){ if(v>=0.5) return "var(--good)"; if(v>=-0.5) return "var(--warn)"; return "var(--bad)"; }
function linkChart(points){
  const svg=$("#linksvg"); svg.innerHTML="";
  if(!points||!points.length){ svg.innerHTML=`<text x="10" y="24" fill="#8b97a6">no data</text>`; return; }
  const t0=points[0][0], t1=points[points.length-1][0], tspan=Math.max(1,t1-t0), W=1000;
  let html="";
  for(let i=0;i<points.length;i++){
    const p=points[i], pn=points[i+1];
    const x0=((p[0]-t0)/tspan)*W;
    const x1=pn?((pn[0]-t0)/tspan)*W:W;
    html+=`<rect x="${x0.toFixed(1)}" y="0" width="${Math.max(0.5,x1-x0).toFixed(1)}"
      height="40" fill="${linkColor(p[1])}" opacity="0.85"/>`;
  }
  svg.innerHTML=html;
  $("#linkx0").textContent=tlabel(t0); $("#linkx1").textContent=tlabel(t1);
}

// ---- render summary (fast, every few seconds) ----------------------------
function renderSummary(s){
  META=s.metrics;
  $("#host").textContent = location.host + " · anchor " + s.config.anchor;
  const v=RATE_CLASS(({HEALTHY:"good",DEGRADED:"warn",PROBLEMS:"bad"})[s.verdict]);
  const vp=$("#verdict"); vp.className="pill "+v; $("#verdicttext").textContent=s.verdict;
  $("#uptime").textContent=dur(s.uptime);
  $("#dtoday").textContent="↓"+mb(s.data.today.down)+" ↑"+mb(s.data.today.up);
  $("#dmonth").textContent="↓"+mb(s.data.month.down)+" ↑"+mb(s.data.month.up);

  // link pill
  const lt=s.link.type||"unknown";
  const lc = lt==="wired"?"good":lt==="wireless"?"warn":lt==="down"?"bad":"none";
  const lp=$("#link"); lp.className="pill "+lc;
  $("#linktext").textContent = (lt==="wired"?"Wired":lt==="wireless"?"Wi-Fi":lt)
    + (s.link.iface?(" · "+s.link.iface):"");

  // cards: current value + rating (charts/stats come from history)
  ensureCards();
  for(const [name, m] of Object.entries(s.metrics)){
    const el=document.getElementById("card-"+name); if(!el) continue;
    const rc=RATE_CLASS(m.rating);
    el.className = "card " + rc + (m.error ? " err" : "");   // colour-code whole widget
    // title + unit come from the summary too, so cards are fully labelled even
    // if the (heavier) history poll is slow or fails.
    el.querySelector(".name").textContent = m.label;
    el.querySelector(".u").textContent = m.unit;
    el.querySelector(".val").className="val "+rc;
    el.querySelector(".valn").textContent=fmt(m.value, m.decimals);
    const r=el.querySelector(".rate"); r.className="rate "+rc; r.textContent=rc==="none"?"—":rc;
    const planEl=el.querySelector(".plan");
    planEl.textContent = m.expected ? ("plan "+fmt(m.expected,0)+" "+m.unit) : "";
    const noteEl=el.querySelector(".note"); noteEl.textContent = m.error? ("! "+m.error):"";
  }
  // events
  $("#events").innerHTML = s.events.map(e=>{
    const t=new Date(e.ts*1000).toLocaleString([], {month:"short",day:"numeric",
      hour:"2-digit",minute:"2-digit",second:"2-digit"});
    return `<div class="ev ${e.severity}"><time>${t}</time><span class="msg">${escapeHtml(e.msg)}</span></div>`;
  }).join("") || `<div class="ev info"><span class="msg">no events yet — quiet is good</span></div>`;
  $("#foot").textContent = "throughput every "+Math.round(s.config.throughput_interval/60)
    +" min (≤ "+s.config.download_mb+"+"+s.config.upload_mb+" MB, skipped when the link is busy) · "
    +"retention "+s.config.retention_days+" days · latency every "+s.config.latency_interval+"s";
  $("#updated").textContent="updated "+new Date(s.generated*1000).toLocaleTimeString();
}

function escapeHtml(x){ return (x||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function ensureCards(){
  if($("#cards").childElementCount) return;
  const order=["latency","jitter","loss","dns","ttfb","download","upload","bufferbloat"];
  $("#cards").innerHTML = order.map(name=>`
    <div class="card" id="card-${name}">
      <div class="top"><span class="name" data-name="${name}"></span><span class="rate none">—</span></div>
      <div class="val none"><span class="valn num">—</span><span class="u"></span></div>
      <div class="mini"><span>avg <b class="s-avg num">—</b></span>
        <span>p95 <b class="s-p95 num">—</b></span><span>max <b class="s-max num">—</b></span>
        <span class="plan" style="margin-left:auto;color:var(--muted)"></span></div>
      <div class="chartbox"></div>
      <div class="xr"><span class="x0"></span>
        <span class="thr" style="color:var(--bad);font-weight:600"></span>
        <span class="x1"></span></div>
      <div class="note" style="color:var(--bad);font-size:11.5px;min-height:14px"></div>
    </div>`).join("");
}

// ---- render history (charts + window stats) ------------------------------
function renderHistory(h){
  if(!META) return;
  for(const [name, m] of Object.entries(META)){
    const el=document.getElementById("card-"+name); if(!el) continue;
    el.querySelector(".name").textContent=m.label;
    el.querySelector(".u").textContent=m.unit;
    const pts=h.series[name]||[];
    const st=h.stats[name];
    el.querySelector(".s-avg").textContent = st?fmt(st.avg,m.decimals):"—";
    el.querySelector(".s-p95").textContent = st?fmt(st.p95,m.decimals):"—";
    el.querySelector(".s-max").textContent = st?fmt(st.max,m.decimals):"—";
    // The graph is a neutral colour on purpose — the VALUE carries the status
    // colour. The only coloured line on the chart is the red danger threshold.
    el.querySelector(".chartbox").innerHTML = chart(pts,
      {color:"var(--accent)", dec:m.decimals, redline:m.warn, nonneg:true});
    // caption for the red annotation line (the threshold into the bad zone)
    const thrEl=el.querySelector(".thr");
    if(m.warn!==null && m.warn!==undefined)
      thrEl.textContent = "red "+(m.higher_better?"below ":"above ")+fmt(m.warn,m.decimals)+" "+m.unit;
    if(pts.length){ el.querySelector(".x0").textContent=tlabel(pts[0][0]);
      el.querySelector(".x1").textContent=tlabel(pts[pts.length-1][0]); }
  }
  linkChart(h.link||[]);
  // link card summary text
  const lp=h.link||[]; if(lp.length){ const lastv=lp[lp.length-1][1];
    const lt=lastv>=0.5?"Wired":lastv>=-0.5?"Wi-Fi":"Down";
    $("#linknow").textContent="Currently: "+lt;
    const wired=lp.filter(p=>p[1]>=0.5).length, tot=lp.length;
    const pct=Math.round(100*wired/tot);
    const lr=$("#linkrate"); lr.textContent=pct+"% wired";
    lr.className="rate "+(pct>=80?"good":pct>=40?"warn":"bad"); }
}

// ---- polling -------------------------------------------------------------
async function loadSummary(){
  try{ const r=await fetch("/api/summary?window="+WINDOW,{cache:"no-store"});
    if(r.ok) renderSummary(await r.json()); }catch(e){}
}
async function loadHistory(){
  try{ const r=await fetch("/api/history?window="+WINDOW,{cache:"no-store"});
    if(r.ok) renderHistory(await r.json()); }catch(e){}
}
document.querySelectorAll("#win button").forEach(b=>b.addEventListener("click",()=>{
  document.querySelectorAll("#win button").forEach(x=>x.classList.remove("on"));
  b.classList.add("on"); WINDOW=+b.dataset.w; loadHistory();
}));
(async ()=>{ await loadSummary(); await loadHistory();
  setInterval(loadSummary, 5000); setInterval(loadHistory, 60000); })();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
