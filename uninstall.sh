#!/usr/bin/env bash
# Removes the DeviceManager client — handles both the Docker deployment and a
# native (systemd) install if present.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# --- Docker deployment ---
if command -v docker >/dev/null 2>&1 && [[ -f docker-compose.yml ]]; then
  echo "Stopping Docker client..."
  docker compose down 2>/dev/null || true
fi

# --- native (systemd) install, if it exists ---
if [[ -f /etc/systemd/system/devicemanager-client.service ]]; then
  if [[ "${EUID}" -ne 0 ]]; then
    echo "A native install exists; re-run with sudo to remove it." >&2
  else
    systemctl disable --now devicemanager-client 2>/dev/null || true
    rm -f /etc/systemd/system/devicemanager-client.service
    systemctl daemon-reload
    rm -rf /opt/devicemanager-client
    rm -f /etc/devicemanager-client.env
    echo "Native install removed."
  fi
fi

echo "Done. (Left .env and the host's authorized_keys entry in place; remove those manually if desired.)"
