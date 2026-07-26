# network-connection-quality-monitor

Tools for diagnosing and continuously monitoring a home broadband connection —
built for a flaky 5G Fixed-Wireless-Access line, but general. They measure the
things you actually feel when the internet is bad and produce evidence clean
enough to hand to an ISP.

| Symptom you notice | What it measures |
|--------------------|------------------|
| Streaming drops to low quality | **download** throughput (rated against your plan) |
| Pages slow to load | **DNS** resolve time, page **time-to-first-byte** |
| Zoom / calls lag and stutter | **latency, jitter, packet loss**, and **bufferbloat** (latency measured *while the link is saturated* — the best predictor of a bad call) |
| Uploads stall | **upload** throughput |

Everything is **pure Python 3 / bash — no third-party dependencies**. A tool for
diagnosing a broken connection must not need a working connection to install
anything.

## Layout

```
probes.py         # shared measurement backend — the ONE source of truth for
                  #   every probe, threshold and rating (ping / dig / curl)
monitor.py        # interactive terminal monitor — run it, watch, Ctrl-C
upload-test.sh    # the original one-shot sustained-upload tester
webui/            # the always-on web dashboard, as a persistent Pi service
```

Both `monitor.py` and `webui/webmon.py` import `probes.py`, so the terminal tool
and the web dashboard always measure identically — change a probe once and both
pick it up.

## `monitor.py` — interactive terminal monitor

Run it, watch every metric build up live, and press **Ctrl-C** whenever you like.
On exit it writes a plain-English report plus raw per-probe CSVs.

```bash
./monitor.py                 # live dashboard, runs until Ctrl-C
./monitor.py --light         # latency / DNS / TTFB only, near-zero data
./monitor.py --duration 600  # auto-stop after 10 minutes
./monitor.py --help
```

Auto-detects the terminal: a live in-place dashboard when interactive, plain
status lines when piped. Output lands in `reports/monitor-<timestamp>/`
(`report.md` + `latency.csv` / `dns.csv` / `http.csv` / `throughput.csv`).
Exit codes: `0` healthy, `1` degraded, `2` problems.

## `webui/` — always-on web dashboard

A 24/7 daemon with a modern web UI you open on your LAN, designed to run on the
home Raspberry Pi without getting in the way of work or calls. Per-stat history
charts (1h / 6h / 1d / 1w / 1m), a wired-vs-wireless link timeline, live
data-usage counter, and HTTP Basic Auth. History is stored in SQLite so it
survives reboots.

```bash
./webui/run.sh               # try it locally at http://localhost:8080
./webui/deploy.sh home-pi    # install on the Pi as a persistent systemd service
```

It runs as a robust persistent service (auto-start on boot, restart on crash,
systemd **watchdog** that also catches hangs, self-healing probe workers). Full
details, configuration, and management commands are in
[`webui/README.md`](webui/README.md).

**Bandwidth-safe:** the cheap probes (latency / DNS / TTFB / link) run often and
cost almost nothing; the expensive throughput test runs infrequently and is
**skipped whenever the link is already busy**, so it never fights an active call
or stream.

## `upload-test.sh` — sustained-upload tester

The original one-shot check that isolated the carrier's upload rate-policing:
saturates the uplink for N seconds and splits burst vs. sustained throughput
(the carrier lets short bursts through at full speed, then throttles once the
burst-token bucket drains). Also reports loaded latency via Apple
`networkQuality`.

```bash
./upload-test.sh             # ~90 s sustained upload + loaded-latency check
./upload-test.sh 180         # longer, more conclusive
./upload-test.sh --quiet     # one-line verdict (cron / status bar)
```

Writes `reports/healthcheck-<timestamp>.md`. Exit codes: `0` resolved,
`1` partial, `2` still broken.
