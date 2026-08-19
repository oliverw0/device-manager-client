#!/usr/bin/env bash
# NATIVE install (systemd + venv) for boxes without Docker. For the normal
# Docker deployment use ./install.sh instead. Do NOT run both on one machine —
# they'd start two clients reporting as the same device.
# Usage: sudo ./install-native.sh
# Non-interactive: sudo HOST_URL=http://host:8000 API_KEY=xxx ./install-native.sh
set -euo pipefail

INSTALL_DIR="/opt/devicemanager-client"
ENV_FILE="/etc/devicemanager-client.env"
SERVICE_FILE="/etc/systemd/system/devicemanager-client.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root (sudo ./install.sh)" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but was not found on PATH" >&2
  exit 1
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    echo "Installing python3-venv via apt-get..."
    apt-get update -y && apt-get install -y python3-venv
  else
    echo "python3-venv is required but not available; install it manually" >&2
    exit 1
  fi
fi

echo "Installing to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
cp -r "${SCRIPT_DIR}/client" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"

python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --no-cache-dir --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --no-cache-dir -r "${INSTALL_DIR}/requirements.txt"

# --- config: write ${ENV_FILE} on first run, then reuse it so your HOST_URL /
# API_KEY persist across reinstalls. Pass HOST_URL=... API_KEY=... to overwrite,
# or delete ${ENV_FILE} to reconfigure from scratch. ---
if [[ -f "${ENV_FILE}" && -z "${HOST_URL:-}" && -z "${API_KEY:-}" ]]; then
  echo "${ENV_FILE} already exists — keeping it (delete it to reconfigure)."
  set -a; . "${ENV_FILE}"; set +a   # load saved values for the connectivity test below
else
  HOST_URL="${HOST_URL:-}"
  API_KEY="${API_KEY:-}"
  REPORT_INTERVAL="${REPORT_INTERVAL:-60}"

  if [[ -z "${HOST_URL}" ]]; then
    echo "Enter the DeviceManager host URL. Include the port. Example: http://192.168.50.225:8000"
    read -rp "Host URL: " HOST_URL
  fi
  if [[ -z "${API_KEY}" ]]; then
    read -rp "API key for this device (from the host dashboard): " API_KEY
  fi

  # Reject empties and add http:// if the user typed a bare host:port.
  if [[ -z "${HOST_URL}" || -z "${API_KEY}" ]]; then
    echo "HOST_URL and API_KEY are both required. Aborting." >&2
    exit 1
  fi
  HOST_URL="${HOST_URL%/}"
  if [[ "${HOST_URL}" != http://* && "${HOST_URL}" != https://* ]]; then
    HOST_URL="http://${HOST_URL}"
  fi

  cat > "${ENV_FILE}" <<EOF
HOST_URL=${HOST_URL}
API_KEY=${API_KEY}
REPORT_INTERVAL=${REPORT_INTERVAL}
SSH_LOG_WINDOW_MINUTES=15
REQUEST_TIMEOUT_SECONDS=10
EOF
  chmod 600 "${ENV_FILE}"
  echo "Wrote ${ENV_FILE}"
fi
REPORT_INTERVAL="${REPORT_INTERVAL:-60}"  # fallback if an older env file omitted it

echo
echo "Using these settings:"
echo "  HOST_URL = ${HOST_URL}"
echo "  API_KEY  = ${API_KEY:0:6}… (${#API_KEY} chars)"
echo

cp "${SCRIPT_DIR}/devicemanager-client.service" "${SERVICE_FILE}"

# Preflight: run one real report (exact URL + key + endpoint) before enabling the
# service, so a bad URL / firewall / wrong key is reported now instead of being
# buried in the journal later.
echo
echo "Running a one-shot connectivity test to ${HOST_URL}..."
if ( cd "${INSTALL_DIR}" && HOST_URL="${HOST_URL}" API_KEY="${API_KEY}" \
     REPORT_INTERVAL="${REPORT_INTERVAL}" ./venv/bin/python -m client.main --once ); then
  echo "  connectivity test PASSED"
else
  echo
  echo "  connectivity test FAILED (see the error above)."
  echo "  The service will still be installed and will keep retrying once started."
  echo "  Fix HOST_URL / API_KEY in ${ENV_FILE} then run: systemctl restart devicemanager-client"
fi

# --- Optional: provision the dashboard's SSH key for in-browser terminal access ---
ENABLE_SSH="${ENABLE_SSH:-}"
if [[ -z "${ENABLE_SSH}" ]]; then
  read -rp "Enable in-browser SSH terminal access from the dashboard? [y/N]: " ENABLE_SSH
fi
if [[ "${ENABLE_SSH}" =~ ^[Yy] ]]; then
  SSH_USER="${SSH_USER:-}"
  if [[ -z "${SSH_USER}" ]]; then
    read -rp "Which local user should the dashboard be able to SSH in as? [root]: " SSH_USER
  fi
  SSH_USER="${SSH_USER:-root}"
  # Single source of truth for provisioning; also usable standalone on Docker hosts.
  HOST_URL="${HOST_URL}" SSH_USER="${SSH_USER}" bash "${SCRIPT_DIR}/provision-ssh.sh" || \
    echo "  SSH provisioning failed; you can re-run ${SCRIPT_DIR}/provision-ssh.sh later." >&2
fi

systemctl daemon-reload
systemctl enable devicemanager-client
# Use restart (not `enable --now`): if the service was already running from a
# previous install, `start` is a no-op and it would keep the STALE environment
# it loaded at its original start. restart always reloads the new env file.
systemctl restart devicemanager-client

echo
echo "Installed and started. Check status with: systemctl status devicemanager-client"
echo "Logs: journalctl -u devicemanager-client -f"
echo "Re-test connectivity any time with:"
echo "  (cd ${INSTALL_DIR} && set -a && . ${ENV_FILE} && set +a && ./venv/bin/python -m client.main --once)"
