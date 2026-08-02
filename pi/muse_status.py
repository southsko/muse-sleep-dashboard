#!/usr/bin/env python3
"""Live signal view for the Muse recorder — a local Mind-Monitor-style page.

Runs ON THE PI, alongside the recorder. Attaches a SECOND read-only inlet to the
same LSL stream `muselsl record` is already consuming: LSL supports multiple
consumers, and the inlet sits downstream of the BLE link, so it cannot affect
the radio. Nothing here writes, and nothing here can stop the recorder.

Why this exists at all: the Muse accepts exactly ONE Bluetooth connection.
Opening Mind Monitor on a phone steals the link and kills the night's recording.
This page is the only way to check fit and signal without costing you data.

Deliberately uses the standard library only — no Flask, no framework. The
recorder Pi should not grow dependencies for a nice-to-have.

    python3 muse_status.py            # serves on :8080
    PORT=9000 python3 muse_status.py
"""

from __future__ import annotations

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

BUFFER_SEC = 12.0                  # rolling window kept in memory
DISPLAY_HZ = 51.2                  # decimated rate sent to the browser
DECIMATE = int(SFREQ / DISPLAY_HZ)  # 5 -> 51.2 Hz, plenty for a visual trace
FRAME_HZ = 20                      # SSE frames per second
QUALITY_SEC = 2.0                  # window for contact quality
BAND_SEC = 4.0                     # window for band power

# Same thresholds the analysis pipeline uses, so what you see while fitting the
# band matches how the night will actually be judged.
RAIL_UV = 900.0
RAIL_FRACTION = 0.20
FLAT_STD_UV = 1.0

BANDS = [("Delta", 0.5, 4.0), ("Theta", 4.0, 8.0), ("Alpha", 8.0, 12.0),
         ("Sigma", 12.0, 16.0), ("Beta", 16.0, 30.0)]


