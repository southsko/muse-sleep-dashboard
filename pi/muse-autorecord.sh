#!/bin/bash
# Muse auto-record with supervision.
#
# The problem this solves: `muselsl record --duration 43200` keeps running for
# 12 hours whether or not data is arriving. When the BLE link drops, the stream
# dies but the recorder sits there happily doing nothing — it never exits, so
# systemd's Restart=on-failure never fires, and the night is silently lost.
# (Observed 2026-07-20: link died 15 min in, recorder idled for 7 more hours.)
#
# So: watch the CSV. muselsl appends to it as chunks arrive, which makes file
# growth a direct liveness signal. If it stops growing, tear everything down,
# reset the adapter, and start a fresh segment.

set -uo pipefail

MUSE_MAC="${MUSE_MAC:-XX:XX:XX:XX:XX:XX}"
OUTDIR="${OUTDIR:-${HOME}/recordings}"
VENV="${VENV:-${HOME}/muse-env}"

# Segment length. NOT 12 hours: muselsl's save routine re-concatenates the whole
# accumulated recording on every periodic write and never frees the buffer
# (record.py _save), so memory grows without bound. A 12 h segment needs >2 GB
# transient near the end — this Pi 3B has 905 MB and would be OOM-killed long
# before morning, even with a perfect Bluetooth link. One hour keeps the buffer
# small and also caps how much a dropout can cost.
SEGMENT_SEC="${SEGMENT_SEC:-3600}"
STALL_SEC="${STALL_SEC:-60}"          # no new bytes for this long => dead
POLL_SEC="${POLL_SEC:-20}"
SETTLE_SEC="${SETTLE_SEC:-20}"        # let the stream come up before recording
RETRY_SEC="${RETRY_SEC:-10}"          # pause between attempts

source "${VENV}/bin/activate"
mkdir -p "${OUTDIR}"

log() { echo "[autorecord] $*"; }

STREAM_PID=""
REC_PID=""

cleanup() {
    [[ -n "${REC_PID}" ]]    && kill "${REC_PID}"    2>/dev/null
    [[ -n "${STREAM_PID}" ]] && kill "${STREAM_PID}" 2>/dev/null
    sleep 2
    [[ -n "${REC_PID}" ]]    && kill -9 "${REC_PID}"    2>/dev/null
    [[ -n "${STREAM_PID}" ]] && kill -9 "${STREAM_PID}" 2>/dev/null
    # muselsl can leave orphans behind that hold the adapter.
    pkill -f "muse_stream.py" 2>/dev/null; pkill -f "muselsl stream" 2>/dev/null
    pkill -f "muselsl record" 2>/dev/null
    REC_PID=""; STREAM_PID=""
}

on_term() { log "received TERM, shutting down"; cleanup; exit 0; }
trap on_term TERM INT

adapter_up() {
    hciconfig hci0 2>/dev/null | grep -q "UP RUNNING"
}

reset_bt() {
    # Frame-reassembly errors on the Pi's UART Bluetooth leave the adapter in a
    # bad state. Escalate until it comes back — and crucially, VERIFY it is up
    # afterwards: a bare `hciconfig reset` can leave the adapter DOWN, and then
    # every subsequent connect fails with "No powered Bluetooth adapters found"
    # forever. (Observed 2026-07-21.)
    adapter_up && return 0

    log "bluetooth adapter is down; attempting recovery"

    sudo hciconfig hci0 down 2>/dev/null
    sleep 1
    sudo hciconfig hci0 up 2>/dev/null
    sleep 2
    adapter_up && { log "adapter recovered (up/down)"; return 0; }

    sudo systemctl restart bluetooth 2>/dev/null
    sleep 5
    sudo hciconfig hci0 up 2>/dev/null
    sleep 2
    adapter_up && { log "adapter recovered (bluetooth service restart)"; return 0; }

    # The UART chip can wedge so hard that nothing short of a reboot clears it
    # ("Can't init device hci0: Connection timed out"). Don't reboot on our own
    # initiative — surface it loudly instead, and keep retrying.
    log "WARNING: bluetooth adapter will not come up (UART chip wedged)."
    log "WARNING: this usually needs a reboot of the Pi. No data can be recorded."
    return 1
}

