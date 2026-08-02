#!/bin/bash
# Install the live status page as its own systemd service.
#
# Run ON THE PI:  bash install-status.sh
#
# Deliberately a SEPARATE unit from muse-record, with hard resource limits. The
# recorder is the fragile, valuable thing; this page is a convenience and must
# never be able to starve or destabilise it.

set -euo pipefail

USER_NAME="$(id -un)"
HOME_DIR="$(getent passwd "${USER_NAME}" | cut -d: -f6)"
VENV="${VENV:-${HOME_DIR}/muse-env}"
PORT="${PORT:-8080}"
SRC="$(dirname "$(readlink -f "$0")")/muse_status.py"

[[ -f "${SRC}" ]] || { echo "muse_status.py not found next to this script" >&2; exit 1; }
sudo -n true 2>/dev/null || { echo "needs sudo — run 'sudo -v' first" >&2; exit 1; }

echo "==> installing script"
DEST="${HOME_DIR}/muse_status.py"
if [[ "${SRC}" -ef "${DEST}" ]]; then
    # Already in place (installer run from the home directory) — nothing to copy.
    chmod 755 "${DEST}"
else
    install -m 755 "${SRC}" "${DEST}"
fi
python3 -c "import ast;ast.parse(open('${DEST}').read())" && echo "    syntax OK"

echo "==> checking dependencies (stdlib + pylsl/numpy/scipy, all already present)"
"${VENV}/bin/python3" -c "import pylsl, numpy, scipy; print('    deps OK')"

echo "==> systemd unit"
sudo tee /etc/systemd/system/muse-status.service >/dev/null <<UNIT
[Unit]
Description=Muse live status page
After=network-online.target
# Intentionally NOT Requires=muse-record: the page must be reachable even when
# the recorder is down — that is exactly when you want to look at it.

[Service]
Type=simple
User=${USER_NAME}
Environment=PORT=${PORT}
ExecStart=${VENV}/bin/python3 ${HOME_DIR}/muse_status.py
Restart=always
RestartSec=10

# Hard caps. The recorder must always win a contest for resources.
CPUQuota=25%
MemoryMax=256M
Nice=10

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now muse-status.service
sleep 2
systemctl is-active muse-status.service

echo
echo "Status page:  http://$(hostname -I | awk '{print $1}'):${PORT}/"
