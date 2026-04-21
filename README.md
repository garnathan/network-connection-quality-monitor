# internet-connection-diagnosis

Tools and reports for diagnosing home broadband problems on a 5G Fixed Wireless
Access (FWA) connection. Built to isolate whether upload throttling / latency
spikes originate in the local router, the modem, or the carrier — and to
produce reports clean enough to hand to an ISP engineer.

## Topology under test

```
Mac ── (WiFi/Eth) ── Eero ── Eth ── 5G modem (outside) ── 5G carrier
      hop 0           hop 1           hop 2                   hop 3+
```

## Symptoms being diagnosed

- Uplink burst capacity: 20 Mbps
- Sustained upload throughput: ~1 Mbps (5% of burst)
- HTTP loaded latency: 665 ms vs 45 ms idle (14.8× penalty)
- Large file uploads stall or fail around the 45–70 % mark

Initial findings: [docs/2026-04-20-initial-findings.md](docs/2026-04-20-initial-findings.md).

## Scripts

### `scripts/quick-check.sh` — "is it still broken?"

Fast health check that replicates the two measurements from the 2026-04-20
baseline and prints a single-line verdict: **RESOLVED / PARTIAL / STILL BROKEN**.

**Run:**

```bash
./scripts/quick-check.sh                  # default: 90s upload + networkQuality
./scripts/quick-check.sh 180              # longer test (more conclusive)
./scripts/quick-check.sh --no-latency 90  # skip networkQuality (upload only)
./scripts/quick-check.sh --quiet          # one-line output (for cron / status bar)
```

Total runtime: ~105 s (90 s sustained upload + ~15 s networkQuality).
Exit codes: `0` = resolved, `1` = partial, `2` = still broken.

**What it measures:**

| Metric | Baseline (broken) | Healthy threshold |
|--------|-------------------|-------------------|
| Upload burst (first 15 s, 2 parallel HTTPS POST to Cloudflare) | — | >= 10 Mbps |
| **Upload sustained (after 15 s)** — verdict driver | ~1 Mbps | >= 10 Mbps |
| Loaded latency (Apple `networkQuality`, RFC 9097) | 665 ms | < 150 ms |

The burst vs sustained split is deliberate: the 2026-04-20 finding was that
the 5 G carrier lets short bursts through at full speed (~20 Mbps) and only
throttles once the burst-token bucket drains, around the 45–70 MB mark.
A short test that only measures burst will **lie** and say "resolved" even
when sustained throughput has collapsed. `quick-check.sh` reports both and
uses the sustained rate as the verdict driver. If burst is healthy but
sustained is < 50% of burst, it flags "rate-policing detected" in the
report.

Reports land in `reports/healthcheck-YYYYMMDD-HHMMSS.md`. Use those for
before/after comparisons across carrier changes, reboots, or ISP tickets.

### `scripts/router-isolation-test.sh`

Isolates the source of queueing / latency under sustained upload load by
pinging two hops in parallel:

1. **Eero LAN IP** — tests the router's internal queue
2. **First upstream hop** (the 5G modem's LAN-side IP) — tests the Eero → modem link

**Run:**

```bash
./scripts/router-isolation-test.sh [duration_seconds]   # default 60s
```

Total runtime: ~80 s (15 s idle baseline + 3 s ramp + N s loaded phase).
Saturates your uplink for the duration — don't run during a video call.

**Interpretation:**

| Eero Δ latency | Modem Δ latency | Verdict                                       |
|----------------|-----------------|-----------------------------------------------|
| Large          | Small           | Router is the bottleneck (Eero queue fills)   |
| Large          | Large           | Modem or carrier (queueing at/above modem)    |
| Small          | Small           | Carrier is rate-policing — no queue anywhere  |

Reports land in `reports/report-YYYYMMDD-HHMMSS.md` with raw ping logs in
an appendix.

**Important: run over Ethernet, not Wi-Fi.** Wi-Fi jitter on our Eero is
severe enough (40ms stddev at idle, spikes to 130ms) that it masks the
upstream queueing signal and invalidates the verdict. Plug the Mac into
an Eero LAN port before running.

## Reports

Committed to `reports/` as a historical record — one report per test run.
Useful for:

- Before/after comparisons (e.g. rebooting Eero, switching WiFi → Ethernet)
- Time-of-day correlation (carrier congestion varies)
- Evidence for the ISP when escalating
