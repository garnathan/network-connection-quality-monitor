# webui — always-on Internet Monitor dashboard

A long-running daemon (`webmon.py`) that continuously probes the connection and
serves a self-contained web dashboard on your LAN. Built to run 24/7 on the home
Raspberry Pi (the one that also runs Pi-hole) as a persistent systemd service.

This folder holds everything needed to **run and operate the service**. It uses
the shared measurement backend `../probes.py` (the same code the CLI
`monitor.py` uses) — `deploy.sh` bundles that file onto the Pi automatically, so
the installed service is self-contained.

```
webui/
├── webmon.py        # the daemon: probes + SQLite store + web server
├── run.sh           # launch locally (dev)
├── deploy.sh        # install/update on the Pi as a persistent service
└── webmon.service   # systemd unit (auto-start, restart, watchdog)
```

## Run locally

```bash
./run.sh                       # http://localhost:8080
./run.sh --port 9000           # any webmon.py flag is passed through
```

Set the dashboard login with `ICCD_USER` / `ICCD_PASS` (see
[Credentials](#credentials)); if you don't, `webmon.py` generates a random
password at startup and prints it.

## Deploy to the Raspberry Pi

```bash
./deploy.sh home-pi            # ssh target; default: home-pi
```

`deploy.sh` copies `webmon.py` + `../probes.py` to `/home/pi/iccd/`, installs the
`webmon` systemd service (binds `:8080` — Pi-hole owns `:80/:443/:53`), and adds
a NetworkManager profile so the Pi prefers a wired uplink when a cable is plugged
in. Then:

```bash
# open the dashboard (log in with your webmon.env credentials)
http://peachypi.local:8080

ssh home-pi 'systemctl status webmon'
ssh home-pi 'journalctl -u webmon -f'
ssh home-pi 'sudo nmcli connection delete iccd-wired-prefer'   # undo wired-preference
```

## What makes it a robust persistent service

- **Auto-start on boot** (`WantedBy=multi-user.target`, enabled).
- **Restarts on any exit** (`Restart=always`, `StartLimitIntervalSec=0` — never
  gives up, even in a crash-loop).
- **Watchdog restarts it on a hang** (not just a crash): the daemon pets
  systemd's watchdog only while genuinely healthy — every probe worker alive,
  fresh samples flowing (monotonic clock, NTP-step-safe), and the HTTP dashboard
  answering a loopback request.
- **Self-healing workers**: a transient probe/DB error is logged and retried,
  not fatal.
- **`ping` works as a non-root service** via `AmbientCapabilities=CAP_NET_RAW`.
- **History survives reboots** — every sample is in SQLite (`data/webmon.db` on
  the Pi), seeded back into the live view on restart.

## Credentials

The dashboard uses HTTP basic auth. **No credentials are stored in this repo.**
Set your own in an untracked env file:

```bash
cp webmon.env.example webmon.env     # webmon.env is gitignored
# edit webmon.env → set ICCD_USER / ICCD_PASS
./deploy.sh home-pi                  # copies webmon.env to the Pi
```

If `ICCD_PASS` is left unset, `webmon.py` generates a random password at startup
and logs it (`journalctl -u webmon | grep password`) — so there is never a
shipped default password to guess.

## Configuration (env vars / flags)

| Setting | Env | Default |
|---------|-----|---------|
| Basic-auth user / pass | `ICCD_USER` / `ICCD_PASS` | see [Credentials](#credentials) |
| Plan download / upload (Mbps) | `ICCD_DOWN_EXPECTED_MBPS` / `ICCD_UP_EXPECTED_MBPS` | `200` / `34` |
| Throughput interval (s) | `ICCD_TP_INTERVAL` | `1800` |
| DB path | `ICCD_DB` (or `--db`) | `<webui>/data/webmon.db` |
| Port / host | `ICCD_PORT` / `ICCD_HOST` | `8080` / `0.0.0.0` |

Throughput is rated **relative to the plan** (good ≥ 70%, amber ≥ 40%, red
below), so a link delivering a fraction of what you pay for reads red even if the
raw number looks "fast enough". The expensive throughput test runs infrequently
and is **skipped whenever the link is already busy**, so it never interferes with
a call or stream.
