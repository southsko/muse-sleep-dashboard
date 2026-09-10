# Pi recorder

Files here run on the **Pi**, not on the server.

| File | Purpose |
|---|---|
| **`install.sh`** | **Start here.** One command: everything below, then verifies it |
| `setup-pi5.sh` | Provisioning: venv + muselsl + liblsl, recorder, systemd unit, Samba share |
| `muse-autorecord.sh` | The supervised recorder itself |
| `muse_status.py` / `install-status.sh` | Live status page (see below) |
| `muse_athena_record.py` / `muse-athena-record.service` / `install-athena.sh` | **Muse S Athena** recorder (OpenMuse). Scaffold — validate before switching (see below) |

## Installing on a fresh Pi

1. Flash Raspberry Pi OS / Debian, enabling SSH and **5 GHz WiFi** in the imager.
2. Copy this whole `pi/` directory to the Pi.
3. On the Pi:

```bash
bash install.sh
```

It prompts for the two things it cannot know — the Muse's Bluetooth MAC and the
Samba password — then provisions everything, starts it, and runs ten checks.
Supply them up front to run unattended:

```bash
MUSE_MAC=00:11:22:33:44:55 SMB_PASSWORD=secret bash install.sh
```

Idempotent: re-running over a working install is safe and re-verifies it.

**The MAC is not in this repo** (scrubbed to a placeholder), and without it the
recorder hunts a nonexistent device forever with no error saying so. `install.sh`
refuses the placeholder and asserts afterwards that the real MAC reached the
systemd unit. Find it with the headband on: `bluetoothctl scan on`, look for
`MuseS-XXXX`.

A CSV appears in `~/recordings/` and starts growing within ~60 s of the headband
being switched on.

## The one thing that matters most

**Put the Pi 5 on your 5 GHz SSID.**

WiFi and Bluetooth are a single chip sharing one antenna. Holding a continuous 256 Hz BLE
stream while all network traffic contends for the same 2.4 GHz band is what destroyed
every recording on the Pi 3B — `dmesg` showed `Bluetooth: hci0: Frame reassembly failed
(-84)` escalating until the link collapsed, every time, within 15 minutes.

Wired ethernet would fix this outright. 5 GHz WiFi achieves nearly the same thing: your
traffic leaves the band Bluetooth is using. The Pi 3B could not do this — it is 2.4 GHz
only. **The Pi 5 can, and this is the main reason it will work where the 3B could not.**

If your AP band-steers on a single SSID, either pin the Pi to 5 GHz or use a band-specific
SSID. `setup-pi5.sh` checks this and warns loudly.

### If you add a USB Bluetooth dongle

Better still — it bypasses the onboard UART chip entirely, which is physically where the
frame corruption occurs. Make it the only adapter:

```bash
echo 'dtoverlay=disable-bt' | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

The dongle then becomes `hci0` and the script needs no change.

## Why segments are one hour, not twelve

`muselsl`'s save routine (`record.py` `_save()`) re-concatenates the **entire accumulated
recording** on every periodic write and never frees the buffer:

```python
res_arr = np.concatenate(res, axis=0)     # everything so far
lr.fit(X, y)                              # regression over all timestamps
data = pd.DataFrame(data=res_arr, ...)    # another full copy
data = data[data['timestamps'] > last_written_timestamp]   # then discards most
```

A 12-hour segment needs well over 2 GB transient near the end. The Pi 3B has 905 MB, so
`--duration 43200` was never achievable there — it would have been OOM-killed before
morning even with a perfect link. The Pi 5's 4–8 GB removes that ceiling, but hourly
segments are still the right call: they bound memory and cap how much a dropout costs.

The stream is held open across segment rollovers, so a rollover costs ~2 s rather than a
full reconnect (~50 s).

## What the supervisor fixes

The original script ran `muselsl record --duration 43200` unsupervised. When the BLE link
dropped, the recorder did **not** exit — so it never "failed", so systemd's
`Restart=on-failure` could never fire. On 2026-07-20 the link died at 21:50 and the
recorder idled until 04:53: seven hours of nothing, with systemd reporting the service
perfectly healthy.

`muselsl` appends to the CSV as chunks arrive, so **file growth is a direct liveness
signal**. The supervisor polls size every 20 s, declares a stall after 180 s of no growth,
tears down both processes, recovers the adapter, discards empty segments, and restarts.

`reset_bt()` **verifies** the adapter reaches `UP RUNNING` and escalates
(down/up → restart bluetooth → loud warning). This matters: a bare `hciconfig hci0 reset`
can leave the adapter `DOWN`, after which every connect fails with "No powered Bluetooth
adapters found" indefinitely. It never reboots on its own initiative.

If the UART chip wedges completely (`Can't init device hci0: Connection timed out`),
nothing short of a reboot clears it — the log will say so explicitly.

