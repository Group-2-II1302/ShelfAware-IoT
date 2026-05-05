# Process C deploy runbook

Process C is the **first-time setup HTTP server**. It runs only when the
Pi is in AP (hotspot) mode, accepts WiFi credentials + a `user_id` from
the companion app, writes `device.json`, and hands off to
`orchestration/wifi_connector.py` to switch to STA mode.

> [!NOTE]
> **Three deploy paths exist for ShelfAware:**
>
> 1. **Production (full system)** — the root [`deploy/install.sh`](../../deploy/install.sh)
>    installs Process A, B, **and C** plus the orchestrator's systemd unit.
>    The orchestrator decides at boot whether to launch Process C
>    (offline / AP mode) or skip it (online / Process A + B mode).
>    **This is what real Pi installs use.** See [`deploy/README.md`](../../deploy/README.md).
> 2. **Standalone Process C (dev / test)** — the steps below. Useful for
>    testing the HTTP API without booting the whole orchestrator state
>    machine.
> 3. **Foreground / interactive run** — same as #2 but with the venv
>    pre-activated and stdout in your terminal. Best for live debugging.

---

## Prerequisites

- Python 3.11+ (`python3 --version`)
- `pip` and `venv` available
- Linux box (most testing) or Windows (CI / unit tests only — `nmcli`
  isn't available so the real WiFi switch will fail; use `SHELFAWARE_DEV=1`
  to skip the real backend and let `RealWifiBackend` import lazily)
- The repo cloned somewhere readable

For the orchestrated production path additionally:
- Raspberry Pi OS Bookworm (or other Debian-derivative with
  `network-manager` installed)
- `nmcli` working (`nmcli general status` should print without error)
- A pre-configured AP profile named `ShelfAware_Setup` — the root
  `deploy/install.sh` calls `orchestration/shelfaware_network_setup.sh`
  to create it on first install

---

## Quick start: foreground / interactive run

For development on your laptop or a Pi over Tailscale.

```bash
cd process-c
python3 -m venv .venv
source .venv/bin/activate                  # Linux / macOS
# .\.venv\Scripts\Activate.ps1              # Windows PowerShell
pip install -e '.[dev]'

# Optional: copy and edit the env file (defaults are sensible for dev)
cp deploy/process-c.env.example deploy/process-c.env
$EDITOR deploy/process-c.env

# Load env and run
set -a; source deploy/process-c.env; set +a
process-c
```

Equivalent direct invocation if you don't want the console script:
`python3 -m process_c` or `python3 process_c.py`.

You should see structured JSON logs on stdout, e.g.:

```json
{"ts":"2026-05-04T07:00:00Z","level":"INFO","logger":"process_c.main","msg":"process-c listening","bind_host":"0.0.0.0","bind_port":80}
```

Hit `Ctrl+C` to stop. Process C exits cleanly on `SIGINT`/`SIGTERM`.

> [!WARNING]
> Binding to port `80` requires root (`sudo`) on Linux. For dev runs use
> `BIND_PORT=8080` (or any port >1024) in your env file.

---

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `BIND_HOST` | `0.0.0.0` | Where the HTTP server listens. `127.0.0.1` for local-only testing. |
| `BIND_PORT` | `80` | Use `8080` (or other >1024) for non-root dev runs. |
| `DEVICE_FILE_PATH` | `/etc/shelfaware/device.json` | Provisioning state file. Process B reads this; Process C writes it. |
| `ALLOW_RESET_ENDPOINT` | `false` | When `true`, exposes `POST /reset` (deletes `device.json`). Off in production. |
| `LOG_LEVEL` | `INFO` | Standard `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`. |
| `SHELFAWARE_DEV` | `false` | When `true`, the real `wifi_connector` import is allowed to fail (so dev boxes without `nmcli` can still boot the server). |

---

