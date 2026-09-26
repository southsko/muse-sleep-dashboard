#!/usr/bin/env python3
"""Muse S Athena overnight capture + decode, via OpenMuse.

ONE Bluetooth connection is held for the whole night. The band streams raw BLE
packets, written as OpenMuse-format lines (ISO-UTC timestamp, UUID, hex) to a raw
.txt. Every SEGMENT_SEC the output is ROTATED to a fresh file on the same live
connection — nothing disconnects — and the finished file is decoded to a clean CSV
in ~/recordings/ by a separate background process while capture carries on:
    timestamps, TP9, AF7, AF8, TP10   (µV, epoch seconds)

Why not `OpenMuse record --duration 3600` in a loop (the old design): that
disconnects at the end of every segment, then decodes, then re-scans and
reconnects — 140-180 s of missing EEG at EVERY hourly boundary even on a perfect
link, i.e. a hole in the night every hour. Measured on 2026-09-24: ~16 of the
night's 23 missing minutes were those self-inflicted handovers.

Optics bursts switch preset ON THE SAME connection (halt, preset, start), and a
real link loss is noticed at once (bleak's disconnect callback, plus a short
no-data watchdog for a link that dies silently) and reconnected immediately.
Each (re)connection starts a new file: a file is always one gap-free stream,
because its CSV timestamps are laid out as a continuous 256 Hz timeline.

Timezone auto-detection on the server depends on two things this script must get
right:
  * the CSV `timestamps` are UTC unix-epoch seconds, and
  * the filename `overnight_YYYYMMDD_HHMMSS.csv` is LOCAL wall-clock time.
The difference between the two is what the server uses to infer the recorder's
timezone, so do not "fix" either to match the other.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
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

SEGMENT_SEC = int(os.environ.get("SEGMENT_SEC", "3600"))  # file rotation; NOT a reconnect
# The band sends ~70 packets/s while streaming, so a few seconds of silence already
# means the link is dead. 15 s leaves room for a preset switch (~1-2 s pause).
STALL_SEC = int(os.environ.get("STALL_SEC", "15"))
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
# Live capture: one connection, rotating raw files.
# ---------------------------------------------------------------------------
def _new_raw_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")  # LOCAL wall-clock (see docstring)
    path = RAWDIR / f"overnight_{stamp}.txt"
    while path.exists():                               # same second as the last file
        time.sleep(1)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = RAWDIR / f"overnight_{stamp}.txt"
    return path


class RawSink:
    """The file the BLE callbacks write to. rotate() swaps it for a fresh one
    between two packets, without touching the connection, and hands the finished
    file to a background decoder process."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.f = None
        self.opened = 0.0
        self.bytes = 0
        self.last_data = 0.0
        self.burst_mode: str | None = None   # optics mode this file was recorded in
        self.decoders: list[tuple[subprocess.Popen, Path, bool]] = []

    def open(self, burst_mode: str | None = None) -> None:
        self.path = _new_raw_path()
        self.f = open(self.path, "a", encoding="utf-8", buffering=1)
        self.opened, self.bytes, self.burst_mode = time.time(), 0, burst_mode
        log.info("writing %s%s", self.path.name,
                 f" (optics burst: {burst_mode})" if burst_mode else "")

    def write(self, uuid: str, data: bytearray) -> None:
        if self.f is None:
            return
        line = f"{datetime.now(timezone.utc).isoformat()}\t{uuid}\t{data.hex()}\n"
        self.f.write(line)
        self.bytes += len(line)
        self.last_data = time.time()

    def close(self, decode: bool = True) -> None:
        if self.f is None:
            return
        f, path, nbytes, burst = self.f, self.path, self.bytes, self.burst_mode
        self.f = self.path = None
        f.close()
        if nbytes <= 0:
            path.unlink(missing_ok=True)
            return
        log.info("closed %s (~%.0f min, %d raw bytes)", path.name,
                 (time.time() - self.opened) / 60.0, nbytes)
        if decode:
            self._spawn_decoder(path, bool(burst))

    def rotate(self, burst_mode: str | None = None) -> None:
        # Open the new file BEFORE closing the old one so no callback ever finds
        # the sink empty (callbacks run on the same event loop, but be safe).
        old_f, old_path, old_bytes, old_burst, old_opened = (
            self.f, self.path, self.bytes, self.burst_mode, self.opened)
        self.open(burst_mode)
        if old_f is not None:
            old_f.close()
            if old_bytes > 0:
                log.info("rotated %s (~%.0f min, %d raw bytes) — link stays up",
                         old_path.name, (time.time() - old_opened) / 60.0, old_bytes)
                self._spawn_decoder(old_path, bool(old_burst))
            else:
                old_path.unlink(missing_ok=True)

    def _spawn_decoder(self, raw: Path, burst: bool) -> None:
        # A separate, niced PROCESS: decoding an hour takes ~35 s of CPU, and doing
        # it in this process would starve the BLE callbacks (GIL) — or, as the old
        # design did, stop recording while it ran.
        cmd = ["nice", "-n", "10", sys.executable, os.path.abspath(__file__),
               "--decode-only", str(raw)]
        if burst:
            cmd.append("--hr")
        try:
            self.decoders.append((subprocess.Popen(cmd), raw, burst))
        except OSError as exc:
            log.warning("could not start decoder for %s (left for orphan recovery): %s",
                        raw.name, exc)

    def reap(self) -> None:
        still = []
        for proc, raw, burst in self.decoders:
            if proc.poll() is None:
                still.append((proc, raw, burst))
            elif proc.returncode != 0:
                log.error("decode of %s failed (raw kept for recovery)", raw.name)
        self.decoders = still


