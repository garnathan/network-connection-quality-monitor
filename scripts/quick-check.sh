#!/usr/bin/env bash
#
# quick-check.sh — "is the home uplink still broken?" health check.
#
# Replicates the two measurements that defined the 2026-04-20 baseline:
#
#   1. Sustained HTTPS upload throughput over N seconds of saturation
#      (baseline: ~1 Mbps, i.e. 5% of the 20 Mbps advertised uplink)
#   2. Loaded latency under combined up+down load via Apple networkQuality
#      (baseline: 665 ms vs 45 ms idle = 14.8x penalty, "LOW" rating)
#
# Prints a one-line verdict and exits:
#   0 = RESOLVED    (both metrics near healthy)
#   1 = PARTIAL     (one metric healthy, one still degraded)
#   2 = STILL BROKEN
#
# Usage: ./quick-check.sh [duration_seconds]          (default 30)
#        ./quick-check.sh --no-latency [duration]     (skip networkQuality)
#        ./quick-check.sh --quiet                     (one-line output)

set -u

DURATION=90     # 2026-04-20 baseline showed policing engages after ~45-70 MB;
                # 30s at burst (20 Mbps) = ~75 MB, borderline. 90s pushes well
                # past the burst-token bucket so sustained/policed rate is what
                # the mean captures.
SKIP_LATENCY=0
QUIET=0
BURST_WINDOW=15  # seconds treated as "burst" when splitting the throughput

while [ $# -gt 0 ]; do
  case "$1" in
    --no-latency) SKIP_LATENCY=1; shift ;;
    --quiet|-q)   QUIET=1; shift ;;
    -h|--help)
      sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)            DURATION="$1"; shift ;;
  esac
done

PARALLEL_UPLOADS=2
PAYLOAD_MB=50
UPLOAD_URL="https://speed.cloudflare.com/__up"

# Thresholds (Mbps / ms) — calibrated to the 2026-04-20 baseline
UL_RESOLVED_MBPS=10      # >= 10 Mbps sustained = resolved
UL_PARTIAL_MBPS=3        # 3..10 = partial, < 3 = broken
LAT_RESOLVED_MS=150      # < 150 ms loaded = resolved
LAT_PARTIAL_MS=400       # 150..400 = partial, > 400 = broken

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPORT_DIR="$PROJECT_DIR/reports"
mkdir -p "$REPORT_DIR"
TS=$(date +%Y%m%d-%H%M%S)
REPORT="$REPORT_DIR/healthcheck-$TS.md"

TMPDIR=$(mktemp -d)
UPLOAD_PIDS=()

cleanup() {
  for pid in "${UPLOAD_PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  rm -rf "$TMPDIR"
}
trap cleanup EXIT INT TERM

log() { [ "$QUIET" = "1" ] || echo "$@"; }

# ---------- Phase 1: sustained upload throughput ----------
log "Quick Health Check"
log "=================="
log "Phase 1: $DURATION s sustained upload ($PARALLEL_UPLOADS workers, payload ${PAYLOAD_MB} MB)..."
log "  -> split: first ${BURST_WINDOW}s = burst window, next $((DURATION - BURST_WINDOW))s = sustained window"

dd if=/dev/urandom of="$TMPDIR/payload.bin" bs=1m count=$PAYLOAD_MB 2>/dev/null

START_EPOCH=$(date +%s)
BURST_END_EPOCH=$((START_EPOCH + BURST_WINDOW))
END_EPOCH_PLANNED=$((START_EPOCH + DURATION))

for i in $(seq 1 $PARALLEL_UPLOADS); do
  (
    # Record per-completion: "<finish_epoch> <bytes_uploaded>" per line.
    # --max-time is bounded by remaining window so the last request
    # is killed cleanly at the test end; curl still reports its
    # partial %{size_upload} so late-flight bytes aren't lost.
    while [ "$(date +%s)" -lt "$END_EPOCH_PLANNED" ]; do
      remaining=$((END_EPOCH_PLANNED - $(date +%s)))
      [ "$remaining" -le 0 ] && break
      size=$(curl -s -X POST --data-binary "@$TMPDIR/payload.bin" \
        --max-time "$remaining" \
        -H "Content-Type: application/octet-stream" \
        -w '%{size_upload}' -o /dev/null \
        "$UPLOAD_URL" 2>/dev/null || echo 0)
      printf '%s %s\n' "$(date +%s)" "${size:-0}" >> "$TMPDIR/worker-$i.txt"
    done
  ) &
  UPLOAD_PIDS+=($!)
done