## API surface

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Captive-portal landing page — serves the HTML credential form. |
| `GET` | `/health` | Liveness + provisioning status (`already_provisioned: bool`). |
| `POST` | `/provision` | Submit `{ssid, password, user_id}` as **JSON** or **form-encoded**. Writes `device.json`, returns `202` (with JSON body for JSON callers, HTML success page for form callers), then fires the WiFi switch in the background. |
| `POST` | `/reset` | (Opt-in) Deletes `device.json`. |
| `GET` | `/generate_204`, `/gen_204` | Android captive-portal probe (returns non-204 to trigger portal). |
| `GET` | `/hotspot-detect.html`, `/library/test/success.html` | iOS / macOS captive-portal probes. |
| `GET` | `/ncsi.txt`, `/connecttest.txt` | Windows captive-portal probes. |
| `OPTIONS` | `*` | CORS preflight. |

CORS is enabled (`Access-Control-Allow-Origin: *`) because the companion
app calls Process C from a browser context while connected to the Pi's
hotspot.

### Captive portal vs JSON API — which to use

There are two valid ways to drive provisioning, depending on what your
client is:

- **Native app / curl / scripts:** `POST /provision` with
  `Content-Type: application/json`. Returns JSON. This is what was
  documented in earlier revisions of this PR and remains the supported
  contract.
- **Browser, no app installed:** the user opens `http://192.168.4.1/`
  (or it auto-pops via captive-portal probes once DNS hijacking is in
  place). The HTML form there `POST`s to `/provision` as either JSON
  (preferred, via JS) or `application/x-www-form-urlencoded` (no-JS
  fallback). Either way the same handler runs; the response format
  matches the request.

The captive-portal probes only do useful work once `nmcli`/`dnsmasq`
hijacks DNS for the AP — see `orchestration/shelfaware_network_setup.sh`.
Until then, users on the hotspot must navigate to `192.168.4.1` manually.

---

## Running the test suite

```bash
cd process-c
source .venv/bin/activate
pytest -v
```

71 tests should pass. The suite uses `aiohttp.test_utils` for the HTTP
handlers (no real socket binding) and a `FakeWifiBackend` so no `nmcli`
calls happen.

---

## Deploying via the orchestrator (production)

You almost never want to deploy Process C by itself in production.
Instead, use the root installer which sets up everything:

```bash
sudo bash deploy/install.sh
sudo systemctl start shelfaware-orchestrator
journalctl -fu shelfaware-orchestrator
```

The orchestrator:
1. Pings `8.8.8.8` on boot.
2. If online → starts Process A + B (no Process C).
3. If offline → switches to AP mode, starts Process B + **Process C**.
4. When Process C calls `wifi_connector.apply_wifi_credentials`, the
   orchestrator detects the connection change and re-runs the boot
   decision.

See [`deploy/README.md`](../../deploy/README.md) (root) and
[`orchestration/orchestrator.py`](../../orchestration/orchestrator.py)
for the full state machine.

---

## Troubleshooting

**`OSError: [Errno 13] Permission denied` on bind**
Port 80 needs root. Use `BIND_PORT=8080` for dev, or `sudo` it.

**`OSError: [Errno 98] Address already in use`**
Another Process C (or other server) is already on the port. Stop it
first or pick a different port.

**`ConfigError: DEVICE_FILE_PATH ... is not writeable`**
The default `/etc/shelfaware/device.json` requires root. For dev runs
point it somewhere else: `DEVICE_FILE_PATH=/tmp/device.json`.

**`POST /provision` returns 500 with `{"error":"persistence"}`**
`device.json` couldn't be written. Check disk space, permissions on
`DEVICE_FILE_PATH`'s parent directory, and the structured log line right
before the response — it includes the underlying `OSError`.

**The WiFi switch never happens after a successful `POST /provision`**
Process C returns `202` immediately and fires the switch in a
background task. On dev machines without `nmcli` the
`RealWifiBackend.apply_wifi_credentials` call will log an error and
stop. That's expected — the server will accept the credentials and
write `device.json`, but it can't actually change the network.

**`POST /reset` returns 404**
The endpoint is gated behind `ALLOW_RESET_ENDPOINT=true`. By design;
flip it on intentionally for one-shot recovery, then off again.
