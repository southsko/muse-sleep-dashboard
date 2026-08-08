#!/usr/bin/env python3
"""Batch sleep-staging for overnight Muse S recordings made by muselsl.

Reads muselsl CSVs, filters them, exports EDF, runs YASA sleep staging on a
frontal channel, and writes hypnograms + probability plots + statistics.

Designed to run unattended: one bad recording never takes down the batch.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt

# Plots are embedded in a dark dashboard; a white canvas glares badly against it.
plt.rcParams.update({
    "figure.facecolor": "#161a20",
    "axes.facecolor": "#161a20",
    "savefig.facecolor": "#161a20",
    "text.color": "#e6e8eb",
    "axes.labelcolor": "#e6e8eb",
    "axes.edgecolor": "#252b34",
    "xtick.color": "#98a2b3",
    "ytick.color": "#98a2b3",
    "grid.color": "#252b34",
    "legend.facecolor": "#1b2027",
    "legend.edgecolor": "#252b34",
})
import mne
import numpy as np
import pandas as pd
import yasa

log = logging.getLogger("muse")

# Bump when the pipeline changes in a way that invalidates existing results;
# files whose stats JSON carries an older version are reprocessed automatically.
SCHEMA_VERSION = 6   # v6: bipolar staging on concatenated real signal

SFREQ = 256.0
EEG_CHANNELS = ["TP9", "AF7", "AF8", "TP10"]
EXPECTED_COLUMNS = ["timestamps", "TP9", "AF7", "AF8", "TP10", "Right AUX"]

# Channel-health thresholds, applied to the raw signal in microvolts.
RAIL_UV = 900.0        # |x| beyond this means the amplifier is pinned
RAIL_FRACTION = 0.20   # railed for >20% of the night => unusable
FLAT_STD_UV = 1.0      # standard deviation below this means no signal at all

# YASA is trained on 30s epochs; anything under a few minutes is meaningless.
MIN_STAGING_MINUTES = 5.0

EPOCH_SEC = 30.0
# Brief dropouts (a shift in bed, a moment of bad contact) shouldn't split the
# worn period in two. Bridge gaps up to this many epochs before picking the
# longest contiguous run.
WEAR_GAP_EPOCHS = 4          # 2 minutes
MIN_WEAR_EPOCHS = 10         # 5 minutes; below this there is nothing to score

# A recording whose file changed more recently than this is assumed to still
# be in progress and is left alone until the next run.
STABLE_MINUTES = float(os.environ.get("STABLE_MINUTES", "10"))

# Consecutive recordings separated by less than this are treated as one night.
# The recorder rolls hourly segments and restarts on a dropped link, so a single
# night routinely arrives as several files. Generous enough to absorb a stream
# rebuild (~50 s) or a trip to the bathroom, tight enough that a nap and the
# following night stay separate.
MERGE_GAP_MINUTES = float(os.environ.get("MERGE_GAP_MINUTES", "30"))

# Where raw CSVs are copied before analysis. Recordings live on the Pi's SD
# card and are read over a share; cards fail. Defaults to <output>/raw, resolved
# at run time — a hardcoded path silently archived into the wrong place when the
# output directory was anything but the default. Set empty to disable.
ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR") or None

# Preference order for the staging channel. Frontal first: the ear electrodes
# routinely lose contact overnight.
CHANNEL_PREFERENCE = ["AF7", "AF8", "TP9", "TP10"]

# yasa's 5-stage hypnograms label wake/REM as "WAKE"/"REM"; accept the short
# forms too so the fallback plot path works whatever the label convention.
STAGE_TO_INT = {"W": 0, "WAKE": 0, "N1": 1, "N2": 2, "N3": 3, "R": 4, "REM": 4}


@dataclass
class ChannelQuality:
    name: str
    verdict: str          # "usable" | "railed" | "flat"
    railed_fraction: float
    std_uv: float

    @property
    def usable(self) -> bool:
        return self.verdict == "usable"


@dataclass
class Result:
    """Everything we learned about one recording. Serialized to <name>_stats.json."""

    schema_version: int = SCHEMA_VERSION
    source: str = ""
    status: str = "ok"                  # "ok" | "bad" | "error"
    reason: str = ""
    start_time: str | None = None
    duration_minutes: float = 0.0
    # The subset of the recording during which the band was actually worn;
    # all sleep scoring is restricted to this window.
    wear_start_min: float | None = None
    wear_minutes: float | None = None
    wear_start_time: str | None = None
    sleep_onset_time: str | None = None
    final_wake_time: str | None = None
    sfreq_measured: float | None = None
    channels: list[dict] = field(default_factory=list)
    staging_channel: str | None = None
    stats: dict = field(default_factory=dict)
    stage_counts: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    analyzed_at: str = ""
    # A night can arrive as several files: the recorder rolls hourly segments
    # and starts a new one whenever the link drops.
    segments: list[str] = field(default_factory=list)
    edfs: list[str] = field(default_factory=list)
    n_segments: int = 1
    gap_minutes: float = 0.0


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_csv(path: Path) -> tuple[pd.DataFrame, datetime | None, float | None]:
    """Read a muselsl CSV and return (eeg_uv, start_datetime, measured_sfreq)."""
    df = pd.read_csv(path)

    missing = [c for c in EEG_CHANNELS if c not in df.columns]
    if missing:
        raise ValueError(
            f"missing EEG columns {missing}; found {list(df.columns)}"
        )

    start_dt = None
    measured = None
    if "timestamps" in df.columns:
        ts = pd.to_numeric(df["timestamps"], errors="coerce").dropna().to_numpy()
        if ts.size >= 2:
            # muselsl writes unix epoch seconds.
            try:
                start_dt = datetime.fromtimestamp(float(ts[0]), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                start_dt = None
            # Measure from the total span, not the median inter-sample delta.
            # muselsl writes timestamps at millisecond precision, and
            # 1/256 = 0.0039 s rounds to 0.004 or 0.003 — so the median delta
            # is 0.004 and implies a bogus 250 Hz on a perfectly good file.
            span = float(ts[-1] - ts[0])
            if span > 0:
                measured = float((ts.size - 1) / span)

    eeg = df[EEG_CHANNELS].apply(pd.to_numeric, errors="coerce")
    # Dropped samples show up as NaN; interpolate short gaps, zero the rest so
    # the filter doesn't propagate NaN across the whole recording.
    eeg = eeg.interpolate(limit=int(SFREQ), limit_direction="both").fillna(0.0)
    return eeg, start_dt, measured


def csv_time_range(path: Path) -> tuple[datetime, datetime] | None:
    """First and last timestamp of a recording, without loading the whole file.

    Reads the first data line, then seeks to the tail — these files run to tens
    of megabytes and grouping should not cost a full parse.
    """
    try:
        with path.open("rb") as fh:
            fh.readline()                      # header
            first = fh.readline().decode("utf-8", "replace")
            if not first:
                return None
            size = path.stat().st_size
            fh.seek(max(0, size - 4096))
            tail = fh.read().decode("utf-8", "replace").strip().splitlines()
        last = next((ln for ln in reversed(tail) if ln.count(",") >= 5), None)
        if last is None:
            return None
        t0 = float(first.split(",", 1)[0])
        t1 = float(last.split(",", 1)[0])
        if not (t1 >= t0 > 0):
            return None
        return (datetime.fromtimestamp(t0, tz=timezone.utc),
                datetime.fromtimestamp(t1, tz=timezone.utc))
    except (OSError, ValueError, IndexError):
        return None


def group_nights(csvs: list[Path]) -> list[list[Path]]:
    """Group recording segments that belong to the same night.

    The recorder rolls hourly segments and starts a fresh file whenever the BLE
    link drops, so one night routinely spans several CSVs. Scoring them
    separately would report a night as a handful of disconnected 40-minute
    fragments, and every derived statistic (sleep onset, REM latency, WASO)
    would be computed against the wrong baseline.

    Segments are joined when the gap between one ending and the next starting is
    under MERGE_GAP_MINUTES. Files whose timestamps cannot be read are treated as
    standalone rather than silently dropped.
    """
    dated: list[tuple[datetime, datetime, Path]] = []
    undated: list[list[Path]] = []
    for p in csvs:
        rng = csv_time_range(p)
        if rng is None:
            log.warning("%s: cannot read timestamps; treating as its own night", p.name)
            undated.append([p])
        else:
            dated.append((rng[0], rng[1], p))

    dated.sort(key=lambda t: t[0])
    groups: list[list[Path]] = []
    for start, end, path in dated:
        if groups and (start - prev_end).total_seconds() / 60.0 <= MERGE_GAP_MINUTES:
            groups[-1].append(path)
        else:
            groups.append([path])
        prev_end = end
    return groups + undated


def assess_channels(eeg_uv: pd.DataFrame) -> list[ChannelQuality]:
    """Judge each channel's health from the unfiltered signal in microvolts."""
    out = []
    for name in EEG_CHANNELS:
        x = eeg_uv[name].to_numpy(dtype=float)
        railed = float(np.mean(np.abs(x) > RAIL_UV)) if x.size else 1.0
        std = float(np.std(x)) if x.size else 0.0

        if railed > RAIL_FRACTION:
            verdict = "railed"
        elif std < FLAT_STD_UV:
            verdict = "flat"
        else:
            verdict = "usable"
        out.append(ChannelQuality(name, verdict, round(railed, 4), round(std, 2)))
    return out


