#!/usr/bin/env bash
#
# router-isolation-test.sh
#
# Isolates the source of upload-induced latency / throttling on a home
# 5G FWA connection by measuring RTT to two hops in parallel during a
# saturated upload vs an idle baseline:
#
#   Hop 1: the Eero's LAN IP            (router's internal queue)
#   Hop 2: the Eero's WAN gateway       (5G modem, first upstream hop)
#
# Usage: ./router-isolation-test.sh [duration_seconds]     (default 60)
#
# Output: a markdown report under reports/ plus a one-line verdict.

set -u

DURATION="${1:-60}"
BASELINE_DUR=15
PARALLEL_UPLOADS=4
PAYLOAD_MB=50
UPLOAD_URL="https://speed.cloudflare.com/__up"

THRESH_AVG=50     # ms — Δavg above this counts as "spikes"
THRESH_STD=20     # ms — Δstddev above this counts as "spikes"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPORT_DIR="$PROJECT_DIR/reports"
mkdir -p "$REPORT_DIR"
TS=$(date +%Y%m%d-%H%M%S)
REPORT="$REPORT_DIR/report-$TS.md"

TMPDIR=$(mktemp -d)
UPLOAD_PIDS=()

cleanup() {
  for pid in "${UPLOAD_PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  rm -rf "$TMPDIR"
}
trap cleanup EXIT INT TERM

# ---------- Topology detection ----------
EERO_IP=$(route -n get default 2>/dev/null | awk '/gateway:/ {print $2}')
INTERFACE=$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')
if [ -z "${EERO_IP:-}" ]; then
  echo "FATAL: could not detect default gateway" >&2
  exit 1
fi

MODEM_IP=$(traceroute -n -m 3 -q 1 -w 2 1.1.1.1 2>/dev/null \
  | awk '/^ *[0-9]+ / {print $2}' \
  | grep -v "^$EERO_IP$" \
  | grep -v '^\*$' \
  | head -1)

# Link type best-effort detection (macOS)
LINK_TYPE="unknown"
WIFI_DEV=$(networksetup -listallhardwareports 2>/dev/null \
  | awk '/Wi-Fi/{getline; print $2}')
ETH_DEV=$(networksetup -listallhardwareports 2>/dev/null \
  | awk '/Ethernet/{getline; print $2}' | head -1)
if [ "$INTERFACE" = "$WIFI_DEV" ]; then
  LINK_TYPE="Wi-Fi"
elif [ "$INTERFACE" = "$ETH_DEV" ]; then
  LINK_TYPE="Ethernet"
fi

echo "Router Isolation Test"
echo "====================="
echo "Interface:        $INTERFACE ($LINK_TYPE)"
echo "Router (Eero):    $EERO_IP"
echo "Upstream hop:     ${MODEM_IP:-<not detected>}"
echo "Duration:         $DURATION s loaded phase"
echo ""

# ---------- Helpers ----------
# Parses macOS BSD ping output. Returns "min|avg|max|stddev|loss_pct".
ping_stats() {
  local f="$1"
  if [ ! -s "$f" ]; then
    echo "0|0|0|0|100"
    return
  fi
  local loss_pct
  loss_pct=$(grep 'packet loss' "$f" | grep -Eo '[0-9.]+% packet loss' \
             | grep -Eo '[0-9.]+' | head -1)
  loss_pct="${loss_pct:-100}"

  local sum_line
  sum_line=$(grep -E 'round-trip|rtt' "$f" | tail -1)
  if [ -z "$sum_line" ]; then
    echo "0|0|0|0|$loss_pct"
    return
  fi
  local nums
  nums=$(echo "$sum_line" | grep -Eo '[0-9]+\.[0-9]+' | head -4 | tr '\n' ' ')
  local pmin pavg pmax pstd
  read -r pmin pavg pmax pstd <<< "$nums"
  printf "%s|%s|%s|%s|%s\n" \
    "${pmin:-0}" "${pavg:-0}" "${pmax:-0}" "${pstd:-0}" "$loss_pct"
}

awk_gt() { awk "BEGIN{exit !($1 > $2)}"; }

# ---------- Phase 1: Idle baseline ----------
echo "Phase 1: Idle baseline ($BASELINE_DUR s)..."
ping -c "$BASELINE_DUR" -i 1 "$EERO_IP" > "$TMPDIR/eero-idle.txt" 2>&1 &
PE=$!
if [ -n "$MODEM_IP" ]; then
  ping -c "$BASELINE_DUR" -i 1 "$MODEM_IP" > "$TMPDIR/modem-idle.txt" 2>&1 &
  PM=$!
