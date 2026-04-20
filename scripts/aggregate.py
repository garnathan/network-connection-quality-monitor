#!/usr/bin/env python3
"""
Pool ping samples across multiple router-isolation-test reports and produce
an aggregate analysis robust to Wi-Fi jitter.

Percentile deltas (P50, P95, P99) are more meaningful than mean deltas when
the idle baseline is itself noisy: a systematic upstream queue shifts the
whole distribution, so the median rises; random Wi-Fi spikes only widen
the tail and leave the median alone.

Usage:
    ./aggregate.py                          # aggregate all reports in reports/
    ./aggregate.py report1.md report2.md    # aggregate specific reports
"""

import re
import sys
import statistics
from datetime import datetime
from pathlib import Path

SECTIONS = ("eero-idle", "eero-load", "modem-idle", "modem-load")
THRESH_P50_MS = 10   # P50 rise above this counts as "spikes"
THRESH_P95_MS = 30   # P95 rise above this counts as "spikes"


def extract_rtts(report_text: str, section: str) -> list[float]:
    """Pull all `time=X ms` samples out of a named appendix fenced code block."""
    m = re.search(
        rf'###\s+{re.escape(section)}\s*\n\s*```\s*\n(.*?)```',
        report_text, re.S,
    )
    if not m:
        return []
    return [float(t) for t in re.findall(r'time=([\d.]+)\s*ms', m.group(1))]


def percentile(sorted_samples: list[float], p: float) -> float:
    if not sorted_samples:
        return 0.0
    idx = int(p / 100.0 * (len(sorted_samples) - 1))
    return sorted_samples[idx]


def summarize(samples: list[float]) -> dict | None:
    if not samples:
        return None
    s = sorted(samples)
    return {
        "n":      len(s),
        "mean":   statistics.mean(s),
        "stddev": statistics.pstdev(s) if len(s) > 1 else 0.0,
        "min":    s[0],
        "max":    s[-1],
        "p50":    percentile(s, 50),
        "p95":    percentile(s, 95),
        "p99":    percentile(s, 99),
    }


def fmt_row(label: str, st: dict | None) -> str:
    if not st:
        return f"| {label} | — | — | — | — | — | — | — | — |"
    return (
        f"| {label} | {st['n']} | {st['mean']:.1f} | {st['stddev']:.1f} | "
        f"{st['p50']:.1f} | {st['p95']:.1f} | {st['p99']:.1f} | "
        f"{st['min']:.1f} | {st['max']:.1f} |"
    )


def delta_row(idle: dict | None, load: dict | None) -> str:
    if not idle or not load:
        return "| **Δ** | — | — | — | — | — | — | — | — |"
    return (
        f"| **Δ** | — | **{load['mean'] - idle['mean']:+.1f}** | "
        f"{load['stddev'] - idle['stddev']:+.1f} | "
        f"**{load['p50'] - idle['p50']:+.1f}** | "
        f"**{load['p95'] - idle['p95']:+.1f}** | "
        f"{load['p99'] - idle['p99']:+.1f} | — | "
        f"{load['max'] - idle['max']:+.1f} |"
    )


def verdict(eero_idle, eero_load, modem_idle, modem_load) -> str:
    def spiked(idle, load):
        if not idle or not load:
            return None
        return (
            (load["p50"] - idle["p50"]) > THRESH_P50_MS
            or (load["p95"] - idle["p95"]) > THRESH_P95_MS
        )

    eero_spike = spiked(eero_idle, eero_load)
    modem_spike = spiked(modem_idle, modem_load)

    if eero_spike is None:
        return "Insufficient data for Eero hop."
    if modem_spike is None:
        if eero_spike:
            return ("**Eero queue spikes under load** "
                    "(modem hop data missing — can't isolate further).")
        return ("**Eero stays clean under load.** "
                "Bottleneck is upstream (modem or carrier).")

    if eero_spike and not modem_spike:
        return ("**ROUTER (Eero) is the bottleneck.** Median RTT to the Eero "
                "rises under load while the upstream hop stays flat — queue "
                "is inside the router.")
    if eero_spike and modem_spike:
        return ("**Modem or carrier is the bottleneck.** Both hops' medians "
                "rise together — the queue is at or above the modem. The "
                "Eero is just passing the upstream congestion through.")
    if not eero_spike and not modem_spike:
        return ("**Carrier rate-policing, no local queueing.** Neither hop's "
                "median shifts under load. The carrier drops excess traffic "
                "rather than buffering it — the upload collapses without "
                "anything queueing locally. Router is not the cause.")
    return ("**Inconclusive pattern** (Eero clean, modem spikes). Rare — "
            "inspect raw data.")