async def _set_preset(client, preset: str) -> None:
    """Switch preset on a live connection: the same halt -> preset -> start
    sequence OpenMuse uses at connect time. Streaming pauses ~1 s."""
    from OpenMuse.muse import MuseS
    await MuseS.send_command(client, "h"); await asyncio.sleep(0.2)
    await MuseS.send_command(client, preset); await asyncio.sleep(0.2)
    await MuseS.send_command(client, "dc001"); await asyncio.sleep(0.05)
    await MuseS.send_command(client, "dc001"); await asyncio.sleep(0.1)
    try:
        await MuseS.send_command(client, "L1")
    except Exception:
        pass


async def capture_session(mac: str, sink: RawSink, state: dict) -> float:
    """Connect once and stream until the link dies, stop is requested, or
    STOP_HOUR passes. Rotates files and runs optics bursts without dropping the
    connection. Returns seconds of real data captured (0 = never connected)."""
    import bleak
    from OpenMuse.muse import MuseS

    lost = asyncio.Event()

    def on_disconnect(_client) -> None:
        lost.set()

    def cb(uuid: str):
        def inner(_, data: bytearray) -> None:
            sink.write(uuid, data)
        return inner

    log.info("connecting to %s (preset %s)", mac, PRESET)
    started = 0.0
    try:
        async with bleak.BleakClient(mac, timeout=15.0,
                                     disconnected_callback=on_disconnect) as client:
            sink.open()
            await MuseS.connect_and_initialize(
                client, PRESET, {u: cb(u) for u in MuseS.DATA_CHARACTERISTICS},
                verbose=True)
            t0 = time.time()
            burst_until = 0.0
            while not lost.is_set() and not _stop:
                await asyncio.sleep(1.0)
                now = time.time()
                sink.reap()

                if sink.bytes > 0 and not started:
                    started = sink.opened
                    log.info("streaming — link up")
                if not started and now - t0 > CONNECT_GRACE_SEC:
                    log.warning("connected but no data after %ss — reconnecting",
                                CONNECT_GRACE_SEC)
                    break
                if started and now - sink.last_data > STALL_SEC:
                    log.warning("STALL: no data for %.0fs — link presumed dead, "
                                "reconnecting", now - sink.last_data)
                    break

                # Optics burst: on demand (status page) or on the schedule.
                if burst_until == 0.0:
                    req = _burst_request()
                    due = (OPTICS_EVERY_MIN > 0
                           and now - state["last_burst"] >= OPTICS_EVERY_MIN * 60)
                    if req or due:
                        req = req or {"preset": OPTICS_PRESET,
                                      "duration": OPTICS_BURST_SEC, "mode": "quick"}
                        OPTICS_REQ.unlink(missing_ok=True)
                        state["last_burst"] = now
                        log.info("optics burst (%s): %s for %ds (capturing vitals)",
                                 req["mode"], req["preset"], req["duration"])
                        sink.rotate(burst_mode=req["mode"])
                        await _set_preset(client, req["preset"])
                        burst_until = now + req["duration"]
                        continue
                elif now >= burst_until:
                    log.info("optics burst done — back to %s", PRESET)
                    await _set_preset(client, PRESET)
                    sink.rotate()
                    burst_until = 0.0
                    continue

                if burst_until == 0.0 and now - sink.opened >= SEGMENT_SEC:
                    sink.rotate()

                if _past_stop_hour():
                    log.info("past STOP_HOUR=%s — stopping for the day", STOP_HOUR)
                    state["stop_hour"] = True
                    break

            if lost.is_set():
                log.warning("link lost (device disconnected) — reconnecting now")
            else:
                await MuseS.stop_streaming(client)
    except Exception as exc:
        msg = str(exc).strip() or type(exc).__name__
        if sink.bytes > 0:
            log.warning("link error: %s — reconnecting now", msg)
        else:
            log.debug("connect failed: %s", msg)
    finally:
        sink.close()
    return (time.time() - started) if started else 0.0


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


