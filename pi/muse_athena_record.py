#!/usr/bin/env python3
"""Muse S Athena overnight capture + decode, via OpenMuse.

VALIDATE-WHEN-IT-LANDS. This is a scaffold written before the Athena was in
hand. Everything marked `# VALIDATE` is an assumption from the brief or the
OpenMuse README that MUST be confirmed against a real 60-second recording before
it is trusted (see pi/README.md → "Bringing the Athena online"). The structure —
supervision loop, Bluetooth recovery, decode → clean CSV — is settled; the exact
OpenMuse CLI flags, preset, decoded column names, and signal units are not.

Why Python (not the Gen-1 shell recorder): OpenMuse `record` writes *raw BLE
packets* to a .txt file, not a ready CSV. The samples only exist after a Python
decode step (`OpenMuse.decode_rawdata`), so capture and decode live together.

Flow per segment:
    OpenMuse record --address <MAC> --preset <p> --duration <s> --outfile raw.txt
    -> watchdog the .txt for growth (liveness, exactly like the Gen-1 recorder)
    -> decode raw.txt to a clean CSV in ~/recordings/ that the server-side
       analyzer already accepts:  timestamps, TP9, AF7, AF8, TP10  (µV, epoch s)

The analyzer only requires those four EEG columns plus a `timestamps` column in
unix epoch seconds; it needs no AUX. Timezone auto-detection on the server side
depends on two things this script must get right:
  * the CSV `timestamps` are UTC unix-epoch seconds, and
  * the filename `overnight_YYYYMMDD_HHMMSS.csv` is LOCAL wall-clock time.
The difference between the two is what the server uses to infer the recorder's
timezone, so do not "fix" either to match the other.

Does NOT touch the running Gen-1 muselsl recorder. Installed disabled; you switch
over deliberately (see install-athena.sh --switch).
"""

from __future__ import annotations

import argparse
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

SEGMENT_SEC = int(os.environ.get("SEGMENT_SEC", "3600"))  # hourly, like Gen 1
STALL_SEC = int(os.environ.get("STALL_SEC", "90"))        # no growth => dead link
POLL_SEC = int(os.environ.get("POLL_SEC", "20"))
RETRY_SEC = int(os.environ.get("RETRY_SEC", "10"))
CONNECT_GRACE_SEC = int(os.environ.get("CONNECT_GRACE_SEC", "120"))
KEEP_RAW = os.environ.get("KEEP_RAW", "0") == "1"         # keep raw .txt after decode
STOP_HOUR = os.environ.get("STOP_HOUR", "").strip()       # optional: stop looping at HH:00 local

FS = 256.0
EEG_COLS = ["TP9", "AF7", "AF8", "TP10"]

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
# Bluetooth adapter recovery (ported from the Gen-1 muse-autorecord.sh, which
# was hard-won: the Pi's UART BLE wedges, and a bare reset can leave it DOWN so
# every later connect fails forever — always verify it comes back up).
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


def record_segment(mac: str, raw_path: Path) -> tuple[float, int]:
    """Returns (seconds_ran, bytes_written). Kills the recorder if the file
    stops growing — OpenMuse, like muselsl, can sit alive after the link dies."""
    # Flags confirmed on fw 3.1.15: record --address --preset --duration --outfile
    # (no --record needed). "Device ... was not found" here just means the band
    # isn't advertising this instant (asleep, or held by the phone app) — the loop
    # retries and latches when it comes back.
    cmd = [OPENMUSE, "record", "--address", mac, "--preset", PRESET,
           "--duration", str(SEGMENT_SEC), "--outfile", str(raw_path)]
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


def main_loop() -> int:
    global MAC
    OUTDIR.mkdir(parents=True, exist_ok=True)
    RAWDIR.mkdir(parents=True, exist_ok=True)

    while not _stop:
        if not reset_bt():
            time.sleep(RETRY_SEC)
            continue
        if not MAC:
            MAC = discover_mac()
            if not MAC:
                time.sleep(RETRY_SEC)
                continue

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")  # LOCAL wall-clock (see docstring)
        raw_path = RAWDIR / f"overnight_{stamp}.txt"
        csv_path = OUTDIR / f"overnight_{stamp}.csv"
        start_epoch = time.time()

        ran, size = record_segment(MAC, raw_path)

        if size <= 0:
            # No packets at all: the link never came up. Back off and rebuild
            # rather than spinning empty files every few seconds.
            log.warning("segment produced no data — link down; retrying in %ss", RETRY_SEC)
            raw_path.unlink(missing_ok=True)
            time.sleep(RETRY_SEC)
            continue

        try:
            n = decode_txt_to_csv(raw_path, csv_path, start_epoch)
            log.info("segment saved: %s (%d samples, ~%.0f min, %d raw bytes)",
                     csv_path.name, n, ran / 60.0, size)
        except Exception as exc:
            # Keep the raw file when decode fails — it is the only source of
            # record and is exactly what you need to fix the decode assumptions.
            log.exception("decode failed for %s (raw kept): %s", raw_path.name, exc)
            n = -1

        if n >= 0 and not KEEP_RAW:
            raw_path.unlink(missing_ok=True)

        if _past_stop_hour():
            log.info("past STOP_HOUR=%s — stopping for the day", STOP_HOUR)
            break

        # A segment far shorter than its duration means the link is flaky; the
        # loop rebuilds the adapter/stream on the next pass.
        if ran < SEGMENT_SEC / 2:
            log.info("segment ended early (%.0fs of %ss) — rebuilding", ran, SEGMENT_SEC)
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
