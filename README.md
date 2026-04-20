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

## Reports

Committed to `reports/` as a historical record — one report per test run.
Useful for:

- Before/after comparisons (e.g. rebooting Eero, switching WiFi → Ethernet)
- Time-of-day correlation (carrier congestion varies)
- Evidence for the ISP when escalating
