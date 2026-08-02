#!/bin/bash
# Provision a Raspberry Pi 5 as the Muse EEG recorder.
#
# Run ON THE PI, as the normal user (not root):
#   bash setup-pi5.sh
#
# Idempotent — safe to re-run. Installs the venv + muselsl, the supervised
# recorder script, the systemd unit, and the Samba share the server reads from.

set -euo pipefail

# ldconfig lives in /sbin, which is not on a normal user's PATH on Debian.
# Without this, the liblsl verification below silently "fails" on a perfectly
# good build and aborts the whole script.
export PATH="${PATH}:/sbin:/usr/sbin"

MUSE_MAC="${MUSE_MAC:-XX:XX:XX:XX:XX:XX}"
USER_NAME="$(id -un)"
HOME_DIR="$(getent passwd "${USER_NAME}" | cut -d: -f6)"
VENV="${HOME_DIR}/muse-env"
OUTDIR="${HOME_DIR}/recordings"
SCRIPT_SRC="$(dirname "$(readlink -f "$0")")/muse-autorecord.sh"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }

# Fail fast rather than hanging forever on an invisible password prompt when
# run non-interactively (over ssh, from a script, etc).
if ! sudo -n true 2>/dev/null; then
    echo "This script needs sudo. Either run it from a terminal where you can" >&2
    echo "type your password, or prime the cache first:  sudo -v" >&2
    exit 1
fi

# ---------------------------------------------------------------- sanity ----
say "host check"
tr -d '\0' < /proc/device-tree/model 2>/dev/null; echo
echo "arch: $(uname -m)   ram: $(free -h | awk '/Mem:/{print $2}')"

say "radio check — THIS IS THE ONE THAT MATTERS"
# nmcli ships with Raspberry Pi OS / Debian by default; iw usually does not
# (and this check runs before apt could install it).
FREQ=""
if command -v nmcli >/dev/null 2>&1; then
    FREQ=$(nmcli -t -f ACTIVE,FREQ dev wifi list 2>/dev/null |
           awk -F: '$1=="yes"{gsub(/[^0-9]/,"",$2); print $2; exit}')
fi
if [[ -z "${FREQ}" ]] && command -v iw >/dev/null 2>&1; then
    FREQ=$(iw dev wlan0 link 2>/dev/null | awk '/freq:/{print $2}')
fi

if true; then
    if [[ -z "${FREQ}" ]]; then
        warn "could not determine WiFi band — check manually with: nmcli dev wifi list"
    elif [[ "${FREQ}" -lt 3000 ]]; then
        warn "wlan0 is on ${FREQ} MHz — that is the 2.4 GHz band."
        warn "WiFi and Bluetooth share one antenna on the Pi. Streaming BLE while"
        warn "all network traffic fights for 2.4 GHz is what killed the Pi 3B."
        warn "MOVE THIS PI TO YOUR 5 GHz SSID before relying on it overnight."
    else
        echo "wlan0 on ${FREQ} MHz (5 GHz band) — good, Bluetooth has 2.4 GHz to itself"
    fi
fi

say "bluetooth adapters"
hciconfig -a 2>/dev/null | grep -E "^hci|Bus:|UP|DOWN" || warn "no adapters found"
if lsusb 2>/dev/null | grep -qi bluetooth; then
    echo "USB Bluetooth dongle detected."
    echo "To make the dongle the ONLY adapter (recommended — bypasses the onboard"
    echo "UART chip entirely, which is where frame corruption happens):"
    echo "    echo 'dtoverlay=disable-bt' | sudo tee -a /boot/firmware/config.txt"
    echo "    sudo reboot"
else
    echo "No USB dongle — using onboard Bluetooth."
fi

# ------------------------------------------------------------- packages ----
say "system packages"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    python3-venv python3-pip git iw bluez samba samba-common-bin \
    build-essential cmake libbluetooth-dev

# liblsl: pylsl needs the native library, and there is no arm64 wheel that
# bundles it. Prefer the distro package; fall back to building from source.
say "liblsl"
if ldconfig -p | grep -q liblsl; then
    echo "liblsl already present"
elif sudo apt-get install -y liblsl 2>/dev/null && ldconfig -p | grep -q liblsl; then
    echo "liblsl installed from apt"
else
    warn "building liblsl from source (a few minutes)"
    tmp=$(mktemp -d)
    git clone --depth 1 https://github.com/sccn/liblsl.git "${tmp}/liblsl"
    cmake -S "${tmp}/liblsl" -B "${tmp}/build" -DCMAKE_BUILD_TYPE=Release >/dev/null
    cmake --build "${tmp}/build" -j"$(nproc)" >/dev/null
    sudo cmake --install "${tmp}/build" >/dev/null
    sudo ldconfig
    rm -rf "${tmp}"
    ldconfig -p | grep -q liblsl && echo "liblsl built and installed" \
        || { warn "liblsl install failed — muselsl will not work"; exit 1; }
