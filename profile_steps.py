#!/usr/bin/env python3
"""Time each pipeline stage separately, to find where the time and memory go."""
import resource
import sys
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import mne
import numpy as np
import yasa

import analyze

mne.set_log_level("ERROR")
path = Path(sys.argv[1])
t0 = time.time()


def mark(label):
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    print(f"{time.time() - t0:8.1f}s  peak_rss={rss:6.2f}GB  {label}", flush=True)


mark("start")
eeg, start_dt, measured = analyze.load_csv(path)
mark(f"load_csv  rows={len(eeg)}  ({len(eeg)/256/60:.1f} min)")

q = analyze.assess_channels(eeg)
mark("assess_channels: " + ", ".join(f"{c.name}={c.verdict}" for c in q))

raw = analyze.build_raw(eeg)
mark("build_raw")

raw.filter(0.5, 40.0, fir_design="firwin", verbose="ERROR")
mark("filter")

mne.export.export_raw("/tmp/p.edf", raw, fmt="edf",
                      physical_range="channelwise", overwrite=True, verbose="ERROR")
mark("export_edf")

ch = analyze.pick_staging_channel(q)
sls = yasa.SleepStaging(raw, eeg_name=ch)
mark(f"SleepStaging init (ch={ch})")

staged = sls.predict()
proba = staged.proba
mark(f"predict (+proba)  n_epochs={len(staged)}")

hyp = yasa.Hypnogram(staged.hypno.tolist(), n_stages=5, freq="30s",
                     start=start_dt.replace(tzinfo=None) if start_dt else None,
                     proba=proba)
mark("Hypnogram")

stats = hyp.sleep_statistics()
mark("sleep_statistics")

analyze.plot_hypnogram(hyp, Path("/tmp/p_hypno.png"), "profile")
mark("plot_hypnogram")

analyze.plot_proba(proba, Path("/tmp/p_proba.png"), "profile")
mark("plot_proba")

print("\nstage counts:", staged.hypno.value_counts().to_dict())
print("\nsleep_statistics keys:", sorted(stats.keys()))
print("\nTST:", stats.get("TST"), " SE:", stats.get("SE"), " SFI:", stats.get("SFI"))
