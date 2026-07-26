#!/usr/bin/env python3
"""
monitor.py — live, interactive broadband monitor.

Run it, watch the numbers build up in real time, and press Ctrl-C whenever you
like. On exit it writes a plain-English report plus raw per-probe CSVs so you
have hard data to hand your ISP.

Where the earlier tooling in this repo focused on *sustained upload* (the
2026-04 token-bucket diagnosis), this monitor is deliberately general and runs
every kind of test continuously, mapped to the symptoms you actually feel:

  streaming at low bitrate   -> download throughput
  pages slow to load         -> DNS resolve time, HTTP time-to-first-byte
  Zoom laggy / choppy         -> latency, jitter, packet loss, and bufferbloat
                                (latency measured *while the link is saturated* —
                                 the single best predictor of a bad video call)

Zero third-party dependencies: pure Python 3 stdlib driving the same standard
tools the rest of the repo uses (ping, dig, curl). That is on purpose — a tool
for diagnosing a broken connection must not need a working connection to
install anything.

Usage:
    ./monitor.py                     # run until Ctrl-C, pretty dashboard
    ./monitor.py --duration 600      # auto-stop after 10 minutes
    ./monitor.py --light             # latency/DNS/TTFB only (near-zero data)
    ./monitor.py --plain             # line output (no live redraw)
    ./monitor.py --help              # all options

Exit codes: 0 healthy, 1 degraded, 2 problems detected.
"""

import argparse
import os
import re
import shutil
import signal
import statistics
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Shared probe layer (sibling module) + presentation constants
# --------------------------------------------------------------------------- #

from probes import (  # noqa: E402  (shared backend, sibling at the repo root)
    DEFAULT_ANCHOR, DEFAULT_DOWN_EXPECTED_MBPS, DEFAULT_UP_EXPECTED_MBPS,
    DNS_DOMAINS, HTTP_TARGETS, THRESHOLDS, Series, configure_expected,
    percentile, probe_dns, probe_http, probe_ping, probe_transfer_loaded,
    rating,
)

SPARK = "▁▂▃▄▅▆▇█"
MAX_UI_WIDTH = 102          # cap so the dashboard stays readable on wide terms
SPARK_WINDOW = 40           # samples shown in each sparkline

# --------------------------------------------------------------------------- #
# ANSI helpers (visible-length aware, so colours don't break alignment)
# --------------------------------------------------------------------------- #

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GRN = "\033[32m"
    YEL = "\033[33m"
    BLU = "\033[34m"
    MAG = "\033[35m"
    CYN = "\033[36m"
    GRY = "\033[90m"


USE_COLOR = True


def color(s, c):
    return f"{c}{s}{C.RESET}" if USE_COLOR else str(s)


def strip_ansi(s):
    return _ANSI_RE.sub("", s)


def vlen(s):
    return len(strip_ansi(s))


def vljust(s, n):
    pad = n - vlen(s)
    return s + " " * pad if pad > 0 else s


def vtrunc(s, n):
    if vlen(s) <= n:
        return s
    # Truncate on visible characters while preserving any codes seen so far.
    out, count = [], 0
    i = 0
    while i < len(s) and count < n:
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        out.append(s[i])
        count += 1
        i += 1
    return "".join(out) + C.RESET


RATING_COLOR = {"good": C.GRN, "warn": C.YEL, "bad": C.RED, "none": C.GRY}


def rate_color(value, key):
    return color(fmt(value), RATING_COLOR[rating(value, key)])