def build_raw(eeg_uv: pd.DataFrame) -> mne.io.RawArray:
    """microvolts -> volts, into an MNE Raw with a 10-20 montage."""
    data = eeg_uv[EEG_CHANNELS].to_numpy(dtype=float).T * 1e-6
    info = mne.create_info(ch_names=list(EEG_CHANNELS), sfreq=SFREQ, ch_types="eeg")
    raw = mne.io.RawArray(data, info, verbose="ERROR")
    raw.set_montage("standard_1020", on_missing="ignore", verbose="ERROR")
    return raw


def epoch_usable(eeg_uv: pd.DataFrame, ch: str) -> np.ndarray:
    """Per-30s-epoch usability for one channel, using the whole-file thresholds."""
    n_per = int(SFREQ * EPOCH_SEC)
    x = eeg_uv[ch].to_numpy(dtype=float)
    n_ep = x.size // n_per
    if n_ep == 0:
        return np.zeros(0, dtype=bool)
    x = x[: n_ep * n_per].reshape(n_ep, n_per)
    railed = (np.abs(x) > RAIL_UV).mean(axis=1)
    std = x.std(axis=1)
    return (railed <= RAIL_FRACTION) & (std >= FLAT_STD_UV)


def detect_wear_window(eeg_uv: pd.DataFrame) -> tuple[int, int] | None:
    """Find the epoch range over which the headband was actually being worn.

    Recordings routinely start before the band is on and continue after it comes
    off; those epochs are electrically dead but YASA will still happily label
    them 'WAKE'. Scoring them would inflate time-in-bed and destroy sleep
    efficiency for reasons that have nothing to do with sleep.

    Returns (start_epoch, end_epoch) exclusive of end, or None if never worn.
    """
    frontal = [c for c in ("AF7", "AF8") if c in eeg_uv.columns]
    if not frontal:
        return None

    worn = np.zeros(0, dtype=bool)
    for ch in frontal:
        u = epoch_usable(eeg_uv, ch)
        worn = u if worn.size == 0 else (worn | u)
    if not worn.any():
        return None

    # Bridge short dropouts so one bad minute doesn't split the night.
    idx = np.flatnonzero(worn)
    bridged = worn.copy()
    for a, b in zip(idx[:-1], idx[1:]):
        if 1 < b - a <= WEAR_GAP_EPOCHS + 1:
            bridged[a:b] = True

    # Longest contiguous run wins.
    best_len = best_start = cur_start = 0
    cur_len = 0
    for i, v in enumerate(bridged):
        if v:
            if cur_len == 0:
                cur_start = i
            cur_len += 1
            if cur_len > best_len:
                best_len, best_start = cur_len, cur_start
        else:
            cur_len = 0

    if best_len < MIN_WEAR_EPOCHS:
        return None
    return best_start, best_start + best_len


