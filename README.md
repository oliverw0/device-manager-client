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

## Install (Windows desktop or Server)

Works on Windows 10/11 and Windows Server 2016+. (The PowerShell console needs
OpenSSH Server, built in from Windows 10 1809 / Server 2019; on Server 2016 the
installer points you to the Win32-OpenSSH MSI.) In an
**elevated** PowerShell — the device's page on the dashboard has this prefilled:

```
$env:HOST_URL='http://your-host:8000'; $env:API_KEY='your-device-key'
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'
irm https://raw.githubusercontent.com/oliverw0/device-manager-client/main/install.ps1 | iex
```

Or from a checkout: `powershell -ExecutionPolicy Bypass -File .\install.ps1 -HostUrl http://your-host:8000 -ApiKey your-device-key`.

`install.ps1`:

- Uses an existing all-users Python 3.10+, or installs Python 3.12 (signature-checked).
- Installs to `C:\Program Files\DeviceManagerClient` (venv + code) and writes
  config to `C:\ProgramData\DeviceManagerClient\client.env` (Administrators/SYSTEM only).
- Runs a one-shot connectivity test, then registers the scheduled task
  `DeviceManagerClient` that runs the client as SYSTEM at boot and restarts it
  within 5 minutes if it ever stops.
- Optionally sets up the in-browser **PowerShell** console: installs/enables
  OpenSSH Server, opens port 22, and authorizes the host's key for the account
  you choose (`provision-ssh.ps1`, re-runnable on its own). For admin accounts
  the key goes in `C:\ProgramData\ssh\administrators_authorized_keys`, which is
  where Windows OpenSSH looks for them.

Useful commands afterwards:

```
Get-ScheduledTask DeviceManagerClient | Select-Object State
Get-Content "$env:ProgramData\DeviceManagerClient\logs\client.log" -Tail 50 -Wait
Restart-ScheduledTask DeviceManagerClient   # or Stop-/Start-ScheduledTask
```

On Windows the dashboard's console opens PowerShell 7 (`pwsh`) if installed,
otherwise Windows PowerShell. Container controls work with Docker Desktop or
Docker Engine. **Install Windows updates** installs pending updates through the
Windows Update API (as a one-off SYSTEM task, since Windows blocks installs from
SSH sessions) and never reboots — it tells you when a restart is needed.
**Update client** re-runs `install.ps1 -Update`. Both need the authorized account
to be a local administrator.

Uninstall: `powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\DeviceManagerClient\uninstall.ps1"`
(add `-RemoveConfig` to delete the config and logs too).

## Configuration (.env)

| Variable | Default | Purpose |
|----------|---------|---------|
| `HOST_URL` | (required) | Host URL including port, e.g. `http://10.0.0.5:8000`. |
| `API_KEY` | (required) | Per-device key from the dashboard. |
| `REPORT_INTERVAL` | `60` | Seconds between reports. |
| `SSH_LOG_WINDOW_MINUTES` | `15` | Window for counting recent SSH logins. |
| `REQUEST_TIMEOUT_SECONDS` | `10` | Per-request timeout when posting a report. |
| `LOG_FILE` | (unset) | Also log to this file (rotated at 1 MB). Set by the Windows installer. |
| `SSH_USER` | (unset) | Windows: the account the host uses for updates; written by `install.ps1` when it authorizes the key. |
| `SSH_EXTRA_USERS` | (unset) | Windows: extra comma-separated logins for the console dropdown, e.g. domain accounts (`CORP\alice`). |

On Windows the same keys live in `C:\ProgramData\DeviceManagerClient\client.env`;
restart the scheduled task after editing it.

## What gets reported

- System: hostname, CPU/memory/disk percent, uptime, primary LAN IP.
- Tailscale: read from the local `tailscaled` socket (no CLI needed). Reports
  "No Tailscale client" if not present. Override the socket path with
  `TAILSCALE_SOCKET`.
- SSH auth: recent accepted/failed logins from the journal or auth log.
- Docker containers: name, image, status, CPU/memory, and Compose stack.

On Windows the same report is built from Windows sources: the system drive for
disk usage, the `tailscale` CLI, the `OpenSSH/Operational` event log for SSH
logins, enabled local accounts (administrators first) for the console's user
list, and Windows Update (pending updates from the local cache, last
check/install, reboot pending) in place of apt.

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
Windows: the dashboard's **Update client** button, or
`powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\DeviceManagerClient\install.ps1" -Update`.
