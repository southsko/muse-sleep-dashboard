#!/bin/bash
# Install the Muse S Athena recorder (OpenMuse) on the Pi. install.sh calls this,
# or run it directly from the copied pi/ directory:
#
#     bash install-athena.sh
#     MUSE_MAC=00:11:22:33:44:55 bash install-athena.sh
#     MUSE_NO_START=1 bash install-athena.sh    # install but don't start yet
#
# Installs OpenMuse + its system deps into the venv, the recorder script, its
# systemd unit, and the Bluetooth sudoers rule. Validated on hardware 2026-09-09
# (fw 3.1.15). Idempotent — safe to re-run.

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
USER_NAME="$(id -un)"
HOME_DIR="$(getent passwd "${USER_NAME}" | cut -d: -f6)"
VENV="${VENV:-${HOME_DIR}/muse-env}"
PRESET="${MUSE_PRESET:-p21}"     # EEG4, optics/LED OFF — low power for overnight (see README)
NO_START=0
[[ "${MUSE_NO_START:-}" == "1" || "${1:-}" == "--no-start" ]] && NO_START=1

bold() { printf '\n\033[1;36m━━ %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight --
bold "Preflight"
for f in muse_athena_record.py muse-athena-record.service; do
    [[ -f "${HERE}/${f}" ]] || die "${f} missing from ${HERE} — copy the whole pi/ directory across"
done
ok "Athena component files present"

MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
echo "   ${MODEL}, $(uname -m)"
case "${MODEL}" in
    *"Raspberry Pi 3"*) warn "Pi 3: WiFi and BLE share one 2.4 GHz antenna — a USB BLE" ;
                        warn "dongle (CSR8510) is the fix if overnight drops are frequent." ;;
esac

if ! sudo -n true 2>/dev/null; then
    echo "   this needs sudo — you will be prompted once"; sudo -v || die "sudo unavailable"
fi
ok "sudo available"

[[ -x "${VENV}/bin/python" ]] || die "venv not found at ${VENV} (run install.sh first, or set VENV=)"
ok "venv at ${VENV}"

# ------------------------------------------------------------------ inputs --
bold "Configuration"
MUSE_MAC="${MUSE_MAC:-}"
if [[ -z "${MUSE_MAC}" ]]; then
    echo "   The Athena's MAC is required (or leave blank to let the recorder discover"
    echo "   it at run time with 'OpenMuse find'). With the band on and charged:"
    echo "     ${VENV}/bin/OpenMuse find"
    read -rp "   Athena MAC (blank = discover at runtime): " MUSE_MAC || true
