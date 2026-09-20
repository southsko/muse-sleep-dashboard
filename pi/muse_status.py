#!/usr/bin/env python3
"""Live signal view for the Muse S Athena recorder — a local Mind-Monitor page.

Runs ON THE PI, alongside the OpenMuse recorder. The recorder writes raw BLE
packets to a growing .txt in ~/recordings/raw/; this page *tails* that file (reads
only the last chunk), decodes the recent packets with OpenMuse, and shows live
EEG, contact quality, per-channel band power, and battery.

Why tail the file instead of a second Bluetooth connection: the Muse accepts
exactly ONE BLE link, and the recorder holds it. Reading the file the recorder is
already writing costs nothing on the radio and cannot perturb or stop the night.

Reads only the TAIL of the raw file (never the whole thing, which is hundreds of
MB by morning), so memory stays flat regardless of how long the night runs.

Standard library for the server; the decode borrows OpenMuse + numpy/scipy from
the recorder's venv (the systemd unit runs this under that venv).

    <venv>/bin/python3 muse_status.py       # serves on :8080
    PORT=9000 <venv>/bin/python3 muse_status.py
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

PORT = int(os.environ.get("PORT", "8080"))
SFREQ = 256.0
CHANNELS = ["TP9", "AF7", "AF8", "TP10"]
# Plain-English position for each 10-20 code, so nobody has to remember that odd
# numbers are left and TP sits behind the ear. Frontals do the staging; the ears
# routinely lose contact overnight and that is expected, not a fault.
CHANNEL_LABELS = {"TP9": "left ear", "AF7": "left forehead",
                  "AF8": "right forehead", "TP10": "right ear"}

RAWDIR = os.path.expanduser("~/recordings/raw")
RECDIR = os.path.expanduser("~/recordings")
TAIL_BYTES = 131072                # only ever read the last 128 KB of the raw file
# How often to re-tail and decode. With OpenMuse line-buffered (install-athena.sh
# patches it), data lands in the file ~10x/sec, so 0.1s keeps the view genuinely
# live. Without that patch OpenMuse only flushes ~1x/sec and this can't help.
POLL_SEC = float(os.environ.get("POLL_SEC", "0.1"))

BUFFER_SEC = 12.0                  # rolling window kept in memory
DISPLAY_HZ = 51.2                  # decimated rate sent to the browser
DECIMATE = int(SFREQ / DISPLAY_HZ)  # 5 -> 51.2 Hz, plenty for a visual trace
FRAME_HZ = 12                      # SSE frames per second (data now lands ~10 Hz;
#                                    20 fps of full traces was heavy on mobile and
#                                    dropped the SSE connection — 12 matches the data)
QUALITY_SEC = 2.0                  # window for contact quality
BAND_SEC = 4.0                     # window for band power

# Same thresholds the analysis pipeline uses, so what you see while fitting the
# band matches how the night will actually be judged. Rail check is on the
# excursion from the channel's own median — Athena rides a ~700µV DC offset, so an
# absolute |x|>900 test would read a clean channel as railed (see analyze.py).
RAIL_UV = 900.0
RAIL_FRACTION = 0.20
FLAT_STD_UV = 1.0

BANDS = [("Delta", 0.5, 4.0), ("Theta", 4.0, 8.0), ("Alpha", 8.0, 12.0),
         ("Sigma", 12.0, 16.0), ("Beta", 16.0, 30.0)]

# --- Live sleep-state estimate --------------------------------------------
# NOT clinical staging: two forehead sensors can't tell REM from wake, or exact
# N-stages — the morning YASA pass owns the real hypnogram. This fuses three
# live-computable signals into Awake / Drowsy / Asleep:
#   * slow-wave ratio (delta+theta)/(alpha+beta) on the bipolar frontal-to-ear
#     derivation YASA itself stages on — the strongest single cue;
#   * muscle/EMG calm (30-50 Hz power falls asleep);
#   * stillness (IMU) — thrashing means awake regardless of EEG.
# Thresholds below were calibrated against YASA hypnograms from recorded nights
# (combined score AUC ~0.77; the "asleep" band was ~83% real sleep, "awake"
# ~78% real wake). Kept honest: it reports a confidence and its inputs, and says
# "can't tell" when the forehead sensors lose contact.
SLEEP_COMPUTE_SEC = 2.0            # recompute cadence (a Welch isn't free at frame rate)
SLEEP_WIN_SEC = 8.0               # EEG window for the spectral ratio
SLEEP_SMOOTH_SEC = 90.0           # rolling window the score is averaged over
SLEEP_ONSET_SEC = 180.0           # sustained sleep before we stamp "asleep since"
SLEEP_MIN_SAMPLES = 5             # clean EEG windows needed before trusting a stage —
#                                   one lucky glitch must NOT read as deep sleep
SLEEP_ENTER = 0.60                # smoothed score to call it asleep …
SLEEP_STAY = 0.45                 # … and to keep it (hysteresis, no flicker)
SLEEP_DROWSY = 0.35              # below this = awake

# Per-wearer calibration. The generic thresholds were tuned on one night's YASA
# staging; a given person's band/fit/contact shifts where their "asleep" cluster
# sits (especially forehead-only, when their ears rail). Drop a JSON here — from a
# labelled capture of them actually asleep — to retune anchors/weights/thresholds
# to that wearer; absent or unreadable → the generic defaults below. Reloaded ~10s
# so an update takes effect without restarting.
SLEEP_CALIB_PATH = os.path.join(RECDIR, ".sleep_calib.json")
_CALIB = {"val": None, "at": 0.0}


def sleep_calib() -> dict:
    now = time.time()
    if _CALIB["val"] is not None and now - _CALIB["at"] < 10.0:
        return _CALIB["val"]
    cal = {"name": None, "sw_lo": 3.0, "sw_hi": 20.0, "emg_lo": 0.10, "emg_hi": 0.01,
           "w_sw": 0.5, "w_emg": 0.3, "w_still": 0.2,
           "enter": SLEEP_ENTER, "stay": SLEEP_STAY, "drowsy": SLEEP_DROWSY}
    try:
        with open(SLEEP_CALIB_PATH, encoding="utf-8") as f:
            cal.update(json.load(f))
    except (OSError, ValueError):
        pass
    _CALIB["val"], _CALIB["at"] = cal, now
    return cal

# The Athena's optics (fNIRS/PPG) and IMU are on the same BLE stream, so the page
# can show heart rate and movement "for free" from data already arriving — the
# silver lining of not being able to turn the optics LEDs off.
PPG_FS = 64.0                      # OPTICS sample rate
PPG_SEC = 40.0                     # optics window: long enough for HRV + respiration
#                                    (respiration lives at 0.1-0.4 Hz, so it needs
#                                    tens of seconds, not the ~16 s HR alone wants)
ACC_FS = 52.0                      # ACCGYRO sample rate (approximate)
ACC_SEC = 8.0                      # window for the movement metric


class Collector:
    """Tails the recorder's raw .txt in a background thread, decoding recent
    packets into a rolling EEG buffer. No Bluetooth, no LSL — just the file."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.buf = deque(maxlen=int(BUFFER_SEC * SFREQ))   # rows of 4 channels, µV
        self.connected = False
        self.samples = 0
        self.last_sample_at = 0.0
        self.started = time.time()
        self._recent = deque(maxlen=64)
        self.battery = None            # from the Athena BATTERY frame; None until seen
        self.battery_at = 0.0
        self._cur_file = None
        self._last_ts = ""             # ISO timestamp of the last raw line ingested
        self.ppg = deque(maxlen=int(PPG_SEC * PPG_FS))   # optics rows (PPG channels)
        self.ppg_cols: list[str] = []  # live optics column names (varies by preset)
        self.ppg_at = 0.0              # last time optics rows actually arrived
        self.acc = deque(maxlen=int(ACC_SEC * ACC_FS))   # accel magnitude, g
        self._vit = None               # cached optics vitals bundle (recomputed ~1/s)
        self._vit_at = 0.0
        self.sleep_hist = deque(maxlen=90)  # (t, score, delta_dom) at ~2s cadence
        self._sleep = None                  # cached sleep-state bundle
        self._sleep_at = 0.0
        self.sleep_name = "awake"           # last committed state (for hysteresis)
        self.asleep_since = 0.0             # epoch sustained sleep began (0 = awake)
        self._sleep_cand = 0.0              # epoch a sleep candidate started (onset delay)

    def _newest_raw(self) -> str | None:
        files = glob.glob(os.path.join(RAWDIR, "*.txt"))
        return max(files, key=os.path.getmtime) if files else None

    def _tail_lines(self, path: str) -> list[str]:
        """Last TAIL_BYTES of the file as whole lines (drop a partial first)."""
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
            data = f.read()
        lines = data.decode("utf-8", errors="replace").split("\n")
        if size > TAIL_BYTES:
            lines = lines[1:]          # first line is probably truncated
        return [ln for ln in lines if ln.strip()]

    def run(self) -> None:
        import OpenMuse
        import pandas as pd
        while True:
            try:
                path = self._newest_raw()
                if not path:
                    self.connected = False
                    time.sleep(1.0)
                    continue
                if path != self._cur_file:          # rolled to a new segment
                    self._cur_file = path
                    self._last_ts = ""
                lines = self._tail_lines(path)
                # Each raw line begins with an ISO-8601 timestamp; they sort
                # lexically = chronologically, so keep only lines newer than the
                # last one ingested. This dedups the overlapping tail robustly,
                # without depending on how decode assigns its own 'time' column.
                new = [ln for ln in lines
                       if ln.split("\t", 1)[0] > self._last_ts]
                if new:
                    self._last_ts = new[-1].split("\t", 1)[0]
                    self._ingest(OpenMuse.decode_rawdata(new), pd)
                # Stale only after 6 s of no new data — longer than the gap
                # between OpenMuse's flush bursts, so a steady link never trips it.
                # A real dead link stops the file growing and this still fires well
                # before the recorder's own ~100 s stall/rollover.
                if time.time() - self.last_sample_at > 6:
                    self.connected = False
                time.sleep(POLL_SEC)
            except Exception:
                self.connected = False
                time.sleep(1.0)

    def _ingest(self, data: dict, pd) -> None:
        now = time.time()
        eeg = data.get("EEG")
        if eeg is not None and len(eeg):
            # Athena columns are prefixed EEG_TP9 etc.; map tolerantly.
            colmap = {str(c).lower().replace(" ", "").removeprefix("eeg_"): c
                      for c in eeg.columns}
            if all(ch.lower() in colmap for ch in CHANNELS):
                arr = np.column_stack([
                    pd.to_numeric(eeg[colmap[ch.lower()]], errors="coerce").to_numpy()
                    for ch in CHANNELS])
                with self.lock:
                    for row in arr:
                        self.buf.append(row)
                    self.samples += len(arr)
                    self._recent.append((now, len(arr)))
                self.last_sample_at = now
                self.connected = True
        opt = data.get("OPTICS")
        if opt is not None and len(opt):
            chans = [c for c in opt.columns if c != "time"]
            if chans:
                arr = opt[chans].apply(pd.to_numeric, errors="coerce").to_numpy()
                # Optics rows are all-zero on the EEG-only presets even when the
                # frame decodes (see recorder note); only treat it as a live burst
                # when the LEDs are actually driving signal.
                if np.any(np.abs(arr) > 1e-9):
                    with self.lock:
                        self.ppg_cols = chans
                        for row in arr:
                            self.ppg.append(row)
                    self.ppg_at = now
        acc = data.get("ACCGYRO")
        if acc is not None and len(acc):
            axc = [c for c in acc.columns if c.startswith("ACC")]
            if axc:
                mag = np.linalg.norm(
                    acc[axc].apply(pd.to_numeric, errors="coerce").to_numpy(), axis=1)
                with self.lock:
                    for m in mag:
                        self.acc.append(float(m))
        bat = data.get("BATTERY")
        if bat is not None and len(bat) and "battery_percent" in bat.columns:
            with self.lock:
                self.battery = round(float(bat["battery_percent"].iloc[-1]), 1)
                self.battery_at = now

    def snapshot(self) -> np.ndarray:
        with self.lock:
            if not self.buf:
                return np.zeros((0, 4))
            return np.asarray(self.buf, dtype=float)

    def data_rate(self) -> float:
        """Samples per second, averaged over a window WIDE enough to span several
        of OpenMuse's file-flush bursts. A 2 s window saw zero new lines between
        flushes and reported 0 Hz, which made the page flap connected/disconnected
        on a perfectly steady link. 6 s always spans multiple flushes."""
        now = time.time()
        with self.lock:
            pts = [(t, n) for t, n in self._recent if now - t < 6.0]
            newest = self.last_sample_at
        if len(pts) < 2:
            # Too few flushes in the window to divide, but if data landed very
            # recently the link is clearly alive — report the nominal rate rather
            # than a misleading 0 that would blink the page to "no data".
            return SFREQ if (now - newest) < 3.0 else 0.0
        span = pts[-1][0] - pts[0][0]
        return (sum(n for _, n in pts[1:]) / span) if span > 0 else 0.0