def count_awakenings(stages: list[str]) -> int:
    """Number of distinct wake bouts occurring *within* the sleep period.

    Leading and trailing wake are excluded — those are 'not asleep yet' and
    'got up', not awakenings.
    """
    sleep_idx = [i for i, s in enumerate(stages) if s not in ("WAKE", "W", "ART", "UNS")]
    if len(sleep_idx) < 2:
        return 0
    first, last = sleep_idx[0], sleep_idx[-1]

    count = 0
    prev_wake = False
    for s in stages[first : last + 1]:
        is_wake = s in ("WAKE", "W")
        if is_wake and not prev_wake:
            count += 1
        prev_wake = is_wake
    return count


def pick_staging_channel(quality: list[ChannelQuality]) -> str | None:
    by_name = {q.name: q for q in quality}
    for name in CHANNEL_PREFERENCE:
        q = by_name.get(name)
        if q is not None and q.usable:
            return name
    return None


# --------------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------------

# Row order, shallow to deep. Colours come from charts.STAGE_COLORS so the plots
# and the dashboard cannot drift apart.
HYPNO_ROWS = ["WAKE", "REM", "N1", "N2", "N3"]


def _stage_bouts(stages: list[str]):
    """Collapse an epoch list into (stage, start_epoch, end_epoch) runs."""
    if not stages:
        return []
    out, cur, start = [], stages[0], 0
    for i, s in enumerate(stages[1:], 1):
        if s != cur:
            out.append((cur, start, i))
            cur, start = s, i
    out.append((cur, start, len(stages)))
    return out


def plot_hypnogram(hypno, out_path: Path, title: str,
                   start_dt: datetime | None = None) -> None:
    """Draw the night as coloured bouts on stage rows.

    yasa's default plot is a bare line, which makes it hard to see at a glance
    where the night was spent — the shape of the night is the whole point of a
    hypnogram. Each contiguous bout becomes a bar on its stage's row, so deep sleep
    reads as weight low on the chart and fragmentation reads as visible gaps.
    """
    from charts import STAGE_COLORS

    stages = list(hypno.hypno) if hasattr(hypno, "hypno") else list(hypno)
    bouts = _stage_bouts([str(s) for s in stages])
    n = max(len(stages), 1)

    fig, ax = plt.subplots(figsize=(13, 3.6))
    rows = {name: i for i, name in enumerate(HYPNO_ROWS)}

    # Recessive guide line per row, so empty stages still read as a row.
    for name, y in rows.items():
        ax.plot([0, n], [y, y], color="#252b34", lw=1, zorder=1)

    for stage, a, b in bouts:
        if stage not in rows:
            # Gaps between segments: a thin recessive band across the full
            # height, never on a stage row — it is absence of data, not a stage.
            ax.axvspan(a, b, color=STAGE_COLORS["UNS"], alpha=0.35, lw=0, zorder=0)
            continue
        y = rows[stage]
        # 2px-equivalent gap between adjacent bouts is the secondary encoding
        # the one-hue NREM ramp relies on.
        ax.barh(y, b - a - 0.06, left=a + 0.03, height=0.52,
                color=STAGE_COLORS[stage], zorder=3,
                edgecolor="#161a20", linewidth=0.6)

    # Connect bout centres so the descent through the night stays legible.
    xs, ys = [], []
    for stage, a, b in bouts:
        if stage in rows:
            xs += [a, b]
            ys += [rows[stage]] * 2
    if xs:
        ax.plot(xs, ys, color="#5c6673", lw=1.0, zorder=2, drawstyle="steps-post")

    ax.set_yticks(list(rows.values()))
    ax.set_yticklabels(list(rows.keys()), fontsize=9)
    ax.set_ylim(len(rows) - 0.5, -0.7)          # WAKE at top, N3 at bottom
    ax.set_xlim(0, n)

    # Clock times if we know when the night began, else elapsed hours.
    if start_dt is not None:
        step = max(1, int(round(n / 8)))
        ticks = list(range(0, n, step))
        ax.set_xticks(ticks)
        ax.set_xticklabels(
            [(start_dt + timedelta(seconds=t * EPOCH_SEC)).strftime("%H:%M")
             for t in ticks], fontsize=9)
    else:
        ax.set_xlabel("Epoch (30 s)", fontsize=9)

    ax.set_title(title, fontsize=11, pad=10)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#252b34")
    ax.tick_params(length=0)
    ax.grid(False)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


# Frequency bands are ordered (slow -> fast), so this is an ordinal scale, not
# a categorical one: one hue stepped by lightness. Deep-sleep delta reads as the
# heavy dark mass at the bottom and fast activity as the light band on top.
BANDS = [
    ("Delta", 0.5, 4.0, "#0d366b"),
    ("Theta", 4.0, 8.0, "#184f95"),
    ("Alpha", 8.0, 12.0, "#2a78d6"),
    ("Sigma", 12.0, 16.0, "#5598e7"),
    ("Beta", 16.0, 30.0, "#9ec5f4"),
]


def compute_bandpower(raw: mne.io.BaseRaw, ch: str) -> pd.DataFrame:
    """Relative band power per 30 s epoch for one channel.

    Delta rising and alpha/beta falling is the visual signature of descending
    into deep sleep, so this plot is the sanity check on a night's staging.
    """
    # scipy's trapezoid, not np.trapz — the latter was removed in numpy 2.0.
    from scipy.integrate import trapezoid
    from scipy.signal import welch

    x = raw.get_data(picks=[ch])[0] * 1e6      # back to microvolts
    n_per = int(SFREQ * EPOCH_SEC)
    n_ep = x.size // n_per
    if n_ep == 0:
        return pd.DataFrame(columns=[b[0] for b in BANDS])

    epochs = x[: n_ep * n_per].reshape(n_ep, n_per)
    freqs, psd = welch(epochs, fs=SFREQ, nperseg=min(n_per, int(SFREQ * 4)), axis=-1)

    out = {}
    total = trapezoid(psd, freqs, axis=-1)
    total[total <= 0] = np.nan
    for name, lo, hi, _ in BANDS:
        sel = (freqs >= lo) & (freqs < hi)
        out[name] = 100.0 * trapezoid(psd[:, sel], freqs[sel], axis=-1) / total
    return pd.DataFrame(out)


