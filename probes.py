"""
probes — shared broadband-probe layer.

Pure measurement + analysis logic used by both the interactive CLI
(`monitor.py`) and the always-on web daemon (`webmon.py`), so the
probes and thresholds are defined exactly once. No presentation code lives here
(no ANSI, no HTML) — just the tools that shell out to ping/dig/curl, parse their
output, and rate the results.

Zero third-party dependencies — pure Python 3 stdlib.
"""

import os
import re
import statistics
import subprocess

# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #

DEFAULT_ANCHOR = "1.1.1.1"

# Domains rotated through for the DNS-resolve-time probe.
DNS_DOMAINS = ["google.com", "wikipedia.org", "github.com",
               "netflix.com", "cloudflare.com", "amazon.com"]

# Real pages rotated through for the page-load (TTFB) probe.
HTTP_TARGETS = [
    "https://www.google.com/",
    "https://en.wikipedia.org/wiki/Broadband",
    "https://www.cloudflare.com/",
    "https://www.bbc.co.uk/",
]

DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes={n}"
UPLOAD_URL = "https://speed.cloudflare.com/__up"

# Thresholds: (good, warn, higher_is_better).
#   higher_is_better == False -> good if v <= good, warn if v <= warn, else bad
#   higher_is_better == True  -> good if v >= good, warn if v >= warn, else bad
THRESHOLDS = {
    "latency_ms":     (60.0, 150.0, False),
    "jitter_ms":      (15.0, 40.0, False),
    "loss_pct":       (0.5, 2.0, False),
    "dns_ms":         (50.0, 150.0, False),
    "ttfb_ms":        (300.0, 800.0, False),
    "download_mbps":  (15.0, 5.0, True),
    "upload_mbps":    (10.0, 3.0, True),
    "bufferbloat_ms": (30.0, 100.0, False),
}

# Expected line rates (Mbps) for the plan under test. Throughput ratings are
# judged RELATIVE to these, so a link delivering a small fraction of what you
# pay for reads as a problem even when the raw number looks "fast enough".
# Override at startup with configure_expected(); defaults are Gareth's plan.
DEFAULT_DOWN_EXPECTED_MBPS = 350.0
DEFAULT_UP_EXPECTED_MBPS = 34.0

# Fraction-of-plan bands for throughput: good >= 70%, warn >= 40%, else bad.
THROUGHPUT_GOOD_FRAC = 0.70
THROUGHPUT_WARN_FRAC = 0.40


def configure_expected(down_mbps=None, up_mbps=None):
    """Recalibrate the download/upload ratings against the plan's line rate.

    Mutates THRESHOLDS in place (call once at startup, before probe threads
    read it). Both front-ends call this so the CLI report and the web UI agree.
    """
    if down_mbps is not None and down_mbps > 0:
        THRESHOLDS["download_mbps"] = (THROUGHPUT_GOOD_FRAC * down_mbps,
                                       THROUGHPUT_WARN_FRAC * down_mbps, True)
    if up_mbps is not None and up_mbps > 0:
        THRESHOLDS["upload_mbps"] = (THROUGHPUT_GOOD_FRAC * up_mbps,
                                     THROUGHPUT_WARN_FRAC * up_mbps, True)


# Maps a stored series name -> its threshold key. Series names are the stable
# identifiers used by the CLI state, the SQLite store, and the web API.
METRIC_KEYS = {
    "latency": "latency_ms",
    "jitter": "jitter_ms",
    "loss": "loss_pct",
    "dns": "dns_ms",
    "ttfb": "ttfb_ms",
    "download": "download_mbps",
    "upload": "upload_mbps",
    "bufferbloat": "bufferbloat_ms",
}


# --------------------------------------------------------------------------- #
# Analysis helpers
# --------------------------------------------------------------------------- #