fi
wait $PE
[ -n "$MODEM_IP" ] && wait $PM

# ---------- Phase 2: Saturated upload ----------
echo "Phase 2: Saturated upload ($DURATION s, $PARALLEL_UPLOADS workers)..."

# Pre-generate random payload (incompressible — avoids carrier compression skew)
dd if=/dev/urandom of="$TMPDIR/payload.bin" bs=1m count=$PAYLOAD_MB 2>/dev/null

# Launch parallel upload saturators
for i in $(seq 1 $PARALLEL_UPLOADS); do
  (
    while true; do
      curl -s -X POST --data-binary "@$TMPDIR/payload.bin" \
        --max-time 30 \
        -H "Content-Type: application/octet-stream" \
        "$UPLOAD_URL" > /dev/null 2>&1 || true
    done
  ) &
  UPLOAD_PIDS+=($!)
done

# 3s ramp-up so queues fill before we start measuring
sleep 3

# Ping both hops during saturation
ping -c "$DURATION" -i 1 "$EERO_IP" > "$TMPDIR/eero-load.txt" 2>&1 &
PE=$!
if [ -n "$MODEM_IP" ]; then
  ping -c "$DURATION" -i 1 "$MODEM_IP" > "$TMPDIR/modem-load.txt" 2>&1 &
  PM=$!
fi
wait $PE
[ -n "$MODEM_IP" ] && wait $PM

# Stop saturators
for pid in "${UPLOAD_PIDS[@]}"; do
  kill "$pid" 2>/dev/null || true
done
wait 2>/dev/null || true
UPLOAD_PIDS=()

# ---------- Analysis ----------
IFS='|' read -r ei_min ei_avg ei_max ei_std ei_loss <<< "$(ping_stats "$TMPDIR/eero-idle.txt")"
IFS='|' read -r el_min el_avg el_max el_std el_loss <<< "$(ping_stats "$TMPDIR/eero-load.txt")"
EERO_AVG_DELTA=$(awk "BEGIN{printf \"%.1f\", $el_avg - $ei_avg}")
EERO_STD_DELTA=$(awk "BEGIN{printf \"%.1f\", $el_std - $ei_std}")

if [ -n "$MODEM_IP" ]; then
  IFS='|' read -r mi_min mi_avg mi_max mi_std mi_loss <<< "$(ping_stats "$TMPDIR/modem-idle.txt")"
  IFS='|' read -r ml_min ml_avg ml_max ml_std ml_loss <<< "$(ping_stats "$TMPDIR/modem-load.txt")"
  MODEM_AVG_DELTA=$(awk "BEGIN{printf \"%.1f\", $ml_avg - $mi_avg}")
  MODEM_STD_DELTA=$(awk "BEGIN{printf \"%.1f\", $ml_std - $mi_std}")
fi

eero_spikes="no"
modem_spikes="no"
if awk_gt "$EERO_AVG_DELTA" "$THRESH_AVG" || awk_gt "$EERO_STD_DELTA" "$THRESH_STD"; then
  eero_spikes="YES"
fi
if [ -n "$MODEM_IP" ]; then
  if awk_gt "$MODEM_AVG_DELTA" "$THRESH_AVG" || awk_gt "$MODEM_STD_DELTA" "$THRESH_STD"; then
    modem_spikes="YES"
  fi
fi

if [ -n "$MODEM_IP" ]; then
  if [ "$eero_spikes" = "YES" ] && [ "$modem_spikes" = "no" ]; then
    VERDICT="**ROUTER (Eero) is the bottleneck.** Eero queue spikes under load while the upstream link stays clean."
  elif [ "$eero_spikes" = "YES" ] && [ "$modem_spikes" = "YES" ]; then
    VERDICT="**Modem or carrier is the bottleneck.** Queueing is at/above the modem; the Eero spike just reflects the shared queue upstream. Router is not the cause."
  elif [ "$eero_spikes" = "no" ] && [ "$modem_spikes" = "no" ]; then
    VERDICT="**Carrier rate-policing, no local queueing.** Neither hop shows latency rise. The carrier is dropping excess traffic rather than buffering it. Router is not the cause."
  else
    VERDICT="**Inconclusive pattern** (Eero clean, modem spikes). Rare — inspect raw logs."
  fi