def _notch_mains(x: np.ndarray) -> np.ndarray:
    """Remove 60 and 50 Hz mains hum. On this rig the hum is ~570µV — ~70x the real
    EEG — so it must come out before any band analysis or it swamps everything."""
    from scipy.signal import iirnotch, filtfilt
    try:
        for f0 in (60.0, 50.0):
            b, a = iirnotch(f0, 30.0, SFREQ)
            x = filtfilt(b, a, x)
    except ValueError:
        pass
    return x


def _inband_std(x: np.ndarray) -> float:
    """RMS amplitude of the real EEG band (0.5-40 Hz), via Welch — the honest
    measure of contact. The raw µV are swamped by the Athena's DC baseline AND
    common-mode 60 Hz mains hum (both out-of-band, and both cancelled by the bipolar
    montage), which made a pristine electrode read as hundreds of µV of 'noise'.
    A time-domain filter can't do this on a 2 s window (edge artefacts inflate it
    ~50x); integrating the power spectrum over the band is robust and exact."""
    from scipy.signal import welch
    f, p = welch(x - x.mean(), fs=SFREQ, nperseg=min(len(x), 256))
    m = (f >= 0.5) & (f <= 40.0)
    return float(np.sqrt(np.trapezoid(p[m], f[m]))) if m.any() else float(np.std(x))


def quality(arr: np.ndarray) -> dict:
    """Per-channel contact verdict, judged on the in-band RMS (see _inband_std), not
    raw µV: the raw amplitude is dominated by mains hum / DC drift, which cried
    'noisy' at good electrodes and starved the sleep estimate of usable signal."""
    out = {}
    n = int(QUALITY_SEC * SFREQ)
    seg = arr[-n:] if len(arr) >= n else arr
    for i, name in enumerate(CHANNELS):
        if seg.size == 0:
            out[name] = {"verdict": "no data", "std": 0.0, "railed": 0.0}
            continue
        std = _inband_std(seg[:, i])
        if std < FLAT_STD_UV:
            verdict = "flat"
        elif std > 250:            # broadband saturation / movement storm
            verdict = "railed"
        elif std > 100:            # genuine in-band noise (poor contact, EMG)
            verdict = "noisy"
        else:
            verdict = "good"
        out[name] = {"verdict": verdict, "std": round(std, 1), "railed": 0.0}
    return out


def bandpower(arr: np.ndarray, ch_index: int) -> dict:
    """Relative band power over the last few seconds, for one channel."""
    from scipy.signal import welch
    from scipy.integrate import trapezoid

    n = int(BAND_SEC * SFREQ)
    if len(arr) < n // 2:
        return {name: 0.0 for name, _, _ in BANDS}
    x = arr[-n:, ch_index]
    x = x - np.median(x)               # drop the DC offset before the spectrum
    f, p = welch(x, fs=SFREQ, nperseg=min(len(x), 512))
    total = trapezoid(p[(f >= 0.5) & (f < 30)], f[(f >= 0.5) & (f < 30)])
    if total <= 0:
        return {name: 0.0 for name, _, _ in BANDS}
    out = {}
    for name, lo, hi in BANDS:
        sel = (f >= lo) & (f < hi)
        out[name] = round(float(100 * trapezoid(p[sel], f[sel]) / total), 1)
    return out


def _band_filter(x: np.ndarray, lo: float, hi: float, fs: float) -> np.ndarray:
    from scipy.signal import butter, filtfilt
    b, a = butter(2, [lo / (fs / 2), hi / (fs / 2)], btype="band")
    return filtfilt(b, a, x)


