#!/usr/bin/env python3
"""Muse sleep dashboard.

Reads the SQLite database the worker populates and serves three views:
overview (last night + trends), per-night detail, and a sortable nights list.

LAN-only, single user, no auth — per the brief.
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

from flask import (Flask, abort, redirect, render_template, request,
                   send_from_directory)

import charts
import db

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/data/output"))
DB_PATH = OUTPUT_DIR / "sleep.db"
BASELINE_NIGHTS = 30      # trailing window for personal baselines
SPARK_NIGHTS = 14

# --- user display settings (timezone override + clock format) ---------------
SETTINGS_PATH = OUTPUT_DIR / "ui_settings.json"
DEFAULT_SETTINGS = {"timezone": "auto", "time_format": "24"}
# Curated dropdown; "auto" uses the offset detected from each recording.
COMMON_ZONES = [
    "UTC", "America/Los_Angeles", "America/Denver", "America/Phoenix",
    "America/Chicago", "America/New_York", "America/Anchorage",
    "America/Halifax", "America/Sao_Paulo", "Europe/London", "Europe/Paris",
    "Europe/Berlin", "Europe/Moscow", "Asia/Kolkata", "Asia/Shanghai",
    "Asia/Tokyo", "Australia/Sydney", "Pacific/Auckland",
]


def load_settings() -> dict:
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text())}
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(form) -> bool:
    """Persist settings; return True if the plot-affecting prefs changed."""
    tz = form.get("timezone", "auto")
    fmt = "12" if form.get("time_format") == "12" else "24"
    if tz != "auto" and tz not in COMMON_ZONES:
        tz = "auto"
    before = load_settings()
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps({"timezone": tz, "time_format": fmt}))
    return (before.get("timezone"), before.get("time_format")) != (tz, fmt)


def regenerate_plots() -> None:
    """Rebake the hypnogram plots so their (baked-in) time axis matches the new
    settings. The axis is drawn at analysis time and can't be reformatted per
    request like table times, so a settings change means re-running the analyzer
    with --force. Backgrounded and flock-guarded (analyze.py serializes itself),
    so a rapid series of saves can't pile up overlapping runs."""
    import subprocess
    in_dir = os.environ.get("INPUT_DIR", "/data/recordings")
    subprocess.Popen(
        ["python", "/app/analyze.py", in_dir, "-o", str(OUTPUT_DIR),
         "--force", "--no-index"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def clock(iso) -> str:
    """Render a stored ISO time honouring the user's timezone + format settings.

    Stored times are timezone-aware (the offset auto-detected from the recording
    filenames). 'auto' displays them as-is; a chosen IANA zone reconverts the
    same instant. Format is 24h or 12h per the setting.
    """
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return "—"
    s = load_settings()
    if s["timezone"] != "auto" and dt.tzinfo is not None:
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo(s["timezone"]))
        except Exception:
            pass
    if s["time_format"] == "12":
        return dt.strftime("%-I:%M %p") if os.name != "nt" else dt.strftime("%I:%M %p")
    return dt.strftime("%H:%M")


app = Flask(__name__)
app.jinja_env.filters["clock"] = clock
app.jinja_env.filters["dur"] = charts.fmt_dur
app.jinja_env.filters["chlabel"] = charts.channel_label


def rows():
    """All nights, newest first. Empty list if the worker hasn't run yet."""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.all_nights(conn)]
    finally:
        conn.close()


def scored(rs):
    """Only nights that actually produced a hypnogram."""
    return [r for r in rs if r.get("status") == "ok" and r.get("TST") is not None]


def baseline(rs, key):
    """Trailing median — robust to the occasional wrecked night."""
    vals = [r[key] for r in rs[:BASELINE_NIGHTS]
            if isinstance(r.get(key), (int, float))]
    return statistics.median(vals) if len(vals) >= 3 else None


