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
from collections import deque
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
# How often to re-tail and decode. The useful floor is the BLE packet rate
# (~14-20 EEG notifications/sec) and OpenMuse's file flushing — polling faster
# than that just re-reads the same bytes. 0.15s (~7 Hz) sits near that ceiling.
POLL_SEC = float(os.environ.get("POLL_SEC", "0.15"))

BUFFER_SEC = 12.0                  # rolling window kept in memory
DISPLAY_HZ = 51.2                  # decimated rate sent to the browser
DECIMATE = int(SFREQ / DISPLAY_HZ)  # 5 -> 51.2 Hz, plenty for a visual trace
FRAME_HZ = 10                      # SSE frames per second (data updates ~7 Hz;
#                                    20 fps of full traces was heavy on mobile and
#                                    dropped the SSE connection — 10 is plenty)
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

# The Athena's optics (fNIRS/PPG) and IMU are on the same BLE stream, so the page
# can show heart rate and movement "for free" from data already arriving — the
# silver lining of not being able to turn the optics LEDs off.
PPG_FS = 64.0                      # OPTICS sample rate
PPG_SEC = 16.0                     # window for a stable heart-rate estimate
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
        self.acc = deque(maxlen=int(ACC_SEC * ACC_FS))   # accel magnitude, g
        self._hr = None                # cached heart rate (recomputed ~1/s)
        self._hr_at = 0.0

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
                with self.lock:
                    for row in arr:
                        self.ppg.append(row)
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


def quality(arr: np.ndarray) -> dict:
    """Per-channel contact verdict, matching the analysis pipeline's rules."""
    out = {}
    n = int(QUALITY_SEC * SFREQ)
    seg = arr[-n:] if len(arr) >= n else arr
    for i, name in enumerate(CHANNELS):
        if seg.size == 0:
            out[name] = {"verdict": "no data", "std": 0.0, "railed": 0.0}
            continue
        x = seg[:, i]
        xc = x - np.median(x)          # DC-agnostic, like analyze.py
        railed = float(np.mean(np.abs(xc) > RAIL_UV))
        std = float(np.std(x))
        if railed > RAIL_FRACTION:
            verdict = "railed"
        elif std < FLAT_STD_UV:
            verdict = "flat"
        elif std > 100:
            verdict = "noisy"
        else:
            verdict = "good"
        out[name] = {"verdict": verdict, "std": round(std, 1),
                     "railed": round(100 * railed, 1)}
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


def heart_rate(c: "Collector") -> int | None:
    """Beats/min from the strongest pulsatile PPG (optics) channel — the dominant
    spectral peak in 0.7-4 Hz (42-240 bpm). Cached ~1s: a Welch over 16 channels
    is not free at the SSE frame rate."""
    now = time.time()
    if now - c._hr_at < 1.0:
        return c._hr
    from scipy.signal import welch
    with c.lock:
        if len(c.ppg) < int(PPG_FS * 5):
            c._hr, c._hr_at = None, now
            return None
        arr = np.asarray(c.ppg, dtype=float)
    best_bpm, best_pow = None, 0.0
    for j in range(arr.shape[1]):
        x = arr[:, j]
        if not np.isfinite(x).all() or np.std(x) == 0:
            continue
        f, p = welch(x - x.mean(), fs=PPG_FS, nperseg=min(len(x), 512))
        band = (f >= 0.7) & (f <= 4.0)
        if not band.any():
            continue
        pw = float(p[band].max())
        if pw > best_pow:
            best_pow, best_bpm = pw, float(f[band][np.argmax(p[band])]) * 60.0
    hr = int(round(best_bpm)) if best_bpm else None
    with c.lock:
        c._hr, c._hr_at = hr, now
    return hr


def movement(c: "Collector") -> dict:
    """Restlessness from the accelerometer: std of |acc| (g) over the window."""
    with c.lock:
        if len(c.acc) < 8:
            return {"level": None, "label": "—"}
        a = np.asarray(c.acc, dtype=float)
    s = float(np.std(a))
    label = "still" if s < 0.03 else ("slight" if s < 0.12 else "moving")
    return {"level": round(s, 3), "label": label}


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
    # "connected" = data actually arriving (COLLECTOR.connected is the 6 s
    # staleness flag). Do NOT also gate on rate>50: between OpenMuse's bursty file
    # flushes the instantaneous rate briefly reads 0 on a perfectly live link, and
    # gating on it is exactly what made the page flap connected/disconnected.
    live = COLLECTOR.connected
    traces = {}
    if len(arr):
        tail = arr[-int(BUFFER_SEC * SFREQ):][::DECIMATE]
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
        "pulse": heart_rate(COLLECTOR),
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
  <span id="conn" class="stat"><span class="dot dead"></span>connecting…</span>
  <span id="batt" class="stat batt">🔋 <b>—</b></span>
  <span id="pulse" class="stat">❤️ <b>—</b></span>
  <span id="rate" class="stat"></span>
  <span id="seg" class="stat"></span>
</header>
<main>
  <div id="battwarn" class="battwarn"></div>
  <div class="panel"><h2>Pulse &amp; movement <span class="stat" style="text-transform:none">from optics + IMU</span></h2>
    <div class="vitals">
      <div class="vital"><div class="vlbl">Heart rate</div>
        <div class="vval"><span id="hr">—</span><span class="vunit"> bpm</span></div></div>
      <div class="vital"><div class="vlbl">Movement</div>
        <div class="vval"><span id="mv">—</span></div>
        <div class="vsub" id="mvsub"></div></div>
    </div>
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
  document.getElementById('seg').textContent=
    d.segment.name?(d.segment.name+' · '+d.segment.mb+' MB'):'no segment';
  const be=document.getElementById('batt'), bw=document.getElementById('battwarn');
  if(d.battery==null){be.className='stat batt';be.querySelector('b').textContent='—';
    bw.className='battwarn';}
  else{
    const p=Math.round(d.battery);
    const cls=p<=10?'crit':(p<=25?'low':'ok');
    be.className='stat batt '+cls;be.querySelector('b').textContent=p+'%';
    if(p<=10){bw.className='battwarn show';
      bw.textContent='⚠ Headband battery critically low ('+p+'%). Charge it now — '+
        'a low battery drops the Bluetooth link repeatedly and wrecks the recording.';}
    else if(p<=25){bw.className='battwarn show';
      bw.textContent='⚠ Headband battery low ('+p+'%). Charge before bed — below ~20% '+
        'the link starts dropping through the night.';}
    else{bw.className='battwarn';}
  }
  // Pulse (from PPG/optics) + movement (from the accelerometer).
  const pb=document.getElementById('pulse').querySelector('b');
  pb.textContent = d.pulse!=null ? d.pulse+' bpm' : '—';
  document.getElementById('hr').textContent = d.pulse!=null ? d.pulse : '—';
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
    mn.className = m.uv<10?'good':(m.uv<30?'':'warn');}
  CH.forEach(c=>draw(canv[c],d.traces[c]));
  CH.forEach(c=>{const b=d.bands[c]||{};
    BANDS.forEach((name,i)=>{const el=document.getElementById('bar_'+c+'_'+i);
      if(el)el.style.height=Math.max(2,(b[name]||0)*0.9)+'%';});});
};
es.onerror=()=>{document.getElementById('conn').innerHTML=
  '<span class="dot dead"></span>page disconnected';};
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # keep the journal clean
        pass

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