def optics_vitals(c: "Collector") -> dict:
    """Everything the optics burst physically carries, from ONE fused pulse signal.

    Heart rate, HRV, and respiration come from a clean blood-volume-pulse (BVP)
    built with OpenMuse's own multi-channel PPG fusion (ambient subtraction +
    per-channel signal-quality weighting) — far cleaner than any single channel.

    SpO2 needs both RED and IR light on the same side, which only the 16-channel
    (bright) optics mode provides. When those channels are absent we return
    spo2=None with a reason, never a fabricated percentage. When present we report
    the ratio-of-ratios R (uncalibrated): higher R => lower saturation, so a spike
    in R is a desaturation dip. It is an honest relative index, not a medical %.

    Cached ~1 s — the fusion + several Welch/peak passes are not free at frame rate.
    """
    now = time.time()
    if c._vit is not None and now - c._vit_at < 1.0:
        return c._vit
    out = {"hr": None, "hrv": None, "resp": None, "spo2": None,
           "sqi": None, "channels": [], "nch": 0, "live": False}
    with c.lock:
        cols = list(c.ppg_cols)
        stale = (now - c.ppg_at) > 5.0          # burst ended; optics draining out
        enough = len(c.ppg) >= int(PPG_FS * 6)
        arr = np.asarray(c.ppg, dtype=float) if (cols and enough and not stale) else None
    out["channels"] = cols
    out["nch"] = len(cols)
    if arr is None:
        c._vit, c._vit_at = out, now
        return out
    out["live"] = True

    import pandas as pd
    from scipy.signal import welch, find_peaks
    df = pd.DataFrame(arr, columns=cols)

    # --- Fused BVP (OpenMuse), with a strongest-channel fallback --------------
    bvp = None
    try:
        from OpenMuse import process
        bvp, info = process.preprocess_ppg(df, sampling_rate=int(PPG_FS))
        out["sqi"] = round(float(info.get("mean_sqi", 0.0)), 2)
    except Exception:
        bvp = None
    if bvp is None or not np.isfinite(bvp).any():
        best, best_pow = None, 0.0
        for col in cols:
            x = df[col].to_numpy()
            if not np.isfinite(x).all() or np.std(x) == 0:
                continue
            f, p = welch(x - x.mean(), fs=PPG_FS, nperseg=min(len(x), 512))
            b = (f >= 0.7) & (f <= 4.0)
            if b.any() and float(p[b].max()) > best_pow:
                best_pow, best = float(p[b].max()), x - x.mean()
        bvp = best
    if bvp is not None and np.isfinite(bvp).any():
        bvp = np.nan_to_num(bvp.astype(float))
        # Heart rate: dominant cardiac-band spectral peak (robust to noise).
        f, p = welch(bvp, fs=PPG_FS, nperseg=min(len(bvp), 512))
        b = (f >= 0.7) & (f <= 4.0)
        if b.any():
            out["hr"] = int(round(float(f[b][np.argmax(p[b])]) * 60.0))
        # HRV (RMSSD): scatter of successive beat-to-beat intervals.
        try:
            sig = (bvp - np.mean(bvp)) / (np.std(bvp) + 1e-9)
            pk, _ = find_peaks(sig, distance=int(PPG_FS * 0.4), prominence=0.5)
            if len(pk) >= 4:
                ibi = np.diff(pk) / PPG_FS * 1000.0             # ms
                ibi = ibi[(ibi > 300) & (ibi < 1500)]           # physiologic only
                if len(ibi) >= 3:
                    out["hrv"] = int(round(float(np.sqrt(np.mean(np.diff(ibi) ** 2)))))
        except Exception:
            pass
        # Respiration: the breathing rhythm modulates the pulse; find its 0.1-0.4 Hz
        # (6-24 breaths/min) peak in the BVP.
        try:
            fr, pr = welch(bvp, fs=PPG_FS, nperseg=min(len(bvp), 2048))
            rb = (fr >= 0.1) & (fr <= 0.4)
            if rb.any():
                out["resp"] = round(float(fr[rb][np.argmax(pr[rb])]) * 60.0, 1)
        except Exception:
            pass

    # --- SpO2 relative index: ratio-of-ratios (R) from RED vs IR --------------
    # R = (AC/DC)_red / (AC/DC)_ir, the pulse-oximetry ratio. Uncalibrated here:
    # without a reference oximeter it is a RELATIVE index, not a medical %. A side
    # is used only if BOTH its red and IR carry a real pulse — the Athena's inner
    # (LI/RI) sensors routinely saturate (IR pulsatility ~0), and dividing by that
    # near-zero is what makes R explode; gating to clean sides keeps R stable. The
    # outer (LO/RO) ring is usually the pulsatile one on the forehead.
    MIN_IR_PERF, MIN_RED_PERF = 0.008, 0.0015   # empirical floors (measured on band)

    def perfusion(col: str):
        """AC/DC perfusion index for one channel (cardiac-band RMS over the mean)."""
        x = df[col].to_numpy()
        if not np.isfinite(x).all() or len(x) < int(PPG_FS * 4):
            return None
        dc = float(np.mean(x))
        if abs(dc) < 1e-6:
            return None
        try:
            ac = float(np.std(_band_filter(x, 0.6, 4.0, PPG_FS)))
        except Exception:
            return None
        return ac / abs(dc)

    ratios = []
    for side in ("LO", "RO", "LI", "RI"):
        red, ir = f"OPTICS_{side}_RED", f"OPTICS_{side}_IR"
        if red in cols and ir in cols:
            pr, pi = perfusion(red), perfusion(ir)
            if pr and pi and pi >= MIN_IR_PERF and pr >= MIN_RED_PERF:
                ratios.append(pr / pi)
    if ratios:
        out["spo2"] = {"R": round(float(np.median(ratios)), 3), "sides": len(ratios)}
    elif any("_RED" in col for col in cols):
        out["spo2"] = {"reason": "red on, but no clean pulse yet — adjust the fit"}
    else:
        out["spo2"] = {"reason": "no red channel — needs a bright (16-ch) burst"}

    c._vit, c._vit_at = out, now
    return out


def movement(c: "Collector") -> dict:
    """Restlessness from the accelerometer: std of |acc| (g) over the window."""
    with c.lock:
        if len(c.acc) < 8:
            return {"level": None, "label": "—"}
        a = np.asarray(c.acc, dtype=float)
    s = float(np.std(a))
    label = "still" if s < 0.03 else ("slight" if s < 0.12 else "moving")
    return {"level": round(s, 3), "label": label}


