# Deploying Process B on a Raspberry Pi

This directory contains the deployment artifacts for running Process B as a managed `systemd` service on the Pi, plus the runbook for getting it there.

| File                        | Purpose                                                   |
| --------------------------- | --------------------------------------------------------- |
| `process-b.service`         | systemd unit definition.                                  |
| `process-b.env.example`     | Template for the environment file (`PI_API_KEY`, etc.).   |

> **Which deploy path should I use?**
>
> - **Production / shipping the Pi** → use the **root-level orchestrator** (`/deploy/install.sh` + `shelfaware-orchestrator.service`). The orchestrator handles AP-mode provisioning, ping-based mode switching, and starts Process A / B / C as child processes. Process B does **not** run as its own systemd service in this path — the orchestrator is its parent.
> - **Running Process B alone** (no orchestrator, no Process A on the same Pi, e.g. for isolated dev/test or a non-Pi Linux box) → use the artifacts in *this* directory. They install Process B as a standalone systemd service.
> - **Foreground / interactive run during development** → see [Quick start](#quick-start-foreground--interactive-run) below. No systemd at all.
>
> The two systemd paths are mutually exclusive — don't enable both at once or you'll have two processes fighting for the same UDP port and SQLite file.

The runbook below assumes the Pi is reached over SSH via [Tailscale](https://tailscale.com). If you're on the same LAN as the Pi, replace `<pi-tailnet-name>` with the Pi's hostname or IP everywhere — every step after SSH is identical.

---

## Prerequisites

### On your laptop

- Tailscale installed and logged into the same tailnet as the Pi.
- `tailscale status` lists the Pi (e.g. `pi-shelf-1`).
- An SSH key whose public half is in `~pi/.ssh/authorized_keys` on the Pi.

### On the Pi

- Raspberry Pi OS, recently updated (`sudo apt update && sudo apt full-upgrade -y`).
- **Python 3.11+** (`python3 --version`). Recent Raspberry Pi OS (Bookworm) ships 3.11; older Bullseye ships 3.9 and won't work without manual install.
- Tailscale running (`tailscale up` already done) and SSH allowed by your tailnet ACLs.
- Network reachability to `BACKEND_URL`.
- Process A running, or able to be started, on the same Pi — Process B is the broker, not the sampler.

Confirm prerequisites before deploying:

```bash
# From your laptop:
tailscale status                           # Pi appears in the list
ssh pi@<pi-tailnet-name>                   # SSH works
ssh pi@<pi-tailnet-name> python3 --version # 3.11+
```

If `python3 --version` shows 3.10 or older, fix that first. Everything below assumes 3.11+.

---

## Step-by-step deployment

Every command below runs **on the Pi** unless otherwise noted. SSH in once and stay there.

### Step 1 — Get the code onto the Pi

Two options. Use **A** if the Pi can reach GitHub; use **B** if it can't.

**A. Clone directly (preferred — future updates are `git pull`):**

```bash
ssh pi@<pi-tailnet-name>
cd ~
git clone https://github.com/your-org/ShelfAware-IoT.git
```

**B. Push from laptop via `scp` (firewall-isolated Pi):**

From your laptop:

```bash
cd /path/to/ShelfAware-IoT
git archive --format=tar.gz --output=shelfaware.tar.gz HEAD
scp shelfaware.tar.gz pi@<pi-tailnet-name>:~/
```

Then on the Pi:

```bash
mkdir -p ~/ShelfAware-IoT
tar -xzf ~/shelfaware.tar.gz -C ~/ShelfAware-IoT
```

> Tip: long commands (like `pip install`) over Tailscale can drop if your laptop's tailnet connection blips. Run inside `tmux` or `screen` on the Pi for safety: `tmux new -s deploy`, do the work, `Ctrl+B D` to detach, `tmux attach -t deploy` to reattach.

### Step 2 — Create the service user and directories

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin process-b

sudo install -d -o process-b -g process-b /var/lib/process-b
sudo install -d -o root -g process-b -m 0750 /etc/process-b
sudo install -d -o process-b -g process-b /opt/process-b
```

Verify:

```bash
id process-b
ls -ld /var/lib/process-b /etc/process-b /opt/process-b
```

You should see three directories with the right ownerships, and `/etc/process-b` mode `0750`.

### Step 3 — Install Process B into a venv

```bash
sudo -u process-b python3 -m venv /opt/process-b/.venv
sudo -u process-b /opt/process-b/.venv/bin/pip install --upgrade pip
sudo -u process-b /opt/process-b/.venv/bin/pip install ~/ShelfAware-IoT/process-b
```

Note the path: we install just the `process-b/` subdirectory of the repo, not the repo root.

Confirm the entrypoint:

```bash
ls -l /opt/process-b/.venv/bin/process-b
```

### Step 4 — Install the environment file

```bash
sudo cp ~/ShelfAware-IoT/process-b/deploy/process-b.env.example /etc/process-b/process-b.env
sudo nano /etc/process-b/process-b.env
```

At minimum, fill in the required vars (see the file's comments for the full list):

```
PI_API_KEY=<bearer token from the backend>
BACKEND_URL=https://api.shelfaware.example.com
DB_PATH=/var/lib/process-b/outbox.db
SHELF_IDS=550e8400-e29b-41d4-a716-446655440000
UDP_LISTEN_PORT=5005
PROC_A_CONTROL_PORT=5006
```

Save, then lock down permissions — `PI_API_KEY` is a secret:

```bash
sudo chown root:process-b /etc/process-b/process-b.env
sudo chmod 0640 /etc/process-b/process-b.env
ls -l /etc/process-b/process-b.env
```

The `process-b` user can read it; nobody else can.

### Step 5 — Smoke-test by hand (recommended)

Before letting `systemd` manage the daemon, run it once manually so you catch typos in `BACKEND_URL` or a wrong `PI_API_KEY` *before* they show up as a confusing systemd "service kept restarting" loop.

```bash
sudo -u process-b bash -c '
  set -a; . /etc/process-b/process-b.env; set +a
  /opt/process-b/.venv/bin/process-b
'
```

What you should see:

- Three startup lines as JSON: `udp listener bound`, `drainer started`, `poller started`.
- Every 3s, a poll log. If those are `commands GET` succeeding (no commands → no further output), you're healthy.

Press **Ctrl+C** to stop. Then move on.

If you see `permanent failure status=401`, your `PI_API_KEY` is wrong. If `transient failure` with a connect error, your `BACKEND_URL` is wrong or the Pi can't reach the internet.

### Step 6 — Install and enable the systemd unit

```bash
sudo cp ~/ShelfAware-IoT/process-b/deploy/process-b.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now process-b
```

`enable --now` enables auto-start at boot *and* starts the daemon immediately.

Check status:

```bash
sudo systemctl status process-b
```

Look for `Active: active (running)` and recent JSON log lines. If you see `failed` or constant restarts, see [Troubleshooting](#troubleshooting).

Tail the live logs:

```bash
sudo journalctl -u process-b -f -o cat
```

Press Ctrl+C to stop tailing — the daemon keeps running. If you have `jq` installed (`sudo apt install jq`), pipe through it for prettier output:

```bash
sudo journalctl -u process-b -f -o cat | jq
```

### Step 7 — End-to-end verification

Once the daemon is up, confirm a real datagram makes it through. From the Pi (or any machine that can reach `127.0.0.1:5005`):

```bash
echo '{"shelf_id":"550e8400-e29b-41d4-a716-446655440000","scale_index":0,"est_grams":750.2,"sampled_at":"2026-04-21T08:30:00Z"}' \
  | nc -u -w1 127.0.0.1 5005
```

Within 5 seconds (one drain tick), `journalctl` should show a `telemetry POST accepted` line. Confirm the row was deleted from the outbox after a successful POST:

```bash
sudo -u process-b sqlite3 /var/lib/process-b/outbox.db \
  "SELECT count(*) FROM pending_readings;"
```

`0` means the drainer successfully POSTed and deleted. If the count is non-zero and stays that way:

- The backend isn't accepting the POST → check the `journalctl` for transient/permanent failure logs.
- The shelf_id isn't recognized by the backend → backend's `shelf_items` table needs the `scale_index` provisioned.

---

## Tailscale-specific notes

These only matter because you're remote.

- **Don't expose the UDP listener on the tailnet.** `UDP_LISTEN_HOST=0.0.0.0` (the default) binds all interfaces. Process A is local to the Pi, so the listener doesn't need to be reachable from your laptop. For stricter security: set `UDP_LISTEN_HOST=127.0.0.1`. Cost: you can't send test datagrams from your laptop without an SSH tunnel; benefit: one less attack surface.

- **Long commands and tailnet drops.** If your Tailscale connection blips during `pip install`, `tmux` keeps the install running on the Pi. Reattach when the tailnet recovers.

- **`journalctl -f` over SSH.** Works fine; high-latency tailnets show logs in bursts rather than line-by-line. Add `--no-pager` if your terminal does anything weird with the buffer.

- **Updating later** (from your laptop):

  ```bash
  ssh pi@<pi-tailnet-name>
  cd ~/ShelfAware-IoT && git pull
  sudo -u process-b /opt/process-b/.venv/bin/pip install --upgrade ~/ShelfAware-IoT/process-b
  sudo systemctl restart process-b
  sudo systemctl status process-b
  ```

  Pending readings in `/var/lib/process-b/outbox.db` survive across upgrades.

---

## Operating

| Action                          | Command                                |
| ------------------------------- | -------------------------------------- |
| Status + last 10 log lines      | `systemctl status process-b`           |
| Tail logs live                  | `journalctl -u process-b -f`           |
| Tail with JSON pretty-print     | `journalctl -u process-b -f -o cat \| jq` |
| Stop                            | `sudo systemctl stop process-b`        |
| Start                           | `sudo systemctl start process-b`       |
| Restart (e.g. after env change) | `sudo systemctl restart process-b`     |
| Disable auto-start at boot      | `sudo systemctl disable process-b`     |

### Adding a shelf

1. Edit `/etc/process-b/process-b.env`. Append the new UUID to `SHELF_IDS` (comma-separated).
2. `sudo systemctl restart process-b`.

The drainer and listener don't need to be told — they discover `shelf_id` from datagrams. Only the poller needs the configured set.

### Reading the outbox manually

```bash
sudo -u process-b sqlite3 /var/lib/process-b/outbox.db \
  "SELECT shelf_id, scale_index, est_grams, sampled_at, reject_count
   FROM pending_readings
   ORDER BY sampled_at DESC LIMIT 20;"
```

Quarantined rows (`reject_count >= 3`) are skipped by the drainer:

```bash
sudo -u process-b sqlite3 /var/lib/process-b/outbox.db \
  "SELECT * FROM pending_readings WHERE reject_count >= 3;"
```

If they're irrecoverable (likely `unknown_scale` config), delete by hand once the underlying issue is fixed:

```bash
sudo -u process-b sqlite3 /var/lib/process-b/outbox.db \
  "DELETE FROM pending_readings WHERE reject_count >= 3;"
```

---

## What "healthy" looks like

On a quiet system (no datagrams arriving, no commands pending), you should see:

- Three startup log lines, then quiet.
- A `commands GET` log every ~3s (that's the poll cadence; no commands → no further output).
- No drainer logs unless the listener has inserted a row.
- Memory under 30 MB; CPU near 0%.

If the daemon is logging anything other than that pattern when nothing is happening, something's wrong — see below.

---

## Troubleshooting

| Symptom                                         | First thing to check                                                                              |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `systemctl status` says `Active: failed`        | `journalctl -u process-b -n 50 -o cat` — last lines tell you why.                                 |
| Service starts then stops within seconds        | Probably `ConfigError` (missing/typo'd env var). Error goes to stderr → journald.                 |
| Poller logs `permanent failure status=401`      | `PI_API_KEY` is wrong, or backend doesn't recognize the token.                                    |
| Poller logs `transient failure` repeatedly      | `BACKEND_URL` is wrong, backend is down, or the Pi has no internet.                               |
| Listener never inserts rows                     | Process A is sending to a different port. Confirm `UDP_LISTEN_PORT` matches A's config.           |
| `pending_readings` row count keeps growing      | Backend unreachable (check transient logs) **or** rows quarantined (check `reject_count` column). |
| `Permission denied` on `outbox.db`              | `ReadWritePaths=` in `process-b.service` doesn't match `DB_PATH`. They must match.                |
| `process-b` command not found                   | The venv path doesn't match `ExecStart=`. Confirm `/opt/process-b/.venv/bin/process-b` exists.    |
| `journalctl` shows nothing after start          | `systemd-journald` may have rate-limited. Check `journalctl --vacuum-size=100M` or wait it out.   |

---

## Hardening

The unit file enables a standard set of `systemd` sandbox knobs (`NoNewPrivileges`, `ProtectSystem=strict`, `MemoryDenyWriteExecute`, etc.). The daemon can only:

- Read the world (the venv, the env file).
- Write to `/var/lib/process-b` (the SQLite outbox).
- Open AF_INET / AF_INET6 / AF_UNIX sockets.

Everything else is denied. If you ever need to add a writable path, append it to `ReadWritePaths=` in the unit file.

---

## Uninstalling

```bash
sudo systemctl disable --now process-b
sudo rm /etc/systemd/system/process-b.service
sudo systemctl daemon-reload

sudo rm -rf /opt/process-b
sudo rm -rf /etc/process-b
# /var/lib/process-b contains the outbox — keep or delete deliberately.
sudo userdel process-b
```