def fmt(v, nd=1):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def sparkline(values, width=SPARK_WINDOW):
    vals = [v for v in values[-width:] if v is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo
    if span <= 0:
        return SPARK[3] * len(vals)
    out = []
    for v in vals:
        idx = int((v - lo) / span * (len(SPARK) - 1))
        out.append(SPARK[idx])
    return "".join(out)


# --------------------------------------------------------------------------- #
# Shared state
# --------------------------------------------------------------------------- #

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.start = time.time()
        self.series = {
            "latency": Series("ms"),
            "jitter": Series("ms"),
            "loss": Series("%"),
            "dns": Series("ms"),
            "ttfb": Series("ms"),
            "http_total": Series("ms"),
            "download": Series("Mbps"),
            "upload": Series("Mbps"),
            "bufferbloat": Series("ms"),
        }
        self.labels = {"dns": "", "ttfb": "", "latency": ""}
        self.errors = {}           # probe name -> last error string
        self.events = deque(maxlen=200)
        self.bytes_down = 0
        self.bytes_up = 0
        self.throughput_running = False
        self.last_throughput = None

    def record(self, name, value, ts=None, label=None):
        ts = ts or time.time()
        with self.lock:
            self.series[name].add(value, ts)
            if label is not None and name in self.labels:
                self.labels[name] = label

    def add_bytes(self, down=0, up=0):
        with self.lock:
            self.bytes_down += down
            self.bytes_up += up

    def set_error(self, probe, err):
        with self.lock:
            if err:
                self.errors[probe] = err
            else:
                self.errors.pop(probe, None)

    def event(self, msg, severity="info"):
        with self.lock:
            self.events.append((time.time(), severity, msg))


# --------------------------------------------------------------------------- #
# Worker threads
# --------------------------------------------------------------------------- #

class Monitor:
    def __init__(self, state, args, stop):
        self.st = state
        self.a = args
        self.stop = stop
        self.dns_i = 0
        self.http_i = 0
        # One reusable random payload sized to the largest upload we'll do.
        self.payload = None
        if not args.light:
            fd, self.payload = tempfile.mkstemp(prefix="iccd-up-", suffix=".bin")
            with os.fdopen(fd, "wb") as f:
                f.write(os.urandom(int(args.upload_mb * 1_000_000)))

    def cleanup(self):
        if self.payload and os.path.exists(self.payload):
            os.unlink(self.payload)

    # -- latency / jitter / loss ------------------------------------------- #
    def latency_worker(self):
        while not self.stop.is_set():
            r = probe_ping(self.a.anchor, count=5, interval=0.5)
            ts = time.time()
            if r["ok"]:
                self.st.set_error("latency", None)
                self.st.record("latency", r["avg"], ts, label=self.a.anchor)
                self.st.record("jitter", r["jitter"], ts)
                self.st.record("loss", r["loss"], ts)
                if r["loss"] >= THRESHOLDS["loss_pct"][1]:
                    self.st.event(f"packet loss {r['loss']:.0f}% to "
                                  f"{self.a.anchor}", "bad")
                base = self.st.series["latency"].recent_avg(20)
                if base and r["max"] and r["max"] > max(120, base * 3):
                    self.st.event(f"latency spike {r['max']:.0f} ms to "
                                  f"{self.a.anchor}", "warn")
            else:
                self.st.set_error("latency", r["error"])
                # Record all three in lockstep (None where unmeasurable) so the
                # latency/jitter/loss columns in the CSV stay row-aligned.
                self.st.record("latency", None, ts, label=self.a.anchor)
                self.st.record("jitter", None, ts)
                self.st.record("loss", 100.0, ts)
                self.st.event(f"no ping reply from {self.a.anchor} "
                              f"({r['error']})", "bad")
            self.stop.wait(self.a.latency_interval)

    # -- DNS ---------------------------------------------------------------- #
    def dns_worker(self):
        while not self.stop.is_set():
            domain = DNS_DOMAINS[self.dns_i % len(DNS_DOMAINS)]
            self.dns_i += 1
            r = probe_dns(domain)
            self.st.set_error("dns", r["error"])
            self.st.record("dns", r["ms"], label=domain)
            if r["ms"] and r["ms"] >= THRESHOLDS["dns_ms"][1]:
                self.st.event(f"slow DNS {r['ms']:.0f} ms for {domain}", "warn")
            self.stop.wait(self.a.dns_interval)

    # -- HTTP page load / TTFB --------------------------------------------- #
    def http_worker(self):
        while not self.stop.is_set():
            url = HTTP_TARGETS[self.http_i % len(HTTP_TARGETS)]
            self.http_i += 1
            host = re.sub(r"^https?://", "", url).split("/")[0]
            r = probe_http(url)
            self.st.set_error("http", r["error"])
            self.st.record("ttfb", r["ttfb_ms"], label=host)
            self.st.record("http_total", r["total_ms"])
            if r["ttfb_ms"] and r["ttfb_ms"] >= THRESHOLDS["ttfb_ms"][1]:
                self.st.event(f"slow page load: TTFB {r['ttfb_ms']:.0f} ms "
                              f"for {host}", "warn")
            self.stop.wait(self.a.http_interval)

    # -- throughput + bufferbloat ------------------------------------------ #
    def throughput_worker(self):
        if self.a.light:
            return
        # Small initial delay so the dashboard shows the cheap probes first.
        if self.stop.wait(3):
            return
        while not self.stop.is_set():
            with self.st.lock:
                self.st.throughput_running = True
                baseline = self.st.series["latency"].recent_avg(15)

            # Measure download, then upload, capturing loaded latency during
            # each. A cycle interrupted by Ctrl-C still records whatever it
            # measured — all three metrics are written once, in lockstep, so
            # throughput.csv rows never drift out of alignment.
            dl = probe_transfer_loaded(self.a.anchor, "download",
                                       int(self.a.download_mb * 1_000_000),
                                       self.payload, self.a.throughput_max_time)
            self.st.add_bytes(down=dl["bytes"])
            self.st.set_error("download", dl["error"])

            ul = {"mbps": None, "loaded_avg": None, "bytes": 0, "error": None}
            if not self.stop.is_set():
                ul = probe_transfer_loaded(self.a.anchor, "upload",
                                           int(self.a.upload_mb * 1_000_000),
                                           self.payload,
                                           self.a.throughput_max_time)
                self.st.add_bytes(up=ul["bytes"])
                self.st.set_error("upload", ul["error"])

            # Bufferbloat = worst loaded latency minus the idle baseline.
            loaded_vals = [v for v in (dl["loaded_avg"], ul["loaded_avg"])
                           if v is not None]
            bloat = None
            if loaded_vals and baseline:
                bloat = max(max(loaded_vals) - baseline, 0.0)

            ts = time.time()
            self.st.record("download", dl["mbps"], ts)
            self.st.record("upload", ul["mbps"], ts)
            self.st.record("bufferbloat", bloat, ts)

            if dl["mbps"] is not None and \
                    rating(dl["mbps"], "download_mbps") == "bad":
                exp = self.a.download_expected_mbps
                pct = (100 * dl["mbps"] / exp) if exp else 0
                self.st.event(f"download {dl['mbps']:.0f} Mbps — only {pct:.0f}% "
                              f"of your {exp:.0f} Mbps plan", "bad")
            if bloat is not None:
                rt = rating(bloat, "bufferbloat_ms")
                if rt == "bad":
                    self.st.event(f"bufferbloat +{bloat:.0f} ms under load "
                                  "(video calls will lag)", "bad")
                elif rt == "warn":
                    self.st.event(f"bufferbloat +{bloat:.0f} ms under load",
                                  "warn")

            with self.st.lock:
                self.st.throughput_running = False
                self.st.last_throughput = time.time()

            if self.stop.wait(self.a.throughput_interval):
                return


# --------------------------------------------------------------------------- #
# Rendering — live dashboard
# --------------------------------------------------------------------------- #

def hms(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def mb(n):
    return f"{n / 1_000_000:.1f}MB"


def panel(title, body_lines, width, accent=C.GRY):
    inner = width - 2
    content_w = width - 4
    top_title = f" {title} "
    fill = inner - vlen(top_title)
    left = 2
    right = max(0, fill - left)
    top = (color("┌", accent) + color("─" * left, accent)
           + color(top_title, accent + C.BOLD)
           + color("─" * right, accent) + color("┐", accent))
    out = [top]
    for ln in body_lines:
        out.append(color("│", accent) + " " + vljust(vtrunc(ln, content_w),
                   content_w) + " " + color("│", accent))
    out.append(color("└" + "─" * inner + "┘", accent))
    return out


def join_cols(a, b, gap=1):
    h = max(len(a), len(b))
    a = a + [""] * (h - len(a))
    b = b + [""] * (h - len(b))
    wa = max((vlen(x) for x in a), default=0)
    return [vljust(x, wa) + " " * gap + y for x, y in zip(a, b)]


def accent_for(name, value):
    return RATING_COLOR[rating(value, name)]


def build_panels(st, args, width):
    with st.lock:
        s = st.series
        snap = {k: (v.last, v.avg, v.p95(), v.vmax, v.vmin, v.count,
                    sparkline(v.values)) for k, v in s.items()}
        labels = dict(st.labels)
        errors = dict(st.errors)
        events = list(st.events)[-6:]
        bdown, bup = st.bytes_down, st.bytes_up
        running = st.throughput_running
        last_tp = st.last_throughput
        start = st.start

    # Latency panel
    lat = snap["latency"]
    jit = snap["jitter"]
    los = snap["loss"]
    lat_body = [
        f"now {rate_color(lat[0], 'latency_ms')} ms"
        + color(f"    loss ", C.DIM) + rate_color(los[0], "loss_pct") + "%",
        color(f"avg {fmt(lat[1])}  p95 {fmt(lat[2])}  max {fmt(lat[3])}", C.DIM),
        f"jitter {rate_color(jit[0], 'jitter_ms')} ms",
        color(lat[6], C.CYN) + color(f"  {labels.get('latency','')}", C.DIM),
    ]
    if "latency" in errors:
        lat_body.append(color("! " + errors["latency"], C.RED))
    p_lat = panel("Latency (idle)", lat_body, width, accent_for("latency_ms", lat[0]))

    # DNS panel
    dns = snap["dns"]
    dns_body = [
        f"now {rate_color(dns[0], 'dns_ms')} ms",
        color(f"avg {fmt(dns[1])}  p95 {fmt(dns[2])}  max {fmt(dns[3])}", C.DIM),
        color(dns[6], C.CYN) + color(f"  {labels.get('dns','')}", C.DIM),
    ]
    if "dns" in errors:
        dns_body.append(color("! " + errors["dns"], C.RED))
    p_dns = panel("DNS resolve", dns_body, width, accent_for("dns_ms", dns[0]))

    # TTFB panel
    ttfb = snap["ttfb"]
    tot = snap["http_total"]
    ttfb_body = [
        f"TTFB {rate_color(ttfb[0], 'ttfb_ms')} ms"
        + color(f"   total {fmt(tot[0])} ms", C.DIM),
        color(f"avg {fmt(ttfb[1])}  p95 {fmt(ttfb[2])}  max {fmt(ttfb[3])}", C.DIM),
        color(ttfb[6], C.CYN) + color(f"  {labels.get('ttfb','')}", C.DIM),
    ]
    if "http" in errors:
        ttfb_body.append(color("! " + errors["http"], C.RED))
    p_ttfb = panel("Page load (TTFB)", ttfb_body, width, accent_for("ttfb_ms", ttfb[0]))

    # Bufferbloat panel
    bloat = snap["bufferbloat"]
    if bloat[5] == 0:
        bloat_body = [color("waiting for first load test…", C.DIM),
                      color("(latency measured under saturation)", C.DIM)]
        bloat_acc = C.GRY
    else:
        sev = {"good": "OK", "warn": "NOTABLE", "bad": "SEVERE",
               "none": ""}[rating(bloat[0], "bufferbloat_ms")]
        bloat_body = [
            f"+{rate_color(bloat[0], 'bufferbloat_ms')} ms added under load  "
            + color(sev, RATING_COLOR[rating(bloat[0], 'bufferbloat_ms')] + C.BOLD),
            color(f"avg +{fmt(bloat[1])}  p95 +{fmt(bloat[2])}  max +{fmt(bloat[3])}", C.DIM),
            color(bloat[6], C.CYN),
        ]
        bloat_acc = accent_for("bufferbloat_ms", bloat[0])
    p_bloat = panel("Bufferbloat (lag under load)", bloat_body, width, bloat_acc)

    # Download / Upload
    def tp_panel(key, keyfor, title):
        d = snap[key]
        if args.light:
            body = [color("disabled (--light mode)", C.DIM)]
            return panel(title, body, width, C.GRY)
        if d[5] == 0:
            ago = "running…" if running else "pending first test"
            body = [color(f"{ago}", C.DIM),
                    color("(time-bounded, uses data)", C.DIM)]
            return panel(title, body, width, C.GRY)
        ago_s = ("running now" if running and last_tp is None
                 else f"{int(time.time() - last_tp)}s ago" if last_tp else "—")
        body = [
            f"now {rate_color(d[0], keyfor)} Mbps",
            color(f"avg {fmt(d[1])}  min {fmt(d[4])}  max {fmt(d[3])}", C.DIM),
            color(d[6], C.CYN) + color(f"  (last: {ago_s})", C.DIM),
        ]
        if key in errors:
            body.append(color("! " + errors[key], C.RED))
        return panel(title, body, width, accent_for(keyfor, d[0]))

    p_dl = tp_panel("download", "download_mbps", "Download")
    p_ul = tp_panel("upload", "upload_mbps", "Upload")

    return p_lat, p_dns, p_ttfb, p_bloat, p_dl, p_ul, events, bdown, bup, start, running


def render_dashboard(st, args):
    term = shutil.get_terminal_size((100, 30))
    avail = min(term.columns, MAX_UI_WIDTH)
    two_col = avail >= 74
    if two_col:
        col_w = (avail - 1) // 2
        width = col_w * 2 + 1   # full-width rows line up exactly with 2 columns
    else:
        col_w = width = avail

    (p_lat, p_dns, p_ttfb, p_bloat, p_dl, p_ul, events,
     bdown, bup, start, running) = build_panels(st, args, col_w)

    elapsed = time.time() - start
    tp_state = color(" ● testing", C.YEL + C.BOLD) if running else ""
    header_body = [
        color(f"started {datetime.fromtimestamp(start):%Y-%m-%d %H:%M:%S}", C.DIM)
        + color(f"    data ↓{mb(bdown)} ↑{mb(bup)}", C.CYN)
        + tp_state,
    ]
    header = panel(f"Broadband Monitor   {hms(elapsed)} elapsed",
                   header_body, width, C.BLU)

    lines = list(header)
    lines.append("")
    if two_col:
        lines += join_cols(p_lat, p_dns)
        lines += join_cols(p_ttfb, p_bloat)
        lines += join_cols(p_dl, p_ul)
    else:
        for p in (p_lat, p_dns, p_ttfb, p_bloat, p_dl, p_ul):
            lines += p

    # Events panel (full width)
    ev_lines = []
    for ts, sev, msg in reversed(events):
        c = {"bad": C.RED, "warn": C.YEL, "info": C.GRY}.get(sev, C.GRY)
        ev_lines.append(color(f"{datetime.fromtimestamp(ts):%H:%M:%S}  ", C.DIM)
                        + color(msg, c))
    if not ev_lines:
        ev_lines = [color("no notable events yet — quiet is good", C.DIM)]
    lines += panel("Recent events", ev_lines, width, C.GRY)
    lines.append(color("  Press Ctrl-C to stop and write the report.", C.DIM))

    # Paint in place using the alternate screen buffer.
    buf = ["\033[H"]
    for ln in lines:
        buf.append(ln + "\033[K\n")
    buf.append("\033[J")
    sys.stdout.write("".join(buf))
    sys.stdout.flush()


def render_plain(st, args, seen_events):
    with st.lock:
        s = st.series
        lat = s["latency"].last
        jit = s["jitter"].last
        los = s["loss"].last
        dns = s["dns"].last
        ttfb = s["ttfb"].last
        dl = s["download"].last
        ul = s["upload"].last
        bloat = s["bufferbloat"].last
        bdown, bup = st.bytes_down, st.bytes_up
        new_events = list(st.events)[seen_events:]
        total_events = len(st.events)
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] lat {fmt(lat)}ms jit {fmt(jit)} loss {fmt(los)}% | "
          f"dns {fmt(dns)}ms ttfb {fmt(ttfb)}ms | dl {fmt(dl)} ul {fmt(ul)} Mbps | "
          f"bloat +{fmt(bloat)}ms | data ↓{mb(bdown)} ↑{mb(bup)}", flush=True)
    for ets, sev, msg in new_events:
        et = datetime.fromtimestamp(ets).strftime("%H:%M:%S")
        print(f"    * {et} [{sev}] {msg}", flush=True)
    return total_events


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def metric_row(name, key, series, nd=1):
    vals = [v for v in series.values if v is not None]
    if not vals:
        return f"| {name} | 0 | — | — | — | — | — | — |"
    mn, mx = min(vals), max(vals)
    avg = statistics.fmean(vals)
    med = statistics.median(vals)
    p95 = percentile(vals, 95)
    rt = rating(p95 if not THRESHOLDS.get(key, (0, 0, False))[2] else avg, key)
    badge = {"good": "healthy", "warn": "degraded", "bad": "**PROBLEM**",
             "none": "—"}[rt]
    return (f"| {name} | {len(vals)} | {mn:.{nd}f} | {avg:.{nd}f} | "
            f"{med:.{nd}f} | {p95:.{nd}f} | {mx:.{nd}f} | {badge} |")