for pid in "${UPLOAD_PIDS[@]}"; do
  wait "$pid" 2>/dev/null || true
done
UPLOAD_PIDS=()
END_EPOCH=$(date +%s)

ELAPSED=$((END_EPOCH - START_EPOCH))
[ "$ELAPSED" -lt 1 ] && ELAPSED=1

# Split bytes by completion timestamp into burst vs sustained windows.
# Attribution is by finish-time: an upload that started in burst but
# finished in sustained counts toward sustained. At high throughput
# this is noise; at low throughput it's conservative (makes sustained
# look slightly better than it really is), which only weakens the
# "broken" verdict — never falsely strengthens the "resolved" verdict.
read TOTAL_BYTES BURST_BYTES SUSTAINED_BYTES < <(
  awk -v be="$BURST_END_EPOCH" '
    { t += $2; if ($1 <= be) b += $2; else s += $2 }
    END { printf "%d %d %d\n", t+0, b+0, s+0 }
  ' "$TMPDIR"/worker-*.txt 2>/dev/null
)
TOTAL_BYTES=${TOTAL_BYTES:-0}
BURST_BYTES=${BURST_BYTES:-0}
SUSTAINED_BYTES=${SUSTAINED_BYTES:-0}

BURST_ELAPSED=$BURST_WINDOW
SUSTAINED_ELAPSED=$((ELAPSED - BURST_WINDOW))
[ "$SUSTAINED_ELAPSED" -lt 1 ] && SUSTAINED_ELAPSED=1

UL_MBPS=$(awk "BEGIN{printf \"%.2f\", ($TOTAL_BYTES * 8) / ($ELAPSED * 1000000)}")
UL_BURST_MBPS=$(awk "BEGIN{printf \"%.2f\", ($BURST_BYTES * 8) / ($BURST_ELAPSED * 1000000)}")
UL_SUSTAINED_MBPS=$(awk "BEGIN{printf \"%.2f\", ($SUSTAINED_BYTES * 8) / ($SUSTAINED_ELAPSED * 1000000)}")

# ---------- Phase 2: loaded latency (optional) ----------
LOADED_LAT_MS="skipped"
IDLE_LAT_MS="skipped"
RPM="skipped"
NQ_RESPONSIVENESS="skipped"
NQ_INTERFACE=""
NQ_RAW=""

# Extract a time-in-ms from a paren-wrapped segment like "(3.501 seconds | 17 RPM)"
# or "(85.920 milliseconds)" or "(85.920 milliseconds | 698 RPM)". Returns empty
# if no time found.
parse_paren_ms() {
  local s="$1" val unit
  val=$(echo "$s" | grep -oE '[0-9]+\.[0-9]+' | head -1)
  unit=$(echo "$s" | grep -oE '(seconds?|milliseconds?)' | head -1)
  [ -z "$val" ] || [ -z "$unit" ] && return
  case "$unit" in
    seconds|second)           awk "BEGIN{printf \"%.1f\", $val * 1000}" ;;
    milliseconds|millisecond) awk "BEGIN{printf \"%.1f\", $val}" ;;
  esac
}

if [ "$SKIP_LATENCY" = "0" ] && command -v networkQuality >/dev/null 2>&1; then
  log "Phase 2: networkQuality (loaded latency, ~15 s)..."
  NQ_RAW=$(networkQuality -v 2>&1 || true)

  # Apple's SUMMARY section (verbose mode) gives single-line values:
  #   Responsiveness: Low (3.501 seconds | 17 RPM)
  #   Idle Latency: 85.920 milliseconds | 698 RPM
  # Older macOS format uses "Loaded Latency: NNN ms" — kept as a fallback.
  IDLE_LINE=$(echo "$NQ_RAW" | grep -E '^Idle Latency:' | tail -1)
  RESP_LINE=$(echo "$NQ_RAW" | grep -E '^Responsiveness:' | tail -1)
  LOAD_LINE=$(echo "$NQ_RAW" | grep -iE 'Loaded Latency[[:space:]]*:' | head -1)

  IDLE_LAT_MS=$(parse_paren_ms "${IDLE_LINE#Idle Latency:}")
  LOADED_LAT_MS=$(parse_paren_ms "$(echo "$RESP_LINE" | grep -oE '\([^)]+\)')")
  if [ -z "$LOADED_LAT_MS" ] && [ -n "$LOAD_LINE" ]; then
    LOADED_LAT_MS=$(parse_paren_ms "$LOAD_LINE")
  fi

  RPM=$(echo "$RESP_LINE" | grep -oE '[0-9]+ RPM' | head -1 | awk '{print $1}')
  NQ_RESPONSIVENESS=$(echo "$RESP_LINE" \
    | sed -E 's/^Responsiveness:[[:space:]]*//' \
    | sed -E 's/[[:space:]]*\(.*$//')
  NQ_INTERFACE=$(echo "$NQ_RAW" | awk -F': *' '/Interface:/ {print $2; exit}')

  [ -z "${IDLE_LAT_MS:-}" ]       && IDLE_LAT_MS="unknown"
  [ -z "${LOADED_LAT_MS:-}" ]     && LOADED_LAT_MS="unknown"
  [ -z "${RPM:-}" ]               && RPM="unknown"
  [ -z "${NQ_RESPONSIVENESS:-}" ] && NQ_RESPONSIVENESS="unknown"