def plot_bandpower(bp: pd.DataFrame, out_path: Path, title: str) -> None:
    fig, ax = plt.subplots(figsize=(13, 3.2))
    cols = [c for _, _, _, c in BANDS if _ is not None][:len(bp.columns)]
    bp.plot(kind="area", stacked=True, lw=0, ax=ax,
            color=[dict((b[0], b[3]) for b in BANDS).get(c, "#5c6673")
                   for c in bp.columns])
    ax.set_xlabel("Epoch (30 s)", fontsize=9)
    ax.set_ylabel("Relative power (%)", fontsize=9)
    ax.set_xlim(0, max(len(bp) - 1, 1))
    ax.set_ylim(0, 100)
    ax.set_title(title, fontsize=11, pad=10)
    leg = ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18),
                    ncol=len(BANDS), fontsize=8, frameon=False)
    for text in leg.get_texts():
        text.set_color("#98a2b3")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0, labelsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def plot_proba(proba: pd.DataFrame, out_path: Path, title: str) -> None:
    from charts import STAGE_COLORS

    # Same stage colours as the hypnogram and the dashboard, in physiological
    # order, so a band means the same thing everywhere on the page.
    order = [c for c in HYPNO_ROWS if c in proba.columns]
    order += [c for c in proba.columns if c not in order]
    cols = [STAGE_COLORS.get(str(c), "#5c6673") for c in order]

    fig, ax = plt.subplots(figsize=(13, 3.2))
    proba[order].plot(kind="area", stacked=True, lw=0, ax=ax, color=cols)
    ax.set_xlabel("Epoch (30 s)", fontsize=9)
    ax.set_ylabel("Probability", fontsize=9)
    ax.set_ylim(0, 1)
    ax.set_xlim(0, max(len(proba) - 1, 1))
    ax.set_title(title, fontsize=11, pad=10)
    leg = ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18),
                    ncol=len(order), fontsize=8, frameon=False)
    for text in leg.get_texts():
        text.set_color("#98a2b3")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0, labelsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


# --------------------------------------------------------------------------
# Per-file pipeline
# --------------------------------------------------------------------------

@dataclass
class Segment:
    """One recorded file, prepared for assembly into a night."""
    path: Path
    name: str
    duration_minutes: float = 0.0
    sfreq_measured: float | None = None
    edf: str | None = None
    ok: bool = False
    reason: str = ""
    channels: list[dict] = field(default_factory=list)
    staging_channel: str | None = None
    start_utc: datetime | None = None        # start of the WORN window, UTC
    wear_minutes: float | None = None
    wear_start_min: float | None = None
    # The worn stretch of signal, in microvolts. Staging happens once over the
    # whole assembled night, not here.
    eeg: pd.DataFrame | None = None


def _archive_source(csv_path: Path) -> None:
    """Copy the raw CSV to durable local storage before doing anything with it.

    Recordings live on the Pi's SD card and are read over a network share. Cards
    fail — one did, taking every raw CSV with it. The derived EDFs had already
    been written here so nothing analytically important was lost, but the raw
    files (full passband, all columns) were gone.

    Copying costs a few seconds per night and makes the analysis host the system
    of record. Never fails the batch: a missing archive is worse than a lost
    night only if it stops the night being scored.
    """
    if not ARCHIVE_DIR:
        return
    try:
        dest_dir = Path(ARCHIVE_DIR)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / csv_path.name
        # Size check rather than blind copy: re-copying tens of MB every run is
        # pointless, but a truncated earlier copy must be replaced.
        if dest.exists() and dest.stat().st_size == csv_path.stat().st_size:
            return
        tmp = dest.with_suffix(dest.suffix + ".part")
        shutil.copy2(csv_path, tmp)
        tmp.replace(dest)
        log.info("  %s: archived raw CSV (%.1f MB)", csv_path.name,
                 dest.stat().st_size / 1e6)
    except Exception as exc:
        log.error("  %s: could not archive raw CSV: %s: %s",
                  csv_path.name, type(exc).__name__, exc)


def prepare_segment(csv_path: Path, out_dir: Path) -> Segment:
    """Load, quality-check and archive one recording file.

    Deliberately does NOT stage. Staging happens once, over the whole night —
    see assemble_night(). Only the EDF is written here.
    """
    name = csv_path.stem
    seg = Segment(path=csv_path, name=name)

    _archive_source(csv_path)

    eeg_uv, start_dt, measured = load_csv(csv_path)
    seg.duration_minutes = round(len(eeg_uv) / SFREQ / 60.0, 2)
    seg.sfreq_measured = round(measured, 2) if measured else None

    if measured and abs(measured - SFREQ) / SFREQ > 0.02:
        log.warning("  %s: measured %.1f Hz vs assumed %.0f Hz "
                    "(timestamps suggest dropped samples)", name, measured, SFREQ)

    raw = build_raw(eeg_uv)
    raw.filter(0.5, 40.0, fir_design="firwin", verbose="ERROR")

    # The EDF archives the WHOLE segment, including not-worn stretches — it is
    # the artifact you would hand to another tool. Only scoring is restricted.
    edf_path = out_dir / f"{name}.edf"
    mne.export.export_raw(str(edf_path), raw, fmt="edf",
                          physical_range="channelwise", overwrite=True,
                          verbose="ERROR")
    seg.edf = edf_path.name

    # Wear detection before the quality verdict: judging channel health across
    # the whole file would discard a good night because the band spent an hour
    # on the nightstand, which reads as railed.
    wear = detect_wear_window(eeg_uv)
    if wear is None:
        seg.channels = [asdict(q) for q in assess_channels(eeg_uv)]
        seg.reason = "headband never appears to have been worn"
        log.warning("  %s: %s", name, seg.reason)
        return seg

    w0, w1 = wear
    wear_start_s = w0 * EPOCH_SEC
    wear_end_s = min(w1 * EPOCH_SEC, float(raw.times[-1]))
    seg.wear_start_min = round(wear_start_s / 60.0, 2)
    seg.wear_minutes = round((wear_end_s - wear_start_s) / 60.0, 2)

    worn_uv = eeg_uv.iloc[int(wear_start_s * SFREQ): int(wear_end_s * SFREQ)]
    quality = assess_channels(worn_uv)
    seg.channels = [asdict(q) for q in quality]

    ch = pick_staging_channel(quality)
    if ch is None:
        seg.reason = "no usable channel (all railed or flat)"
        log.warning("  %s: %s", name, seg.reason)
        return seg

    seg.staging_channel = ch
    seg.eeg = worn_uv.reset_index(drop=True)
    if start_dt is not None:
        seg.start_utc = start_dt + timedelta(seconds=wear_start_s)
    seg.ok = True
    log.info("  %s: worn %.1f min of %.1f, %s", name, seg.wear_minutes,
             seg.duration_minutes,
             ", ".join(f"{q.name}={q.verdict}" for q in quality))
    return seg


