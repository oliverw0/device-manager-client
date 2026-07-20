#!/usr/bin/env bash
# Native install for the DeviceManager client, run as root on the target VM/machine.
# Usage: sudo ./install.sh
# Non-interactive: sudo HOST_URL=http://host:8000 API_KEY=xxx ./install.sh
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

HOST_URL="${HOST_URL:-}"
API_KEY="${API_KEY:-}"
REPORT_INTERVAL="${REPORT_INTERVAL:-60}"

if [[ -z "${HOST_URL}" ]]; then
  read -rp "DeviceManager host URL (e.g. http://10.0.0.5:8000): " HOST_URL
fi
if [[ -z "${API_KEY}" ]]; then
  read -rp "API key for this device (from the host dashboard): " API_KEY
fi

cat > "${ENV_FILE}" <<EOF
HOST_URL=${HOST_URL}
API_KEY=${API_KEY}
REPORT_INTERVAL=${REPORT_INTERVAL}
SSH_LOG_WINDOW_MINUTES=15
REQUEST_TIMEOUT_SECONDS=10
EOF
chmod 600 "${ENV_FILE}"

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

systemctl daemon-reload
systemctl enable --now devicemanager-client

echo
echo "Installed and started. Check status with: systemctl status devicemanager-client"
echo "Logs: journalctl -u devicemanager-client -f"
echo "Re-test connectivity any time with:"
echo "  (cd ${INSTALL_DIR} && set -a && . ${ENV_FILE} && set +a && ./venv/bin/python -m client.main --once)"
