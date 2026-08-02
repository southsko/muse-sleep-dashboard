#!/usr/bin/env python3
"""Generate fake muselsl CSVs for testing the pipeline.

This exercises code paths, not sleep science. Synthetic data proves the
pipeline runs and the quality guard fires; it says nothing about whether the
staging output is sensible. That needs a real overnight recording.

Usage:  python make_synthetic.py /data/recordings
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

SFREQ = 256
COLUMNS = ["timestamps", "TP9", "AF7", "AF8", "TP10", "Right AUX"]

# Rough band structure per sleep stage: (dominant Hz, amplitude uV)
STAGE_BANDS = {
    "W":  [(10, 20), (20, 8)],
    "N1": [(6, 25), (10, 10)],
    "N2": [(4, 35), (13, 15)],
    "N3": [(1.5, 90), (3, 40)],
    "R":  [(6, 22), (18, 7)],
}

# A plausible night: mostly N2/N3 early, more REM toward morning.
NIGHT = (["W"] * 2 + ["N1"] * 2 + ["N2"] * 8 + ["N3"] * 10 + ["N2"] * 6 +
         ["R"] * 5 + ["N2"] * 8 + ["N3"] * 5 + ["N2"] * 6 + ["R"] * 8 +
         ["N2"] * 5 + ["R"] * 6 + ["W"] * 2)


def make_signal(minutes: float, rng: np.random.Generator) -> np.ndarray:
    """Build a stage-varying EEG-ish trace of the requested length, in uV."""
    n_total = int(minutes * 60 * SFREQ)
    per_block = max(1, n_total // len(NIGHT))
    chunks = []
    for stage in NIGHT:
        t = np.arange(per_block) / SFREQ
        sig = rng.normal(0, 6, per_block)
        for freq, amp in STAGE_BANDS[stage]:
            phase = rng.uniform(0, 2 * np.pi)
            sig += amp * np.sin(2 * np.pi * freq * t + phase)
        chunks.append(sig)
    out = np.concatenate(chunks)
    if out.size < n_total:
        out = np.pad(out, (0, n_total - out.size), mode="wrap")
    return out[:n_total]


def write_csv(path: Path, minutes: float, mode: str, seed: int = 0,
              start_offset_min: float | None = None) -> None:
    rng = np.random.default_rng(seed)
    n = int(minutes * 60 * SFREQ)
    # start_offset_min places this segment that many minutes before "now", so
    # several files can be given a realistic relative timeline.
    start = time.time() - (start_offset_min if start_offset_min is not None
                           else minutes) * 60

    data = {"timestamps": start + np.arange(n) / SFREQ}

    for ch in ("TP9", "AF7", "AF8", "TP10"):
        sig = make_signal(minutes, rng)

        if mode == "railed_ears" and ch in ("TP9", "TP10"):
            # Electrode fell off: amplifier pinned at the rails, flipping sign.
            sig = np.where(rng.random(n) > 0.5, 1000.0, -1000.0)
        elif mode == "flat":
            sig = np.zeros(n) + rng.normal(0, 0.05, n)
        elif mode == "edges_not_worn":
            # Band recording on the nightstand for the first and last 5 minutes.
            pad = int(5 * 60 * SFREQ)
            sig[:pad] = np.where(rng.random(pad) > 0.5, 1000.0, -1000.0)
            sig[-pad:] = np.where(rng.random(pad) > 0.5, 1000.0, -1000.0)

        data[ch] = sig

    data["Right AUX"] = np.zeros(n)
    pd.DataFrame(data, columns=COLUMNS).to_csv(path, index=False)
    print(f"wrote {path}  ({minutes:.0f} min, mode={mode}, {path.stat().st_size/1e6:.1f} MB)")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/data/recordings")
    out.mkdir(parents=True, exist_ok=True)

    # 15 minutes exercises every code path (>5 min staging floor, 30 epochs)
    # while keeping the build-time smoke test fast. A realistic 60-minute night
    # produced 90 MB fixtures and bought nothing.
    # Fixtures must be spaced further apart than MERGE_GAP_MINUTES, or the
    # grouper will (correctly) treat them as segments of one night.
    write_csv(out / "synthetic_clean.csv", 15, "clean", seed=1, start_offset_min=600)
    write_csv(out / "synthetic_railed_ears.csv", 15, "railed_ears", seed=2,
              start_offset_min=500)
    write_csv(out / "synthetic_flat.csv", 10, "flat", seed=3, start_offset_min=400)
    # 25 min total: 5 min on the nightstand, 15 worn, 5 back on the nightstand.
    write_csv(out / "synthetic_edges_not_worn.csv", 25, "edges_not_worn", seed=4,
              start_offset_min=300)

    # A night that arrived as three segments, as the supervisor produces when
    # the BLE link drops: 15 min, 2 min gap, 15 min, 3 min gap, 15 min.
    # These must be merged into ONE scored night, not three fragments.
    write_csv(out / "seg_night_a.csv", 15, "clean", seed=5, start_offset_min=50)
    write_csv(out / "seg_night_b.csv", 15, "clean", seed=6, start_offset_min=33)
    write_csv(out / "seg_night_c.csv", 15, "clean", seed=7, start_offset_min=15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