def assemble_night(segs: list[Segment]) -> tuple[mne.io.RawArray, np.ndarray, datetime, float]:
    """Lay every worn stretch onto ONE continuous signal for the whole night.

    Staging each segment separately was measurably wrong: YASA's classifier uses
    temporal context across a recording, and a night arriving as twenty short
    files gave it almost none. On the first real overnight (2026-07-22, 21
    segments) per-segment staging returned TST 98 min and 26 min of N2; staging
    the identical data as one continuous night returned 146 min and 91.5 min.
    Do not go back to per-segment staging.

    Gaps between segments are zero-filled so the timeline keeps real elapsed
    time — closing them would shift every later epoch and corrupt clock times,
    sleep onset and REM latency. Those epochs are marked unscored afterwards,
    since whatever the classifier says about silence is meaningless.

    Returns (raw, is_gap_per_epoch, night_start_utc, gap_minutes).
    """
    ok = sorted((s for s in segs if s.ok and s.start_utc is not None),
                key=lambda s: s.start_utc)
    t0 = ok[0].start_utc
    end = max(s.start_utc + timedelta(seconds=len(s.eeg) / SFREQ) for s in ok)
    total = int(round((end - t0).total_seconds() * SFREQ))

    buf = np.zeros((len(EEG_CHANNELS), total))
    covered = np.zeros(total, dtype=bool)
    for s in ok:
        off = int(round((s.start_utc - t0).total_seconds() * SFREQ))
        arr = s.eeg[EEG_CHANNELS].to_numpy(dtype=float).T
        n = min(arr.shape[1], total - off)
        if n > 0:
            buf[:, off:off + n] = arr[:, :n]
            covered[off:off + n] = True

    info = mne.create_info(list(EEG_CHANNELS), SFREQ, "eeg")
    raw = mne.io.RawArray(buf * 1e-6, info, verbose="ERROR")
    raw.set_montage("standard_1020", on_missing="ignore", verbose="ERROR")
    # Deliberately NOT filtered. yasa's SleepStaging docs are explicit: "Do NOT
    # transform (e.g. z-score) or filter the signal before running the sleep
    # staging algorithm" — it does its own preprocessing. The per-segment EDFs
    # are filtered for archival and band power; the staging input is not.

    # An epoch counts as a gap if most of it has no data behind it.
    per = int(SFREQ * EPOCH_SEC)
    n_ep = total // per
    is_gap = np.array([covered[i*per:(i+1)*per].mean() < 0.5 for i in range(n_ep)],
                      dtype=bool)
    return raw, is_gap, t0, round(float(is_gap.sum()) * EPOCH_SEC / 60.0, 2)


def stage_bipolar(segs, channels, n_epochs: int, t0: datetime):
    """Stage the night on bipolar frontal-to-ear derivations and map to timeline.

    Two things have to be right together or the sleep numbers are nonsense:

    1. The DERIVATION. YASA is trained on frontal/central-to-mastoid montages.
       The Muse's native forehead-to-FPz signal reads as mostly WAKE (5-36% of a
       real night scored as sleep, never any N3). Subtracting the ear electrode
       (which sits at the mastoid) from the forehead recreates the long-dipole
       derivation YASA expects, and recovers realistic sleep and deep sleep —
       even though the ears are cracked and intermittently railed, because the
       subtraction tolerates an imperfect reference.

    2. The SIGNAL FED TO YASA MUST BE CONTINUOUS REAL DATA, not the zero-filled
       display timeline. YASA normalizes its features across the whole recording;
       feeding it the gap zeros destroys that normalization and collapses even
       the bipolar signal back to mostly WAKE. So we stage the CONCATENATION of
       the worn segments, then place each segment's per-epoch result back at its
       real position on the timeline (gaps stay unscored).

    Returns (stages, proba_on_timeline, used_name) or None to fall back.
    """
    per = int(SFREQ * EPOCH_SEC)
    verdict = {c["name"]: c["verdict"] for c in channels}
    ok = sorted((s for s in segs if s.ok and s.start_utc is not None),
                key=lambda s: s.start_utc)

    blocks, placement = [], []          # placement: (start_epoch, n_epochs_i)
    for s in ok:
        arr = s.eeg[EEG_CHANNELS].to_numpy(dtype=float).T
        ne = arr.shape[1] // per
        if ne == 0:
            continue
        blocks.append(arr[:, : ne * per])
        placement.append((int(round((s.start_utc - t0).total_seconds() / EPOCH_SEC)), ne))
    if not blocks:
        return None

    cat = np.concatenate(blocks, axis=1)
    chan = {n: cat[i] for i, n in enumerate(EEG_CHANNELS)}

    derivations = []
    for front, ear in (("AF7", "TP10"), ("AF8", "TP9")):
        if verdict.get(front) == "usable":
            derivations.append((f"{front}-{ear}", chan[front] - chan[ear]))
    if not derivations:                 # no usable frontal — plain fallback
        for front in ("AF7", "AF8"):
            if verdict.get(front) == "usable":
                derivations.append((front, chan[front]))
    if not derivations:
        return None

    info1 = mne.create_info(["bip"], SFREQ, "eeg")
    probas, used = [], []
    for dname, sig in derivations:
        try:
            r = mne.io.RawArray(sig[None, :] * 1e-6, info1, verbose="ERROR")
            probas.append(yasa.SleepStaging(r, eeg_name="bip").predict().proba)
            used.append(dname)
        except Exception as exc:
            log.warning("  staging on %s failed, skipping: %s", dname, exc)
    if not probas:
        return None

    pc = probas[0].copy()
    for p in probas[1:]:
        pc = pc.add(p, fill_value=0)
    pc = pc / len(probas)
    stages_cat = pc.idxmax(axis=1).tolist()

    # Place each segment's epochs back onto the real-time timeline.
    stages = ["UNS"] * n_epochs
    proba = pd.DataFrame(np.nan, index=range(n_epochs), columns=pc.columns)
    k = 0
    for start_ep, ne in placement:
        for j in range(ne):
            ep = start_ep + j
            if 0 <= ep < n_epochs:
                stages[ep] = stages_cat[k + j]
                proba.iloc[ep] = pc.iloc[k + j].values
        k += ne
    return stages, proba, "+".join(used)


