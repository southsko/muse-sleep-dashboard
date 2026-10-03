# Changelog

## 2026-10-03

### The ~80 s hole at every hourly seam — actually fixed this time
Nights were still showing a gap at every file rotation (~80 s, every hour). The
2026-09-25 "one connection, rotate in place" change was correct — the link never
drops across a rotation — and so was moving to device-tick timing, but neither
killed the hole. Root cause, proven from 2026-10-02's logs: each raw file captured a
full **60 real minutes** and decoded to **"100% of samples present, 0 s lost"** — a
completely full grid — yet that grid spanned only **58.6 min**. So a perfect,
never-dropped hour was being *compressed* ~2.3%, and the missing ~80 s surfaced as a
hole before the next file (which is anchored to real arrival time).

- **Why:** the Athena's packet tick advances 1000 per EEG sample at its *true* ~250 Hz
  rate, but OpenMuse's `make_timestamps` divides ticks by `DEVICE_CLOCK_HZ = 256000`
  (i.e. assumes exactly 256 Hz), compressing every hour by 250/256 ≈ 2.3%.
- **Fix (`_decode_eeg_timed`):** rescale each file's tick timeline to its real elapsed
  arrival-time span (`message_time` of the last packet − the first). Self-calibrating
  against the true rate/drift, so seams now butt up to within one packet interval. As a
  bonus it also corrects EEG frequencies, which were reading ~2.3% high (spindles etc.).
  Falls back to raw tick timing if the arrival span is implausible (e.g. a 1-packet file).
- Verified with a synthetic 250 Hz hour through the real `make_timestamps`: buggy span
  3515.6 s → fixed 3599.98 s (seam error 0 ms); a true-256 Hz file is left unchanged.
- Pi backup: `~/muse_athena_record.py.pre-seamfix-20261003`.

### Keep the raw capture by default
`KEEP_RAW` now defaults to **on**. Raw `.txt` was being deleted the moment it decoded,
which repeatedly left no way to re-decode a night when a decode/timing bug (like the
seam gap above) turned up later. Raw is ~0.2 GB/night and the card has 187 GB free, so
there's no reason to throw it away. A low-space safety valve (`MIN_FREE_GB`, default 15)
only ever trims the **oldest raw first** if free space actually drops that low — decoded
CSVs are never touched. Pi backup: `~/muse_athena_record.py.pre-keepraw-20261003`.

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