def tiles(rs):
    """Headline metrics for the most recent scored night, vs personal baseline."""
    ok = scored(rs)
    if not ok:
        return []
    latest = ok[0]
    history = ok[1:]

    spec = [
        ("Time asleep", "TST", "min", True),
        ("Efficiency", "SE_worn", "%", False),
        ("REM", "pct_REM", "%", False),
        ("Deep (N3)", "pct_N3", "%", False),
        ("Onset latency", "SOL", " min", False),
        ("Awakenings", "awakenings", "", False),
    ]

    def show(v, unit):
        if not isinstance(v, (int, float)):
            return "—"
        # One decimal at most; whole numbers stay whole.
        return f"{round(v, 1):g}{unit}"

    out = []
    for label, key, unit, as_dur in spec:
        val = latest.get(key)
        base = baseline(history, key)
        delta = (val - base) if isinstance(val, (int, float)) and base is not None else None
        # Oldest-to-newest so the sparkline reads left to right.
        series = [r.get(key) for r in ok[:SPARK_NIGHTS]][::-1]
        out.append({
            "label": label,
            "value": charts.fmt_dur(val) if as_dur else show(val, unit),
            "delta": delta,
            "delta_txt": (f"{'+' if delta > 0 else ''}{delta:.1f}{unit}"
                          if delta is not None else None),
            "n_baseline": len([r for r in history[:BASELINE_NIGHTS]
                               if isinstance(r.get(key), (int, float))]),
            "spark": charts.sparkline(series),
        })
    return out


def trend_block(rs, window):
    ok = scored(rs)
    if window != "all":
        ok = ok[: int(window)]
    ok = ok[::-1]                      # chronological
    labels = [r.get("night_date") or "?" for r in ok]

    def col(k):
        return [r.get(k) for r in ok]

    return {
        "n": len(ok),
        "tst": charts.line_chart(labels, [("Time asleep", col("TST"), charts.ACCENT)],
                                 unit=" min"),
        "eff": charts.line_chart(labels, [("Efficiency", col("SE_worn"), "#4ade80")],
                                 unit="%", y_min=0, y_max=100),
        "stages": charts.line_chart(
            labels,
            [("REM", col("pct_REM"), charts.STAGE_COLORS["REM"]),
             ("N3", col("pct_N3"), charts.STAGE_COLORS["N3"]),
             ("N2", col("pct_N2"), charts.STAGE_COLORS["N2"])],
            unit="%"),
    }


def health() -> dict:
    """Is the pipeline actually receiving data? Surfaced prominently.

    Every serious failure in this system has been silent — a recorder idling for
    hours, a share mounting empty, a stale cache truncating a file. None of them
    raised anything; the dashboard just kept showing the last good night. So the
    dashboard has to say out loud when it is looking at stale data.
    """
    src = Path(os.environ.get("INPUT_DIR", "/data/recordings"))
    info: dict = {"input_dir": str(src), "input_visible": src.is_dir()}
    try:
        info["input_files"] = len(list(src.glob("*.csv"))) if src.is_dir() else 0
    except OSError:
        info["input_visible"] = False
        info["input_files"] = 0

    rs = rows()
    latest_date = next((r.get("night_date") for r in rs if r.get("night_date")), None)
    info["last_night"] = latest_date
    info["days_stale"] = None
    if latest_date:
        try:
            d = datetime.strptime(latest_date, "%Y-%m-%d").date()
            info["days_stale"] = (datetime.now().date() - d).days
        except ValueError:
            pass

    problems = []
    if not info["input_visible"]:
        problems.append("Recordings folder is not visible — the share is probably "
                        "not mounted, or the container was started before it was.")
    elif info["input_files"] == 0:
        problems.append("No recordings are present at all. Either nothing was "
                        "recorded, or the mount is empty inside the container.")
    if info["days_stale"] is not None and info["days_stale"] >= 2:
        problems.append(f"No new night in {info['days_stale']} days — "
                        "the most recent result is stale.")
    if not rs:
        problems.append("Nothing has ever been analyzed.")
    info["problems"] = problems
    return info


