# Muse S Sleep Dashboard

Turn overnight [Muse S](https://choosemuse.com/) EEG recordings into sleep staging and a
browsable web dashboard, self-hosted on your LAN.

Two machines:

- A **Raspberry Pi** wears the recording job: it streams the headband over Bluetooth
  (`muselsl`) and serves the raw recordings over SMB. See [`pi/`](pi/).
- A **server** (built for Unraid + Docker, but any Docker host works) mounts that share,
  runs the analysis, and serves the dashboard.

Open **http://your-server:842/** to see last night — not a folder of PNGs.

This repo is the analysis + dashboard half. Nothing here touches Bluetooth.

---

## Quick start

```bash
cd /mnt/user/appdata/muse-analysis
docker compose build          # smoke test runs here; green build == working pipeline
docker compose up -d
docker logs -f muse-analysis
```

Then browse to `http://<your-server>:842/`.

## Layout

| | Host | Container |
|---|---|---|
| Recordings (read-only) | `/mnt/user/data/eeg/recordings/` | `/data/recordings` |
| Results + database | `/mnt/user/data/eeg/output/` | `/data/output` |
| Dashboard | port `842` | `842` |

```
output/
├── sleep.db          # SQLite; the dashboard reads this
├── good/             # scored nights: .edf, _hypnogram.png, _proba.png,
│                     #   _bandpower.png, _stats.json, _stats.txt
└── failed/           # unusable nights — EDF still kept for inspection
```

Outputs never go next to the source CSVs: the source is a read-only mount of the Pi's
Samba share, and writing EDFs back would land them on the Pi's SD card. Source recordings
are never moved, renamed or deleted — the Pi's recorder owns that directory.

---

## Getting recordings onto the server

Currently **the server pulls**: the Pi's `recordings` share (`//PI_ADDRESS/recordings`) is
mounted read-only. A hand-made mount does not survive a reboot — Unraid rebuilds its
rootfs from RAM each boot. To persist it:

```bash
bash /mnt/user/appdata/muse-analysis/install-mount.sh
```

That prompts for the SMB credentials, stores them root-only on the flash drive, installs a
mount helper that waits for the array, and wires it into `/boot/config/go` (backing up the
existing file first). It is idempotent.

**To switch to Pi-push later** (rsync from the Pi each morning), change only the recordings
volume line in `docker-compose.yml` to point at the local destination. Everything else is
path-agnostic.

---

## Modes

Set `MODE` in `docker-compose.yml`.

| Mode | Behavior |
|---|---|
| `cron` *(default)* | Dashboard runs continuously; batch runs daily at `RUN_AT` and once on start. |
| `once` | Run the batch and exit. No dashboard. |
| `web` | Dashboard only, no scheduler. |
| `watch` | Poll `INPUT_DIR` every `WATCH_INTERVAL`s; a file is only processed once its size has held steady for a full interval, so a half-copied CSV is never analyzed. |

The scheduler is a loop in `entrypoint.sh`, not a cron daemon, so all output reaches
`docker logs`. A cron daemon would swallow it.

**`TZ` matters** — it sets both the nightly run time and which calendar night a recording
is filed under. Set TZ in docker-compose.yml to your own timezone.

### By hand

```bash
docker compose run --rm -e MODE=once muse-analysis            # one batch
docker compose run --rm -e MODE=once muse-analysis --force    # reprocess everything
docker compose run --rm muse-analysis python /app/analyze.py \
  /data/recordings/some_night.csv -o /data/output              # a single file
```

Processed recordings are skipped. Skip detection keys off `schema_version` in each
`_stats.json`, so bumping `SCHEMA_VERSION` in `analyze.py` invalidates old results and they
reprocess automatically without `--force`.

---

## What the pipeline does

1. Reads the CSV (`timestamps, TP9, AF7, AF8, TP10, Right AUX`; `Right AUX` discarded).
   Derives start time from the first timestamp and measures the true sample rate from the
   **total span** — not the median delta, which reads a bogus 250 Hz because muselsl writes
   millisecond-precision timestamps and 1/256 s rounds to 0.004.
2. µV → V, MNE Raw, `standard_1020` montage, bandpass 0.5–40 Hz.
3. Exports EDF of the **whole** recording with `physical_range="channelwise"` — with the
   default, railed ±1000 µV ear channels set the range for every channel and crush the
   frontal channels' resolution.
4. **Detects the worn window** (see below) and restricts all scoring to it.
5. Judges channel quality *inside* that window: *railed* (>20 % of samples beyond ±900 µV),
   *flat* (std < 1 µV), else *usable*.
6. Picks a staging channel: **AF7 → AF8 → TP9 → TP10**, first usable wins. Frontal first;
   the ear electrodes routinely lose contact overnight.
7. Runs `yasa.SleepStaging`, writes hypnogram / stage-probability / band-power plots.
8. Computes statistics, awakenings and clock times; writes a row to `sleep.db`.

One bad recording never takes down the batch: failures are caught, logged, recorded, and
the run continues.

### Sleep detection

Scoring is **not** run over the whole recording. Recordings routinely start before the band
is on and continue after it comes off; those epochs are electrically dead but YASA will
still label them `WAKE`, which inflates time-in-bed and destroys sleep efficiency for
reasons that have nothing to do with sleep.

`detect_wear_window()` marks each 30 s epoch where a frontal channel is alive, bridges
dropouts up to 2 minutes (a shift in bed shouldn't split the night), and takes the longest
contiguous run. Quality is judged inside that window too — otherwise an hour on the
nightstand would push a perfectly good night over the railed threshold and discard it.

Three efficiency figures are reported, because they answer different questions:

| Metric | Meaning |
|---|---|
| `SE_worn` | TST ÷ time the band was worn. **The dashboard default.** |
| `SE_recording` | TST ÷ whole recording. Shows recording overhead. |
| `SME` | TST ÷ sleep period time (yasa). Ignores time awake before sleep and after final waking. |

None of these is a clinical time-in-bed efficiency — there is no way to know when you got
into bed. Wear detection is *electrical*: a band on your head while you read in bed counts
as worn. `SME` is the metric least affected by that.

---

## Dashboard

- **Overview** — last night's hypnogram, headline tiles with delta vs a trailing 30-night
  median plus 14-night sparklines, quality badge, and trend charts (7 / 30 / 90 / all).
- **Night detail** — hypnogram, stage probability, band power, full statistics with
  baseline deltas, per-electrode quality, EDF/CSV download, prev/next navigation.
- **Nights** — sortable table; failed nights are flagged inline, never hidden.

Charts are **server-rendered inline SVG** — no CDN, no JS build step, nothing fetched at
runtime, so it works with the LAN offline. Baselines need ≥3 prior nights and say
"no baseline" until then; windows with <3 nights say so rather than drawing a confident
trend line.

Quality badge is three-state: `good`, `ear-contact-lost` (frontals fine, ears railed —
still fully scoreable), `failed` (nothing usable). A sensor problem is never presented as
a sleep result.

---

## Dependencies

Everything is baked into the image — Unraid does not persist `pip install` across reboots,
and nothing is fetched at runtime. yasa's classifiers ship inside the wheel
(`yasa/classifiers/*.joblib`, loaded from local disk), so staging needs no network.

`requirements.txt` holds top-level pins; `requirements.lock` is the full frozen tree and is
what the image actually installs. To re-freeze after changing a pin:

```bash
docker compose run --rm --entrypoint pip muse-analysis freeze > requirements.lock
docker compose build
```

### The build-time smoke test

`docker build` ends by running four synthetic recordings end-to-end through real staging
and asserting the results. This is the most valuable part of the build — it has already
caught four genuine breakages (an edfio version that mne couldn't import, `np.trapz`
removed in numpy 2, a missing band-power plot, and the wear-window ordering bug). A red
build means the pipeline is broken; a green one means it demonstrably works.

Fixtures: clean night, railed ears, all-flat, and band-on-the-nightstand-at-both-ends.
They prove code paths only — **they are not realistic sleep and thresholds must never be
tuned against them.**

---

## Notes

- `yasa.sleep_statistics()` (the module-level function both briefs reference) was removed
  in yasa 0.7. It is now `Hypnogram.sleep_statistics()`. Don't "fix" it back.
- `SleepStaging.predict()` returns a `Hypnogram` carrying `.proba`. Do **not** call
  `predict_proba()` — it is deprecated and hangs indefinitely on real recordings.
- sklearn logs an `InconsistentVersionWarning` for yasa's pickled `LabelEncoder`. It is
  benign: probability columns decode as `WAKE, N1, N2, N3, REM` in the correct order.
- `report.py` still writes a static `output/index.html`. The dashboard supersedes it; it
  remains only as a no-server fallback.
