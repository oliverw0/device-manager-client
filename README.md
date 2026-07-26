# DeviceManager — Client

Monitoring agent for [device-manager](https://github.com/oliverw0/device-manager).
Runs on a machine, VM, LXC or container and pushes a periodic report to the
host: CPU / memory / disk, uptime, Tailscale status, recent SSH auth activity,
and Docker containers (with per-container CPU/memory and Compose stack).

The agent only makes outbound requests to the host. It never listens for
inbound connections.

## Requirements

- Docker and Docker Compose v2 (default deployment), or Python 3.12 for the
  native install on a box without Docker.
- An API key for this device, created on the host dashboard.

## Install (Docker)

```
git clone https://github.com/oliverw0/device-manager-client && cd device-manager-client
sudo HOST_URL=http://your-host:8000 API_KEY=your-device-key ./install.sh
```

`install.sh` prepares the host and starts the client:

- Writes `.env` from the values you pass (or prompts if omitted).
- Enables the Tailscale socket mount only if Tailscale is running on this
  machine, so `docker compose` never fails on a missing socket.
- Optionally authorizes the dashboard's SSH key for the in-browser terminal.
- Runs `docker compose up -d --build`.

`sudo` is only needed if you enable SSH provisioning; otherwise plain Docker
access is enough. Useful commands afterwards:

```
docker compose logs -f devicemanager-client
docker compose restart devicemanager-client
docker compose down
```

## Install (native, no Docker)

```
sudo ./install-native.sh
```

Installs a virtualenv and a systemd unit (`devicemanager-client`). Do not run
both the Docker and native installs on one machine — they would report as the
same device twice.

## Configuration (.env)

| Variable | Default | Purpose |
|----------|---------|---------|
| `HOST_URL` | (required) | Host URL including port, e.g. `http://10.0.0.5:8000`. |
| `API_KEY` | (required) | Per-device key from the dashboard. |
| `REPORT_INTERVAL` | `60` | Seconds between reports. |
| `SSH_LOG_WINDOW_MINUTES` | `15` | Window for counting recent SSH logins. |
| `REQUEST_TIMEOUT_SECONDS` | `10` | Per-request timeout when posting a report. |

## What gets reported

- System: hostname, CPU/memory/disk percent, uptime, primary LAN IP.
- Tailscale: read from the local `tailscaled` socket (no CLI needed). Reports
  "No Tailscale client" if not present. Override the socket path with
  `TAILSCALE_SOCKET`.
- SSH auth: recent accepted/failed logins from the journal or auth log.
- Docker containers: name, image, status, CPU/memory, and Compose stack.

Any collector that has nothing to read degrades to empty rather than failing.

Data sources inside a container are limited to what you mount. `docker.sock` is
mounted by default (for container info); Tailscale and SSH-log visibility need
extra mounts — `install.sh` handles the Tailscale one automatically when
present.

## In-browser SSH / container controls

The host can open a terminal and run container start/stop/restart/logs over SSH.
This requires SSH access to the machine. `install.sh` offers to set it up; to do
it later or on a Docker host, run:

```
sudo ./provision-ssh.sh
```

It installs an SSH server if missing, fetches the host's public key, and adds it
to the chosen user's `authorized_keys`. Then enable SSH for the device on its
dashboard page.

## Connectivity self-test

```
cd /opt/devicemanager-client   # native install path; for Docker use the repo dir
./venv/bin/python -m client.main --once
```

Sends one report and prints the result, including the Tailscale state it read.

## Updating

Docker: `git pull && docker compose up -d --build`.
Native: `git pull && sudo ./install-native.sh`.