def _expand_gaps(is_gap, guard: int) -> np.ndarray:
    """The gap mask, widened by `guard` epochs on each side of every gap.

    Used to trim the artifact-heavy epochs flanking each dropout — see the call
    site in process_night.
    """
    a = np.asarray(is_gap, dtype=bool)
    if guard <= 0 or not a.any():
        return a
    out = a.copy()
    for i in np.flatnonzero(a):
        out[max(0, i - guard): i + guard + 1] = True
    return out


def process_night(csv_paths: list[Path], out_dir: Path) -> Result:
    """Score one night, which may span several recorded segments."""
    paths = sorted(csv_paths)
    name = paths[0].stem
    res = Result(source=paths[0].name, analyzed_at=_now_iso())
    res.segments = [p.name for p in paths]
    res.n_segments = len(paths)

    out_dir.mkdir(parents=True, exist_ok=True)
    hypno_png = out_dir / f"{name}_hypnogram.png"
    proba_png = out_dir / f"{name}_proba.png"
    bandpower_png = out_dir / f"{name}_bandpower.png"
    stats_json = out_dir / f"{name}_stats.json"
    stats_txt = out_dir / f"{name}_stats.txt"

    if len(paths) > 1:
        log.info("  night spans %d segments", len(paths))

    # One unreadable segment must not sink the whole night. A file still being
    # written, truncated, or clobbered by an overlapping run used to raise and
    # error the entire night (FileNotFoundError on its EDF, seen 2026-08-08).
    # Skip the bad one and score from the rest.
    segs = []
    for p in paths:
        try:
            segs.append(prepare_segment(p, out_dir))
        except Exception as exc:
            log.error("  %s: segment unreadable, skipping (%s: %s)",
                      p.name, type(exc).__name__, exc)
    if not segs:
        res.status = "error"
        res.reason = "every segment failed to load"
        log.error("  %s: %s", name, res.reason)
        _write_stats(res, stats_json, stats_txt)
        return res

    res.duration_minutes = round(sum(s.duration_minutes for s in segs), 2)
    res.sfreq_measured = next((s.sfreq_measured for s in segs if s.sfreq_measured), None)
    res.outputs["edf"] = next((s.edf for s in segs if s.edf), None)
    res.edfs = [s.edf for s in segs if s.edf]
    res.channels = next((s.channels for s in segs if s.ok), segs[0].channels)

    scored = [s for s in segs if s.ok]
    if not scored:
        res.status = "bad"
        res.reason = segs[0].reason or "no segment could be staged"
        res.start_time = next((s.start_utc.isoformat() for s in segs if s.start_utc), None)
        log.warning("  %s: %s — EDF kept, staging skipped", name, res.reason)
        _write_stats(res, stats_json, stats_txt)
        return res

    ch = scored[0].staging_channel
    res.wear_minutes = round(sum(s.wear_minutes or 0 for s in scored), 2)
    res.wear_start_min = scored[0].wear_start_min

    raw, is_gap, t0, gap_min = assemble_night(scored)
    res.gap_minutes = gap_min
    res.start_time = t0.isoformat()

    if raw.times[-1] / 60.0 < MIN_STAGING_MINUTES:
        res.status = "bad"
        res.reason = f"only {raw.times[-1]/60.0:.1f} min of usable signal"
        log.warning("  %s: %s", name, res.reason)
        _write_stats(res, stats_json, stats_txt)
        return res

    # Stage on bipolar frontal-to-ear derivations over the CONCATENATED real
    # signal — see stage_bipolar() for why both matter.
    n_epochs = len(is_gap)
    result = stage_bipolar(scored, res.channels, n_epochs, t0)
    if result is not None:
        stages, proba, res.staging_channel = result
    else:
        # Last-ditch fallback: single channel on the zero-filled assembly.
        staged = yasa.SleepStaging(raw, eeg_name=ch).predict()
        stages, proba, res.staging_channel = (
            staged.hypno.tolist(), staged.proba, ch)

    log.info("  assembled %.1f min continuous (%.1f min of gaps), staged on %s",
             raw.times[-1] / 60.0, gap_min, res.staging_channel)

    # Mark unscored: the zero-filled gaps themselves, PLUS a guard band of a few
    # epochs on each side of every gap. When the Bluetooth link dies and later
    # reconnects, the signal on either side of the gap is artifact-heavy (the
    # link failing, then the amplifier re-settling), and the classifier reads
    # that noise as WAKE — inflating time-awake and awakenings on exactly the
    # dropout-riddled nights this headband produces. Trimming those boundary
    # epochs stops a flaky link from masquerading as broken sleep.
    guard = int(os.environ.get("DROPOUT_GUARD_EPOCHS", "2"))   # ~1 min each side
    unscored = _expand_gaps(is_gap, guard)
    trimmed = (unscored.sum() - int(np.asarray(is_gap).sum())) * EPOCH_SEC / 60.0
    for i in range(min(len(stages), len(unscored))):
        if unscored[i]:
            stages[i] = "UNS"
    if proba is not None and len(proba):
        proba = proba.copy()
        proba.iloc[[i for i in range(min(len(proba), len(unscored)))
                    if unscored[i]]] = np.nan
    if trimmed > 0:
        log.info("  trimmed %.1f min of artifact around %d dropouts",
                 trimmed, int(np.diff(np.asarray(is_gap, int)).clip(min=0).sum()))

    local_start = t0.astimezone()
    hypno = yasa.Hypnogram(stages, n_stages=5, freq="30s",
                           start=local_start.replace(tzinfo=None))

    stats = {k: _jsonable(v) for k, v in hypno.sleep_statistics().items()}
    tst = stats.get("TST") or 0.0
    stats["SE_worn"] = stats.get("SE")
    stats["SE_recording"] = round(100.0 * tst / res.duration_minutes, 3) \
        if res.duration_minutes else None
    stats["awakenings"] = count_awakenings(stages)
    res.stats = stats
    res.stage_counts = {k: int(v) for k, v in pd.Series(stages).value_counts().items()}

    res.wear_start_time = local_start.isoformat(timespec="seconds")
    sleep_idx = [i for i, s in enumerate(stages)
                 if s not in ("WAKE", "W", "ART", "UNS")]
    if sleep_idx:
        res.sleep_onset_time = (
            local_start + timedelta(seconds=sleep_idx[0] * EPOCH_SEC)
        ).isoformat(timespec="seconds")
        res.final_wake_time = (
            local_start + timedelta(seconds=(sleep_idx[-1] + 1) * EPOCH_SEC)
        ).isoformat(timespec="seconds")

    suffix = f" ({len(paths)} segments)" if len(paths) > 1 else ""
    plot_hypnogram(hypno, hypno_png, f"{name} — staged on {ch}{suffix}",
                   start_dt=local_start)
    res.outputs["hypnogram"] = hypno_png.name

    if proba is not None and not proba.empty:
        plot_proba(proba, proba_png, f"{name} — stage probability ({ch})")
        res.outputs["proba"] = proba_png.name

    try:
        # Band power is illustrative and needs a single real channel (the used
        # names may be bipolar derivations like "AF7-TP10"); staging deliberately
        # is not filtered, band power is.
        bp_ch = "AF7" if "AF7" in raw.ch_names else (
            "AF8" if "AF8" in raw.ch_names else ch)
        bp = compute_bandpower(
            raw.copy().filter(0.5, 40.0, fir_design="firwin", verbose="ERROR"), bp_ch)
        if not bp.empty:
            plot_bandpower(bp, bandpower_png,
                           f"{name} — relative band power ({bp_ch})")
            res.outputs["bandpower"] = bandpower_png.name
    except Exception as exc:
        log.warning("  band power failed: %s: %s", type(exc).__name__, exc)

    _write_stats(res, stats_json, stats_txt)

    se = res.stats.get("SE_worn")
    if tst is not None and se is not None:
        log.info("  TST %.0f min, efficiency %.1f%%", float(tst), float(se))
    return res


