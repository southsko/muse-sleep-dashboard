#!/bin/bash
# Install the Muse S Athena recorder (OpenMuse) alongside the existing Gen-1
# recorder, WITHOUT disturbing it. Run on the Pi, from the copied pi/ directory:
#
#     bash install-athena.sh                 # install, leave Gen-1 running
#     MUSE_MAC=00:11:22:33:44:55 bash install-athena.sh
#     bash install-athena.sh --switch        # ALSO stop Gen-1 and start Athena
#
# By default the Athena service is installed and enabled but NOT started, and the
# Gen-1 muse-record service is left exactly as it is. You switch over on purpose,
# once a 60-second test recording has validated the decode (see pi/README.md).
#
# Idempotent — safe to re-run.

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
USER_NAME="$(id -un)"
HOME_DIR="$(getent passwd "${USER_NAME}" | cut -d: -f6)"
VENV="${VENV:-${HOME_DIR}/muse-env}"
PRESET="${MUSE_PRESET:-p60}"
SWITCH=0
[[ "${1:-}" == "--switch" ]] && SWITCH=1

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
# VALIDATE: confirm the install source. Try PyPI, fall back to the GitHub repo.
if "${VENV}/bin/python" -c "import OpenMuse" 2>/dev/null; then
    ok "OpenMuse already importable"
else
    "${VENV}/bin/pip" install --quiet OpenMuse 2>/dev/null \
      || "${VENV}/bin/pip" install --quiet "git+https://github.com/DominiqueMakowski/OpenMuse" \
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
ok "installed and enabled muse-athena-record.service (not started)"

# --------------------------------------------------------------- switchover --
if [[ "${SWITCH}" -eq 1 ]]; then
    bold "Switching from Gen-1 to Athena"
    if systemctl list-unit-files | grep -q '^muse-record\.service'; then
        sudo systemctl disable --now muse-record 2>/dev/null || true
        ok "stopped and disabled Gen-1 muse-record"
    fi
    sudo systemctl start muse-athena-record
    sleep 3
    systemctl is-active --quiet muse-athena-record \
        && ok "muse-athena-record is running" \
        || warn "muse-athena-record did not start — journalctl -u muse-athena-record -e"
else
    warn "Gen-1 muse-record left untouched. Validate first, then switch:"
    echo "       bash install-athena.sh --switch"
fi

# ------------------------------------------------------------------- next ----
cat <<NEXT

   Validate BEFORE trusting overnight (device in hand, band charged & on head):

     ${VENV}/bin/OpenMuse find                       # confirm MAC + discovery format
     ${VENV}/bin/OpenMuse record --address <MAC> --preset ${PRESET} \\
         --duration 60 --outfile /tmp/athena_test.txt
     ${VENV}/bin/python ${HOME_DIR}/muse_athena_record.py --decode-only /tmp/athena_test.txt
     column -s, -t < /tmp/athena_test.csv | head          # check columns + values

   Confirm: 4 EEG columns TP9,AF7,AF8,TP10 present; values look like µV (tens–
   hundreds on-head, ±1000 rails on a table); 'timestamps' are ~unix-epoch secs;
   the LED is OFF on preset ${PRESET}. If any of that is wrong, fix the VALIDATE
   points in muse_athena_record.py before switching.

     Logs   journalctl -u muse-athena-record -f
NEXT
