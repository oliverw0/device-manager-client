#!/usr/bin/env bash
# Authorize the DeviceManager dashboard's SSH key on THIS machine, so the
# in-browser terminal can connect. Safe to run on any monitored box (Docker
# client or native) and safe to re-run (won't duplicate the key).
#
# Usage:
#   sudo ./provision-ssh.sh                       # interactive
#   sudo ./provision-ssh.sh http://host:8000 root # non-interactive
#   sudo HOST_URL=http://host:8000 SSH_USER=oliver ./provision-ssh.sh
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (sudo ./provision-ssh.sh)" >&2
  exit 1
fi

HOST_URL="${1:-${HOST_URL:-}}"
SSH_USER="${2:-${SSH_USER:-}}"

# Fall back to the host URL an installed client already uses (Docker .env in the
# client dir, or the native /etc env file).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for envfile in "${SCRIPT_DIR}/.env" /etc/devicemanager-client.env; do
  if [[ -z "${HOST_URL}" && -f "${envfile}" ]]; then
    HOST_URL="$(grep -E '^HOST_URL=' "${envfile}" | cut -d= -f2- || true)"
  fi
done
if [[ -z "${HOST_URL}" ]]; then
  read -rp "DeviceManager host URL (e.g. http://192.168.50.225:8000): " HOST_URL
fi
HOST_URL="${HOST_URL%/}"
if [[ "${HOST_URL}" != http://* && "${HOST_URL}" != https://* ]]; then
  HOST_URL="http://${HOST_URL}"
fi

if [[ -z "${SSH_USER}" ]]; then
  read -rp "Local user to authorize [root]: " SSH_USER
fi
SSH_USER="${SSH_USER:-root}"

if ! id "${SSH_USER}" >/dev/null 2>&1; then
  echo "User '${SSH_USER}' does not exist on this machine." >&2
  exit 1
fi

# Ensure an SSH server exists (LXC/containers often ship without one).
if ! command -v sshd >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    echo "Installing openssh-server..."
    apt-get update -y && apt-get install -y openssh-server
  else
    echo "WARNING: no sshd found and can't auto-install it; install an SSH server manually." >&2
  fi
fi
systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd 2>/dev/null || true

echo "Fetching the dashboard's public key from ${HOST_URL}/api/v1/ssh-pubkey..."
PUBKEY="$(curl -fsS "${HOST_URL}/api/v1/ssh-pubkey" || true)"
if [[ -z "${PUBKEY}" ]]; then
  echo "ERROR: could not fetch the host public key from ${HOST_URL}." >&2
  echo "       Check the URL is reachable from this machine, then re-run." >&2
  exit 1
fi

USER_HOME="$(getent passwd "${SSH_USER}" | cut -d: -f6)"
SSH_DIR="${USER_HOME}/.ssh"
AUTH_KEYS="${SSH_DIR}/authorized_keys"
install -d -m 700 "${SSH_DIR}"
touch "${AUTH_KEYS}"
if grep -qF "${PUBKEY}" "${AUTH_KEYS}"; then
  echo "Key already authorized in ${AUTH_KEYS}"
else
  printf '%s\n' "${PUBKEY}" >> "${AUTH_KEYS}"
  echo "Added dashboard key to ${AUTH_KEYS}"
fi
chmod 600 "${AUTH_KEYS}"
chown -R "${SSH_USER}:$(id -gn "${SSH_USER}")" "${SSH_DIR}"

# Root key login is blocked outright when PermitRootLogin is "no".
if [[ "${SSH_USER}" == "root" ]] && grep -Eiq '^[[:space:]]*PermitRootLogin[[:space:]]+no' /etc/ssh/sshd_config 2>/dev/null; then
  echo
  echo "NOTE: sshd has 'PermitRootLogin no' — root key login is blocked."
  echo "      Either set it to 'prohibit-password' and run: systemctl restart ssh"
  echo "      or re-run this script for a non-root user."
fi

echo
echo "Done. '${SSH_USER}' on this machine now authorizes the DeviceManager host."
echo "Enable SSH for this device in the dashboard, then click the terminal icon."