elif [ "$SKIP_LATENCY" = "0" ]; then
  log "Phase 2: networkQuality not found - skipping latency check."
fi

# ---------- Verdict ----------
awk_gt() { awk "BEGIN{exit !($1 > $2)}"; }
awk_ge() { awk "BEGIN{exit !($1 >= $2)}"; }
awk_lt() { awk "BEGIN{exit !($1 < $2)}"; }

# Upload verdict: driven by SUSTAINED rate (post-burst-window), because the
# carrier's rate-policing only reveals itself after the burst-token bucket
# drains. Burst rate is reported alongside for context / policing detection.
classify_mbps() {
  if awk_ge "$1" "$UL_RESOLVED_MBPS"; then echo "resolved"
  elif awk_ge "$1" "$UL_PARTIAL_MBPS"; then echo "partial"
  else echo "broken"; fi
}
UL_BURST_STATUS=$(classify_mbps "$UL_BURST_MBPS")
UL_SUSTAINED_STATUS=$(classify_mbps "$UL_SUSTAINED_MBPS")
UL_STATUS="$UL_SUSTAINED_STATUS"

# Detect policing: burst looks fine but sustained collapses to <50% of burst.
POLICING_FLAG="no"
if awk_ge "$UL_BURST_MBPS" "$UL_PARTIAL_MBPS" \
   && awk_lt "$UL_SUSTAINED_MBPS" "$(awk "BEGIN{printf \"%.2f\", $UL_BURST_MBPS * 0.5}")"; then
  POLICING_FLAG="yes"
fi

# Latency verdict
LAT_STATUS="skipped"
if [[ "$LOADED_LAT_MS" =~ ^[0-9.]+$ ]]; then
  if awk_lt "$LOADED_LAT_MS" "$LAT_RESOLVED_MS"; then
    LAT_STATUS="resolved"
  elif awk_lt "$LOADED_LAT_MS" "$LAT_PARTIAL_MS"; then
    LAT_STATUS="partial"
  else
    LAT_STATUS="broken"
  fi
fi

# Overall: worst of the two (skipped doesn't count)
rank() { case "$1" in resolved) echo 0;; partial) echo 1;; broken) echo 2;; *) echo -1;; esac; }
worst=$(rank "$UL_STATUS")
lr=$(rank "$LAT_STATUS")
[ "$lr" -gt "$worst" ] && worst=$lr

case "$worst" in
  0) VERDICT="RESOLVED";     EXIT=0 ;;
  1) VERDICT="PARTIAL";      EXIT=1 ;;
  *) VERDICT="STILL BROKEN"; EXIT=2 ;;
esac

# VPN warning — networkQuality results over a VPN tunnel include tunnel
# overhead; flag it so the reader can interpret.
VPN_WARN=""
if [ -n "$NQ_INTERFACE" ] && [[ "$NQ_INTERFACE" =~ ^utun ]]; then
  VPN_WARN="networkQuality ran over VPN interface '$NQ_INTERFACE' - loaded latency includes tunnel overhead. For a clean read, disconnect VPN and re-run."
fi

