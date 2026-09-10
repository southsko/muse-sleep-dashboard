#!/bin/bash
# One-shot setup of the Muse S Athena recorder on a fresh Raspberry Pi.
#
#   1. Flash Raspberry Pi OS (64-bit), enabling SSH + 5 GHz WiFi in the imager
#   2. Copy this whole `pi/` directory to the Pi
#   3. On the Pi:   bash install.sh
#
# Provisions the venv + OpenMuse, the recorder, the live status page, and the
# Samba share the analysis server reads from — then verifies it. Supply the
# Athena's MAC and the Samba password up front to run unattended:
#
#   MUSE_MAC=00:55:DA:.. SMB_PASSWORD=secret bash install.sh
#
# The MAC is optional: leave it blank and the recorder discovers the band with
# `OpenMuse find` at run time. Idempotent — safe to re-run.

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
USER_NAME="$(id -un)"
HOME_DIR="$(getent passwd "${USER_NAME}" | cut -d: -f6)"
VENV="${VENV:-${HOME_DIR}/muse-env}"
OUTDIR="${OUTDIR:-${HOME_DIR}/recordings}"
STATUS_PORT="${STATUS_PORT:-8080}"

bold() { printf '\n\033[1;36m━━ %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight --
bold "Preflight"
for f in muse_athena_record.py muse-athena-record.service install-athena.sh \
         muse_status.py install-status.sh; do
    [[ -f "${HERE}/${f}" ]] || die "${f} is missing from ${HERE} — copy the whole pi/ directory across"
done
ok "all component files present"

MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
echo "   ${MODEL}, $(uname -m), $(free -h | awk '/Mem:/{print $2}') RAM"

if ! sudo -n true 2>/dev/null; then
    echo "   this needs sudo — you will be prompted once"
    sudo -v || die "sudo unavailable"
fi
ok "sudo available"

# THE radio check. WiFi and Bluetooth share one antenna on a Pi; a continuous
# 256 Hz BLE stream competing with network traffic on 2.4 GHz corrupts frames and
# collapses the link. 5 GHz gets the traffic out of Bluetooth's band.
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

# A USB BLE dongle bypasses the onboard UART radio entirely (where frame
# corruption happens) and is the fix if overnight drops persist.
if lsusb 2>/dev/null | grep -qi bluetooth; then
    ok "USB Bluetooth dongle detected — to make it the only adapter:"
    echo "       echo 'dtoverlay=disable-bt' | sudo tee -a /boot/firmware/config.txt && sudo reboot"
else
    echo "   onboard Bluetooth (no USB dongle)"
fi

# ------------------------------------------------------------------ inputs --
bold "Configuration"
# The Athena MAC is optional — the recorder can discover it with `OpenMuse find`.
MUSE_MAC="${MUSE_MAC:-}"
if [[ -n "${MUSE_MAC}" ]]; then
    [[ "${MUSE_MAC}" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] || die "'${MUSE_MAC}' is not a MAC"
    ok "Athena MAC ${MUSE_MAC}"
else
    warn "no MAC given — the recorder will discover the band at run time (fine)"
fi

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

# -------------------------------------------------------------- base packages --
bold "System packages (venv tooling + Samba)"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    python3-venv python3-pip git samba samba-common-bin
ok "installed"

# ----------------------------------------------------------------- venv ----
bold "Python virtual environment (${VENV})"
[[ -d "${VENV}" ]] || python3 -m venv "${VENV}"
"${VENV}/bin/pip" install --quiet --upgrade pip
mkdir -p "${OUTDIR}"
ok "venv ready"

# ------------------------------------------------------- recorder (OpenMuse) --
# install-athena.sh installs OpenMuse + its system deps, the recorder script,
# its systemd unit and the Bluetooth sudoers rule. Install without starting; we
# start everything together at the end after the share is up.
bold "Recorder"
MUSE_MAC="${MUSE_MAC}" VENV="${VENV}" bash "${HERE}/install-athena.sh" --no-start

# ----------------------------------------------------------------- samba ---
bold "Samba share"
printf '%s\n%s\n' "${SMB_PASSWORD}" "${SMB_PASSWORD}" | sudo smbpasswd -s -a "${USER_NAME}" >/dev/null
if ! grep -q '^\[recordings\]' /etc/samba/smb.conf; then
    sudo tee -a /etc/samba/smb.conf >/dev/null <<SMB

[recordings]
   path = ${OUTDIR}
   browseable = yes
   read only = yes
   guest ok = no
   valid users = ${USER_NAME}
SMB
    ok "share added"
else
    ok "share already configured"
fi
sudo systemctl restart smbd
ok "smbd $(systemctl is-active smbd)"

# ----------------------------------------------------------- status page ---
bold "Live status page"
STATUS_PORT="${STATUS_PORT}" VENV="${VENV}" bash "${HERE}/install-status.sh" >/dev/null 2>&1 \
    && ok "installed on :${STATUS_PORT}" \
    || warn "status page install failed — recorder is unaffected, retry: bash install-status.sh"

# ------------------------------------------------------------------ start --
bold "Starting the recorder"
sudo systemctl start muse-athena-record
sleep 3

# ------------------------------------------------------------------ verify --
bold "Verification"
FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else warn "$1 — FAILED"; FAIL=1; fi; }
check "muse-athena-record running"      "systemctl is-active --quiet muse-athena-record"
check "muse-athena-record enabled"      "systemctl is-enabled --quiet muse-athena-record"
check "muse-status running"             "systemctl is-active --quiet muse-status"
check "smbd running"                    "systemctl is-active --quiet smbd"
check "bluetooth adapter up"            "hciconfig hci0 | grep -q 'UP RUNNING'"
check "recordings directory exists"     "test -d ${OUTDIR}"
check "OpenMuse importable"             "${VENV}/bin/python -c 'import OpenMuse'"
check "status page responding"          "curl -sf -o /dev/null http://localhost:${STATUS_PORT}/"

IP="$(hostname -I | awk '{print $1}')"
bold "Done"
if [[ "${FAIL}" -eq 0 ]]; then
    echo "   Everything is up. Put the charged headband on and the recorder latches"
    echo "   onto it within about a minute."
else
    warn "some checks failed — see above; journalctl -u muse-athena-record -f"
fi

cat <<NEXT

   Status page   http://${IP}:${STATUS_PORT}/
   Watch logs    journalctl -u muse-athena-record -f
   Recordings    ${OUTDIR}
   SMB share     //${IP}/recordings  (user ${USER_NAME})

   On the analysis host, point the mount at this Pi and restart the container:

     PI_HOST=${IP} bash install-mount.sh
     docker compose up -d --force-recreate

NEXT
