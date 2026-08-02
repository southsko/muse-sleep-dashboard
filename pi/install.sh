#!/bin/bash
# One-shot install of the Muse recorder on a fresh Raspberry Pi.
#
#   1. Flash Raspberry Pi OS / Debian, enable SSH + WiFi in the imager
#   2. Copy this whole `pi/` directory to the Pi
#   3. On the Pi:   bash install.sh
#
# Everything is prompted for, or can be supplied up front:
#
#   MUSE_MAC=00:11:22:33:44:55 SMB_PASSWORD=secret bash install.sh
#
# Idempotent — safe to re-run. Existing config is left alone unless it is wrong.

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
USER_NAME="$(id -un)"
HOME_DIR="$(getent passwd "${USER_NAME}" | cut -d: -f6)"
STATUS_PORT="${STATUS_PORT:-8080}"

bold() { printf '\n\033[1;36m━━ %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight --
bold "Preflight"

for f in setup-pi5.sh muse-autorecord.sh muse_status.py install-status.sh; do
    [[ -f "${HERE}/${f}" ]] || die "${f} is missing from ${HERE} — copy the whole pi/ directory across"
done
ok "all component scripts present"

MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
echo "   ${MODEL}, $(uname -m), $(free -h | awk '/Mem:/{print $2}') RAM"
case "${MODEL}" in
    *"Raspberry Pi 5"*) ok "Pi 5 — good" ;;
    *"Raspberry Pi 4"*) ok "Pi 4 — should be fine" ;;
    *"Raspberry Pi 3"*) warn "Pi 3: 2.4 GHz-only WiFi shares its antenna with Bluetooth." ;
                        warn "This hardware never sustained a link beyond ~15 min. Expect trouble." ;;
    *) warn "unrecognised hardware — continuing anyway" ;;
esac

# sudo up front: without this the script hangs invisibly on a password prompt
# when run over ssh with no tty.
if ! sudo -n true 2>/dev/null; then
    echo "   this needs sudo — you will be prompted once"
    sudo -v || die "sudo unavailable"
fi
ok "sudo available"

# THE radio check. WiFi and Bluetooth share one antenna on a Pi; a continuous
# 256 Hz BLE stream competing with network traffic on 2.4 GHz is what destroyed
# every early recording. 5 GHz gets the traffic out of Bluetooth's band.
FREQ=""
command -v nmcli >/dev/null 2>&1 && FREQ=$(nmcli -t -f ACTIVE,FREQ dev wifi list 2>/dev/null |
    awk -F: '$1=="yes"{gsub(/[^0-9]/,"",$2); print $2; exit}')
if [[ -z "${FREQ}" ]]; then
    warn "could not determine WiFi band — check with: nmcli dev wifi list"
elif [[ "${FREQ}" -lt 3000 ]]; then
    warn "wlan0 is on ${FREQ} MHz (2.4 GHz) — the band Bluetooth needs."
    warn "MOVE THIS PI TO YOUR 5 GHz SSID before relying on it overnight."
else
    ok "WiFi on ${FREQ} MHz (5 GHz) — Bluetooth has 2.4 GHz to itself"
fi

# ------------------------------------------------------------------ inputs --
bold "Configuration"

# The MAC is NOT stored in this repo. Without it the recorder hunts a device
# that does not exist, forever, with no error that says so.
MUSE_MAC="${MUSE_MAC:-}"
if [[ -z "${MUSE_MAC}" ]]; then
    echo "   The Muse's Bluetooth MAC is required. To find it, with the headband on:"
    echo "     bluetoothctl scan on      # look for 'MuseS-XXXX'"
    read -rp "   Muse MAC (AA:BB:CC:DD:EE:FF): " MUSE_MAC