def _first_line_epoch(raw_path: Path) -> float | None:
    """UTC epoch of the first packet in a raw file (each line starts with an ISO
    timestamp). That is when data really began — after the connect handshake."""
    try:
        with open(raw_path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                if ln.strip():
                    return datetime.fromisoformat(ln.split("\t", 1)[0]).timestamp()
    except (OSError, ValueError):
        pass
    return None


def decode_txt_to_csv(raw_path: Path, csv_path: Path,
                      start_epoch: float | None = None) -> int:
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

    if start_epoch is None:
        start_epoch = _first_line_epoch(raw_path)
    if start_epoch is None:
        raise RuntimeError("raw file has no parsable first timestamp")

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
    tmp = csv_path.with_suffix(".csv.part")
    out.to_csv(tmp, index=False)
    os.replace(tmp, csv_path)
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
            start_epoch = _first_line_epoch(raw) or datetime.strptime(
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
    sink = RawSink()
    state = {"last_burst": 0.0, "stop_hour": False}
    no_data_streak = 0   # consecutive connects that found no band (drives backoff)

    def backoff() -> int:
        return min(RETRY_SEC * (2 ** min(no_data_streak, 6)), RETRY_MAX_SEC)

    while not _stop:
        if not reset_bt():
            time.sleep(RETRY_SEC)
            continue
        if not MAC:
            MAC = discover_mac()
            if not MAC:
                no_data_streak += 1
                time.sleep(backoff())
                continue

        streamed = asyncio.run(capture_session(MAC, sink, state))
        if state["stop_hour"] or _stop:
            break
        if streamed > 0:
            # Real data flowed, so the band is on: reconnect IMMEDIATELY. Every
            # second spent here is a second of the night not recorded.
            no_data_streak = 0
            continue
        no_data_streak += 1
        wait = backoff()
        if no_data_streak == 1 or no_data_streak % 30 == 0:
            log.warning("band not reachable (x%d) — retrying in %ss", no_data_streak, wait)
        _recover_orphans()
        time.sleep(wait)

    # Let in-flight decoders finish so the last file is not left stranded.
    for proc, _raw, _b in sink.decoders:
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pass
    log.info("recorder exiting cleanly")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Muse S Athena capture + decode (OpenMuse)")
    ap.add_argument("--decode-only", metavar="RAW_TXT",
                    help="decode an existing raw .txt to a CSV beside it and exit "
                         "(for validating the decode against a test recording)")
    ap.add_argument("--hr", action="store_true",
                    help="with --decode-only: also extract heart rate (optics burst)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )

    if args.decode_only:
        # Also the recorder's background decoder: raw in RAWDIR -> CSV in OUTDIR.
        raw = Path(args.decode_only)
        csv = (OUTDIR if raw.parent == RAWDIR else raw.parent) / f"{raw.stem}.csv"
        try:
            n = decode_txt_to_csv(raw, csv)
        except Exception:
            log.exception("decode failed for %s (raw kept)", raw.name)
            return 1
        log.info("segment saved: %s (%d samples, ~%.0f min)", csv.name, n, n / FS / 60)
        if args.hr:
            bpm = extract_and_log_hr(raw)
            if bpm:
                log.info("heart rate from optics burst: %d bpm", bpm)
        if n > 0 and raw.parent == RAWDIR and not KEEP_RAW:
            raw.unlink(missing_ok=True)
        return 0

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    return main_loop()


if __name__ == "__main__":
    sys.exit(main())