else
  if [ "$eero_spikes" = "YES" ]; then
    VERDICT="**Eero queue spikes under load** (modem hop not detected — can't fully isolate)."
  else
    VERDICT="**Eero stays clean under load** — bottleneck is upstream (modem or carrier)."
  fi
fi

# ---------- Report ----------
{
  echo "# Router Isolation Test — $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo ""
  echo "## Topology"
  echo ""
  echo "- Interface: \`$INTERFACE\` ($LINK_TYPE)"
  echo "- Router (Eero LAN IP): \`$EERO_IP\`"
  echo "- 5G modem / first upstream hop: \`${MODEM_IP:-not detected}\`"
  echo "- Loaded phase duration: ${DURATION}s, $PARALLEL_UPLOADS parallel upload workers"
  echo "- Upload sink: \`$UPLOAD_URL\`"
  echo ""
  echo "## Results"
  echo ""
  echo "### Router (Eero) RTT"
  echo ""
  echo "| Phase | min (ms) | avg (ms) | max (ms) | stddev (ms) | loss (%) |"
  echo "|-------|----------|----------|----------|-------------|----------|"
  echo "| Idle  | $ei_min | $ei_avg | $ei_max | $ei_std | $ei_loss |"
  echo "| Load  | $el_min | $el_avg | $el_max | $el_std | $el_loss |"
  echo "| **Δ** | — | **$EERO_AVG_DELTA** | — | **$EERO_STD_DELTA** | — |"
  echo ""
  if [ -n "$MODEM_IP" ]; then
    echo "### 5G modem (first upstream hop) RTT"
    echo ""
    echo "| Phase | min (ms) | avg (ms) | max (ms) | stddev (ms) | loss (%) |"
    echo "|-------|----------|----------|----------|-------------|----------|"
    echo "| Idle  | $mi_min | $mi_avg | $mi_max | $mi_std | $mi_loss |"
    echo "| Load  | $ml_min | $ml_avg | $ml_max | $ml_std | $ml_loss |"
    echo "| **Δ** | — | **$MODEM_AVG_DELTA** | — | **$MODEM_STD_DELTA** | — |"
    echo ""
  fi
  echo "## Verdict"
  echo ""
  echo "$VERDICT"
  echo ""
  echo "### Decision key"
  echo ""
  echo "- **Router bottleneck**: Eero Δ large, modem Δ small — the queue is inside the Eero."
  echo "- **Modem/carrier bottleneck**: Both Δ large — queue is at/above the modem, the Eero spike just reflects upstream congestion."
  echo "- **Carrier rate-policing**: Both Δ small but throughput still collapses — carrier drops excess rather than queueing."
  echo ""
  echo "Thresholds: Δavg > ${THRESH_AVG} ms OR Δstddev > ${THRESH_STD} ms counts as a spike."
  echo ""
  echo "## Appendix: raw ping logs"
  echo ""
  for f in eero-idle eero-load modem-idle modem-load; do
    if [ -f "$TMPDIR/$f.txt" ]; then
      echo "### $f"
      echo ""
      echo '```'
      cat "$TMPDIR/$f.txt"
      echo '```'
      echo ""
    fi
  done
} > "$REPORT"

# ---------- Console summary ----------
echo ""
echo "Results"
echo "-------"
printf "  Eero   idle:  avg=%s ms  stddev=%s ms  loss=%s%%\n" "$ei_avg" "$ei_std" "$ei_loss"
printf "  Eero   load:  avg=%s ms  stddev=%s ms  loss=%s%%   (Δavg=%s, Δstd=%s)\n" \
  "$el_avg" "$el_std" "$el_loss" "$EERO_AVG_DELTA" "$EERO_STD_DELTA"
if [ -n "$MODEM_IP" ]; then
  printf "  Modem  idle:  avg=%s ms  stddev=%s ms  loss=%s%%\n" "$mi_avg" "$mi_std" "$mi_loss"
  printf "  Modem  load:  avg=%s ms  stddev=%s ms  loss=%s%%   (Δavg=%s, Δstd=%s)\n" \
    "$ml_avg" "$ml_std" "$ml_loss" "$MODEM_AVG_DELTA" "$MODEM_STD_DELTA"
fi
echo ""
echo "Verdict: $VERDICT"
echo ""
echo "Full report: $REPORT"