def process_file(csv_path: Path, out_dir: Path, force: bool = False) -> Result:
    """Single-file entry point (CLI convenience); a night of one segment."""
    return process_night([csv_path], out_dir)


def _write_stats(res: Result, json_path: Path, txt_path: Path) -> None:
    res.outputs["stats_json"] = json_path.name
    payload = asdict(res)
    json_path.write_text(json.dumps(payload, indent=2, default=str))

    lines = [
        f"Recording : {res.source}",
        f"Start     : {res.start_time or 'unknown'}",
        f"Duration  : {res.duration_minutes:.1f} min (whole recording)"
        + (f" across {res.n_segments} segments" if res.n_segments > 1 else ""),
        f"Worn      : {f'{res.wear_minutes:.1f} min' if res.wear_minutes else 'n/a'}"
        + (f", from +{res.wear_start_min:.1f} min" if res.wear_start_min else ""),
        f"Status    : {res.status}" + (f" ({res.reason})" if res.reason else ""),
        f"Staged on : {res.staging_channel or 'n/a'}",
        f"Asleep    : {res.sleep_onset_time or 'n/a'} -> {res.final_wake_time or 'n/a'}",
        f"Gaps      : {res.gap_minutes:.1f} min unscored between segments",
        "",
        "Channel quality:",
    ]
    for c in res.channels:
        lines.append(
            f"  {c['name']:<5} {c['verdict']:<7} "
            f"railed={c['railed_fraction']*100:5.1f}%  std={c['std_uv']:.1f} uV"
        )
    if res.stats:
        lines += ["", "Sleep statistics:"]
        for k, v in res.stats.items():
            lines.append(f"  {k:<12} {v}")
    txt_path.write_text("\n".join(lines) + "\n")
    res.outputs["stats_txt"] = txt_path.name


def _jsonable(v):
    """Coerce a yasa statistic into something JSON and SQLite can hold.

    NaN/inf must become None: yasa returns NaN for undefined statistics (SFI
    when TST is zero, Lat_REM when there is no REM), and json.dumps would emit
    a bare `NaN` literal, which is invalid JSON and breaks any strict parser.
    """
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        if not np.isfinite(f):
            return None
        return round(f, 3)
    return v


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Batch driver
# --------------------------------------------------------------------------

def already_done(paths: list[Path], out_root: Path) -> Path | None:
    """Existing result for this night, or None if it needs (re)processing.

    Compares the recorded segment list against the night as it now stands. This
    matters: a container restart mid-night triggers RUN_ON_START, which would
    score the segments recorded so far and mark the night complete. Come 09:00
    the full night regroups, and a name-only check would skip it as "already
    processed" — silently discarding every hour recorded after the restart.
    """
    name = paths[0].stem
    want = sorted(p.name for p in paths)

    for sub in ("good", "failed"):
        candidate = out_root / sub / f"{name}_stats.json"
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if data.get("schema_version") != SCHEMA_VERSION:
            return None

        have = sorted(data.get("segments") or [data.get("source")])
        if have != want:
            log.info("%s: night now has %d segment(s), previously %d — reprocessing",
                     name, len(want), len(have))
            return None
        return candidate
    return None


