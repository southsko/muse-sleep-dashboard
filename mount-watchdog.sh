#!/bin/bash
# Self-healing watchdog for the Muse recordings CIFS mount.
#
# The server pulls recordings from the Pi over a CIFS mount. That mount silently
# goes stale — the Pi keeps recording but the server's mount freezes on an old
# directory (observed 2026-08-05: frozen ~4 days, dashboard showed no new nights
# while the Pi recorded normally). A dead SMB session serves a frozen view
# without erroring, so nothing notices.
#
# This checks every few minutes and remounts when the mount is hung, dropped, or
# frozen (the Pi has newer files than the mount can see). Started from
# /boot/config/go; also safe to run by hand.

HELPER="/boot/config/mount-muse-recordings.sh"
INTERVAL="${WATCHDOG_INTERVAL:-600}"     # seconds between checks

val() { grep -m1 "^$1=" "$HELPER" | cut -d'"' -f2; }
SHARE="$(val SHARE)"
MOUNTPOINT="$(val MOUNTPOINT)"
CRED="$(val CRED)"
HOST="$(echo "$SHARE" | sed 's|//\([^/]*\)/.*|\1|')"
SVC="$(echo "$SHARE"  | sed 's|//[^/]*/||')"

newest_local()  { ls "$MOUNTPOINT"/ 2>/dev/null | grep -oE 'overnight_[0-9_]+\.csv' | sort | tail -1; }
newest_remote() { timeout 25 smbclient "//$HOST/$SVC" -A "$CRED" -c 'ls' 2>/dev/null \
                    | grep -oE 'overnight_[0-9_]+\.csv' | sort | tail -1; }

remount() {
    logger -t muse-watchdog "remounting ${MOUNTPOINT}: $1"
    umount -l "$MOUNTPOINT" 2>/dev/null
    sleep 2
    bash "$HELPER"          # re-mounts (retries + make-rshared) if not mounted
}

logger -t muse-watchdog "started (interval ${INTERVAL}s, host ${HOST})"
while true; do
    sleep "$INTERVAL"

    # 1) Not mounted, or hung (ls does not return within 20s).
    if ! mountpoint -q "$MOUNTPOINT" || ! timeout 20 ls "$MOUNTPOINT" >/dev/null 2>&1; then
        remount "not mounted or unresponsive"
        continue
    fi

    # 2) Frozen: the Pi genuinely has a newer recording than the mount shows.
    #    Only fires when we can confirm it via a fresh SMB connection, so a Pi
    #    that is merely off never triggers a pointless remount.
    R="$(newest_remote)"; L="$(newest_local)"
    if [[ -n "$R" && "$R" > "$L" ]]; then
        remount "stale — pi has ${R}, mount shows ${L:-nothing}"
    fi
done
