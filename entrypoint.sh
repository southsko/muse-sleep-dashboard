#!/usr/bin/env bash
# Mode dispatch for the Muse analysis container.
#
#   MODE=once   run the batch and exit
#   MODE=cron   run daily at RUN_AT, stay resident (default)
#   MODE=watch  process recordings as they appear
#
# The scheduler is a plain loop rather than a cron daemon so that every line of
# output lands on stdout, where `docker logs` can see it. cron would swallow it.

set -euo pipefail

INPUT_DIR="${INPUT_DIR:-/data/recordings}"
OUTPUT_DIR="${OUTPUT_DIR:-/data/output}"
MODE="${MODE:-cron}"
RUN_AT="${RUN_AT:-09:00}"
RUN_ON_START="${RUN_ON_START:-1}"
WATCH_INTERVAL="${WATCH_INTERVAL:-60}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [entrypoint] $*"; }

run_batch() {
  log "starting batch over ${INPUT_DIR}"
  # Never let one bad batch kill the container in cron/watch mode.
  if python /app/analyze.py "${INPUT_DIR}" -o "${OUTPUT_DIR}" "$@"; then
    log "batch complete"
  else
    log "batch exited nonzero (continuing)"
  fi
}

# Allow `docker run <image> --force` etc. to pass straight through to the script.
if [[ "${1:-}" == "python" || "${1:-}" == "bash" || "${1:-}" == "sh" ]]; then
  exec "$@"
fi

case "${MODE}" in
  once)
    run_batch "$@"
    ;;

  web)
    log "web mode: dashboard only on :${WEB_PORT:-842}"
    exec python /app/app.py
    ;;

  cron)
    log "cron mode: daily at ${RUN_AT} (TZ=${TZ:-UTC})"

    # One container serves both roles: the dashboard runs in the background so
    # the site stays up between nightly batches, and the scheduler loop stays in
    # the foreground as PID 1's child so docker restart policies behave.
    if [[ "${SERVE_WEB:-1}" == "1" ]]; then
      log "starting dashboard on :${WEB_PORT:-842}"
      python /app/app.py &
      WEB_PID=$!
      trap 'kill "${WEB_PID}" 2>/dev/null' TERM INT
    fi

    if [[ "${RUN_ON_START}" == "1" ]]; then
      log "running once at startup"
      run_batch "$@"
    fi
    # Re-scan periodically as well as at RUN_AT. A batch is cheap — finished
    # nights are skipped by their segment list — and without it a night that
    # ends after RUN_AT (a lie-in, or a recorder still writing at 09:00) would
    # not be scored until the following morning, nearly 24 hours later.
    RESCAN_HOURS="${RESCAN_HOURS:-4}"

    while true; do
      now=$(date +%s)
      today_target=$(date -d "today ${RUN_AT}" +%s 2>/dev/null || echo 0)
      if [[ "${today_target}" == "0" ]]; then
        log "FATAL: RUN_AT='${RUN_AT}' is not a valid time (want HH:MM)"
        exit 1
      fi
      if (( today_target > now )); then
        target=${today_target}
      else
        target=$(date -d "tomorrow ${RUN_AT}" +%s)
      fi

      rescan=$(( now + RESCAN_HOURS * 3600 ))
      if (( rescan < target )); then
        sleep_for=$(( rescan - now ))
        log "next re-scan in ${RESCAN_HOURS}h (daily run at $(date -d "@${target}" '+%H:%M %Z'))"
      else
        sleep_for=$(( target - now ))
        log "next run at $(date -d "@${target}" '+%Y-%m-%d %H:%M %Z') (in $(( sleep_for / 60 )) min)"
      fi

      sleep "${sleep_for}"
      run_batch "$@"
    done
    ;;

  watch)
    log "watch mode: polling ${INPUT_DIR} every ${WATCH_INTERVAL}s"
    declare -A SEEN_SIZE
    while true; do
      shopt -s nullglob
      for f in "${INPUT_DIR}"/*.csv; do
        size=$(stat -c %s "$f" 2>/dev/null || echo 0)
        prev="${SEEN_SIZE[$f]:-}"
        SEEN_SIZE[$f]="${size}"
        # Only touch a file once its size has held steady for a full interval,
        # so a half-copied recording mid-rsync is never analyzed.
        if [[ -n "${prev}" && "${prev}" == "${size}" ]]; then
          run_batch "$@"
          break
        fi
      done
      shopt -u nullglob
      sleep "${WATCH_INTERVAL}"
    done
    ;;

  *)
    log "FATAL: unknown MODE='${MODE}' (want once|cron|watch)"
    exit 1
    ;;
esac