class Collector:
    """Pulls from LSL in a background thread into a rolling buffer."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.buf = deque(maxlen=int(BUFFER_SEC * SFREQ))
        self.connected = False
        self.samples = 0
        self.last_sample_at = 0.0
        self.started = time.time()
        self.rate = 0.0
        self._recent = deque(maxlen=64)
        # Battery arrives on a separate low-rate LSL stream (see muse_stream.py).
        # None until a reading lands — the page shows "—" rather than a fake 0%.
        self.battery = None
        self.battery_at = 0.0

    def run(self) -> None:
        from pylsl import StreamInlet, resolve_byprop
        while True:
            try:
                streams = resolve_byprop("type", "EEG", timeout=5)
                if not streams:
                    self.connected = False
                    time.sleep(2)
                    continue
                inlet = StreamInlet(streams[0], max_chunklen=32, recover=False)
                self.connected = True
                while True:
                    chunk, _ = inlet.pull_chunk(timeout=2.0, max_samples=64)
                    if not chunk:
                        # The stream went away; drop back to resolving.
                        if time.time() - self.last_sample_at > 5:
                            self.connected = False
                            break
                        continue
                    now = time.time()
                    with self.lock:
                        for row in chunk:
                            self.buf.append(row[:4])
                        self.samples += len(chunk)
                        self._recent.append((now, len(chunk)))
                    self.last_sample_at = now
            except Exception:
                self.connected = False
                time.sleep(2)

    def run_battery(self) -> None:
        """Separate thread: read the low-rate Muse_Battery stream if present.

        Kept apart from the EEG loop so a missing battery stream (e.g. the
        recorder is still on the old `muselsl stream`) never affects EEG or
        contact display — battery just stays None and the page shows "—".
        """
        from pylsl import StreamInlet, resolve_byprop
        while True:
            try:
                streams = resolve_byprop("type", "Battery", timeout=5)
                if not streams:
                    time.sleep(5)
                    continue
                inlet = StreamInlet(streams[0], recover=False)
                while True:
                    sample, _ = inlet.pull_sample(timeout=5.0)
                    if sample is None:
                        if time.time() - self.battery_at > 30:
                            with self.lock:
                                self.battery = None
                        continue
                    with self.lock:
                        self.battery = round(float(sample[0]), 1)
                        self.battery_at = time.time()
            except Exception:
                time.sleep(5)

    def snapshot(self) -> np.ndarray:
        with self.lock:
            if not self.buf:
                return np.zeros((0, 4))
            return np.asarray(self.buf, dtype=float)

    def data_rate(self) -> float:
        """Samples per second over the last couple of seconds."""
        now = time.time()
        with self.lock:
            pts = [(t, n) for t, n in self._recent if now - t < 2.0]
        if len(pts) < 2:
            return 0.0
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
        railed = float(np.mean(np.abs(x) > RAIL_UV))
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
    f, p = welch(x, fs=SFREQ, nperseg=min(len(x), 512))
    total = trapezoid(p[(f >= 0.5) & (f < 30)], f[(f >= 0.5) & (f < 30)])
    if total <= 0:
        return {name: 0.0 for name, _, _ in BANDS}
    out = {}
    for name, lo, hi in BANDS:
        sel = (f >= lo) & (f < hi)
        out[name] = round(float(100 * trapezoid(p[sel], f[sel]) / total), 1)
    return out


def current_segment() -> dict:
    """Whatever the recorder is writing right now."""
    import glob
    d = os.path.expanduser("~/recordings")
    files = sorted(glob.glob(os.path.join(d, "*.csv")), key=os.path.getmtime)
    if not files:
        return {"name": None, "mb": 0.0, "age": None}
    f = files[-1]
    return {"name": os.path.basename(f),
            "mb": round(os.path.getsize(f) / 1e6, 1),
            "age": round(time.time() - os.path.getmtime(f), 1)}


COLLECTOR = Collector()


def frame() -> dict:
    arr = COLLECTOR.snapshot()
    rate = COLLECTOR.data_rate()
    # Treat "connected" as data actually arriving, not merely a resolved stream:
    # a stalled link keeps the inlet open while delivering nothing.
    live = COLLECTOR.connected and rate > 50
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
        "bands": bandpower(arr, CHANNELS.index("AF7")),
        "segment": current_segment(),
        "battery": COLLECTOR.battery,
        "traces": traces,
        "display_hz": round(SFREQ / DECIMATE, 1),
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
canvas{width:100%;height:90px;display:block;background:#12151a;border-radius:6px}
.wrap{margin-bottom:.5rem}
.wrap .lbl{font-family:ui-monospace,monospace;font-size:.72rem;color:var(--muted);
  margin-bottom:.15rem}
.bars{display:grid;grid-template-columns:repeat(5,1fr);gap:.5rem;align-items:end;height:90px}
.bar{background:var(--accent);border-radius:3px 3px 0 0;min-height:2px;transition:height .2s}
.blbl{display:grid;grid-template-columns:repeat(5,1fr);gap:.5rem;margin-top:.3rem;
  font-size:.7rem;color:var(--muted);text-align:center}
</style></head><body>
<header>
  <h1>MUSE · LIVE</h1>
  <span id="conn" class="stat"><span class="dot dead"></span>connecting…</span>
  <span id="batt" class="stat batt">🔋 <b>—</b></span>
  <span id="rate" class="stat"></span>
  <span id="seg" class="stat"></span>
</header>
<main>
  <div id="battwarn" class="battwarn"></div>
  <div class="panel"><h2>Electrode contact</h2><div id="q" class="chgrid"></div></div>
  <div class="panel"><h2>Live signal <span id="hz" class="stat"></span></h2><div id="waves"></div></div>
  <div class="panel"><h2>Band power · AF7</h2>
    <div class="bars" id="bars"></div>
    <div class="blbl"><div>Delta</div><div>Theta</div><div>Alpha</div><div>Sigma</div><div>Beta</div></div>
  </div>
</main>
<script>
const CH=["TP9","AF7","AF8","TP10"], canv={};
const LBL={TP9:'left ear',AF7:'left forehead',AF8:'right forehead',TP10:'right ear'};
const wraps=document.getElementById('waves');
CH.forEach(c=>{const d=document.createElement('div');d.className='wrap';
  d.innerHTML='<div class="lbl">'+c+' <span style="opacity:.6">· '+LBL[c]+'</span></div>';
  const cv=document.createElement('canvas');d.appendChild(cv);wraps.appendChild(d);canv[c]=cv;});
const bars=CH.map(()=>0);
document.getElementById('bars').innerHTML=[0,1,2,3,4].map(i=>'<div class="bar" id="b'+i+'"></div>').join('');

function draw(cv,data){
  const dpr=window.devicePixelRatio||1, w=cv.clientWidth, h=cv.clientHeight;
  if(cv.width!==w*dpr){cv.width=w*dpr;cv.height=h*dpr;}
  const x=cv.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);
  x.clearRect(0,0,w,h);
  if(!data||!data.length)return;
  // Autoscale to the window, clamped so a flat trace does not explode.
  let mx=0;for(const v of data)mx=Math.max(mx,Math.abs(v));
  mx=Math.max(mx,20);
  x.strokeStyle='#252b34';x.lineWidth=1;x.beginPath();x.moveTo(0,h/2);x.lineTo(w,h/2);x.stroke();
  x.strokeStyle='#5598e7';x.lineWidth=1.2;x.beginPath();
  for(let i=0;i<data.length;i++){
    const px=i/(data.length-1)*w, py=h/2-(data[i]/mx)*(h/2-4);
    i?x.lineTo(px,py):x.moveTo(px,py);
  }
  x.stroke();
}

const es=new EventSource('/stream');
es.onmessage=e=>{
  const d=JSON.parse(e.data);
  document.getElementById('conn').innerHTML=
    '<span class="dot '+(d.connected?'live':'dead')+'"></span>'+
    (d.connected?'streaming':'no data');
  document.getElementById('rate').textContent=d.rate.toFixed(0)+' Hz';
  document.getElementById('hz').textContent='· '+d.display_hz+' Hz shown';
  document.getElementById('seg').textContent=
    d.segment.name?(d.segment.name+' · '+d.segment.mb+' MB'):'no segment';
  // Battery: null until a reading arrives (recorder may still be on the old
  // stream). Colour and warn by level — the thing that would have flagged the
  // low-battery night before it happened.
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
  document.getElementById('q').innerHTML=CH.map(c=>{
    const q=d.quality[c]||{verdict:'no data',std:0,railed:0};
    const cls=q.verdict.replace(' ','');
    return '<div class="ch"><div class="n">'+c+' · '+LBL[c]+'</div>'+
      '<div class="v '+cls+'">'+q.verdict+'</div>'+
      '<div class="m">'+q.std+' µV · '+q.railed+'% railed</div></div>';
  }).join('');
  CH.forEach(c=>draw(canv[c],d.traces[c]));
  ["Delta","Theta","Alpha","Sigma","Beta"].forEach((b,i)=>{
    document.getElementById('b'+i).style.height=Math.max(2,(d.bands[b]||0)*0.9)+'%';
  });
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
    threading.Thread(target=COLLECTOR.run_battery, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"muse status page on :{PORT}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
