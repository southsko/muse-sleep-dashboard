#!/usr/bin/env python3
"""Server-rendered inline SVG charts.

Deliberately no JS charting library: the dashboard must work on a LAN with no
internet, and the Unraid rule is that nothing gets fetched at runtime. Inline
SVG also means charts survive being saved or screenshotted.
"""

from __future__ import annotations

import html
from datetime import datetime

# Stage colours — defined once, reused by every chart, plot and stage bar.
#
# Sleep stages are not a flat categorical set: N1/N2/N3 are a *depth* ordinal
# scale, while WAKE and REM are distinct states. So this is one blue ordinal
# ramp plus two categorical hues, validated separately on the #161a20 panel
# (scripts/validate_palette.js):
#
#   NREM ramp  #5598e7 -> #2a78d6 -> #184f95   ordinal: ALL CHECKS PASS
#   WAKE/REM/N1 as categorical                 CVD ΔE 9.4, normal ΔE 19.5
#
# The earlier palette failed hard — N1 vs REM was ΔE 2.6 for deuteranopia,
# i.e. indistinguishable. Do not hand-pick replacements; re-run the validator.
#
# N1 vs N2 sit close by construction (one hue, stepped by lightness). That is
# what an ordinal ramp is, and it is why every stacked mark carries a 2px
# surface gap and every stage is directly labelled — the secondary encoding
# the ramp depends on. N3 is below 3:1 on the panel, so its label is mandatory.
STAGE_COLORS = {
    "WAKE": "#d95926",   # categorical — orange
    "REM":  "#199e70",   # categorical — aqua
    "N1":   "#5598e7",   # depth ramp, lightest
    "N2":   "#2a78d6",
    "N3":   "#184f95",   # depth ramp, darkest
    "UNS":  "#3d434d",   # recording gap: recessive, deliberately not a stage hue
}

# Physiological order, shallow -> deep. Used for stacked marks and plot rows.
STAGE_ORDER = ["WAKE", "REM", "N1", "N2", "N3"]

# Plain-English position for each 10-20 electrode code. The codes stay primary
# (they are the standard, and what yasa and the EDFs use), but nobody should
# have to remember that odd numbers are left and TP is behind the ear.
CHANNEL_LABELS = {
    "TP9":  "left ear",
    "AF7":  "left forehead",
    "AF8":  "right forehead",
    "TP10": "right ear",
}


def channel_label(name: str) -> str:
    return CHANNEL_LABELS.get(str(name), "")

ACCENT = "#5eb0ef"
GRID = "#2b3038"
MUTED = "#98a2b3"


def _finite(values):
    return [v for v in values if isinstance(v, (int, float))]


def sparkline(values, width=110, height=26, color=ACCENT):
    """A bare trend line — no axes, for use inside a stat tile."""
    vals = [v if isinstance(v, (int, float)) else None for v in values]
    real = _finite(vals)
    if len(real) < 2:
        return '<span class="spark-empty">—</span>'

    lo, hi = min(real), max(real)
    rng = (hi - lo) or 1.0
    n = len(vals)
    step = width / max(n - 1, 1)

    pts, seg, segs = [], [], []
    for i, v in enumerate(vals):
        if v is None:
            if len(seg) > 1:
                segs.append(seg)
            seg = []
            continue
        x = i * step
        y = height - ((v - lo) / rng) * (height - 4) - 2
        seg.append(f"{x:.1f},{y:.1f}")
    if len(seg) > 1:
        segs.append(seg)
    if not segs:
        return '<span class="spark-empty">—</span>'

    paths = "".join(
        f'<polyline points="{" ".join(s)}" fill="none" stroke="{color}" '
        f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
        for s in segs
    )
    last = segs[-1][-1].split(",")
    dot = f'<circle cx="{last[0]}" cy="{last[1]}" r="2.2" fill="{color}"/>'
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" '
            f'height="{height}" preserveAspectRatio="none">{paths}{dot}</svg>')


