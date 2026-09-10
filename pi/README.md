# Pi recorder (Muse S Athena)

Files here run on the **Pi**, not on the server. The Pi records the Muse S Athena
over Bluetooth with [OpenMuse](https://github.com/DominiqueMakowski/OpenMuse),
decodes each segment to a clean CSV, and serves those CSVs to the analysis server
over SMB.

| File | Purpose |
|---|---|
| **`install.sh`** | **Start here.** One command: venv + OpenMuse, recorder, status page, Samba share, then verifies it |
| `muse_athena_record.py` / `muse-athena-record.service` / `install-athena.sh` | The recorder (OpenMuse capture → decode → CSV) and its systemd unit |
| `muse_status.py` / `install-status.sh` | Live status page (see below) |

## Installing on a fresh Pi

1. Flash Raspberry Pi OS (64-bit), enabling SSH and **5 GHz WiFi** in the imager.
2. Copy this whole `pi/` directory to the Pi.
3. On the Pi:

```bash
bash install.sh
```

It prompts for the Samba password (and optionally the Athena's MAC), provisions
everything, starts it, and runs its checks. Unattended:

```bash
MUSE_MAC=00:55:DA:.. SMB_PASSWORD=secret bash install.sh
```

The MAC is optional — leave it out and the recorder finds the band with
`OpenMuse find` at run time. It is never committed to this repo; it lives in
`~/.config/muse/athena.env`. Idempotent: re-running over a working install is safe.

A CSV appears in `~/recordings/` within a few minutes of the charged headband
being put on.

## The one thing that matters most

**Put the Pi on your 5 GHz SSID.** WiFi and Bluetooth share one antenna; holding a
continuous 256 Hz BLE stream while network traffic contends for the same 2.4 GHz
band corrupts frames and collapses the link. 5 GHz moves your traffic out of
Bluetooth's band. `install.sh` checks this and warns loudly.

### If you add a USB Bluetooth dongle

Better still — it bypasses the onboard UART radio entirely, where the frame
corruption happens. Make it the only adapter:

```bash
echo 'dtoverlay=disable-bt' | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

The dongle becomes `hci0` and nothing else needs to change.

## How the recorder works

OpenMuse `record` writes raw BLE packets to a `.txt`; the samples only exist after
a Python decode step. So `muse_athena_record.py` does both — capture and decode —
in one supervised loop:

- **Hourly segments** (`SEGMENT_SEC=3600`), merged back into one night on the
  server. Bounds memory and caps what any single dropout costs.
- **Liveness watchdog:** the raw `.txt` growing is the signal the link is alive; a
  stall tears the segment down and reconnects.
- **Bluetooth recovery:** resets the adapter and *verifies* it returns to
  `UP RUNNING` (a bare reset can leave it `DOWN`, after which every connect fails).
- **Chunked decode:** OpenMuse's decoder aborts the whole file on a single odd
  packet (the Athena emits an occasional stray 8-channel EEG packet). Decoding in
  small chunks and skipping only the bad one keeps the loss to a few samples
  instead of the night.
- **Self-healing:** any raw whose decode failed is re-decoded automatically on the
  next pass, so a stranded segment is never lost silently.

Each segment lands as `~/recordings/overnight_YYYYMMDD_HHMMSS.csv`
(`timestamps,TP9,AF7,AF8,TP10`, µV, unix-epoch seconds) — exactly what the server
expects.

## Preset = sensor set AND power draw (the whole game for overnight)

The preset selects channels and the optics LEDs. From BrainFlow's table, confirmed
against decoded data on firmware 3.1.15:

| Preset | EEG | Optics / LED | Use |
|---|---|---|---|
| **p21** (default here) | EEG4 | **none / off** | **overnight sleep** — no glow, low power |
| p20, p50, p51, p60, p61 | EEG4 | none / off | equivalents to p21 |
| p1035 | EEG4 | dim | adds PPG (heart rate) |
| p1041 (OpenMuse default) | EEG8 | bright | full fNIRS + PPG |

**Bright optics (p1041) drains the battery ~0.7 %/min — ~2 h per charge, nowhere
near a night.** EEG-only (p21) keeps the LEDs off and lasts a full night; it also
sheds the heavy optics stream, which makes the BLE link far more stable. The trade
is losing PPG/heart-rate (the status page shows those only on an optics preset). A
stray detail worth knowing: an OPTICS *frame* can decode as present-but-**empty**
on the EEG-only presets — judge by optics **row count/values**, not the key. Set
via `MUSE_PRESET` in `~/.config/muse/athena.env`.

Quick manual re-check with the band on:

```bash
~/muse-env/bin/OpenMuse record --address <MAC> --preset p21 --duration 60 --outfile /tmp/t.txt
~/muse-env/bin/python ~/muse_athena_record.py --decode-only /tmp/t.txt
column -s, -t < /tmp/t.csv | head        # 4 EEG cols, µV-ish, epoch timestamps
```

## Live status page

`muse_status.py` + `install-status.sh` — a Mind-Monitor-style live view served from
the Pi, at `http://<pi>:8080/`.

**Why it exists:** the Muse accepts exactly ONE Bluetooth connection, held by the
recorder. This page instead **tails the raw file the recorder is already writing**
and decodes the last chunk — so it costs nothing on the radio and can never
perturb or stop the night. (It reads only the tail, never the whole file, so
memory stays flat all night.)

Shows: connection state and data rate, per-channel contact quality (same
thresholds the analysis pipeline scores with), four live waveforms, per-channel
band power, battery, movement (from the IMU), and heart rate (when an optics
preset is used). Streams over Server-Sent Events, standard library only.

- **Separate systemd unit** with `CPUQuota=25%`, `MemoryMax=512M`, `Nice=10` — the
  recorder is the valuable thing and must always win a contest for resources.
- Reachable even when the recorder is down, which is exactly when you want to look.

## Afterwards, on the server

Point the mount at the Pi and restart the container:

```bash
PI_HOST=<pi-ip> bash install-mount.sh      # run on the server
docker compose up -d --force-recreate
```