def overall_verdict(st):
    """Return (verdict_text, exit_code) from the worst metric ratings."""
    worst = 0  # 0 good, 1 warn, 2 bad
    checks = {
        "latency": "latency_ms", "jitter": "jitter_ms", "loss": "loss_pct",
        "dns": "dns_ms", "ttfb": "ttfb_ms", "download": "download_mbps",
        "upload": "upload_mbps", "bufferbloat": "bufferbloat_ms",
    }
    for name, key in checks.items():
        series = st.series[name]
        vals = [v for v in series.values if v is not None]
        if not vals:
            continue
        hib = THRESHOLDS[key][2]
        val = statistics.fmean(vals) if hib else percentile(vals, 95)
        r = rating(val, key)
        worst = max(worst, {"good": 0, "warn": 1, "bad": 2, "none": 0}[r])
    return (["HEALTHY", "DEGRADED", "PROBLEMS DETECTED"][worst], worst)


def write_report(st, args, report_dir):
    os.makedirs(report_dir, exist_ok=True)
    end = time.time()
    verdict, code = overall_verdict(st)
    s = st.series

    # Raw per-probe CSVs.
    def dump_csv(fname, header, keys):
        rows = max(len(s[k].times) for k in keys)
        path = os.path.join(report_dir, fname)
        with open(path, "w") as f:
            f.write(header + "\n")
            for i in range(rows):
                cells = []
                ts = None
                for k in keys:
                    if i < len(s[k].times):
                        ts = ts or s[k].times[i]
                cells.append(datetime.fromtimestamp(ts).isoformat()
                             if ts else "")
                for k in keys:
                    v = s[k].values[i] if i < len(s[k].values) else None
                    cells.append("" if v is None else f"{v:.3f}")
                f.write(",".join(cells) + "\n")

    if s["latency"].times:
        dump_csv("latency.csv", "ts,latency_ms,jitter_ms,loss_pct",
                 ["latency", "jitter", "loss"])
    if s["dns"].times:
        dump_csv("dns.csv", "ts,dns_ms", ["dns"])
    if s["ttfb"].times:
        dump_csv("http.csv", "ts,ttfb_ms,total_ms", ["ttfb", "http_total"])
    if s["download"].times or s["upload"].times:
        dump_csv("throughput.csv", "ts,download_mbps,upload_mbps,bufferbloat_ms",
                 ["download", "upload", "bufferbloat"])

    report = os.path.join(report_dir, "report.md")
    dur = end - st.start
    with open(report, "w") as f:
        w = f.write
        w(f"# Broadband Monitor — {datetime.fromtimestamp(st.start):%Y-%m-%d %H:%M:%S %Z}\n\n")
        w(f"**Verdict:** {verdict}\n\n")
        w(f"- Session: {datetime.fromtimestamp(st.start):%H:%M:%S} → "
          f"{datetime.fromtimestamp(end):%H:%M:%S}  ({hms(dur)})\n")
        w(f"- Anchor host (latency/bufferbloat): `{args.anchor}`\n")
        w(f"- Data used: ↓ {mb(st.bytes_down)} down, ↑ {mb(st.bytes_up)} up\n")
        w(f"- Notable events: {len(st.events)}\n\n")

        w("## Measurements\n\n")
        w("| Metric | Samples | Min | Avg | Median | P95 | Max | Rating |\n")
        w("|--------|---------|-----|-----|--------|-----|-----|--------|\n")
        w(metric_row("Idle latency (ms)", "latency_ms", s["latency"]) + "\n")
        w(metric_row("Jitter (ms)", "jitter_ms", s["jitter"]) + "\n")
        w(metric_row("Packet loss (%)", "loss_pct", s["loss"]) + "\n")
        w(metric_row("DNS resolve (ms)", "dns_ms", s["dns"]) + "\n")
        w(metric_row("Page TTFB (ms)", "ttfb_ms", s["ttfb"]) + "\n")
        w(metric_row("Download (Mbps)", "download_mbps", s["download"]) + "\n")
        w(metric_row("Upload (Mbps)", "upload_mbps", s["upload"]) + "\n")
        w(metric_row("Bufferbloat (+ms under load)", "bufferbloat_ms",
                     s["bufferbloat"]) + "\n\n")

        w("## What this means for your symptoms\n\n")
        w(_symptom_analysis(s))
        w("\n")

        if st.events:
            w("## Notable events\n\n")
            for ts, sev, msg in st.events:
                t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                w(f"- `{t}` **{sev}** — {msg}\n")
            w("\n")

        w("## How to read these numbers\n\n")
        w("| Metric | Healthy | Degraded | Problem |\n")
        w("|--------|---------|----------|--------|\n")
        w("| Idle latency | ≤ 60 ms | ≤ 150 ms | > 150 ms |\n")
        w("| Jitter | ≤ 15 ms | ≤ 40 ms | > 40 ms |\n")
        w("| Packet loss | ≤ 0.5% | ≤ 2% | > 2% |\n")
        w("| DNS resolve | ≤ 50 ms | ≤ 150 ms | > 150 ms |\n")
        w("| Page TTFB | ≤ 300 ms | ≤ 800 ms | > 800 ms |\n")
        dg, dw, _ = THRESHOLDS["download_mbps"]
        ug, uw, _ = THRESHOLDS["upload_mbps"]
        w(f"| Download (plan {args.download_expected_mbps:.0f} Mbps) | ≥ {dg:.0f} Mbps "
          f"| ≥ {dw:.0f} Mbps | < {dw:.0f} Mbps |\n")
        w(f"| Upload (plan {args.upload_expected_mbps:.0f} Mbps) | ≥ {ug:.0f} Mbps "
          f"| ≥ {uw:.0f} Mbps | < {uw:.0f} Mbps |\n")
        w("| Bufferbloat | ≤ 30 ms | ≤ 100 ms | > 100 ms |\n\n")
        w(f"*Download/upload are judged against your plan's line rate "
          f"({args.download_expected_mbps:.0f}/{args.upload_expected_mbps:.0f} Mbps): "
          f"healthy ≥ 70%, degraded ≥ 40%, problem below. So a link that tests 'fast "
          f"enough' in absolute terms still flags if it is far below what you pay for.*\n\n")
        w("*Bufferbloat is the extra latency that appears only when the link is "
          "saturated. It is the single best predictor of a bad video call: a "
          "large number here means Zoom stutters whenever anything else is "
          "using the connection, even if the raw speed looks fine.*\n\n")

        w("## Configuration\n\n")
        w(f"- Latency ping every {args.latency_interval}s, DNS every "
          f"{args.dns_interval}s, page load every {args.http_interval}s\n")
        if not args.light:
            w(f"- Throughput every {args.throughput_interval}s "
              f"(download {args.download_mb} MB / upload {args.upload_mb} MB "
              f"cap, {args.throughput_max_time}s max each)\n")
        else:
            w("- `--light` mode: throughput probes disabled\n")
        w(f"- Raw samples: `latency.csv`, `dns.csv`, `http.csv`, "
          f"`throughput.csv` in this directory\n")

    return report, verdict, code