## Afterwards, on the server

Point the mount at the new Pi:

```bash
umount /mnt/user/data/eeg/recordings
mount -t cifs //<pi5-ip>/recordings /mnt/user/data/eeg/recordings \
  -o username=<user>,password=<pass>,ro,vers=3.1.1,uid=99,gid=100
```

If you installed the persistent mount, update the address in
`/boot/config/mount-muse-recordings.sh` too.


## Live status page

`muse_status.py` + `install-status.sh` — a Mind-Monitor-style live view served
from the Pi itself.

```bash
bash install-status.sh          # PORT=8080 by default
```

Then open `http://<pi>:8080/`.

**Why it exists:** the Muse accepts exactly ONE Bluetooth connection. Opening
Mind Monitor on a phone steals the link and kills the night's recording. This
page attaches a second *read-only* LSL inlet to the stream the recorder is
already consuming, so it is the only way to check fit and signal without
costing you data.

Shows: connection state and actual data rate, per-channel contact quality
(using the same thresholds the analysis pipeline scores with, so what you see
while fitting matches how the night will be judged), four live scrolling
waveforms, band power, and the segment currently being written.

Streams over Server-Sent Events at 20 fps — native `EventSource`, plain HTTP,
no WebSocket, no framework, and **standard library only**: the recorder Pi does
not grow dependencies for a convenience feature.

### Design constraints

- **Separate systemd unit** with `CPUQuota=25%`, `MemoryMax=256M`, `Nice=10`.
  The recorder is the fragile, valuable thing and must always win a contest for
  resources.
- Deliberately **not** `Requires=muse-record` — the page must be reachable when
  the recorder is down, which is precisely when you want to look at it.
- "Streaming" means data is actually arriving above 50 Hz, not merely that an
  LSL stream resolved. A stalled link keeps the inlet open while delivering
  nothing, and a reassuring green dot in that state would be worse than useless.
- Expect ~0.5-1 s more latency than Mind Monitor's direct BLE.

### Verified (2026-07-23)

A second LSL inlet does not perturb the recorder. Measured with the headband on
and the page streaming: the recorder held 13,771 B/s — full rate, unchanged —
while the page cost 1.7% CPU at load 0.32 on 4 cores. Safe to leave running
overnight.


## Battery level

`muse_stream.py` — a drop-in replacement for `muselsl stream` that also publishes
the headband battery. `muselsl stream` only publishes EEG/PPG/ACC/GYRO/optics; it
never subscribes the Muse's telemetry characteristic, so battery is neither sent
nor available. And since the Muse allows only one Bluetooth connection (held by
the recorder), battery can only reach anything by riding this same stream.

`muse_stream.py` monkeypatches exactly one thing — the muselsl device factory —
to attach a telemetry callback, then hands off to muselsl's own `stream()`. The
EEG outlet is produced by unchanged muselsl code, so `muselsl record` and the
status page see a byte-identical EEG stream. A second outlet, name `Muse_Battery`
type `Battery`, carries `[battery %, adc_volt, temperature]`.

Note: muselsl's package init rebinds `muselsl.stream` to the *function*, shadowing
the submodule. The module (where `create_device` lives) is reached via
`sys.modules['muselsl.stream']`; the function via `from muselsl import stream`.

The status page shows a battery pill (green / amber ≤25 % / red ≤10 %) and a
warning banner below ~25 %, since a low battery drops the BLE link repeatedly
through the night — the failure that produced 34 disconnects on 2026-07-23.

### To enable (needs a live test first)