def run_batch(input_dir: Path, out_root: Path, force: bool = False) -> list[Result]:
    csvs = sorted(p for p in input_dir.glob("*.csv") if p.is_file())
    if not csvs:
        # Distinguish "the recorder produced nothing" from "we cannot see the
        # recordings at all". Both look identical in a log that just says
        # "0 files", and the second has silently cost a night before.
        if not input_dir.exists():
            log.error("INPUT UNREACHABLE: %s does not exist — is the share mounted?",
                      input_dir)
        elif not any(input_dir.iterdir()):
            log.error("INPUT EMPTY: %s has no files at all. Either the recorder "
                      "produced nothing, or the mount is not visible here "
                      "(check `mountpoint` on the host and that the container "
                      "binds the parent with rslave propagation).", input_dir)
        else:
            log.warning("no CSV files found in %s (directory is not empty)", input_dir)
        return []

    # Drop anything still being written before grouping. muselsl appends as it
    # goes, so an in-progress file looks perfectly valid — analysing it would
    # produce a partial night AND poison the skip check, so the finished
    # recording would later be passed over as "already processed".
    ready = []
    for p in csvs:
        age_min = (time.time() - p.stat().st_mtime) / 60.0
        if age_min < STABLE_MINUTES:
            log.info("%s: still being written (modified %.1f min ago), skipping",
                     p.name, age_min)
        else:
            ready.append(p)

    nights = group_nights(ready)
    log.info("found %d recording(s) in %s -> %d night(s)",
             len(csvs), input_dir, len(nights))
    results: list[Result] = []

    for paths in nights:
        stem = paths[0].stem
        existing = already_done(paths, out_root)
        if existing and not force:
            log.info("%s: already processed, skipping", paths[0].name)
            try:
                results.append(_result_from_json(existing))
            except Exception:
                pass
            continue

        total_mb = sum(p.stat().st_size for p in paths) / 1e6
        log.info("%s: processing (%.1f MB, %d segment%s)", paths[0].name,
                 total_mb, len(paths), "" if len(paths) == 1 else "s")

        # Stage into a temp dir, then move to good/ or failed/ once we know
        # which it is. Sorting never touches the source CSV — it may live on
        # the Pi's card and the recorder owns that directory.
        work = out_root / ".work" / stem
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)

        try:
            res = process_night(paths, work)
        except Exception as exc:
            res = Result(source=paths[0].name, status="error",
                         reason=f"{type(exc).__name__}: {exc}",
                         segments=[p.name for p in paths],
                         analyzed_at=_now_iso())
            log.error("%s: FAILED — %s", paths[0].name, res.reason)
            log.debug("%s", traceback.format_exc())
            work.mkdir(parents=True, exist_ok=True)
            _write_stats(res, work / f"{stem}_stats.json",
                         work / f"{stem}_stats.txt")

        dest = out_root / ("good" if res.status == "ok" else "failed")
        _move_outputs(work, dest, stem)
        _record(out_root, res, dest.name)
        results.append(res)

    _prune_workdir(out_root)
    return results


def _record(out_root: Path, res: Result, asset_dir: str) -> None:
    """Persist one result to SQLite. Never fail the batch over the database."""
    try:
        import db
        conn = db.connect(out_root / "sleep.db")
        try:
            db.upsert_night(conn, asdict(res), asset_dir)
        finally:
            conn.close()
    except Exception as exc:
        log.error("  database write failed: %s: %s", type(exc).__name__, exc)


def _move_outputs(work: Path, dest: Path, stem: str) -> None:
    """Move this recording's artifacts into good/ or failed/."""
    dest.mkdir(parents=True, exist_ok=True)
    if not work.exists():
        return

    for item in work.iterdir():
        target = dest / item.name
        if target.exists():
            target.unlink()
        shutil.move(str(item), str(target))

    # Clear any copy left in the other bucket, so a recording that changes
    # verdict on reprocessing doesn't show up twice in the index.
    other = dest.parent / ("failed" if dest.name == "good" else "good")
    if other.exists():
        for stale in other.glob(f"{stem}*"):
            stale.unlink()

    shutil.rmtree(work, ignore_errors=True)


def _prune_workdir(out_root: Path) -> None:
    workdir = out_root / ".work"
    if workdir.exists() and not any(workdir.iterdir()):
        shutil.rmtree(workdir, ignore_errors=True)


def _result_from_json(path: Path) -> Result:
    data = json.loads(path.read_text())
    known = {f for f in Result.__dataclass_fields__}
    return Result(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", nargs="?",
                    default=os.environ.get("INPUT_DIR", "/data/recordings"),
                    help="directory of muselsl CSVs (or a single .csv file)")
    ap.add_argument("-o", "--output",
                    default=os.environ.get("OUTPUT_DIR", "/data/output"),
                    help="where results are written")
    ap.add_argument("--force", action="store_true",
                    help="reprocess recordings that already have results")
    ap.add_argument("--no-index", action="store_true",
                    help="skip regenerating index.html")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    # force=True is required: importing mne installs its own handler on the
    # root logger, which makes a plain basicConfig() a silent no-op and hides
    # every per-file line this script logs.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    mne.set_log_level("ERROR")

    in_path = Path(args.input)
    out_root = Path(args.output)

    global ARCHIVE_DIR
    if ARCHIVE_DIR is None:
        ARCHIVE_DIR = str(out_root / "raw")

    if not in_path.exists():
        log.error("input path does not exist: %s", in_path)
        return 2

    # Serialize every run. The scheduled batch, the run-on-start, and a manual
    # `docker exec ... analyze.py` all share one .work directory; overlapping
    # runs prune each other's temp files mid-write and error a whole night
    # (FileNotFoundError on a segment EDF, 2026-08-08). A non-blocking flock
    # makes a second run exit cleanly rather than collide — the next scan, at
    # most a few hours later, does the work.
    import fcntl
    out_root.mkdir(parents=True, exist_ok=True)
    lock_path = out_root / ".batch.lock"
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("another analyze run holds the lock — exiting without doing work")
        return 0

    started = datetime.now()

    if in_path.is_file():
        work = out_root / ".work" / in_path.stem
        shutil.rmtree(work, ignore_errors=True)
        try:
            res = process_file(in_path, work)
        except Exception as exc:
            log.error("%s: FAILED — %s: %s", in_path.name, type(exc).__name__, exc)
            log.debug("%s", traceback.format_exc())
            return 1
        dest = out_root / ("good" if res.status == "ok" else "failed")
        _move_outputs(work, dest, in_path.stem)
        _record(out_root, res, dest.name)
        _prune_workdir(out_root)
        results = [res]
    else:
        results = run_batch(in_path, out_root, force=args.force)

    if not args.no_index and results:
        try:
            from report import write_index
            write_index(out_root)
            log.info("index written to %s", out_root / "index.html")
        except Exception as exc:
            log.error("index generation failed: %s: %s", type(exc).__name__, exc)

    ok = sum(1 for r in results if r.status == "ok")
    bad = sum(1 for r in results if r.status == "bad")
    err = sum(1 for r in results if r.status == "error")
    log.info(
        "done in %.0fs — %d ok, %d unusable, %d errored",
        (datetime.now() - started).total_seconds(), ok, bad, err,
    )
    # A batch containing bad recordings is still a successful batch; only a
    # hard error in the driver itself is worth a nonzero exit.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
