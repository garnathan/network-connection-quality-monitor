#!/usr/bin/env bash
#
# deploy.sh — install / update the web dashboard on a Raspberry Pi as a
# persistent systemd service.
#
# Idempotent: re-run to push code changes. It bundles the web daemon and the
# shared measurement backend (../probes.py) onto the Pi, installs a systemd
# service (auto-start on boot, restart on crash, watchdog on hang), and — since
# you asked the Pi to prefer a wired uplink — creates a NetworkManager ethernet
# profile that outranks Wi-Fi when a cable is plugged in.
#
# Safe to run while SSHed in over Wi-Fi: eth0 has no carrier until a cable is
# connected, so the current Wi-Fi route is untouched until you actually plug in.
#
# Usage:  ./webui/deploy.sh [ssh-target]        # default ssh-target: home-pi
#
# Pi prereqs: python3, curl, dig, ping, ip, nmcli (all standard on Raspberry
# Pi OS) + passwordless sudo for the invoking user.

set -euo pipefail

PI="${1:-home-pi}"
REMOTE_DIR="/home/pi/iccd"
SERVICE="webmon"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

echo "==> [1/6] Ensuring $REMOTE_DIR exists on $PI"
ssh "$PI" "mkdir -p '$REMOTE_DIR/data'"

echo "==> [2/6] Migrating any old iccd-webmon service"
ssh "$PI" '
if systemctl list-unit-files 2>/dev/null | grep -q "^iccd-webmon.service"; then
  sudo systemctl disable --now iccd-webmon 2>/dev/null || true
  sudo rm -f /etc/systemd/system/iccd-webmon.service
  echo "    removed old iccd-webmon service"
fi
# drop stale iccd-*-named files from the earlier layout
rm -f '"$REMOTE_DIR"'/iccd-webmon.py '"$REMOTE_DIR"'/iccd-monitor.py '"$REMOTE_DIR"'/iccd_probes.py || true'

echo "==> [3/6] Copying web daemon + shared backend + credentials"
rsync -az "$HERE/webmon.py" "$REPO/probes.py" "$PI:$REMOTE_DIR/"
ssh "$PI" "chmod +x '$REMOTE_DIR/webmon.py'"
if [ -f "$HERE/webmon.env" ]; then
  rsync -az "$HERE/webmon.env" "$PI:$REMOTE_DIR/webmon.env"
  ssh "$PI" "chmod 600 '$REMOTE_DIR/webmon.env'"
  echo "    copied webmon.env (credentials kept off git)"
else
  echo "    no local webmon.env — webmon.py will generate a random password"
  echo "    (retrieve it with: ssh $PI 'journalctl -u webmon | grep password')"
fi

echo "==> [4/6] Installing systemd service ($SERVICE): enable + restart"
rsync -az "$HERE/webmon.service" "$PI:/tmp/$SERVICE.service"
ssh "$PI" "sudo mv /tmp/$SERVICE.service /etc/systemd/system/$SERVICE.service \
  && sudo systemctl daemon-reload \
  && sudo systemctl enable $SERVICE >/dev/null 2>&1 \
  && sudo systemctl restart $SERVICE"

echo "==> [5/6] Configuring wired-preference (only applies when a cable is plugged in)"
ssh "$PI" '
if nmcli -t -f NAME connection show 2>/dev/null | grep -qx iccd-wired-prefer; then
  echo "    wired-preference profile already present"
else
  sudo nmcli connection add type ethernet ifname eth0 con-name iccd-wired-prefer \
    autoconnect yes connection.autoconnect-priority 100 \
    ipv4.method auto ipv4.route-metric 100 ipv6.method auto ipv6.route-metric 100 >/dev/null \
  && echo "    created iccd-wired-prefer (eth0 route-metric 100 beats Wi-Fi 600)"
fi'

echo "==> [6/6] Health check"
sleep 3
# No credentials here: an unauthenticated request returns 401 when the server
# is up (200 if auth is somehow disabled) — either means it is serving.
CODE=$(ssh "$PI" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/ || true")
case "$CODE" in 200|401) STATUS="up (HTTP $CODE)";; *) STATUS="NOT RESPONDING (HTTP $CODE)";; esac
HOST=$(ssh "$PI" "hostname"); IP=$(ssh "$PI" "hostname -I | awk '{print \$1}'")
echo "    localhost:8080 -> $STATUS"
echo
echo "Done. Dashboard:  http://${HOST}.local:8080   (or http://${IP}:8080)"
echo "Login: the credentials in webui/webmon.env (kept out of git)."
echo "Manage:  ssh $PI 'systemctl status $SERVICE'   |   ssh $PI 'journalctl -u $SERVICE -f'"
echo "Undo wired-preference:  ssh $PI 'sudo nmcli connection delete iccd-wired-prefer'"