def percentile(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    frac = k - lo
    return vals[lo] + (vals[hi] - vals[lo]) * frac


def rating(value, key):
    """Return 'good' | 'warn' | 'bad' for a metric value, or 'none'."""
    if value is None or key not in THRESHOLDS:
        return "none"
    good, warn, hib = THRESHOLDS[key]
    if hib:
        if value >= good:
            return "good"
        return "warn" if value >= warn else "bad"
    if value <= good:
        return "good"
    return "warn" if value <= warn else "bad"


# --------------------------------------------------------------------------- #
# Low-level probe helpers
# --------------------------------------------------------------------------- #

def run(cmd, timeout):
    """Run a command, return (returncode, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except Exception as e:  # noqa: BLE001 - probe must never crash the monitor
        return 1, "", str(e)


def parse_ping(out):
    """Parse macOS/BSD/Linux ping output -> dict(avg,min,max,jitter,loss,rtts)."""
    rtts = [float(m) for m in re.findall(r"time[=<]([\d.]+)\s*ms", out)]
    loss_m = re.search(r"([\d.]+)%\s*packet loss", out)
    loss = float(loss_m.group(1)) if loss_m else (0.0 if rtts else 100.0)
    # macOS: "round-trip min/avg/max/stddev = ...";  Linux: "rtt min/avg/max/mdev = ..."
    summ = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", out)
    if summ:
        mn, avg, mx, jit = (float(summ.group(i)) for i in range(1, 5))
    elif rtts:
        mn, mx = min(rtts), max(rtts)
        avg = statistics.fmean(rtts)
        jit = statistics.pstdev(rtts) if len(rtts) > 1 else 0.0
    else:
        mn = avg = mx = jit = None
    return {"avg": avg, "min": mn, "max": mx, "jitter": jit,
            "loss": loss, "rtts": rtts}


def probe_ping(host, count, interval=0.5):
    rc, out, err = run(["ping", "-c", str(count), "-i", str(interval), host],
                       timeout=count * interval + 8)
    res = parse_ping(out)
    res["ok"] = res["avg"] is not None
    res["error"] = None if res["ok"] else (err or "no reply").strip()
    return res


def probe_dns(domain):
    rc, out, err = run(["dig", "+tries=1", "+time=3", domain], timeout=8)
    qt = re.search(r"Query time:\s*(\d+)\s*msec", out)
    srv = re.search(r"SERVER:\s*(\S+)", out)
    if qt:
        return {"ms": float(qt.group(1)),
                "server": srv.group(1) if srv else "", "error": None}
    return {"ms": None, "server": "", "error": (err or "no answer").strip()}


def probe_http(url, max_time=20):
    fmt_str = ("%{time_namelookup} %{time_connect} %{time_appconnect} "
               "%{time_starttransfer} %{time_total} %{size_download} "
               "%{http_code}")
    rc, out, err = run(["curl", "-sS", "-o", os.devnull, "--max-time",
                        str(max_time), "-w", fmt_str, url],
                       timeout=max_time + 5)
    parts = out.split()
    if rc == 0 and len(parts) >= 7:
        ns, cn, ac, ttfb, total, size, code = parts[:7]
        return {"ttfb_ms": float(ttfb) * 1000, "total_ms": float(total) * 1000,
                "dns_ms": float(ns) * 1000, "connect_ms": float(cn) * 1000,
                "size": int(float(size)), "code": code, "error": None}
    return {"ttfb_ms": None, "total_ms": None, "size": 0, "code": "0",
            "error": (err.strip() or f"curl rc={rc}")}


def probe_transfer_loaded(anchor, direction, nbytes, payload, max_time):
    """Saturate the link (download or upload) and ping the anchor *during* the
    transfer, so we get throughput AND loaded latency in one shot.

    Returns dict(mbps, bytes, loaded_avg, loaded_max, loss, error).
    """
    if direction == "download":
        cmd = ["curl", "-sS", "-o", os.devnull, "--max-time", str(max_time),
               "-w", "%{speed_download} %{size_download}",
               DOWNLOAD_URL.format(n=nbytes)]
    else:
        cmd = ["curl", "-sS", "-o", os.devnull, "--max-time", str(max_time),
               "-X", "POST", "--data-binary", f"@{payload}",
               "-w", "%{speed_upload} %{size_upload}", UPLOAD_URL]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    except Exception as e:  # noqa: BLE001
        return {"mbps": None, "bytes": 0, "loaded_avg": None,
                "loaded_max": None, "loss": None, "error": str(e)}

    ping_count = max(4, int(max_time / 0.5))
    loaded = probe_ping(anchor, count=ping_count, interval=0.5)

    try:
        out, _ = proc.communicate(timeout=max_time + 6)
    except subprocess.TimeoutExpired:
        proc.kill()
        out = ""
    parts = out.split()
    mbps = nbytes_done = None
    if len(parts) >= 2:
        try:
            speed = float(parts[0])
            nbytes_done = int(float(parts[1]))
            mbps = speed * 8 / 1_000_000
        except ValueError:
            pass
    return {"mbps": mbps, "bytes": nbytes_done or 0,
            "loaded_avg": loaded["avg"], "loaded_max": loaded["max"],
            "loss": loaded["loss"],
            "error": None if mbps is not None else "transfer failed"}


# --------------------------------------------------------------------------- #
# Link (wired / wireless) detection
# --------------------------------------------------------------------------- #

# Numeric encoding of the active link, used for the link-over-time graph.
LINK_VALUE = {"wired": 1.0, "wireless": 0.0, "down": -1.0, "unknown": 0.0}


def detect_link(anchor=DEFAULT_ANCHOR):
    """Which interface does traffic to the internet actually leave by, and is
    it wired or wireless? Because the probes run from this host, the egress
    interface IS the link every test traverses.

    Returns dict(iface, type, up). type is 'wired' | 'wireless' | 'down' |
    'unknown'. Authoritative on Linux (via /sys/class/net); best-effort by
    name on macOS (used only for local dev).
    """
    iface = None
    rc, out, _ = run(["ip", "route", "get", anchor], 4)   # Linux
    if rc == 0:
        m = re.search(r"\bdev\s+(\S+)", out)
        iface = m.group(1) if m else None
    if not iface:
        rc, out, _ = run(["route", "-n", "get", anchor], 4)   # macOS/BSD
        m = re.search(r"interface:\s*(\S+)", out)
        iface = m.group(1) if m else None
    if not iface:
        return {"iface": None, "type": "down", "up": False}

    if os.path.isdir(f"/sys/class/net/{iface}/wireless"):
        typ = "wireless"
    elif os.path.exists(f"/sys/class/net/{iface}"):
        typ = "wired"
    elif iface.startswith(("wl", "wlan")) or iface == "en0":
        typ = "wireless"                 # macOS Wi-Fi is usually en0
    elif iface.startswith(("eth", "en", "bridge")):
        typ = "wired"
    else:
        typ = "unknown"
    return {"iface": iface, "type": typ, "up": True}


# --------------------------------------------------------------------------- #
# Metric series (used by the CLI; the web daemon persists to SQLite instead)
# --------------------------------------------------------------------------- #

class Series:
    """A time series for one metric: full history + O(1) running aggregates."""

    def __init__(self, unit=""):
        self.unit = unit
        self.times = []
        self.values = []
        self.count = 0
        self.total = 0.0
        self.vmin = None
        self.vmax = None
        self.last = None

    def add(self, value, ts):
        self.times.append(ts)
        self.values.append(value)
        self.last = value
        if value is None:
            return
        self.count += 1
        self.total += value
        self.vmin = value if self.vmin is None else min(self.vmin, value)
        self.vmax = value if self.vmax is None else max(self.vmax, value)

    @property
    def avg(self):
        return self.total / self.count if self.count else None

    def p95(self):
        return percentile(self.values, 95)

    def recent_avg(self, n=10):
        vals = [v for v in self.values[-n:] if v is not None]
        return statistics.fmean(vals) if vals else None