stop_recorder() {
    [[ -n "${REC_PID}" ]] && kill "${REC_PID}" 2>/dev/null
    sleep 2
    [[ -n "${REC_PID}" ]] && kill -9 "${REC_PID}" 2>/dev/null
    pkill -f "muselsl record" 2>/dev/null
    REC_PID=""
}

# Outer loop: owns the BLE stream. Only torn down when the link actually fails.
while true; do
    if ! reset_bt; then
        sleep "${RETRY_SEC}"
        continue
    fi

    log "starting stream to ${MUSE_MAC}"
    python "${HOME}/muse_stream.py" --backend bleak --address "${MUSE_MAC}" &
    STREAM_PID=$!
    sleep "${SETTLE_SEC}"

    if ! kill -0 "${STREAM_PID}" 2>/dev/null; then
        log "stream process died during startup; retrying in ${RETRY_SEC}s"
        cleanup; sleep "${RETRY_SEC}"; continue
    fi

    # Inner loop: rolls segments while the stream stays healthy. Keeping the
    # stream up across rollovers costs a couple of seconds per segment instead
    # of a full reconnect (~50 s), which matters when segments are hourly.
    link_ok=1
    while [[ "${link_ok}" == "1" ]]; do

        FILENAME="${OUTDIR}/overnight_$(date +%Y%m%d_%H%M%S).csv"
        log "recording to ${FILENAME}"
        muselsl record --duration "${SEGMENT_SEC}" --filename "${FILENAME}" &
        REC_PID=$!

        # --- watchdog -----------------------------------------------------
        last_size=-1
        stalled=0
        started=$(date +%s)

        while kill -0 "${REC_PID}" 2>/dev/null; do
            sleep "${POLL_SEC}"
            size=$(stat -c %s "${FILENAME}" 2>/dev/null || echo 0)

            if [[ "${size}" -gt "${last_size}" ]]; then
                last_size="${size}"
                stalled=0
            else
                stalled=$(( stalled + POLL_SEC ))
            fi

            # Grace period: nothing is written until the first chunk lands.
            elapsed=$(( $(date +%s) - started ))
            if [[ "${last_size}" -le 0 && "${elapsed}" -lt 120 ]]; then
                continue
            fi

            if [[ "${stalled}" -ge "${STALL_SEC}" ]]; then
                log "STALL: no new data for ${stalled}s (${last_size} bytes) — link presumed dead"
                link_ok=0
                break
            fi
        done

        # If the stream process itself died, the link is gone regardless.
        if ! kill -0 "${STREAM_PID}" 2>/dev/null; then
            log "stream process exited — link dead"
            link_ok=0
        fi

        stop_recorder
        ran_for=$(( $(date +%s) - started ))

        if [[ -s "${FILENAME}" ]]; then
            log "segment saved: ${FILENAME} ($(stat -c %s "${FILENAME}") bytes, ${ran_for}s)"
        else
            # An empty segment means the recorder never saw an EEG stream. That
            # is a dead link, NOT a rollover — `muselsl stream` stays alive while
            # it retries internally, so checking the stream PID is not enough.
            # Without this the inner loop spins every ~20 s creating and binning
            # empty files and never backs off to reset the adapter.
            log "segment produced no data — link is down, not a rollover"
            rm -f "${FILENAME}"
            link_ok=0
        fi

        # A segment that ended far short of its duration also indicates trouble;
        # go back out and rebuild the stream rather than immediately retrying.
        if [[ "${link_ok}" == "1" && "${ran_for}" -lt $(( SEGMENT_SEC / 2 )) ]]; then
            log "segment ended early (${ran_for}s of ${SEGMENT_SEC}s) — rebuilding stream"
            link_ok=0
        fi

        [[ "${link_ok}" == "1" ]] && log "segment rollover (stream still up)"
    done

    cleanup
    log "restarting stream in ${RETRY_SEC}s"
    sleep "${RETRY_SEC}"
done