def _symptom_analysis(s):
    out = []

    def worst_rating(name, key):
        vals = [v for v in s[name].values if v is not None]
        if not vals:
            return "none", None
        hib = THRESHOLDS[key][2]
        val = statistics.fmean(vals) if hib else percentile(vals, 95)
        return rating(val, key), val

    dl_r, dl_v = worst_rating("download", "download_mbps")
    bloat_r, bloat_v = worst_rating("bufferbloat", "bufferbloat_ms")
    dns_r, dns_v = worst_rating("dns", "dns_ms")
    ttfb_r, ttfb_v = worst_rating("ttfb", "ttfb_ms")
    loss_r, loss_v = worst_rating("loss", "loss_pct")
    jit_r, jit_v = worst_rating("jitter", "jitter_ms")

    def verdict_line(sym, findings):
        problems = [f for f in findings if f[1] in ("bad", "warn")]
        if problems:
            detail = "; ".join(f[0] for f in problems)
            return f"- **{sym}** — likely affected: {detail}.\n"
        return f"- **{sym}** — nothing anomalous in this session.\n"

    out.append("**Streaming at low bitrate**\n")
    out.append(verdict_line(
        "Streaming",
        [(f"download P95 {dl_v:.1f} Mbps" if dl_v is not None else "", dl_r),
         (f"bufferbloat +{bloat_v:.0f} ms under load" if bloat_v is not None else "", bloat_r)]))
    out.append("\n**Pages slow to load**\n")
    out.append(verdict_line(
        "Page loads",
        [(f"DNS P95 {dns_v:.0f} ms" if dns_v is not None else "", dns_r),
         (f"TTFB P95 {ttfb_v:.0f} ms" if ttfb_v is not None else "", ttfb_r)]))
    out.append("\n**Zoom laggy / choppy**\n")
    out.append(verdict_line(
        "Video calls",
        [(f"bufferbloat +{bloat_v:.0f} ms under load" if bloat_v is not None else "", bloat_r),
         (f"jitter {jit_v:.0f} ms" if jit_v is not None else "", jit_r),
         (f"packet loss {loss_v:.1f}%" if loss_v is not None else "", loss_r)]))
    return "".join(out)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Live broadband monitor — run, watch, Ctrl-C for a report.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--duration", type=int, default=0,
                   help="auto-stop after N seconds (0 = until Ctrl-C)")
    p.add_argument("--anchor", default=DEFAULT_ANCHOR,
                   help="host pinged for latency / jitter / bufferbloat")
    p.add_argument("--latency-interval", type=float, default=5.0)
    p.add_argument("--dns-interval", type=float, default=15.0)
    p.add_argument("--http-interval", type=float, default=15.0)
    p.add_argument("--throughput-interval", type=float, default=180.0,
                   help="seconds between download+upload+bufferbloat tests")
    p.add_argument("--download-mb", type=float, default=25.0,
                   help="download size cap per throughput test (MB)")
    p.add_argument("--upload-mb", type=float, default=8.0,
                   help="upload size cap per throughput test (MB)")
    p.add_argument("--throughput-max-time", type=int, default=15,
                   help="max seconds per download/upload (bounds data on slow links)")
    p.add_argument("--download-expected-mbps", type=float,
                   default=DEFAULT_DOWN_EXPECTED_MBPS,
                   help="plan's download line rate; ratings are judged vs this")
    p.add_argument("--upload-expected-mbps", type=float,
                   default=DEFAULT_UP_EXPECTED_MBPS,
                   help="plan's upload line rate; ratings are judged vs this")
    p.add_argument("--light", action="store_true",
                   help="latency/DNS/TTFB only — no throughput, near-zero data")
    p.add_argument("--plain", action="store_true",
                   help="line output instead of the live dashboard")
    p.add_argument("--refresh", type=float, default=1.0,
                   help="dashboard refresh interval (seconds)")
    p.add_argument("--report-dir", default=None,
                   help="override output directory")
    return p.parse_args(argv)