@app.route("/")
def overview():
    rs = rows()
    ok = scored(rs)
    window = request.args.get("window", "30")
    if window not in ("7", "30", "90", "all"):
        window = "30"
    return render_template(
        "overview.html",
        health=health(),
        latest=ok[0] if ok else None,
        latest_raw=rs[0] if rs else None,
        tiles=tiles(rs),
        trends=trend_block(rs, window),
        window=window,
        n_total=len(rs),
        n_scored=len(ok),
        stage_bar=charts.stage_bar,
    )


@app.route("/nights")
def nights():
    rs = rows()
    sort = request.args.get("sort", "night_date")
    desc = request.args.get("dir", "desc") == "desc"
    hide_failed = request.args.get("hide_failed") == "1"

    if hide_failed:
        rs = [r for r in rs if r.get("status") == "ok"]

    valid = {"night_date", "TST", "SE_worn", "pct_REM", "pct_N3",
             "awakenings", "quality", "duration_minutes"}
    if sort in valid:
        rs.sort(key=lambda r: (r.get(sort) is None, r.get(sort)), reverse=desc)

    return render_template("nights.html", nights=rs, sort=sort,
                           dir="desc" if desc else "asc",
                           hide_failed=hide_failed, stage_bar=charts.stage_bar)


@app.route("/night/<path:source>")
def night(source):
    rs = rows()
    match = next((r for r in rs if r["source"] == source), None)
    if match is None:
        abort(404)
    idx = rs.index(match)
    ok = scored(rs)
    hist = [r for r in ok if r["source"] != source]

    deltas = {}
    for key in ("TST", "SE_worn", "pct_REM", "pct_N3", "SOL", "WASO", "awakenings"):
        base = baseline(hist, key)
        val = match.get(key)
        if base is not None and isinstance(val, (int, float)):
            deltas[key] = {"base": base, "delta": val - base,
                           "n": len([r for r in hist[:BASELINE_NIGHTS]
                                     if isinstance(r.get(key), (int, float))])}

    import json
    return render_template(
        "night.html",
        n=match,
        channels=json.loads(match.get("channels_json") or "[]"),
        assets=json.loads(match.get("assets_json") or "{}"),
        deltas=deltas,
        prev=rs[idx + 1] if idx + 1 < len(rs) else None,
        next=rs[idx - 1] if idx > 0 else None,
        stage_bar=charts.stage_bar,
    )


@app.route("/asset/<sub>/<path:filename>")
def asset(sub, filename):
    if sub not in ("good", "failed"):
        abort(404)
    directory = OUTPUT_DIR / sub
    if not directory.is_dir():
        abort(404)
    return send_from_directory(directory, filename)


@app.route("/recording/<path:filename>")
def recording(filename):
    """Download the original CSV straight from the (read-only) input mount."""
    src = Path(os.environ.get("INPUT_DIR", "/data/recordings"))
    if not src.is_dir():
        abort(404)
    return send_from_directory(src, filename, as_attachment=True)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        if save_settings(request.form):
            regenerate_plots()   # rebake plot axes to match the new prefs
        return redirect("/settings?saved=1")
    # Show the offset auto-detected from the data, for reference.
    detected = None
    for r in rows():
        iso = r["sleep_onset_time"] or r["wear_start_time"] or r["start_time"]
        if not iso:
            continue
        try:
            off = datetime.fromisoformat(iso).utcoffset()
        except (ValueError, TypeError):
            off = None
        if off is not None:
            mins = int(off.total_seconds() // 60)
            detected = f"UTC{'+' if mins >= 0 else '-'}{abs(mins)//60:02d}:{abs(mins)%60:02d}"
            break
    return render_template("settings.html", active="settings",
                           settings=load_settings(), zones=COMMON_ZONES,
                           detected=detected, saved=request.args.get("saved"))


@app.route("/healthz")
def healthz():
    h = health()
    return {"ok": not h["problems"], "db": DB_PATH.exists(),
            "nights": len(rows()), **h}


if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("WEB_PORT", "842"))
    print(f"muse dashboard on :{port}  (db={DB_PATH}, exists={DB_PATH.exists()})",
          flush=True)
    serve(app, host="0.0.0.0", port=port, threads=4)
