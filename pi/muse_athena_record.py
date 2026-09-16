#!/usr/bin/env python3
"""Muse S Athena overnight capture + decode, via OpenMuse.

OpenMuse `record` writes *raw BLE packets* to a .txt file, not a ready CSV — the
samples only exist after a Python decode step (`OpenMuse.decode_rawdata`), so
capture and decode live together here.

Flow per segment:
    OpenMuse record --address <MAC> --preset <p> --duration <s> --outfile raw.txt
    -> watchdog the .txt for growth (file growing = the link is alive)
    -> decode raw.txt to a clean CSV in ~/recordings/ that the server-side
       analyzer accepts:  timestamps, TP9, AF7, AF8, TP10  (µV, epoch seconds)

Timezone auto-detection on the server depends on two things this script must get
right:
  * the CSV `timestamps` are UTC unix-epoch seconds, and
  * the filename `overnight_YYYYMMDD_HHMMSS.csv` is LOCAL wall-clock time.
The difference between the two is what the server uses to infer the recorder's
timezone, so do not "fix" either to match the other.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("athena")

HOME = Path.home()   # systemd sets HOME for the service User; no name hardcoded
OUTDIR = Path(os.environ.get("OUTDIR", str(HOME / "recordings")))
RAWDIR = Path(os.environ.get("RAWDIR", str(OUTDIR / "raw")))

# The MAC is NOT hardcoded (this file is committed to a public repo). It comes
# from the environment — the systemd unit loads it from ~/.config/muse/athena.env
# — and if still empty we try to discover it with `OpenMuse find`.
MAC = os.environ.get("MUSE_MAC", "").strip()

# VALIDATED 2026-09-09 on Athena firmware 3.1.15 (MuseS-D605). The preset selects
# the sensor set AND the optics LEDs (BrainFlow's table, confirmed against decoded
# data): p20/p21/p50/p51/p60/p61 = EEG4, optics OFF, LED OFF; p1035 = EEG4 + dim
# optics; p1041 (OpenMuse's default) = EEG8 + bright optics. For overnight sleep
# use p21 — EEG-only keeps the LEDs off (no glow) and, decisively, far lower power:
# bright optics drains ~0.7%/min (~2h per charge), EEG-only lasts far longer. The
# trade is losing PPG/heart-rate, which only the optics presets carry.
# Caveat that misled an earlier pass: an OPTICS *frame* can decode as present-but-
# EMPTY on the EEG-only presets — judge by optics ROW COUNT/values, not the key.
PRESET = os.environ.get("MUSE_PRESET", "p21")

SEGMENT_SEC = int(os.environ.get("SEGMENT_SEC", "3600"))  # hourly segments
STALL_SEC = int(os.environ.get("STALL_SEC", "90"))        # no growth => dead link
POLL_SEC = int(os.environ.get("POLL_SEC", "20"))
RETRY_SEC = int(os.environ.get("RETRY_SEC", "10"))
# When the band is off (all day, while not worn) the link never comes up and the
# loop would otherwise retry every RETRY_SEC forever — ~120 pointless reconnects an
# hour that spam the journal and churn the adapter. Back off toward RETRY_MAX_SEC on
# a run of empty attempts; the first segment with real data resets it. Capped low
# enough that putting the band on at bedtime is still noticed within a couple minutes.
RETRY_MAX_SEC = int(os.environ.get("RETRY_MAX_SEC", "120"))
CONNECT_GRACE_SEC = int(os.environ.get("CONNECT_GRACE_SEC", "120"))
KEEP_RAW = os.environ.get("KEEP_RAW", "0") == "1"         # keep raw .txt after decode
STOP_HOUR = os.environ.get("STOP_HOUR", "").strip()       # optional: stop looping at HH:00 local

FS = 256.0
EEG_COLS = ["TP9", "AF7", "AF8", "TP10"]

# Optics "light stabs": we normally record EEG-only (p21, LEDs off, low power). A
# burst runs one short segment on an optics preset so the LEDs come on and the
# status page can read vitals from the PPG, then it drops straight back to p21.
# On demand via the status-page button (writes a mode word to OPTICS_REQ);
# OPTICS_EVERY_MIN>0 also schedules them. The burst's EEG is still normal EEG4, so
# the night stays continuous.
#   quick (dim, p1035): heart rate + HRV + respiration, minimal battery cost.
#   deep  (bright, p1041): also lights the RED LED = 16-channel optics, which is
#         what a relative blood-oxygen (SpO2) index needs — at ~0.7%/min it is the
#         costly one, so it is opt-in, never the default or the schedule.
OPTICS_PRESET = os.environ.get("OPTICS_PRESET", "p1035")          # dim: HR/HRV/resp
DEEP_PRESET = os.environ.get("DEEP_PRESET", "p1041")              # bright 16-ch: +O2
OPTICS_BURST_SEC = int(os.environ.get("OPTICS_BURST_SEC", "300"))  # 5 min
OPTICS_EVERY_MIN = int(os.environ.get("OPTICS_EVERY_MIN", "0"))    # 0 = only on demand
OPTICS_REQ = Path(os.environ.get("OUTDIR", str(HOME / "recordings"))) / ".optics_now"
HR_LOG = Path(os.environ.get("OUTDIR", str(HOME / "recordings"))) / "hr.jsonl"


def _burst_request() -> dict | None:
    """The pending optics-burst request, or None. The status page writes a mode
    word into the flag file: 'deep' picks the bright preset (red LED, adds the O2
    index) at a heavier battery cost; anything else is the dim quick burst. A bare
    'pXXXX' is honoured as a literal preset for debugging."""
    if not OPTICS_REQ.exists():
        return None
    try:
        mode = OPTICS_REQ.read_text(encoding="utf-8").strip().lower()
    except OSError:
        mode = ""
    if mode in ("deep", DEEP_PRESET):
        return {"preset": DEEP_PRESET, "duration": OPTICS_BURST_SEC, "mode": "deep"}
    if mode.startswith("p") and mode[1:].isdigit():
        return {"preset": mode, "duration": OPTICS_BURST_SEC, "mode": mode}
    return {"preset": OPTICS_PRESET, "duration": OPTICS_BURST_SEC, "mode": "quick"}

# The OpenMuse console script lives in the venv's bin/ next to this python. The
# systemd unit runs `<venv>/bin/python muse_athena_record.py` directly (not an
# activated venv), so the venv bin/ is NOT on PATH and a bare "OpenMuse" isn't
# found. Resolve it by absolute path; fall back to PATH for a manual run.
_cli = Path(sys.executable).with_name("OpenMuse")
OPENMUSE = str(_cli) if _cli.exists() else "OpenMuse"

_stop = False


def _on_term(signum, frame):
    global _stop
    _stop = True
    log.info("received signal %s — finishing current segment and exiting", signum)


# ---------------------------------------------------------------------------
# Bluetooth adapter recovery. Hard-won: the Pi's UART BLE wedges, and a bare
# reset can leave it DOWN so every later connect fails forever — so always
# verify it comes back up.
# ---------------------------------------------------------------------------
def _sh(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def adapter_up() -> bool:
    return "UP RUNNING" in _sh(["hciconfig", "hci0"]).stdout


def reset_bt() -> bool:
    if adapter_up():
        return True
    log.warning("bluetooth adapter is down; attempting recovery")
    _sh(["sudo", "hciconfig", "hci0", "down"]); time.sleep(1)
    _sh(["sudo", "hciconfig", "hci0", "up"]);   time.sleep(2)
    if adapter_up():
        log.info("adapter recovered (up/down)")
        return True
    _sh(["sudo", "systemctl", "restart", "bluetooth"]); time.sleep(5)
    _sh(["sudo", "hciconfig", "hci0", "up"]);           time.sleep(2)
    if adapter_up():
        log.info("adapter recovered (bluetooth service restart)")
        return True
    log.error("bluetooth adapter will not come up (UART chip wedged) — usually "
              "needs a Pi reboot. No data can be recorded.")
    return False


def discover_mac() -> str:
    """`OpenMuse find` prints nearby Muse MACs. VALIDATE the exact output format
    on the real device — this parse is a best guess at a MAC-looking token."""
    log.info("no MUSE_MAC set — scanning with `OpenMuse find`")
    try:
        out = _sh([OPENMUSE, "find"]).stdout
    except FileNotFoundError:
        log.error("OpenMuse not found on PATH — is the venv active?")
        return ""
    import re
    macs = re.findall(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", out)
    if macs:
        log.info("discovered Muse at %s (set MUSE_MAC to pin it)", macs[0])
        return macs[0]
    log.error("no Muse found by `OpenMuse find`; output was:\n%s", out.strip())
    return ""


# ---------------------------------------------------------------------------
# Record one segment to a raw .txt, watchdogging file growth for liveness.
# ---------------------------------------------------------------------------
def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(10):
        if proc.poll() is not None:
            return
        time.sleep(0.5)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


def record_segment(mac: str, raw_path: Path,
                   preset: str = PRESET, duration_sec: int = SEGMENT_SEC,
                   interruptible: bool = False) -> tuple[float, int]:
    """Returns (seconds_ran, bytes_written). Kills the recorder if the file
    stops growing — OpenMuse can sit alive after the link dies.

    interruptible: end this segment early if an optics burst is requested, so a
    tap on the status page lights up within seconds instead of waiting out the
    rest of an hour-long segment. Only normal segments are interruptible; a burst
    itself runs to completion."""
    # Flags confirmed on fw 3.1.15: record --address --preset --duration --outfile
    # (no --record needed). "Device ... was not found" here just means the band
    # isn't advertising this instant (asleep, or held by the phone app) — the loop
    # retries and latches when it comes back.
    cmd = [OPENMUSE, "record", "--address", mac, "--preset", preset,
           "--duration", str(duration_sec), "--outfile", str(raw_path)]
    log.info("recording: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, start_new_session=True)
    except FileNotFoundError:
        log.error("OpenMuse not found on PATH — is the venv active?")
        return 0.0, 0

    start = time.time()
    last, stalled = -1, 0
    try:
        while proc.poll() is None:
            if _stop:
                break
            if interruptible and OPTICS_REQ.exists():
                log.info("optics burst requested — ending this segment early to start it")
                break
            time.sleep(POLL_SEC)
            size = raw_path.stat().st_size if raw_path.exists() else 0
            if size > last:
                last, stalled = size, 0
            else:
                stalled += POLL_SEC
            elapsed = time.time() - start
            if last <= 0 and elapsed < CONNECT_GRACE_SEC:
                continue  # nothing written until the link comes up and first packet lands
            if stalled >= STALL_SEC:
                log.warning("STALL: no new data for %ss (%d bytes) — link presumed dead",
                            stalled, max(last, 0))
                break
    finally:
        _kill(proc)
    ran = time.time() - start
    size = raw_path.stat().st_size if raw_path.exists() else 0
    return ran, size


# ---------------------------------------------------------------------------
# Decode a raw .txt to the clean CSV the analyzer expects.
# ---------------------------------------------------------------------------
def _decode_eeg_chunked(messages: list, chunk: int = 200):
    """Decode raw notification lines to one concatenated EEG DataFrame, decoding
    in small chunks and SKIPPING any chunk OpenMuse cannot decode.

    Why chunk: OpenMuse's decode_rawdata aborts the ENTIRE file on a single odd
    packet. The Athena occasionally emits a stray 8-channel EEG packet mid-stream;
    OpenMuse sizes its array for 4 channels and dies with a shape-broadcast error
    (decode.py make_timestamps), taking the whole hour with it. That is exactly
    how the first real overnight decoded to nothing. Chunking limits the loss to
    the ~100-200 samples in the offending chunk (measured: 3 bad chunks in ~60 min
    = <2 min lost) instead of the entire night.

    Returns (eeg_dataframe_or_None, n_chunks_skipped).
    """
    import OpenMuse
    import pandas as pd
    frames, skipped = [], 0
    for i in range(0, len(messages), chunk):
        try:
            d = OpenMuse.decode_rawdata(messages[i:i + chunk])
        except Exception:
            skipped += 1
            continue
        key = next((k for k in d if str(k).upper().startswith("EEG")), None)
        if key is not None and len(d[key]):
            frames.append(d[key])
    if not frames:
        return None, skipped
    return pd.concat(frames, ignore_index=True), skipped


def decode_txt_to_csv(raw_path: Path, csv_path: Path, start_epoch: float) -> int:
    """Decode a raw .txt and write timestamps,TP9,AF7,AF8,TP10 (µV, epoch s).

    Robust to a truncated raw file (battery death mid-segment) AND to the stray
    undecodable packets OpenMuse chokes on — see _decode_eeg_chunked.
    """
    import numpy as np
    import pandas as pd

    with open(raw_path, "r", encoding="utf-8", errors="replace") as f:
        messages = [ln for ln in f if ln.strip()]
    if not messages:
        return 0

    eeg, skipped = _decode_eeg_chunked(messages)
    if eeg is None:
        raise RuntimeError("no EEG decoded from any chunk of the raw file")
    if skipped:
        log.warning("%s: skipped %d undecodable chunk(s) — a few lost samples, "
                    "not the night", raw_path.name, skipped)

    # Athena decodes the EEG columns PREFIXED: EEG_TP9, EEG_AF7, EEG_AF8, EEG_TP10.
    # Normalise by stripping a leading "eeg_" so prefixed and bare names both match.
    def _norm(c: str) -> str:
        return str(c).lower().replace(" ", "").removeprefix("eeg_")
    lut = {_norm(c): c for c in eeg.columns}
    missing = [w for w in EEG_COLS if w.lower() not in lut]
    if missing:
        raise RuntimeError(f"missing EEG channels {missing}; got {list(eeg.columns)}")

    # Units are microvolts, with a ~700 µV DC offset the server mean-centres away.
    out = pd.DataFrame(
        {w: pd.to_numeric(eeg[lut[w.lower()]], errors="coerce") for w in EEG_COLS}
    )
    n = len(out)

    # Synthesize timestamps as a continuous 256 Hz UTC-epoch timeline from the
    # record-start clock. The per-chunk decode resets each chunk's 'time' column,
    # so it can't be stitched — but a nominal timeline is correct to within the
    # handful of samples any skipped chunk drops, which staging does not care about.
    out.insert(0, "timestamps", start_epoch + np.arange(n) / FS)
    out.to_csv(csv_path, index=False)
    return n


# ---------------------------------------------------------------------------
def _past_stop_hour() -> bool:
    if not STOP_HOUR:
        return False
    try:
        return datetime.now().hour >= int(STOP_HOUR)
    except ValueError:
        return False


def extract_and_log_hr(raw_path: Path) -> int | None:
    """Decode the OPTICS (PPG) from an optics-burst raw file, estimate heart rate
    (strongest 0.7-4 Hz peak), and append {ts_local, bpm} to hr.jsonl — which rides
    the recordings share to the server like annotations do. Best-effort."""
    try:
        import OpenMuse
        import numpy as np
        import pandas as pd
        from scipy.signal import welch
        with open(raw_path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln for ln in f if ln.strip()]
        frames = []
        for i in range(0, len(lines), 200):
            try:
                d = OpenMuse.decode_rawdata(lines[i:i + 200])
            except Exception:
                continue
            opt = d.get("OPTICS")
            if opt is not None and len(opt):
                frames.append(opt)
        if not frames:
            return None
        opt = pd.concat(frames, ignore_index=True)
        cols = [c for c in opt.columns if c != "time"]
        arr = opt[cols].apply(pd.to_numeric, errors="coerce").to_numpy()
        best_bpm, best_pow = None, 0.0
        for j in range(arr.shape[1]):
            x = arr[:, j]
            if not np.isfinite(x).all() or np.std(x) == 0:
                continue
            fr, p = welch(x - x.mean(), fs=64.0, nperseg=min(len(x), 512))
            band = (fr >= 0.7) & (fr <= 4.0)
            if not band.any():
                continue
            pw = float(p[band].max())
            if pw > best_pow:
                best_pow, best_bpm = pw, float(fr[band][np.argmax(p[band])]) * 60.0
        if not best_bpm:
            return None
        bpm = int(round(best_bpm))
        with open(HR_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts_local": datetime.now().astimezone().isoformat(
                timespec="seconds"), "bpm": bpm}) + "\n")
        return bpm
    except Exception as exc:
        log.warning("HR extraction failed: %s", exc)
        return None


def _recover_orphans() -> None:
    """Re-decode any raw .txt that has no matching CSV — a segment whose in-loop
    decode failed (kept, not deleted), or a raw left behind by a crash/reboot
    mid-decode. This makes the by-hand recovery automatic: a stranded segment is
    swept up on the next pass instead of silently sitting there until someone runs
    a recovery script.

    Skips a raw still being written (recent mtime) so it never touches the segment
    currently recording. A raw that still won't decode is set aside as .txt.failed
    so it is preserved for inspection but not retried forever.
    """
    import re
    for raw in sorted(RAWDIR.glob("*.txt")):
        csv = OUTDIR / f"{raw.stem}.csv"
        if csv.exists():
            continue
        try:
            if time.time() - raw.stat().st_mtime < 45:   # still being written
                continue
        except OSError:
            continue
        m = re.search(r"(\d{8})_(\d{6})", raw.stem)
        if not m:
            continue
        try:
            start_epoch = datetime.strptime(
                m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()
            n = decode_txt_to_csv(raw, csv, start_epoch)
        except Exception as exc:
            log.warning("orphan %s could not be decoded, setting aside: %s",
                        raw.name, exc)
            csv.unlink(missing_ok=True)
            raw.rename(raw.with_suffix(".txt.failed"))
            continue
        if n > 0:
            log.info("recovered orphan %s: %d samples (~%.0f min)",
                     raw.name, n, n / FS / 60)
            if not KEEP_RAW:
                raw.unlink(missing_ok=True)
        else:
            csv.unlink(missing_ok=True)
            raw.rename(raw.with_suffix(".txt.failed"))


def main_loop() -> int:
    global MAC
    OUTDIR.mkdir(parents=True, exist_ok=True)
    RAWDIR.mkdir(parents=True, exist_ok=True)
    _recover_orphans()   # sweep up anything a prior run left undecoded
    last_burst = 0.0     # time.time() of the last optics burst (for the schedule)
    no_data_streak = 0   # consecutive attempts that found no band (drives backoff)

    def backoff() -> int:
        return min(RETRY_SEC * (2 ** min(no_data_streak, 6)), RETRY_MAX_SEC)

    while not _stop:
        _recover_orphans()   # and again each cycle (cheap when there's nothing to do)
        if not reset_bt():
            time.sleep(RETRY_SEC)
            continue
        if not MAC:
            MAC = discover_mac()
            if not MAC:
                no_data_streak += 1
                time.sleep(backoff())
                continue

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")  # LOCAL wall-clock (see docstring)
        raw_path = RAWDIR / f"overnight_{stamp}.txt"
        csv_path = OUTDIR / f"overnight_{stamp}.csv"
        start_epoch = time.time()

        # A short optics "light stab" (LEDs on, capture vitals) if the button
        # requested one or it's due on the schedule; otherwise normal EEG-only p21.
        req = _burst_request()
        scheduled = (OPTICS_EVERY_MIN > 0
                     and time.time() - last_burst >= OPTICS_EVERY_MIN * 60)
        burst = bool(req) or scheduled
        if burst:
            if req:
                preset, duration, mode = req["preset"], req["duration"], req["mode"]
            else:
                preset, duration, mode = OPTICS_PRESET, OPTICS_BURST_SEC, "quick"
            OPTICS_REQ.unlink(missing_ok=True)
            last_burst = time.time()
            log.info("optics burst (%s): %s for %ds (capturing vitals)",
                     mode, preset, duration)
        else:
            preset, duration = PRESET, SEGMENT_SEC

        # Normal segments end early when a burst is requested, so a tap lights the
        # LEDs within seconds instead of waiting out the rest of the hour.
        ran, size = record_segment(MAC, raw_path, preset, duration,
                                   interruptible=not burst)

        if size <= 0:
            # No packets at all: the link never came up (band off/not worn). Back
            # off on a run of these rather than spinning empty files every RETRY_SEC.
            no_data_streak += 1
            wait = backoff()
            log.warning("segment produced no data — link down (x%d); retrying in %ss",
                        no_data_streak, wait)
            raw_path.unlink(missing_ok=True)
            time.sleep(wait)
            continue
        no_data_streak = 0   # real data arrived — back to prompt retries

        try:
            n = decode_txt_to_csv(raw_path, csv_path, start_epoch)
            log.info("segment saved: %s (%d samples, ~%.0f min, %d raw bytes)",
                     csv_path.name, n, ran / 60.0, size)
        except Exception as exc:
            # Keep the raw file when decode fails — it is the only source of
            # record and is exactly what you need to fix the decode assumptions.
            log.exception("decode failed for %s (raw kept): %s", raw_path.name, exc)
            n = -1

        # Pull heart rate out of an optics burst before the raw is discarded.
        if burst and n >= 0 and raw_path.exists():
            bpm = extract_and_log_hr(raw_path)
            if bpm:
                log.info("heart rate from optics burst: %d bpm", bpm)

        if n >= 0 and not KEEP_RAW:
            raw_path.unlink(missing_ok=True)

        if _past_stop_hour():
            log.info("past STOP_HOUR=%s — stopping for the day", STOP_HOUR)
            break

        # A segment far shorter than its duration means the link is flaky; the
        # loop rebuilds the adapter/stream on the next pass. (A burst is meant to
        # be short, so judge it against its own duration, not the hourly default.)
        if ran < duration / 2:
            log.info("segment ended early (%.0fs of %ss) — rebuilding", ran, duration)
            time.sleep(RETRY_SEC)

    log.info("recorder exiting cleanly")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Muse S Athena capture + decode (OpenMuse)")
    ap.add_argument("--decode-only", metavar="RAW_TXT",
                    help="decode an existing raw .txt to a CSV beside it and exit "
                         "(for validating the decode against a test recording)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )

    if args.decode_only:
        raw = Path(args.decode_only)
        csv = raw.with_suffix(".csv")
        # No real record start for an offline file — synthesize from the file's
        # mtime so timestamps are still plausible epoch seconds.
        n = decode_txt_to_csv(raw, csv, start_epoch=raw.stat().st_mtime)
        log.info("decoded %d samples -> %s", n, csv)
        return 0

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    return main_loop()


if __name__ == "__main__":
    sys.exit(main())