fi

# ----------------------------------------------------------------- venv ----
say "python environment"
[[ -d "${VENV}" ]] || python3 -m venv "${VENV}"
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet muselsl bleak pylsl

# pylsl looks for the native library inside its own package directory first and
# only then falls back to the system loader — a source-built liblsl in
# /usr/local/lib is NOT found, and every import dies with
#   OSError: .../pylsl/liblsl64.so: cannot open shared object file
# Point it at the real library explicitly.
# Locate the package by PATH, not by importing it — importing pylsl is exactly
# what fails here, so `import pylsl; pylsl.__file__` can never work.
PYLSL_DIR="$(ls -d "${VENV}"/lib/python*/site-packages/pylsl 2>/dev/null | head -1)"
LIBLSL="$(/sbin/ldconfig -p | awk '/liblsl\.so\.2/{print $NF; exit}')"
[[ -z "${LIBLSL}" && -e /usr/local/lib/liblsl.so.2 ]] && LIBLSL=/usr/local/lib/liblsl.so.2

if [[ -n "${PYLSL_DIR}" && -n "${LIBLSL}" ]]; then
    ln -sf "${LIBLSL}" "${PYLSL_DIR}/liblsl64.so"
    echo "linked ${LIBLSL} -> ${PYLSL_DIR}/liblsl64.so"
else
    warn "could not link liblsl (pylsl dir='${PYLSL_DIR}' lib='${LIBLSL}')"
fi

if "${VENV}/bin/python3" -c "import pylsl; pylsl.resolve_streams" >/dev/null 2>&1; then
    echo "pylsl imports cleanly"
else
    warn "pylsl still cannot load liblsl — muselsl will not work"
    "${VENV}/bin/python3" -c "import pylsl" 2>&1 | tail -3
    exit 1
fi

# --------------------------------------------------------------- recorder --
say "recorder script"
mkdir -p "${OUTDIR}"
DEST="${HOME_DIR}/muse-autorecord.sh"
if [[ ! -f "${SCRIPT_SRC}" ]]; then
    warn "muse-autorecord.sh not found next to this script — copy it manually"
elif [[ "${SCRIPT_SRC}" -ef "${DEST}" ]]; then
    # Already in place (script run from the home directory) — nothing to copy.
    chmod 755 "${DEST}"
    bash -n "${DEST}" && echo "already in place, syntax OK"
else
    [[ -f "${DEST}" ]] && cp "${DEST}" "${DEST}.bak-$(date +%Y%m%d%H%M%S)"
    install -m 755 "${SCRIPT_SRC}" "${DEST}"
    bash -n "${DEST}" && echo "installed + syntax OK"
fi

# passwordless hciconfig, so the recorder can reset a wedged adapter
say "sudoers rule for adapter reset"
echo "${USER_NAME} ALL=(root) NOPASSWD: /usr/bin/hciconfig, /bin/systemctl restart bluetooth" \
    | sudo tee /etc/sudoers.d/muse-record >/dev/null
sudo chmod 440 /etc/sudoers.d/muse-record
sudo visudo -c -f /etc/sudoers.d/muse-record

# ---------------------------------------------------------------- systemd --
say "systemd unit"
sudo tee /etc/systemd/system/muse-record.service >/dev/null <<UNIT
[Unit]
Description=Muse EEG auto-record
After=bluetooth.target network-online.target
Wants=bluetooth.target network-online.target

[Service]
Type=simple
User=${USER_NAME}
Environment=MUSE_MAC=${MUSE_MAC}
Environment=OUTDIR=${OUTDIR}
Environment=VENV=${VENV}
ExecStart=${HOME_DIR}/muse-autorecord.sh
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable muse-record.service
echo "unit installed and enabled (not started yet)"

# ----------------------------------------------------------------- samba ---
say "samba share"
if ! grep -q '^\[recordings\]' /etc/samba/smb.conf; then
    sudo tee -a /etc/samba/smb.conf >/dev/null <<SMB

[recordings]
   path = ${OUTDIR}
   browseable = yes
   read only = yes
   guest ok = no
   valid users = ${USER_NAME}
SMB
    echo "share added"
else
    echo "share already configured"
fi
sudo systemctl restart smbd
echo "Set the Samba password if you have not already:  sudo smbpasswd -a ${USER_NAME}"

# ------------------------------------------------------------------ done ---
say "done"
cat <<DONE
Next:
  1. Confirm 5 GHz:   iw dev wlan0 link | grep freq
  2. Put the headband on, then:  sudo systemctl start muse-record.service
  3. Watch it:        journalctl -u muse-record.service -f
  4. Expect a file in ${OUTDIR} growing within ~60 s.

On the server, point the mount at this Pi's address (and update
/boot/config/mount-muse-recordings.sh if you installed the persistent mount).
DONE
