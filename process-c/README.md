# Process C — Setup Daemon

Process C is the **first-time setup HTTP server** for the ShelfAware Pi.

It runs **only when the Pi has no internet** (i.e. fresh out of the box, or
after the user's WiFi credentials become invalid). The boot orchestrator
(`/orchestration/orchestrator.py`) detects the offline state, switches the
Pi into AP mode (broadcasting `ShelfAware_Setup`), and starts Process C +
Process B together.

The user joins the Pi's hotspot from their phone, opens the ShelfAware app,
and submits their home WiFi credentials + their `user_id`. Process C:

1. Validates the input.
2. Generates a fresh `shelf_id` (UUIDv4) and persists `device.json` to
   `/etc/shelfaware/device.json`.
3. Returns `202 Accepted` to the app immediately.
4. Hands the credentials to `wifi_connector.apply_wifi_credentials()` in a
   background task. wifi_connector either successfully joins the home WiFi
   (and the orchestrator restarts everything in online mode), or rolls
   back to AP mode within ~60s so the user can retry.
5. Process B reads the freshly-written `device.json` on its next start
   and registers the shelf with the backend via `POST /shelves`.

Process C dies once provisioning completes — the orchestrator kills it
when it switches to online mode.

## Endpoints

| Method | Path        | Purpose                                    |
| ------ | ----------- | ------------------------------------------ |
| GET    | `/health`   | Frontend probe to detect "I'm on a Pi"     |
| POST   | `/provision`| Submit WiFi creds + user_id                |
| POST   | `/reset`    | (debug) wipe device.json — disabled by default |

See `process_c/handlers.py` for full request/response schemas.

## Running

For local dev (no real WiFi switching):

```bash
cd process-c
python -m venv .venv
source .venv/bin/activate          # PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

export SHELFAWARE_DEV=1            # mocks wifi_connector — safe over Tailscale
export DEVICE_FILE_PATH=./device.json
export BIND_HOST=127.0.0.1
export BIND_PORT=8080

process-c
```

Then in another terminal:

```bash
curl http://127.0.0.1:8080/health
curl -X POST http://127.0.0.1:8080/provision \
  -H "Content-Type: application/json" \
  -d '{"ssid":"MyWifi","password":"hunter2","user_id":"550e8400-e29b-41d4-a716-446655440000"}'
cat ./device.json
```

In production (on the Pi) the orchestrator launches Process C with
`SHELFAWARE_DEV=0` and the default bind `0.0.0.0:80`.