def sleep_state(c: "Collector") -> dict:
    """Live Awake / Drowsy / Asleep estimate — see the SLEEP_* notes above.

    A slow-wave EEG ratio + EMG calm give a 0-1 sleepiness score; sustained
    movement forces awake. The score is smoothed over ~90 s and crosses hysteretic
    thresholds so it settles like real sleep rather than flickering. Cached ~2 s."""
    import math
    from scipy.signal import welch
    now = time.time()
    if c._sleep is not None and now - c._sleep_at < SLEEP_COMPUTE_SEC:
        return c._sleep
    out = {"state": "—", "score": None, "conf": 0, "deep": False,
           "depth": None, "stage": None,
           "asleep_min": None, "factors": {}, "reason": None}

    def done(**kw):
        out.update(kw); c._sleep, c._sleep_at = out, now; return out

    arr = c.snapshot()
    if len(arr) < int(6 * SFREQ):
        return done(reason="warming up")
    # Staging leans on the forehead sensors; if they've lost contact, say so
    # rather than guess.
    q = quality(arr)
    # Only bail outright when the forehead is truly dead (flat / no data). Railed or
    # noisy still flows through — the reliability check below decides per-sample
    # whether to trust it, so a brief railed patch is dropped, not treated as "awake".
    if q["AF7"]["verdict"] in ("flat", "no data") and q["AF8"]["verdict"] in ("flat", "no data"):
        return done(state="unknown", reason="forehead sensors have no signal")

    seg = arr[-int(SLEEP_WIN_SEC * SFREQ):]
    idx = {ch: i for i, ch in enumerate(CHANNELS)}
    # The LIVE estimate uses the forehead sum (AF7+AF8). The morning pipeline's
    # bipolar frontal-to-EAR is right for whole-night YASA (it normalises across the
    # night), but over an 8 s live window the ear subtraction cancels this wearer's
    # low-amplitude signal down to noise (slow-wave ratio collapses to ~0). The
    # forehead sum keeps real amplitude, and it's what the per-wearer calibration was
    # built on. Mains hum sits at 60 Hz — outside every band the ratio uses.
    sig = (seg[:, idx["AF7"]] + seg[:, idx["AF8"]]) / 2.0
    deriv = "frontal"
    sig = _notch_mains(sig - np.median(sig))    # strip the ~570µV mains first
    f, p = welch(sig, fs=SFREQ, nperseg=min(len(sig), 512))

    def band(lo, hi):
        m = (f >= lo) & (f < hi)
        return float(np.trapezoid(p[m], f[m])) if m.any() else 0.0
    d, th, al, be, emg = (band(.5, 4), band(4, 8), band(8, 12), band(16, 30), band(30, 50))
    tot = d + th + al + be + emg + 1e-9
    slow_ratio = (d + th) / (al + be + 1e-9)
    emg_frac = emg / tot
    delta_dom = d / tot
    cal = sleep_calib()          # kept only for an optional wearer name in the readout
    mv = movement(c)
    mlvl = mv.get("level")

    # Real thrashing = awake, whatever the electrodes say — the ONLY thing that pulls
    # someone out of sleep (a railed sensor must not).
    if mlvl is not None and mlvl > 0.12:
        c.sleep_hist.clear(); c.sleep_name = "awake"
        c.asleep_since = 0.0; c._sleep_cand = 0.0
        return done(state="awake", conf=65,
                    factors={"slow_ratio": round(slow_ratio, 1), "emg": round(emg_frac, 3),
                             "still": mv.get("label"), "deriv": deriv, "who": cal.get("name")})

    # Trust the sample only if the forehead is picking up BRAIN, not ambient mains or
    # muscle: a railed electrode or an artefact-dominated spectrum (emg_frac far above
    # any real stage's ~0.12) says nothing about sleep — drop it, don't average it in.
    front_std = (q["AF7"]["std"] + q["AF8"]["std"]) / 2.0
    reliable = front_std < 150.0 and emg_frac < 0.35
    if reliable:
        c.sleep_hist.append((now, slow_ratio, emg_frac, delta_dom))

    recent = [r for r in c.sleep_hist if now - r[0] <= SLEEP_SMOOTH_SEC]
    if len(recent) < SLEEP_MIN_SAMPLES:
        # Not enough clean EEG to trust a stage — one glitchy window is not deep
        # sleep. Fall back to actigraphy, like a wrist tracker: motionless in bed =>
        # asleep, low-confidence and motion-based.
        s_still = 1.0 if (mlvl is not None and mlvl < 0.03) else \
                  (0.4 if (mlvl is not None and mlvl < 0.12) else 0.5)
        held = "asleep" if s_still >= 1.0 else ("light" if s_still >= 0.4 else "awake")
        if held in ("asleep", "light"):
            if c.asleep_since == 0.0:
                if c._sleep_cand == 0.0:
                    c._sleep_cand = now
                if now - c._sleep_cand >= SLEEP_ONSET_SEC:
                    c.asleep_since = c._sleep_cand
        else:
            c._sleep_cand = 0.0; c.asleep_since = 0.0
        c.sleep_name = held
        asleep_min = round((now - c.asleep_since) / 60.0) if c.asleep_since > 0 else None
        return done(state=held, conf=25, asleep_min=asleep_min,
                    depth=(20 if held != "awake" else None),
                    reason="EEG buried in muscle/noise — going by stillness",
                    factors={"slow_ratio": None, "emg": None,
                             "still": mv.get("label"), "deriv": deriv, "who": cal.get("name")})

    # Smoothed features (~90 s; stages last minutes, single 8 s windows are noisy),
    # classified with the UNIVERSAL thresholds derived from real YASA-staged nights:
    #   slow-wave content sets depth (N3 deep vs N2 light); among low-slow-wave epochs,
    #   muscle atonia (low EMG) marks REM vs wake. Deep is reliable (~78% vs YASA);
    #   REM is a LOW-confidence flag only (~29% live — it overlaps wake/light on a
    #   forehead sensor); the morning YASA pass owns the real staging.
    slow_s = float(np.median([r[1] for r in recent]))
    emg_s = float(np.median([r[2] for r in recent]))
    if slow_s >= 12.0:
        state = "deep"
    elif slow_s >= 4.0:
        state = "light"
    elif emg_s < 0.10:
        state = "rem"                     # low slow-wave + atonia -> possible dreaming
    else:
        state = "awake"
    c.sleep_name = state

    if state in ("light", "deep", "rem"):
        if c.asleep_since == 0.0:
            if c._sleep_cand == 0.0:
                c._sleep_cand = now
            if now - c._sleep_cand >= SLEEP_ONSET_SEC:
                c.asleep_since = c._sleep_cand
    else:
        c._sleep_cand = 0.0; c.asleep_since = 0.0
    asleep_min = round((now - c.asleep_since) / 60.0) if c.asleep_since > 0 else None

    depth = int(max(0, min(100, (math.log10(max(slow_s, 1e-3)) - math.log10(2.0)) /
                           (math.log10(20.0) - math.log10(2.0)) * 100)))
    conf = {"deep": 80, "awake": 65, "light": 60, "rem": 35}.get(state, 40)
    return done(state=state, conf=conf, deep=(state == "deep"),
                depth=depth, stage=state, asleep_min=asleep_min,
                factors={"slow_ratio": round(slow_ratio, 1), "emg": round(emg_frac, 3),
                         "slow_smooth": round(slow_s, 1),
                         "still": mv.get("label"), "deriv": deriv, "who": cal.get("name")})


_MAINS = {"val": {"hz": None, "pct": 0.0}, "at": 0.0}


def mains(arr: np.ndarray) -> dict:
    """Mains (power-line) hum: the worst channel's RMS amplitude (µV) at 50 or
    60 Hz. Reported as µV, not a fraction — the fraction saturates near 100% (the
    hum dwarfs the tiny EEG), but the amplitude drops as contact/grounding
    improves, so it's an actionable "seat the band to minimise this" meter.
    (Idea borrowed from MuseScope.) Cached ~1s. Returns {hz, uv}."""
    now = time.time()
    if now - _MAINS["at"] < 1.0:
        return _MAINS["val"]
    from scipy.signal import welch
    n = int(4 * SFREQ)
    if len(arr) < n // 2:
        _MAINS["val"], _MAINS["at"] = {"hz": None, "uv": 0.0}, now
        return _MAINS["val"]
    worst, whz = 0.0, 60
    for i in range(arr.shape[1]):
        x = arr[-n:, i]
        x = x - np.median(x)
        f, p = welch(x, fs=SFREQ, nperseg=min(len(x), 512))
        for hz in (50, 60):
            band = (f >= hz - 1.5) & (f <= hz + 1.5)
            if not band.any():
                continue
            rms = float(np.trapezoid(p[band], f[band])) ** 0.5   # µV RMS in the notch
            if rms > worst:
                worst, whz = rms, hz
    _MAINS["val"] = {"hz": whz, "uv": round(worst, 1)}
    _MAINS["at"] = now
    return _MAINS["val"]


def current_segment() -> dict:
    """What the recorder is writing right now — the growing raw .txt if a segment
    is live, else the most recent decoded CSV."""
    raws = glob.glob(os.path.join(RAWDIR, "*.txt"))
    f = max(raws, key=os.path.getmtime) if raws else None
    if not f:
        csvs = glob.glob(os.path.join(RECDIR, "*.csv"))
        f = max(csvs, key=os.path.getmtime) if csvs else None
    if not f:
        return {"name": None, "mb": 0.0, "age": None}
    return {"name": os.path.basename(f),
            "mb": round(os.path.getsize(f) / 1e6, 1),
            "age": round(time.time() - os.path.getmtime(f), 1)}


COLLECTOR = Collector()

try:
    PAGE_VERSION = str(int(os.path.getmtime(__file__)))
except OSError:
    PAGE_VERSION = "0"


