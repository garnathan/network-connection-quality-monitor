#!/usr/bin/env bash
#
# run.sh — launch the web dashboard locally (for development / a quick look).
# Opens on http://localhost:8080. Ctrl-C to stop. Set ICCD_USER/ICCD_PASS for
# the login, or webmon.py generates a random password and logs it.
#
# For the always-on Raspberry Pi service, use ./deploy.sh instead.
#
# Any extra arguments are passed through to webmon.py, e.g.:
#   ./run.sh --port 9000 --download-expected-mbps 500
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/webmon.py" "$@"
