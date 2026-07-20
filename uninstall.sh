#!/usr/bin/env bash
# Removes the DeviceManager client native install. Run as root.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root (sudo ./uninstall.sh)" >&2
  exit 1
fi

systemctl disable --now devicemanager-client 2>/dev/null || true
rm -f /etc/systemd/system/devicemanager-client.service
systemctl daemon-reload

rm -rf /opt/devicemanager-client
rm -f /etc/devicemanager-client.env

echo "devicemanager-client removed."