# ---------- Report ----------
{
  echo "# Home Uplink Health Check — $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo ""
  echo "**Verdict:** $VERDICT"
  echo ""
  echo "## Measurements"
  echo ""
  echo "| Metric | Result | Baseline (2026-04-20) | Healthy threshold | Status |"
  echo "|--------|--------|------------------------|-------------------|--------|"
  echo "| Upload — burst (first ${BURST_WINDOW}s) | $UL_BURST_MBPS Mbps ($BURST_BYTES bytes) | n/a | >= $UL_RESOLVED_MBPS Mbps | $UL_BURST_STATUS |"
  echo "| Upload — **sustained (after ${BURST_WINDOW}s)** | **$UL_SUSTAINED_MBPS Mbps** ($SUSTAINED_BYTES bytes / ${SUSTAINED_ELAPSED}s) | ~1 Mbps | >= $UL_RESOLVED_MBPS Mbps | **$UL_SUSTAINED_STATUS** |"
  echo "| Upload — mean (full $DURATION s) | $UL_MBPS Mbps ($TOTAL_BYTES bytes) | ~1 Mbps | >= $UL_RESOLVED_MBPS Mbps | — |"
  if [ "$LAT_STATUS" != "skipped" ]; then
    echo "| Loaded latency | **$LOADED_LAT_MS ms** (idle $IDLE_LAT_MS ms, $RPM RPM, $NQ_RESPONSIVENESS) | 665 ms | < $LAT_RESOLVED_MS ms | $LAT_STATUS |"
  else
    echo "| Loaded latency | skipped | 665 ms | < $LAT_RESOLVED_MS ms | - |"
  fi
  echo ""
  if [ "$POLICING_FLAG" = "yes" ]; then
    echo "> **Rate-policing detected.** Burst rate ($UL_BURST_MBPS Mbps) is healthy but sustained rate ($UL_SUSTAINED_MBPS Mbps) is less than half of that - the classic fingerprint of a carrier shaping profile that lets bursts through but throttles sustained flows. Matches the 2026-04-20 root cause."
    echo ""
  fi
  if [ -n "$VPN_WARN" ]; then
    echo "> **VPN caveat.** $VPN_WARN"
    echo ""
  fi
  echo "## Thresholds"
  echo ""
  echo "- **Sustained upload** (verdict driver): < $UL_PARTIAL_MBPS Mbps = broken; ${UL_PARTIAL_MBPS}-${UL_RESOLVED_MBPS} = partial; >= $UL_RESOLVED_MBPS = resolved."
  echo "- **Loaded latency:** > $LAT_PARTIAL_MS ms = broken; ${LAT_RESOLVED_MS}-${LAT_PARTIAL_MS} = partial; < $LAT_RESOLVED_MS = resolved."
  echo ""
  echo "## Notes"
  echo ""
  echo "- Upload sink: \`$UPLOAD_URL\`"
  echo "- Run over **Ethernet** for best signal; Wi-Fi jitter masks upstream queueing."
  echo "- Latency check uses Apple networkQuality (RFC 9097). Interface observed: ${NQ_INTERFACE:-n/a}."
  echo "- For root-cause isolation (router vs modem vs carrier), run \`scripts/router-isolation-test.sh\`."
  if [ -n "$NQ_RAW" ]; then
    echo ""
    echo "## Appendix: networkQuality raw output"
    echo ""
    echo '```'
    echo "$NQ_RAW"
    echo '```'
  fi
} > "$REPORT"

# ---------- Console summary ----------
if [ "$QUIET" = "1" ]; then
  echo "$VERDICT  burst=${UL_BURST_MBPS}Mbps  sustained=${UL_SUSTAINED_MBPS}Mbps  loaded_latency=${LOADED_LAT_MS}ms"
else
  echo ""
  echo "Results"
  echo "-------"
  printf "  Upload burst (first %ss):   %s Mbps  (%s bytes)  -> %s\n" \
    "$BURST_WINDOW" "$UL_BURST_MBPS" "$BURST_BYTES" "$UL_BURST_STATUS"
  printf "  Upload sustained (%ss):     %s Mbps  (%s bytes)  -> %s   <-- verdict driver\n" \
    "$SUSTAINED_ELAPSED" "$UL_SUSTAINED_MBPS" "$SUSTAINED_BYTES" "$UL_SUSTAINED_STATUS"
  printf "  Upload mean (full %ss):     %s Mbps  (%s bytes)\n" \
    "$ELAPSED" "$UL_MBPS" "$TOTAL_BYTES"
  if [ "$LAT_STATUS" != "skipped" ]; then
    printf "  Loaded latency:            %s ms    (idle %s ms, %s RPM, %s)  -> %s\n" \
      "$LOADED_LAT_MS" "$IDLE_LAT_MS" "$RPM" "$NQ_RESPONSIVENESS" "$LAT_STATUS"
  fi
  echo ""
  if [ "$POLICING_FLAG" = "yes" ]; then
    echo "  ! Rate-policing detected: sustained < 50% of burst."
  fi
  if [ -n "$VPN_WARN" ]; then
    echo "  ! $VPN_WARN"
  fi
  echo ""
  echo "Verdict: $VERDICT"
  echo "Report:  $REPORT"
fi

exit $EXIT
