# Changelog

## 2026-10-02

### Everything now runs on the Pi (Unraid retired)
Analysis + dashboard were consolidated off the Unraid box onto the Pi itself, so the
whole pipeline — record, YASA analysis, dashboard — lives on one machine.
- Native venv (`muse-analysis-env`, Python 3.13 / aarch64): yasa 0.7, mne 1.12, numpy,
  scipy, scikit-learn, numba, lightgbm, matplotlib, flask, waitress — all prebuilt
  wheels via piwheels, no source builds.
- New `pi/muse-analysis.service`: runs `entrypoint.sh` in cron mode (nightly 09:00 +
  hourly rescan + run-on-start) and serves the dashboard on `:842`. Resource-capped so
  the recorder always wins (Nice 15, 2/4 cores, memory ceiling, high OOM score),
  `CAP_NET_BIND_SERVICE` so the `joey` user can bind `:842`.
- Dashboard moved from `sofaking:842` to `brain.local:842`. Identical output verified
  (a reference night matched Unraid bit-for-bit); full history reprocessed with 0 errors.
- Code made portable so the **same source still builds the Docker container**:
  - `app.py` shells out via `sys.executable` + `analyze.py` next to `__file__`
    (was the hardcoded `python /app/analyze.py`).
  - `entrypoint.sh` honours `PYTHON` / `APP_DIR` (default to the container's `python`
    and `/app`).
- The Unraid container is stopped with auto-restart disabled but kept in place for
  rollback (`docker start muse-analysis`).

### Live sleep-state recalibrated on-head (`pi/muse_status.py`)
The previous `.sleep_calib.json` was captured with the band **off the head** (ears
"railed" ~578 µV = mains hum, not EEG), so a band on the nightstand — and a still but
awake wearer — both read as "asleep". Recalibrated from a real on-head session:
- **Contact gate**: worst-channel mains hum > 120 µV = no skin contact → `unknown`
  (on-head ≈ 45 µV, off-head 150–580 µV).
- **Trust gate** raised 0.35 → 0.45: this wearer's awake forehead EEG is very
  low-amplitude, so its 30–50 Hz fraction hit ~0.36 even on clean on-skin signal, and
  0.35 was rejecting real awake EEG.
- **Atonia required for sleep** (`emg < 0.10`): asleep EMG ≈ 0.03 vs awake ≈ 0.14, so
  awake blinks (large slow-wave transients) no longer read as light/deep.
- **Removed the "still → asleep" actigraphy fallback**: with no trustworthy brain
  signal the screen now shows `unknown` (moving → `awake`), never a sleep guess.
- Verified: a clean night reads as sleep ~78 % / 0 % unknown; a fragmented night's
  bad-signal stretches show `unknown` honestly; off-head never reads a sleep stage.
- Known limit: a perfectly still, **eyes-open**, awake wearer can still read "light" —
  blinks mimic slow waves on a forehead sensor over 8 s. Not a bedtime posture.