def main(argv):
    global USE_COLOR
    args = parse_args(argv)
    # Judge throughput relative to the plan's line rate (same as the web UI).
    configure_expected(args.download_expected_mbps, args.upload_expected_mbps)

    is_tty = sys.stdout.isatty()
    live = is_tty and not args.plain
    USE_COLOR = is_tty and os.environ.get("NO_COLOR") is None

    project = os.path.dirname(os.path.abspath(__file__))
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_dir = args.report_dir or os.path.join(project, "reports",
                                                 f"monitor-{ts}")

    st = State()
    stop = threading.Event()
    mon = Monitor(st, args, stop)

    def handle_sigint(signum, frame):
        stop.set()
    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    workers = [
        threading.Thread(target=mon.latency_worker, daemon=True),
        threading.Thread(target=mon.dns_worker, daemon=True),
        threading.Thread(target=mon.http_worker, daemon=True),
        threading.Thread(target=mon.throughput_worker, daemon=True),
    ]
    for t in workers:
        t.start()

    if live:
        sys.stdout.write("\033[?1049h\033[?25l")  # alt screen + hide cursor
        sys.stdout.flush()
    else:
        print(f"monitor — sampling every ~{args.latency_interval:.0f}s, "
              f"report at {report_dir}")
        print("Press Ctrl-C to stop.\n")

    seen_events = 0
    plain_tick = 0.0
    try:
        while not stop.is_set():
            if live:
                render_dashboard(st, args)
                stop.wait(args.refresh)
            else:
                now = time.time()
                if now - plain_tick >= max(5.0, args.latency_interval):
                    seen_events = render_plain(st, args, seen_events)
                    plain_tick = now
                stop.wait(1.0)
            if args.duration and time.time() - st.start >= args.duration:
                stop.set()
    finally:
        stop.set()
        if live:
            sys.stdout.write("\033[?25h\033[?1049l")  # show cursor, main screen
            sys.stdout.flush()
        for t in workers:
            t.join(timeout=max(6, args.throughput_max_time + 8))
        mon.cleanup()

    report, verdict, code = write_report(st, args, report_dir)

    vc = {"HEALTHY": C.GRN, "DEGRADED": C.YEL,
          "PROBLEMS DETECTED": C.RED}.get(verdict, C.GRY)
    print()
    print(color(f"Verdict: {verdict}", vc + C.BOLD))
    print(f"Report:  {report}")
    print(f"Data:    ↓ {mb(st.bytes_down)} / ↑ {mb(st.bytes_up)}")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