def frame() -> dict:
    arr = COLLECTOR.snapshot()
    rate = COLLECTOR.data_rate()
    vit = optics_vitals(COLLECTOR)
    # "connected" = data actually arriving (COLLECTOR.connected is the 6 s
    # staleness flag). Do NOT also gate on rate>50: between OpenMuse's bursty file
    # flushes the instantaneous rate briefly reads 0 on a perfectly live link, and
    # gating on it is exactly what made the page flap connected/disconnected.
    live = COLLECTOR.connected
    traces = {}
    if len(arr):
        raw_tail = arr[-int(BUFFER_SEC * SFREQ):]
        # Anti-alias BEFORE decimating. Decimating 256->51 Hz with no filter folded the
        # ~570µV of 60 Hz mains down to ~8.7 Hz, drawing a fake clean "alpha" wave that
        # looked like perfect EEG. Low-pass below the display's Nyquist first so the
        # trace shows the real sub-25 Hz brain signal, not aliased hum.
        try:
            from scipy.signal import butter, filtfilt
            bl, al_ = butter(4, 20.0 / (SFREQ / 2), btype="low")
            filt = filtfilt(bl, al_, raw_tail, axis=0)
        except Exception:
            filt = raw_tail
        tail = filt[::DECIMATE]
        for i, name in enumerate(CHANNELS):
            traces[name] = [round(float(v), 1) for v in tail[:, i]]
    return {
        "connected": live,
        "rate": round(rate, 1),
        "uptime": round(time.time() - COLLECTOR.started),
        "quality": quality(arr),
        # Band power for ALL four sensors, not just AF7.
        "bands": {ch: bandpower(arr, i) for i, ch in enumerate(CHANNELS)},
        "segment": current_segment(),
        "battery": COLLECTOR.battery,
        "pulse": vit["hr"],
        "vitals": vit,
        "sleep": sleep_state(COLLECTOR),
        "movement": movement(COLLECTOR),
        "mains": mains(arr),
        "traces": traces,
        "display_hz": round(SFREQ / DECIMATE, 1),
        # Changes whenever this file is redeployed; the page reloads itself when it
        # sees a version different from the one it loaded with, so a cached/stale
        # tab (mobile Chrome keeps them alive over SSE) can't keep rendering old
        # code after a deploy — the exact trap that hid every waveform fix.
        "v": PAGE_VERSION,
    }


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Muse · live</title>
<style>
:root{--bg:#0f1216;--panel:#161a20;--line:#252b34;--fg:#e6e8eb;--muted:#98a2b3;
      --good:#4ade80;--warn:#fbbf24;--bad:#f87171;--accent:#5598e7}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:.8rem 1rem;border-bottom:1px solid var(--line);background:var(--panel);
  display:flex;gap:1rem;align-items:center;flex-wrap:wrap;position:sticky;top:0}
h1{font-size:.9rem;margin:0;letter-spacing:.04em}
main{padding:1rem;max-width:1100px;margin:0 auto}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:.9rem 1rem;margin-bottom:1rem}
.panel h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;
  color:var(--muted);margin:0 0 .7rem;font-weight:600}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:.4rem}