fi
[[ "${MUSE_MAC}" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] \
    || die "'${MUSE_MAC}' is not a MAC address"
[[ "${MUSE_MAC}" =~ ^[Xx][Xx]: ]] && die "that is the scrubbed placeholder, not a real MAC"
ok "Muse MAC ${MUSE_MAC}"

# Samba serves the recordings to the analysis host. Matching the password the
# analysis host already stores means its mount config needs no changes.
SMB_PASSWORD="${SMB_PASSWORD:-}"
if [[ -z "${SMB_PASSWORD}" ]]; then
    echo "   Samba password for '${USER_NAME}' (the analysis host mounts the share with this)."
    read -rsp "   password: " SMB_PASSWORD; echo
    read -rsp "   again:    " SMB_PASSWORD2; echo
    [[ "${SMB_PASSWORD}" == "${SMB_PASSWORD2}" ]] || die "passwords do not match"
fi
[[ -n "${SMB_PASSWORD}" ]] || die "Samba password cannot be empty"
ok "Samba password set"

# ------------------------------------------------------------- provisioning --
bold "Provisioning (installs packages and builds liblsl — several minutes)"
MUSE_MAC="${MUSE_MAC}" bash "${HERE}/setup-pi5.sh"

bold "Samba"
printf '%s\n%s\n' "${SMB_PASSWORD}" "${SMB_PASSWORD}" | sudo smbpasswd -s -a "${USER_NAME}" >/dev/null
sudo systemctl restart smbd
ok "share user configured, smbd $(systemctl is-active smbd)"

bold "Live status page"
STATUS_PORT="${STATUS_PORT}" bash "${HERE}/install-status.sh" >/dev/null 2>&1 \
    && ok "installed on :${STATUS_PORT}" \
    || warn "status page install failed — recorder is unaffected, retry with: bash install-status.sh"

bold "Starting the recorder"
sudo systemctl start muse-record
sleep 4

# ------------------------------------------------------------------ verify --
bold "Verification"
FAIL=0

check() {  # description, command
    if eval "$2" >/dev/null 2>&1; then ok "$1"; else warn "$1 — FAILED"; FAIL=1; fi
}

check "muse-record running"           "systemctl is-active --quiet muse-record"
check "muse-record enabled at boot"   "systemctl is-enabled --quiet muse-record"
check "muse-status running"           "systemctl is-active --quiet muse-status"
check "smbd running"                  "systemctl is-active --quiet smbd"
check "bluetooth adapter up"          "hciconfig hci0 | grep -q 'UP RUNNING'"
check "recordings directory exists"   "test -d ${HOME_DIR}/recordings"
check "muselsl importable"            "${HOME_DIR}/muse-env/bin/python3 -c 'import muselsl'"
check "pylsl can load liblsl"         "${HOME_DIR}/muse-env/bin/python3 -c 'import pylsl; pylsl.resolve_streams'"
check "status page responding"        "curl -sf -o /dev/null http://localhost:${STATUS_PORT}/"

# The MAC actually reaching the service is the failure this script exists to
# prevent, so assert it rather than assuming setup-pi5.sh got it right.
if systemctl show muse-record -p Environment | grep -q "MUSE_MAC=${MUSE_MAC}"; then
    ok "Muse MAC baked into the service"
else
    warn "Muse MAC NOT in the service environment — the recorder will never connect"
    FAIL=1
fi

IP="$(hostname -I | awk '{print $1}')"

bold "Done"
if [[ "${FAIL}" -eq 0 ]]; then
    echo "   Everything is up. The recorder is hunting for the headband and will"
    echo "   latch on within about a minute of it being switched on."
else
    warn "some checks failed — see above; journalctl -u muse-record -f"
fi

cat <<NEXT

   Status page   http://${IP}:${STATUS_PORT}/
   Watch logs    journalctl -u muse-record -f
   Recordings    ${HOME_DIR}/recordings
   SMB share     //${IP}/recordings  (user ${USER_NAME})

   On the analysis host, point the mount at this Pi and restart the container:

     PI_HOST=${IP} bash install-mount.sh
     docker compose up -d --force-recreate

NEXT