The recorder still runs the proven `muselsl stream`. To switch, change the stream
line in `muse-autorecord.sh`:

    muselsl stream --backend bleak --address "${MUSE_MAC}"
    # becomes:
    python "${HOME}/muse_stream.py" --backend bleak --address "${MUSE_MAC}"

**Do not swap unattended.** Dry-tested to the BLE boundary, but whether the real
Muse emits telemetry — and whether the extra subscription perturbs the link — is
unverified. Test with a charged headband on: stop the recorder, run muse_stream.py
by hand for ~30 s, confirm it logs `[battery NN%]` and that both EEG and Battery
LSL streams appear, then swap the line and restart. Revert is one line.

## Muse S Athena (OpenMuse) — validated on hardware 2026-09-09

The Gen-1 stack above uses **muselsl**, which does not support Athena (3rd-gen)
firmware. The Athena recorder uses **OpenMuse** instead, which records raw BLE
packets to a `.txt` and decodes to samples in Python — so capture and decode live
in one script, `muse_athena_record.py`. It writes the **same CSV the server
already accepts** (`timestamps,TP9,AF7,AF8,TP10`, µV, unix-epoch seconds), so
nothing on the analysis side changes.

Confirmed on firmware 3.1.15 (MuseS-D605): OpenMuse CLI flags as scaffolded, EEG
frame key `EEG`, columns prefixed `EEG_TP9…`, `time` = seconds-from-start, µV
with a ~700 µV DC offset (the server mean-centres before its rail check).

### Preset = sensor set AND power draw (this is the whole game for overnight)

The preset selects channels and the optics LEDs. From BrainFlow's table, confirmed
against decoded data:

| Preset | EEG | Optics / LED | Use |
|---|---|---|---|
| **p21** (default here) | EEG4 | **none / off** | **overnight sleep** — no glow, low power |
| p20, p50, p51, p60, p61 | EEG4 | none / off | equivalents to p21 |
| p1035 | EEG4 | dim | adds PPG (heart rate) |
| p1041 (OpenMuse default) | EEG8 | bright | full fNIRS + PPG |

**Bright optics (p1041) drains the battery ~0.7 %/min — ~2 h per charge, nowhere
near a night.** EEG-only (p21) keeps the LEDs off and lasts far longer; it also
drops the heavy optics stream, easing the flaky BLE link. The trade is losing
PPG/heart-rate (the status page shows those only on an optics preset). A stray
detail that misled the first pass: an OPTICS *frame* can decode as present-but-
**empty** on the EEG-only presets — judge by optics **row count/values**, not the
key. Set via `MUSE_PRESET` in `~/.config/muse/athena.env`.

### Install — Athena is THE recorder

    bash install-athena.sh                    # install (p21), retire Gen-1, start; enabled on boot
    MUSE_NO_START=1 bash install-athena.sh    # install but don't start yet

The Athena **replaces** the Gen-1 muselsl recorder. A Muse takes only one
Bluetooth connection and the Pi has one BLE adapter, so **two recorders running
at once fight over the radio and the link flaps connected/disconnected all
night** — the failure this setup is built to avoid. The installer therefore
retires Gen-1 (stops, disables, renames its unit aside), and the Athena unit
declares `Conflicts=muse-record.service` as a hard guarantee that the two can
never be active together. On boot, only the Athena recorder starts.

Revert to Gen-1 is deliberate: rename `muse-record.service.retired-*` back,
`systemctl disable --now muse-athena-record`, `systemctl enable --now muse-record`.
Quick re-check with the band on: `OpenMuse record … --duration 60 …` then
`muse_athena_record.py --decode-only …` and eyeball the CSV.

### Design notes carried over

Segments are still **hourly** (`SEGMENT_SEC=3600`) and merged into one night on
the server. The Bluetooth-recovery logic (reset, verify the adapter actually came
back up, escalate) is ported from the Gen-1 supervisor. On a **Pi 3** a USB BLE
dongle (CSR8510) is the fix if overnight drops are frequent — note it, don't
pre-solve. MAC is never committed: it lives in `~/.config/muse/athena.env`
(written by the installer) or is discovered at runtime via `OpenMuse find`.
