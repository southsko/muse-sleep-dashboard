#!/usr/bin/env python3
"""Drop-in replacement for `muselsl stream` that also publishes battery level.

`muselsl stream` builds LSL outlets for EEG/PPG/ACC/GYRO/optics but never wires
up the Muse's telemetry characteristic, so battery is neither published nor even
requested. Since the Muse accepts only one Bluetooth connection — held by the
recorder — battery can only reach anything by riding along on this same stream.

Rather than reimplement muselsl's connection/retry/backend logic (and risk
getting the EEG path subtly wrong), this monkeypatches ONE thing: the device
factory. After muselsl builds the Muse object, we attach a telemetry callback
and enable it. Everything else — the EEG outlet, the record consumer, the status
page — is byte-identical to before, because it IS muselsl doing the work.

A second LSL outlet, name "Muse_Battery" type "Battery", carries
[battery_percent, adc_volt, temperature] at the telemetry rate (a few Hz).

Usage mirrors `muselsl stream`:
    python muse_stream.py --address AA:BB:CC:DD:EE:FF --backend bleak
"""

from __future__ import annotations

import argparse
import sys

# muselsl's package __init__ does `from .stream import stream`, which rebinds the
# name `muselsl.stream` to the FUNCTION and shadows the submodule. So `import
# muselsl.stream as mstream` gives the function, not the module — the monkeypatch
# target `create_device` lives on the real module, reachable only via sys.modules.
import muselsl                       # noqa: F401  (triggers the shadowing init)
from muselsl import stream as _stream_fn
_mstream = sys.modules["muselsl.stream"]      # the actual module

from pylsl import StreamInfo, StreamOutlet, local_clock

_battery_outlet: StreamOutlet | None = None
_last_logged = [None]


def _make_battery_outlet(address: str) -> StreamOutlet:
    info = StreamInfo("Muse_Battery", "Battery", 3, 0, "float32",
                      f"muse_battery_{address}")
    ch = info.desc().append_child("channels")
    for name, unit in (("battery", "percent"), ("adc_volt", "volts"),
                       ("temperature", "celsius")):
        c = ch.append_child("channel")
        c.append_child_value("label", name)
        c.append_child_value("unit", unit)
    return StreamOutlet(info)


def _telemetry_callback(timestamp, battery, fuel_gauge, adc_volt, temperature):
    """muselsl calls this as data(timestamp, battery, fuel_gauge, adc, temp)."""
    if _battery_outlet is not None:
        _battery_outlet.push_sample([float(battery), float(adc_volt),
                                     float(temperature)], local_clock())
    # Log only on a meaningful change, so the journal shows the battery trend
    # without spamming.
    pct = round(float(battery))
    if _last_logged[0] is None or abs(pct - _last_logged[0]) >= 1:
        print(f"[battery] {pct}%", flush=True)
        _last_logged[0] = pct


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-a", "--address", required=True)
    ap.add_argument("-b", "--backend", default="bleak")
    ap.add_argument("-n", "--name", default=None)
    ap.add_argument("-r", "--retries", type=int, default=1)
    args = ap.parse_args(argv)

    global _battery_outlet
    _battery_outlet = _make_battery_outlet(args.address)

    # The single, surgical hook: wrap the device factory so the Muse it returns
    # already has telemetry enabled. muselsl's stream() sets the eeg/ppg/acc/gyro
    # callbacks afterwards but never touches telemetry, so ours survives and
    # refresh_subscriptions() subscribes it.
    _orig_create = _mstream.create_device

    def _create_with_telemetry(*a, **kw):
        muse = _orig_create(*a, **kw)
        muse.callback_telemetry = _telemetry_callback
        muse.enable_telemetry = True
        return muse

    _mstream.create_device = _create_with_telemetry

    print(f"[muse_stream] streaming {args.address} (EEG + battery)", flush=True)
    # Hand off to muselsl's own stream(); it blocks until the link drops, exactly
    # like `muselsl stream` did.
    _stream_fn(address=args.address, backend=args.backend,
               name=args.name, retries=args.retries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