fi
if [[ -n "${MUSE_MAC}" ]]; then
    [[ "${MUSE_MAC}" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] || die "'${MUSE_MAC}' is not a MAC"
    [[ "${MUSE_MAC}" =~ ^[Xx][Xx]: ]] && die "that is the scrubbed placeholder, not a real MAC"
    ok "Athena MAC ${MUSE_MAC}"
else
    warn "no MAC pinned — the recorder will scan on each start (slower, less reliable)"
fi

# ------------------------------------------------------------- provisioning --
bold "OpenMuse (into ${VENV})"
if "${VENV}/bin/python" -c "import OpenMuse" 2>/dev/null; then
    ok "OpenMuse already importable"
else
    # System deps OpenMuse needs (confirmed 2026-09-09 on Pi 5 / Debian 13):
    # git to install; fontconfig for its font stack; GL ES libs because it
    # imports vispy (a GPU visualiser) even for headless recording.
    echo "   installing system dependencies (git, fontconfig, GL ES libs)"
    sudo apt-get -qq update 2>&1 | tail -1 || true
    sudo apt-get -qq install -y git fontconfig libfontconfig1 \
        libgles2 libegl1 libgl1 libglx-mesa0 libgl1-mesa-dri 2>&1 | tail -2 || true
    # Not on PyPI — install straight from the repo.
    "${VENV}/bin/pip" install --quiet "git+https://github.com/DominiqueMakowski/OpenMuse" \
      || die "could not install OpenMuse into the venv"
    "${VENV}/bin/python" -c "import OpenMuse" || die "OpenMuse installed but not importable"
    ok "OpenMuse installed"
fi

bold "Bluetooth sudoers (hciconfig without a password)"
SUDOERS=/etc/sudoers.d/muse
if sudo test -f "${SUDOERS}" && sudo grep -q "hciconfig" "${SUDOERS}"; then
    ok "hciconfig already permitted in ${SUDOERS}"
else
    # hciconfig + bluetooth service control, matching what the recorder calls.
    printf '%s ALL=(root) NOPASSWD: /usr/bin/hciconfig, /usr/sbin/hciconfig, /usr/bin/systemctl restart bluetooth\n' \
        "${USER_NAME}" | sudo tee "${SUDOERS}" >/dev/null
    sudo chmod 440 "${SUDOERS}"
    sudo visudo -c -f "${SUDOERS}" >/dev/null || { sudo rm -f "${SUDOERS}"; die "sudoers syntax check failed"; }
    ok "wrote ${SUDOERS}"
fi

bold "Recorder script + environment"
install -m 0755 "${HERE}/muse_athena_record.py" "${HOME_DIR}/muse_athena_record.py"
ok "installed ${HOME_DIR}/muse_athena_record.py"

ENVDIR="${HOME_DIR}/.config/muse"
mkdir -p "${ENVDIR}"
{
    echo "# Written by install-athena.sh — the recorder reads this at start."
    [[ -n "${MUSE_MAC}" ]] && echo "MUSE_MAC=${MUSE_MAC}"
    echo "MUSE_PRESET=${PRESET}"
    echo "# STOP_HOUR=10        # optional: stop looping at this local hour"
    echo "# KEEP_RAW=1          # keep raw .txt after decode (debugging)"
} > "${ENVDIR}/athena.env"
chmod 600 "${ENVDIR}/athena.env"
ok "wrote ${ENVDIR}/athena.env (preset ${PRESET})"

bold "systemd unit"
# The committed unit is a template (__USER__/__HOME__ placeholders, so no account
# name is in the public repo). Render it for this account before installing.
UNIT_TMP="$(mktemp)"
sed -e "s|__USER__|${USER_NAME}|g" -e "s|__HOME__|${HOME_DIR}|g" \
    "${HERE}/muse-athena-record.service" > "${UNIT_TMP}"
sudo install -m 0644 "${UNIT_TMP}" /etc/systemd/system/muse-athena-record.service
rm -f "${UNIT_TMP}"
sudo systemctl daemon-reload
sudo systemctl enable muse-athena-record >/dev/null 2>&1 || true
ok "installed and enabled muse-athena-record.service"

# ------------------------------------------------------------------ start --
if [[ "${NO_START}" -eq 1 ]]; then
    warn "install complete; NOT started (MUSE_NO_START). Start when ready:"
    echo "       sudo systemctl start muse-athena-record"
else
    bold "Starting the Athena recorder"
    sudo systemctl restart muse-athena-record
    sleep 3
    systemctl is-active --quiet muse-athena-record \
        && ok "muse-athena-record is running (starts automatically on every boot)" \
        || warn "muse-athena-record did not start — journalctl -u muse-athena-record -e"
fi

# ------------------------------------------------------------------- next ----
cat <<NEXT

   Validated on hardware (fw 3.1.15) 2026-09-09: capture works on preset ${PRESET}.
   ${PRESET} is EEG-only (optics + LEDs OFF) — no glow and low power for overnight.
   The optics presets (p1035 dim, p1041 bright) add PPG/heart-rate but drain the
   battery ~0.7%/min (~2h/charge), so they are not for overnight.

   Quick re-check any time (band on head):
     ${VENV}/bin/OpenMuse record --address <MAC> --preset ${PRESET} \\
         --duration 60 --outfile /tmp/athena_test.txt
     ${VENV}/bin/python ${HOME_DIR}/muse_athena_record.py --decode-only /tmp/athena_test.txt
     column -s, -t < /tmp/athena_test.csv | head    # 4 EEG cols, µV-ish, epoch ts

     Logs   journalctl -u muse-athena-record -f
NEXT