.live{background:var(--good);box-shadow:0 0 8px var(--good)}
.dead{background:var(--bad)}
.stat{color:var(--muted);font-size:.82rem;font-variant-numeric:tabular-nums}
.chgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.6rem}
.ch{background:#1b2027;border:1px solid var(--line);border-radius:7px;padding:.55rem .7rem}
.ch .n{font-family:ui-monospace,monospace;font-size:.8rem;color:var(--muted)}
.ch .v{font-size:1.05rem;font-weight:600;margin:.1rem 0}
.ch .m{font-size:.74rem;color:var(--muted);font-variant-numeric:tabular-nums}
.good{color:var(--good)}.railed{color:var(--warn)}.flat{color:var(--muted)}
.batt{padding:.15rem .5rem;border-radius:99px;border:1px solid var(--line)}
.batt.ok b{color:var(--good)} .batt.low b{color:var(--warn)} .batt.crit b{color:var(--bad)}
.battwarn{background:rgba(248,113,113,.12);border:1px solid var(--bad);color:var(--bad);
  border-radius:8px;padding:.6rem .8rem;margin-bottom:1rem;font-size:.86rem;display:none}
.battwarn.show{display:block}
.noisy{color:var(--warn)}.nodata{color:var(--bad)}
/* A canvas is a replaced element: some mobile browsers ignore its CSS height and
   size it from the height attribute (the backing buffer), giving a too-tall box
   with the trace stranded at the top. Wrapping it in a plain div (which always
   honours CSS height) and letting the canvas fill that div fixes it for good. */
.wave{width:100%;height:72px}
canvas{width:100%;height:100%;display:block;background:#12151a;border-radius:6px}
.wrap{margin-bottom:.5rem}
.wrap .lbl{font-family:ui-monospace,monospace;font-size:.72rem;color:var(--muted);
  margin-bottom:.15rem}
.vitals{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1rem}
.vital{background:#1b2027;border:1px solid var(--line);border-radius:7px;padding:.7rem .8rem}
.vlbl{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em}
.vval{font-size:1.8rem;font-weight:600;margin:.1rem 0;font-variant-numeric:tabular-nums}
.vval .vunit{font-size:.9rem;font-weight:400;color:var(--muted)}
.vsub{font-size:.74rem;color:var(--muted);font-variant-numeric:tabular-nums}
.bandgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:1rem}
.bandcell .lbl{font-family:ui-monospace,monospace;font-size:.72rem;color:var(--muted);
  margin-bottom:.35rem}
.bars{display:grid;grid-template-columns:repeat(5,1fr);gap:.4rem;align-items:end;height:80px}
.bar{background:var(--accent);border-radius:3px 3px 0 0;min-height:2px;transition:height .2s}
.blbl{display:grid;grid-template-columns:repeat(5,1fr);gap:.4rem;margin-top:.3rem;
  font-size:.66rem;color:var(--muted);text-align:center}

.zoom{float:right;font-size:.72rem;color:var(--muted);text-transform:none;letter-spacing:0;
  display:inline-flex;align-items:center;gap:.3rem}
.zoom .zg{color:var(--muted)} .zoom .zg b{color:var(--fg);font-variant-numeric:tabular-nums}
.zoom button{background:var(--panel2,#1b2027);color:var(--fg);border:1px solid var(--line);
  border-radius:5px;width:26px;height:26px;font-size:1rem;line-height:1;cursor:pointer;padding:0}
.zoom button:active{background:var(--accent);color:#0b1016}
.contactwrap{display:flex;gap:1rem;align-items:center;flex-wrap:wrap}
.head{width:150px;height:158px;flex:0 0 auto}
.head .el{fill:#5c6673;stroke:#0f1216;stroke-width:2;transition:fill .3s}
.head .el.good{fill:var(--good)} .head .el.noisy,.head .el.railed{fill:var(--warn)}
.head .el.flat,.head .el.nodata{fill:#5c6673}
.head .ell{fill:#0b1016;font:600 11px ui-monospace,monospace;text-anchor:middle}
.contactwrap #q{flex:1 1 200px}
.sighealth{display:flex;gap:1.2rem;margin-top:.7rem;font-size:.82rem;color:var(--muted)}
.sighealth b{font-variant-numeric:tabular-nums} .sighealth b.warn{color:var(--warn)}
.sighealth b.good{color:var(--good)}
.notepanel form{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
.notepanel input{flex:1 1 220px;background:var(--panel2,#1b2027);color:var(--fg);
  border:1px solid var(--line);border-radius:6px;padding:.55rem .65rem;font-size:.92rem}
.notepanel input:focus{outline:none;border-color:var(--accent)}
.notepanel button{background:var(--accent);color:#0b1016;border:none;border-radius:6px;
  padding:.55rem 1.1rem;font-size:.9rem;font-weight:600;cursor:pointer}
.notepanel button:disabled{opacity:.5}
.lightbtn{background:var(--panel2,#1b2027);color:var(--fg);border:1px solid var(--accent);
  border-radius:7px;padding:.55rem 1rem;font-size:.9rem;font-weight:600;cursor:pointer}
.lightbtn:hover{background:var(--accent);color:#0b1016}
.lightbtn:disabled{opacity:.5;cursor:default}
.sleepbadge{font-weight:600}
.sleeppanel .sleeprow{display:flex;gap:1rem;align-items:center;flex-wrap:wrap}
.sleepbig{font-size:1.7rem;font-weight:700;letter-spacing:.01em;min-width:9rem}
.sleepcol{flex:1 1 220px;min-width:180px}
.sleepmeter{height:12px;border-radius:99px;background:#12151a;border:1px solid var(--line);overflow:hidden}
.sleepfill{height:100%;width:0;border-radius:99px;transition:width .6s ease,background .6s ease}
.barlabel{font-size:.72rem;color:var(--muted);margin:.35rem 0 .2rem;display:flex;justify-content:space-between}
.barval{font-variant-numeric:tabular-nums}
.sleepfactors{margin-top:.55rem;font-size:.78rem;color:var(--muted);font-variant-numeric:tabular-nums}

/* Phone layout: tighter everything, bigger key numbers, contact 2-up, band 1-up */
@media(max-width:640px){
  header{padding:.6rem .8rem;gap:.5rem .7rem}
  h1{font-size:.8rem;width:100%}
  .stat{font-size:.9rem}
  main{padding:.6rem}
  .panel{padding:.7rem .75rem;margin-bottom:.7rem}
  .panel h2{font-size:.68rem;margin-bottom:.55rem}
  .wrap{margin-bottom:.35rem}
  .chgrid{grid-template-columns:1fr 1fr;gap:.5rem}
  .ch .v{font-size:1rem}
  .vitals{grid-template-columns:1fr 1fr}
  .vval{font-size:1.5rem}
  .bandgrid{grid-template-columns:1fr 1fr;gap:.7rem}
  .bars{height:56px}
}
</style></head><body>
<header>
  <h1>MUSE · LIVE</h1>
  <span id="sleepbadge" class="stat sleepbadge"></span>
  <span id="conn" class="stat"><span class="dot dead"></span>connecting…</span>
  <span id="batt" class="stat batt">🔋 <b>—</b></span>
  <span id="pulse" class="stat">❤️ <b>—</b></span>
  <span id="rate" class="stat"></span>
  <span id="seg" class="stat"></span>
</header>
<main>
  <div id="battwarn" class="battwarn"></div>
  <div class="panel sleeppanel">
    <h2>Sleep state <span class="stat" style="text-transform:none">live estimate — not clinical staging</span></h2>
    <div class="sleeprow">
      <div id="sleepbig" class="sleepbig">—</div>
      <div class="sleepcol">
        <div class="barlabel">😴 Sleep <span id="eeglvl" class="barval"></span></div>
        <div class="sleepmeter"><div id="sleepfill" class="sleepfill"></div></div>
        <div class="barlabel">🛌 Stillness <span id="stilllvl" class="barval"></span></div>
        <div class="sleepmeter"><div id="stillfill" class="sleepfill"></div></div>
        <div class="barlabel">🌊 Depth · light→deep <span id="depthlvl" class="barval"></span></div>
        <div class="sleepmeter"><div id="depthfill" class="sleepfill"></div></div>
        <div id="sleepsince" class="vsub"></div>
      </div>
    </div>
    <div id="sleepfactors" class="sleepfactors"></div>
    <div class="note">Awake / Drowsy / Asleep from the EEG slow-wave ratio, muscle tone and
      stillness — a live guess (~0.8 agreement with the morning analysis on recorded nights).
      The real hypnogram still comes from the overnight YASA pass; this is just so you can see,
      in the moment, whether someone's drifting off.</div>
  </div>
  <div class="panel notepanel">
    <form id="notef" onsubmit="return false">
      <input id="notetext" type="text" maxlength="200" autocomplete="off"
             placeholder="Log an event — coffee, weed, workout, screen time…">
      <button id="notebtn" type="submit">Add note</button>
      <span id="notemsg" class="stat"></span>
    </form>
    <div class="note" style="margin-top:.35rem">Stamped with the current time and
      filed under tonight — shows up on the night's hypnogram once it processes.</div>
  </div>
  <div class="panel"><h2>Vitals <span class="stat" style="text-transform:none">from the optics burst (fNIRS/PPG) + IMU</span></h2>
    <div class="vitals">
      <div class="vital"><div class="vlbl">❤️ Heart rate</div>
        <div class="vval"><span id="hr">—</span><span class="vunit"> bpm</span></div></div>
      <div class="vital"><div class="vlbl">〰️ HRV (RMSSD)</div>
        <div class="vval"><span id="hrv">—</span><span class="vunit"> ms</span></div></div>
      <div class="vital"><div class="vlbl">🫁 O₂ index</div>
        <div class="vval"><span id="spo2">—</span></div>
        <div class="vsub" id="spo2sub">relative · uncalibrated</div></div>
      <div class="vital"><div class="vlbl">🌬️ Respiration</div>
        <div class="vval"><span id="resp">—</span><span class="vunit"> /min</span></div></div>
      <div class="vital"><div class="vlbl">🤸 Movement</div>
        <div class="vval"><span id="mv">—</span></div>
        <div class="vsub" id="mvsub"></div></div>
    </div>
    <div class="sighealth" style="margin-top:.6rem">
      <span>pulse quality <b id="sqi">—</b></span>
      <span>optics channels <b id="optch">—</b></span>
    </div>
    <div style="margin-top:.8rem;display:flex;gap:.5rem;flex-wrap:wrap;align-items:center">
      <button id="opticsbtn" class="lightbtn" type="button">🔦 Quick pulse · HR · HRV · breathing</button>
      <button id="deepbtn" class="lightbtn" type="button">🔴 Deep scan · adds O₂ index · more battery</button>
      <span id="opticsmsg" class="stat"></span>
    </div>
    <div class="note" style="margin-top:.35rem">EEG-only keeps the LEDs off to save
      battery, so vitals read “—”. Tap to run a 5-minute optics burst on the next
      segment — the LEDs come on, heart rate is captured to the night, then it drops
      straight back to EEG-only. Heart rate, HRV and respiration come from the pulse;
      the <b>O₂ index needs the red LED</b> (a bright 16-channel burst) and is a
      relative trend, never a medical SpO₂ percentage.</div>
  </div>
  <div class="panel"><h2>Electrode contact</h2>
    <div class="contactwrap">
      <svg class="head" viewBox="0 0 200 210" aria-label="electrode contact map">
        <polygon points="100,6 90,26 110,26" fill="#2a313c"/>
        <ellipse cx="18" cy="112" rx="10" ry="18" fill="none" stroke="#2a313c" stroke-width="3"/>
        <ellipse cx="182" cy="112" rx="10" ry="18" fill="none" stroke="#2a313c" stroke-width="3"/>
        <circle cx="100" cy="112" r="80" fill="#12151a" stroke="#2a313c" stroke-width="3"/>
        <circle id="el_AF7" class="el" cx="66" cy="64" r="15"/><text class="ell" x="66" y="68">AF7</text>
        <circle id="el_AF8" class="el" cx="134" cy="64" r="15"/><text class="ell" x="134" y="68">AF8</text>
        <circle id="el_TP9" class="el" cx="40" cy="140" r="15"/><text class="ell" x="40" y="144">TP9</text>
        <circle id="el_TP10" class="el" cx="160" cy="140" r="15"/><text class="ell" x="160" y="144">TP10</text>
      </svg>
      <div id="q" class="chgrid"></div>
    </div>
    <div class="sighealth">
      <span>reception <b id="recv">—</b></span>
      <span>mains hum <b id="mains">—</b></span>
    </div>
  </div>
  <div class="panel"><h2>Live signal <span id="hz" class="stat"></span>
    <span class="zoom">
      <span class="zg">amp <b id="zamp">1.0×</b></span>
      <button data-z="amp-">–</button><button data-z="amp+">+</button>
      <span class="zg">span <b id="zspan">12s</b></span>
      <button data-z="time-">–</button><button data-z="time+">+</button>
    </span></h2><div id="waves"></div></div>
  <div class="panel"><h2>Band power · all sensors</h2><div id="bandgrid" class="bandgrid"></div></div>
</main>
<script>
const CH=["TP9","AF7","AF8","TP10"], canv={};
const LBL={TP9:'left ear',AF7:'left forehead',AF8:'right forehead',TP10:'right ear'};
const BANDS=["Delta","Theta","Alpha","Sigma","Beta"];
let spo2R=[];   // recent ratio-of-ratios, to anchor the relative O₂ index
function median(a){if(!a.length)return 0;const b=[...a].sort((x,y)=>x-y);
  const m=b.length>>1;return b.length%2?b[m]:(b[m-1]+b[m])/2;}
const wraps=document.getElementById('waves');
CH.forEach(c=>{const d=document.createElement('div');d.className='wrap';
  d.innerHTML='<div class="lbl">'+c+' <span style="opacity:.6">· '+LBL[c]+'</span></div>';
  const wv=document.createElement('div');wv.className='wave';
  const cv=document.createElement('canvas');wv.appendChild(cv);d.appendChild(wv);
  wraps.appendChild(d);canv[c]=cv;});

// One band-power panel per sensor.
const bg=document.getElementById('bandgrid');
CH.forEach(c=>{const d=document.createElement('div');d.className='bandcell';
  d.innerHTML='<div class="lbl">'+c+' <span style="opacity:.6">· '+LBL[c]+'</span></div>'+
    '<div class="bars">'+BANDS.map((b,i)=>'<div class="bar" id="bar_'+c+'_'+i+'"></div>').join('')+'</div>'+
    '<div class="blbl">'+BANDS.map(b=>'<div>'+b+'</div>').join('')+'</div>';
  bg.appendChild(d);});

// Zoom: amp = vertical gain, win = seconds of the 12 s buffer shown (fewer = zoom
// in on time). Persisted per browser.
const ZOOM={amp:1,win:12};
function zLoad(){try{const z=JSON.parse(localStorage.getItem('zoom')||'{}');
  if(z.amp)ZOOM.amp=z.amp; if(z.win)ZOOM.win=z.win;}catch(e){}}
function zShow(){document.getElementById('zamp').textContent=ZOOM.amp.toFixed(1)+'×';
  document.getElementById('zspan').textContent=ZOOM.win+'s';}
function zSave(){try{localStorage.setItem('zoom',JSON.stringify(ZOOM));}catch(e){}}
zLoad();zShow();
document.querySelectorAll('.zoom button').forEach(b=>b.onclick=()=>{
  const z=b.dataset.z;
  if(z==='amp+')ZOOM.amp=Math.min(8,ZOOM.amp*1.5);
  if(z==='amp-')ZOOM.amp=Math.max(0.25,ZOOM.amp/1.5);
  if(z==='time+')ZOOM.win=Math.min(12,ZOOM.win+2);   // + = more seconds (zoom out)
  if(z==='time-')ZOOM.win=Math.max(2,ZOOM.win-2);    // – = fewer seconds (zoom in)
  ZOOM.amp=Math.round(ZOOM.amp*100)/100;zShow();zSave();
});

function draw(cv,data){
  // The canvas now fills its .wave wrapper (a div with a real CSS height), so its
  // clientWidth/Height are stable and the trace fills the whole box — no more
  // stranded-at-the-top traces or runaway growth.
  const dpr=window.devicePixelRatio||1;
  const w=cv.clientWidth||300, h=cv.clientHeight||72;
  if(cv.width!==Math.round(w*dpr)||cv.height!==Math.round(h*dpr)){
    cv.width=Math.round(w*dpr);cv.height=Math.round(h*dpr);}
  const x=cv.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);
  x.clearRect(0,0,w,h);
  if(!data||!data.length)return;
  // Time zoom: show only the last ZOOM.win seconds of the 12 s buffer.
  if(ZOOM.win<12){const k=Math.max(2,Math.round(data.length*ZOOM.win/12));data=data.slice(-k);}
  // Muse EEG rides a big DC offset (~700 µV); subtract each channel's mean so the
  // trace is centred. Scale by the TYPICAL excursion (mean absolute deviation),
  // not the max — the max makes one artifact spike squash the whole trace. ~3.2x
  // MAD ≈ a normal peak; a floor keeps a quiet channel calm. ZOOM.amp is manual
  // gain on top; the canvas clips anything the gain pushes past the box edge.
  let mean=0;for(const v of data)mean+=v;mean/=data.length;
  let mad=0;for(const v of data)mad+=Math.abs(v-mean);mad/=data.length;
  const scale=Math.max(mad*3.2,12);
  const amp=h*0.44*ZOOM.amp;
  x.strokeStyle='#20262f';x.lineWidth=1;x.beginPath();x.moveTo(0,h/2);x.lineTo(w,h/2);x.stroke();
  x.strokeStyle='#5598e7';x.lineWidth=1.1;x.lineJoin='round';x.beginPath();
  for(let i=0;i<data.length;i++){
    const px=i/(data.length-1)*w, py=h/2-((data[i]-mean)/scale)*amp;
    i?x.lineTo(px,py):x.moveTo(px,py);
  }
  x.stroke();
}

// Lifestyle note field: POST to /note, which appends to the annotations file that
// rides the recordings share to the server.
const nbtn=document.getElementById('notebtn'), ntext=document.getElementById('notetext'),
      nmsg=document.getElementById('notemsg');
function sendNote(){
  const v=ntext.value.trim(); if(!v)return;
  nbtn.disabled=true;
  fetch('/note',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:'text='+encodeURIComponent(v)})
   .then(r=>r.json()).then(d=>{
     if(d.ok){nmsg.textContent='✓ noted '+d.entry.ts_local.slice(11,16);nmsg.style.color='var(--good)';ntext.value='';}
     else{nmsg.textContent='could not save';nmsg.style.color='var(--bad)';}
     setTimeout(()=>{nmsg.textContent='';},4000);
   }).catch(()=>{nmsg.textContent='error';nmsg.style.color='var(--bad)';})
   .finally(()=>{nbtn.disabled=false;});
}
document.getElementById('notef').addEventListener('submit',sendNote);

// Optics "light stab": ask the recorder for one 5-min burst of live vitals.
// quick = dim preset (HR/HRV/respiration); deep = bright 16-ch (adds the O₂ index).
const obtn=document.getElementById('opticsbtn'), dbtn=document.getElementById('deepbtn'),
      omsg=document.getElementById('opticsmsg');
function requestBurst(mode){
  obtn.disabled=dbtn.disabled=true;
  fetch('/optics?mode='+mode,{method:'POST'}).then(r=>r.json()).then(d=>{
    omsg.textContent=d.ok?('✓ '+(d.mode==='deep'?'deep scan':'quick pulse')+
      ' queued — the light comes on within a few seconds'):'failed';
    omsg.style.color=d.ok?'var(--good)':'var(--bad)';
  }).catch(()=>{omsg.textContent='error';omsg.style.color='var(--bad)';})
   .finally(()=>{setTimeout(()=>{omsg.textContent='';obtn.disabled=dbtn.disabled=false;},7000);});
}
obtn.onclick=()=>requestBurst('quick');
dbtn.onclick=()=>requestBurst('deep');

let PAGE_V=null;
const es=new EventSource('/stream');
es.onmessage=e=>{
  const d=JSON.parse(e.data);
  // Self-update: if the server was redeployed since this page loaded, reload to
  // pick up the new code instead of rendering stale cached JS forever.
  if(PAGE_V===null){PAGE_V=d.v;}
  else if(d.v && d.v!==PAGE_V){location.reload();return;}
  document.getElementById('conn').innerHTML=
    '<span class="dot '+(d.connected?'live':'dead')+'"></span>'+
    (d.connected?'streaming':'no data');
  document.getElementById('rate').textContent=d.rate.toFixed(0)+' Hz';
  document.getElementById('hz').textContent='· '+d.display_hz+' Hz shown';
  // Live sleep-state estimate.
  const SL=d.sleep||{};
  const SMAP={awake:['👁 Awake','var(--warn)'],light:['🌙 Light sleep','var(--accent)'],
    deep:['💤 Deep sleep','var(--good)'],rem:['🌀 REM? · dreaming','#b98cff'],
    asleep:['😴 Asleep','var(--good)'],drowsy:['😌 Drowsy','var(--accent)'],
    unknown:['🤔 Can’t tell','var(--muted)']};
  let sk=SL.state, sinfo=SMAP[sk]||['—','var(--muted)'];
  let stxt=sinfo[0], scol=sinfo[1];
  const sb=document.getElementById('sleepbig');
  sb.textContent=stxt; sb.style.color=scol;
  document.getElementById('sleepbadge').textContent=(d.connected&&SMAP[sk])?sinfo[0]:'';
  document.getElementById('sleepbadge').style.color=scol;
  // Bar 1 — the sleep status. Fills from the stage (awake→deep is the depth axis).
  const sf=document.getElementById('sleepfill');
  const stateFill={awake:12,drowsy:45,rem:55,light:60,asleep:70,deep:92,unknown:8};
  sf.style.width=(stateFill[sk]||0)+'%'; sf.style.background=scol;
  document.getElementById('eeglvl').textContent=
    {awake:'Awake',light:'Light',deep:'Deep',rem:'REM?',asleep:'Asleep',drowsy:'Drowsy',unknown:'—'}[sk]||'—';
  // Bar 2 — stillness from the accelerometer (independent of the EEG).
  const mvv=d.movement||{}, lvl=mvv.level;
  const still=(lvl==null)?0:Math.max(0,Math.min(100,Math.round(100*(1-lvl/0.15))));
  const stf=document.getElementById('stillfill');
  stf.style.width=still+'%';
  stf.style.background=(still>=80?'var(--good)':(still>=40?'var(--accent)':'var(--warn)'));
  document.getElementById('stilllvl').textContent=(mvv.label||'—')+(lvl!=null?' · '+still+'%':'');
  // Bar 3 — sleep depth (light->deep) from delta-wave dominance. Only while asleep;
  // REM is not detectable on a forehead sensor, so this is depth only.
  const dpf=document.getElementById('depthfill');
  const dep=(SL.depth!=null)?SL.depth:0;
  dpf.style.width=dep+'%'; dpf.style.background='var(--accent)';
  document.getElementById('depthlvl').textContent=(SL.depth!=null)?
    ((SL.depth>=70?'deep':(SL.depth>=35?'light':'shallow'))+' · '+dep+'%'):'—';
  const ssince=document.getElementById('sleepsince');
  if(SL.asleep_min!=null){const h=Math.floor(SL.asleep_min/60),m=SL.asleep_min%60;
    ssince.textContent='asleep for '+(h?h+'h ':'')+m+'m';}
  else if(SL.reason){ssince.textContent=SL.reason;} else {ssince.textContent='';}
  const F=SL.factors||{};
  document.getElementById('sleepfactors').textContent = SL.score!=null
    ? ('score '+SL.score+'/100 · confidence '+SL.conf+'%  ·  slow-wave '+F.slow_ratio
       +' · muscle '+F.emg+' · '+(F.still||''))
    : ('confidence '+(SL.conf!=null?SL.conf:0)+'% · '+(F.still||'')+' · '+(F.who||''));
  document.getElementById('seg').textContent=
    d.segment.name?(d.segment.name+' · '+d.segment.mb+' MB'):'no segment';
  const be=document.getElementById('batt'), bw=document.getElementById('battwarn');
  if(d.battery==null){be.className='stat batt';be.querySelector('b').textContent='—';
    bw.className='battwarn';}
  else{
    const p=Math.round(d.battery);
    const cls=p<15?'crit':(p<30?'low':'ok');
    be.className='stat batt '+cls;be.querySelector('b').textContent=p+'%';
    // Below 15% = red box, ALWAYS (awake or asleep). At this level the headband is
    // about to die and end the recording — that's worth seeing mid-night, unlike a
    // soft "top it off" nudge which only made sense before bed.
    if(p<15){bw.className='battwarn show';
      bw.textContent='🔴 Battery critically low ('+p+'%) — the headband is about to '+
        'die and the recording will stop. Plug it in if you want the rest of the night.';}
    else{bw.className='battwarn';}
  }
  // Pulse (from PPG/optics) + movement (from the accelerometer).
  const pb=document.getElementById('pulse').querySelector('b');
  pb.textContent = d.pulse!=null ? d.pulse+' bpm' : '—';
  const V=d.vitals||{};
  document.getElementById('hr').textContent   = V.hr!=null   ? V.hr   : '—';
  document.getElementById('hrv').textContent  = V.hrv!=null  ? V.hrv  : '—';
  document.getElementById('resp').textContent = V.resp!=null ? V.resp : '—';
  // O₂ index: relative ratio-of-ratios R (higher R = lower saturation, so a rising
  // number is a desat dip). No fake %; the label already says "relative".
  const sp=document.getElementById('spo2'), sps=document.getElementById('spo2sub');
  if(V.spo2 && V.spo2.R!=null){
    spo2R.push(V.spo2.R); if(spo2R.length>120) spo2R.shift();
    // Self-anchored relative index: 100 = this session's baseline (median of the
    // first stable readings). Higher R = lower O₂, so the index moves DOWN on a
    // desaturation. Uncalibrated — a relative trend, never a medical percentage.
    const base=median(spo2R.slice(0,Math.min(20,spo2R.length)));
    const idx=base>0?Math.round(100*base/V.spo2.R):100;
    sp.textContent=idx;
    const d=idx-100;
    sps.textContent='relative · 100=baseline · R='+V.spo2.R+' · '+V.spo2.sides+
      (V.spo2.sides===1?' side':' sides')+
      (Math.abs(d)>=2?(d<0?' · ▼'+(-d)+' dip':' · ▲'+d):'')+' · not medical';
  } else { spo2R=[]; sp.textContent='—';
    sps.textContent=(V.spo2&&V.spo2.reason)?V.spo2.reason:'relative · uncalibrated'; }
  // Pulse signal quality + which optics channels the live burst actually carries.
  const sqi=document.getElementById('sqi');
  sqi.textContent = V.sqi!=null ? V.sqi : '—';
  sqi.className = V.sqi!=null ? (V.sqi>=0.6?'good':(V.sqi>=0.3?'':'warn')) : '';
  document.getElementById('optch').textContent =
    V.live && V.nch ? (V.nch+'-ch'+(V.channels&&V.channels.some(x=>x.includes('_RED'))?' (red ✓)':' (no red)')) : 'off';
  const mv=d.movement||{level:null,label:'—'};
  document.getElementById('mv').textContent = mv.label;
  document.getElementById('mvsub').textContent = mv.level!=null ? ('σ '+mv.level+' g') : '';
  document.getElementById('q').innerHTML=CH.map(c=>{
    const q=d.quality[c]||{verdict:'no data',std:0,railed:0};
    const cls=q.verdict.replace(' ','');
    // Colour the head-map electrode to match this channel's contact.
    const el=document.getElementById('el_'+c);
    if(el)el.setAttribute('class','el '+cls);
    return '<div class="ch"><div class="n">'+c+' · '+LBL[c]+'</div>'+
      '<div class="v '+cls+'">'+q.verdict+'</div>'+
      '<div class="m">'+q.std+' µV · '+q.railed+'% railed</div></div>';
  }).join('');
  // Reception (share of the nominal 256 Hz arriving) and mains hum.
  const rv=document.getElementById('recv');
  if(d.connected){const pc=Math.min(100,Math.round(d.rate/256*100));
    rv.textContent=pc+'%';rv.className=pc>=90?'good':(pc>=60?'':'warn');}
  else{rv.textContent='—';rv.className='';}
  const mn=document.getElementById('mains'), m=d.mains||{hz:null,uv:0};
  if(!d.connected||m.hz===null){mn.textContent='—';mn.className='';}
  else{mn.textContent=m.hz+' Hz · '+m.uv+' µV';
    mn.className = m.uv<20?'good':(m.uv<120?'':'warn');}
  CH.forEach(c=>draw(canv[c],d.traces[c]));
  CH.forEach(c=>{const b=d.bands[c]||{};
    BANDS.forEach((name,i)=>{const el=document.getElementById('bar_'+c+'_'+i);
      if(el)el.style.height=Math.max(2,(b[name]||0)*0.9)+'%';});});
};
es.onerror=()=>{document.getElementById('conn').innerHTML=
  '<span class="dot dead"></span>page disconnected';};
</script></body></html>
"""


ANNOT_PATH = os.path.join(RECDIR, "annotations.jsonl")
OPTICS_REQ_PATH = os.path.join(RECDIR, ".optics_now")   # touch => recorder does one burst


def _night_date_now() -> str:
    d = datetime.now()
    if d.hour < 12:
        d -= timedelta(days=1)
    return d.date().isoformat()


def add_note(text: str) -> dict | None:
    """Append a timestamped lifestyle note to ~/recordings/annotations.jsonl. It
    lives with the recordings, so it rides the same SMB share to the server, where
    the analyzer attaches it to the night and marks it on the hypnogram."""
    text = (text or "").strip()[:200]
    if not text:
        return None
    now = datetime.now().astimezone()
    entry = {"night_date": _night_date_now(),
             "ts_local": now.isoformat(timespec="seconds"),
             "text": text,
             "logged_at": now.isoformat(timespec="seconds")}
    try:
        with open(ANNOT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        return None
    return entry


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # keep the journal clean
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith("/note"):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            text = urllib.parse.parse_qs(raw).get("text", [""])[0] or raw
            entry = add_note(text)
            return self._json({"ok": bool(entry), "entry": entry},
                              200 if entry else 400)
        if self.path.startswith("/optics"):
            # Ask the recorder for one optics burst (LEDs on, live vitals). The mode
            # word picks the preset: 'quick' = dim (HR/HRV/respiration), 'deep' =
            # bright 16-ch (adds the O₂ index, more battery). The recorder ends the
            # current segment early and clears the flag when it starts the burst.
            q = urllib.parse.urlparse(self.path).query
            mode = urllib.parse.parse_qs(q).get("mode", ["quick"])[0]
            mode = "deep" if mode == "deep" else "quick"
            try:
                with open(OPTICS_REQ_PATH, "w", encoding="utf-8") as f:
                    f.write(mode)
                return self._json({"ok": True, "mode": mode})
            except OSError:
                return self._json({"ok": False}, 500)
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/stream"):
            return self._sse()
        if self.path.startswith("/api/status"):
            body = json.dumps(frame()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # Never let the browser cache the page: when the page HTML changes (as it
        # just did, AF7-only -> all sensors), a cached copy runs old JS against
        # new data and silently breaks — e.g. band-power bars that never fill.
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                payload = json.dumps(frame())
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                time.sleep(1.0 / FRAME_HZ)
        except (BrokenPipeError, ConnectionResetError):
            pass          # browser navigated away; nothing to clean up


def main() -> int:
    threading.Thread(target=COLLECTOR.run, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"muse status page on :{PORT}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