def line_chart(labels, series, height=190, unit="", y_min=None, y_max=None):
    """Multi-series time chart with axes, gridlines and hover tooltips.

    `series` is [(name, [values...], colour)]. Values may contain None for
    nights where the metric is undefined — gaps are left as gaps rather than
    interpolated, so a missing night never looks like a real data point.
    """
    all_vals = [v for _, vs, _ in series for v in _finite(vs)]
    if not all_vals:
        return '<p class="empty">Not enough data yet.</p>'

    lo = y_min if y_min is not None else min(all_vals)
    hi = y_max if y_max is not None else max(all_vals)
    if hi == lo:
        hi, lo = hi + 1, max(lo - 1, 0 if lo >= 0 else lo - 1)

    # Only pad axes we chose ourselves; an explicit 0..100 means 0..100.
    if y_min is None or y_max is None:
        pad = (hi - lo) * 0.1
        if y_max is None:
            hi += pad
        if y_min is None:
            # Never show negative headroom for a quantity that can't go below
            # zero — "-0.3 minutes asleep" is nonsense.
            lo = max(lo - pad, 0.0) if min(all_vals) >= 0 else lo - pad

    w, pl, pr, pt, pb = 1000, 46, 12, 12, 26
    plot_w, plot_h = w - pl - pr, height - pt - pb
    n = max(len(labels), 1)
    step = plot_w / max(n - 1, 1)

    def yp(v):
        return pt + plot_h - ((v - lo) / (hi - lo)) * plot_h

    # Gridlines + y labels
    # Enough decimals that adjacent ticks stay distinct — a 0..3 range labelled
    # to zero decimals renders as "3 2 1 1 0".
    span = hi - lo
    dec = 0 if span >= 8 else (1 if span >= 1 else 2)

    grid = ""
    for i in range(5):
        v = lo + span * i / 4
        y = yp(v)
        grid += (f'<line x1="{pl}" y1="{y:.1f}" x2="{w-pr}" y2="{y:.1f}" '
                 f'stroke="{GRID}" stroke-width="1"/>'
                 f'<text x="{pl-6}" y="{y+3.5:.1f}" class="ax" '
                 f'text-anchor="end">{v:.{dec}f}</text>')

    # x labels: first, middle, last only — keeps it readable on a phone
    xlab = ""
    for i in (0, n // 2, n - 1) if n > 2 else range(n):
        if 0 <= i < len(labels):
            x = pl + i * step
            anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
            xlab += (f'<text x="{x:.1f}" y="{height-8}" class="ax" '
                     f'text-anchor="{anchor}">{html.escape(str(labels[i]))}</text>')

    body = ""
    for name, vals, color in series:
        seg, segs = [], []
        for i, v in enumerate(vals):
            if not isinstance(v, (int, float)):
                if len(seg) > 1:
                    segs.append(seg)
                seg = []
                continue
            seg.append(f"{pl + i*step:.1f},{yp(v):.1f}")
        if len(seg) > 1:
            segs.append(seg)
        for s in segs:
            body += (f'<polyline points="{" ".join(s)}" fill="none" '
                     f'stroke="{color}" stroke-width="2" stroke-linejoin="round" '
                     f'stroke-linecap="round"/>')
        # Points carry a native tooltip; no JS needed.
        for i, v in enumerate(vals):
            if isinstance(v, (int, float)):
                lbl = labels[i] if i < len(labels) else ""
                body += (f'<circle cx="{pl + i*step:.1f}" cy="{yp(v):.1f}" r="2.6" '
                         f'fill="{color}"><title>{html.escape(str(lbl))}: '
                         f'{v:g}{html.escape(unit)} ({html.escape(name)})</title></circle>')

    legend = " ".join(
        f'<span class="key"><i style="background:{c}"></i>{html.escape(nm)}</span>'
        for nm, _, c in series
    )
    return (f'<div class="chart"><svg viewBox="0 0 {w} {height}" '
            f'preserveAspectRatio="none" class="linechart">'
            f'{grid}{xlab}{body}</svg><div class="legend">{legend}</div></div>')


def stage_bar(row, labelled=False):
    """Horizontal composition bar of one night's sleep stages.

    Segments carry a 2px surface gap. That is not decoration: the NREM steps are
    one hue separated only by lightness, so the gap is the secondary encoding
    that keeps N1/N2/N3 distinguishable — including for colourblind readers.
    """
    parts = [(k, row.get(k)) for k in STAGE_ORDER]
    parts = [(k, v) for k, v in parts if isinstance(v, (int, float)) and v > 0]
    total = sum(v for _, v in parts)
    if not total:
        return '<span class="empty">—</span>'

    segs = "".join(
        f'<div class="seg" style="width:{100*v/total:.2f}%;'
        f'background:{STAGE_COLORS[k]}" title="{k}: {v:g} min '
        f'({100*v/total:.0f}%)"></div>'
        for k, v in parts
    )
    bar = f'<div class="stagebar">{segs}</div>'
    if not labelled:
        return bar

    # Direct labels. N3 sits below 3:1 on the panel, so a label is mandatory
    # rather than optional — colour alone cannot carry it.
    keys = "".join(
        f'<span class="key"><i style="background:{STAGE_COLORS[k]}"></i>'
        f'{k} {100*v/total:.0f}%</span>'
        for k, v in parts
    )
    return f'{bar}<div class="legend">{keys}</div>'


def fmt_clock(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return "—"


def fmt_dur(minutes):
    """Minutes as e.g. 6h 42m — hours are how you actually think about sleep."""
    if not isinstance(minutes, (int, float)):
        return "—"
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"