def main():
    project_dir = Path(__file__).resolve().parent.parent
    report_dir = project_dir / "reports"

    if len(sys.argv) > 1:
        report_paths = [Path(a).resolve() for a in sys.argv[1:]]
    else:
        report_paths = sorted(report_dir.glob("report-*.md"))

    if not report_paths:
        print("No reports to aggregate.", file=sys.stderr)
        sys.exit(1)

    pools: dict[str, list[float]] = {s: [] for s in SECTIONS}
    per_run: list[tuple[str, dict[str, int]]] = []
    for rp in report_paths:
        text = rp.read_text()
        counts = {}
        for sect in SECTIONS:
            samples = extract_rtts(text, sect)
            pools[sect].extend(samples)
            counts[sect] = len(samples)
        per_run.append((rp.name, counts))

    summaries = {s: summarize(pools[s]) for s in SECTIONS}
    v = verdict(
        summaries["eero-idle"], summaries["eero-load"],
        summaries["modem-idle"], summaries["modem-load"],
    )

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = report_dir / f"aggregate-{ts}.md"

    lines = []
    lines.append(f"# Aggregate Report — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append(f"Pooled across **{len(report_paths)}** run(s):")
    lines.append("")
    for name, counts in per_run:
        lines.append(
            f"- `{name}` — "
            f"eero idle={counts['eero-idle']}, load={counts['eero-load']}; "
            f"modem idle={counts['modem-idle']}, load={counts['modem-load']}"
        )
    lines.append("")
    lines.append("## Pooled RTT distributions")
    lines.append("")

    hdr = ("| Phase | n | mean | stddev | P50 | P95 | P99 | min | max |\n"
           "|-------|---|------|--------|-----|-----|-----|-----|-----|")

    lines.append("### Router (Eero)")
    lines.append("")
    lines.append(hdr)
    lines.append(fmt_row("Idle", summaries["eero-idle"]))
    lines.append(fmt_row("Load", summaries["eero-load"]))
    lines.append(delta_row(summaries["eero-idle"], summaries["eero-load"]))
    lines.append("")

    if summaries["modem-idle"] or summaries["modem-load"]:
        lines.append("### 5G modem (first upstream hop)")
        lines.append("")
        lines.append(hdr)
        lines.append(fmt_row("Idle", summaries["modem-idle"]))
        lines.append(fmt_row("Load", summaries["modem-load"]))
        lines.append(delta_row(summaries["modem-idle"], summaries["modem-load"]))
        lines.append("")

    lines.append("## Verdict")
    lines.append("")
    lines.append(v)
    lines.append("")
    lines.append("### Robustness note")
    lines.append("")
    lines.append(
        "P50 (median) is the primary signal because it is immune to random "
        "Wi-Fi jitter spikes. A real upstream queue shifts the whole "
        "distribution — including the median. Wi-Fi contention only widens "
        "the tail, leaving the median untouched. P95 corroborates: if both "
        "P50 and P95 rise under load, queueing is real."
    )
    lines.append("")
    lines.append(f"Spike thresholds: ΔP50 > {THRESH_P50_MS} ms OR ΔP95 > {THRESH_P95_MS} ms.")
    lines.append("")

    report_md = "\n".join(lines) + "\n"
    out_path.write_text(report_md)

    print(report_md)
    print(f"\nWritten: {out_path}")


if __name__ == "__main__":
    main()
