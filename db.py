#!/usr/bin/env python3
"""SQLite persistence for analyzed nights.

One row per recording, keyed by source filename. The dashboard reads from here;
the per-night JSON files remain on disk as the archival record.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# Bump alongside analyze.SCHEMA_VERSION when stored columns change meaning.
DB_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS nights (
    source           TEXT PRIMARY KEY,   -- source CSV filename
    night_date       TEXT,               -- 'night of' local date, YYYY-MM-DD
    schema_version   INTEGER,
    analyzed_at      TEXT,

    status           TEXT,               -- ok | bad | error
    quality          TEXT,               -- good | ear-contact-lost | failed
    reason           TEXT,

    start_time       TEXT,               -- recording start, UTC ISO
    wear_start_time  TEXT,               -- local ISO
    sleep_onset_time TEXT,
    final_wake_time  TEXT,

    duration_minutes REAL,               -- whole recording
    wear_minutes     REAL,               -- scored window
    wear_start_min   REAL,
    sfreq_measured   REAL,
    staging_channel  TEXT,

    TIB REAL, SPT REAL, WASO REAL, TST REAL,
    SE REAL, SE_worn REAL, SE_recording REAL, SME REAL, SFI REAL,
    SOL REAL, SOL_5min REAL, Lat_REM REAL,
    WAKE REAL, N1 REAL, N2 REAL, N3 REAL, REM REAL,
    pct_N1 REAL, pct_N2 REAL, pct_N3 REAL, pct_REM REAL,
    awakenings INTEGER,

    n_segments       INTEGER,            -- files this night was assembled from
    gap_minutes      REAL,               -- unscored time between segments
    segments_json    TEXT,               -- source filenames

    channels_json    TEXT,               -- per-channel quality verdicts
    assets_json      TEXT,               -- filenames of edf/pngs
    asset_dir        TEXT,               -- 'good' or 'failed'

    -- Seam for later: manual per-night lifestyle annotation (caffeine,
    -- alcohol, exercise, late screens). Nothing writes these yet.
    notes            TEXT,
    tags             TEXT
);

CREATE INDEX IF NOT EXISTS idx_nights_date ON nights(night_date);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# stats key -> column name, where they differ (SQLite dislikes % in identifiers)
STAT_COLUMNS = {
    "TIB": "TIB", "SPT": "SPT", "WASO": "WASO", "TST": "TST",
    "SE": "SE", "SE_worn": "SE_worn", "SE_recording": "SE_recording",
    "SME": "SME", "SFI": "SFI",
    "SOL": "SOL", "SOL_5min": "SOL_5min", "Lat_REM": "Lat_REM",
    "WAKE": "WAKE", "N1": "N1", "N2": "N2", "N3": "N3", "REM": "REM",
    "%N1": "pct_N1", "%N2": "pct_N2", "%N3": "pct_N3", "%REM": "pct_REM",
    "awakenings": "awakenings",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created.

    CREATE TABLE IF NOT EXISTS silently does nothing on an existing table, so
    without this every schema addition breaks inserts on already-deployed
    databases with 'no such column'. Additive only — nothing is dropped.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(nights)")}
    if not have:
        return
    wanted = {
        "n_segments": "INTEGER", "gap_minutes": "REAL", "segments_json": "TEXT",
        "SE_worn": "REAL", "SE_recording": "REAL", "awakenings": "INTEGER",
        "wear_start_time": "TEXT", "sleep_onset_time": "TEXT",
        "final_wake_time": "TEXT", "notes": "TEXT", "tags": "TEXT",
    }
    for col, decl in wanted.items():
        if col not in have:
            conn.execute(f"ALTER TABLE nights ADD COLUMN {col} {decl}")
    conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('db_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(DB_VERSION),),
    )
    conn.commit()
    return conn


def night_date(start_iso: str | None) -> str | None:
    """The date a recording belongs to.

    A session starting after noon belongs to that calendar date; one starting
    before noon belongs to the previous date, so a 23:30->07:00 sleep is filed
    under a single night rather than split across two.
    """
    if not start_iso:
        return None
    try:
        dt = datetime.fromisoformat(start_iso)
    except ValueError:
        return None
    local = dt.astimezone() if dt.tzinfo else dt
    if local.hour < 12:
        local -= timedelta(days=1)
    return local.date().isoformat()


def quality_badge(result: dict) -> str:
    """Three-state badge: good | ear-contact-lost | failed."""
    if result.get("status") != "ok":
        return "failed"
    bad = {c["name"] for c in (result.get("channels") or [])
           if c.get("verdict") != "usable"}
    if bad & {"TP9", "TP10"}:
        return "ear-contact-lost"
    return "good"


def upsert_night(conn: sqlite3.Connection, result: dict, asset_dir: str) -> None:
    """Write one analysis result, replacing any previous row for that file.

    Never clobbers `notes`/`tags` — reprocessing a night must not discard
    annotations the user typed against it.
    """
    stats = result.get("stats") or {}

    row: dict = {
        "source": result.get("source"),
        # Prefer the analyzer's night_date (computed from the recording's own
        # local time); fall back to deriving it from the UTC start_time.
        "night_date": result.get("night_date") or night_date(result.get("start_time")),
        "schema_version": result.get("schema_version"),
        "analyzed_at": result.get("analyzed_at"),
        "status": result.get("status"),
        "quality": quality_badge(result),
        "reason": result.get("reason") or None,
        "start_time": result.get("start_time"),
        "wear_start_time": result.get("wear_start_time"),
        "sleep_onset_time": result.get("sleep_onset_time"),
        "final_wake_time": result.get("final_wake_time"),
        "duration_minutes": result.get("duration_minutes"),
        "wear_minutes": result.get("wear_minutes"),
        "wear_start_min": result.get("wear_start_min"),
        "sfreq_measured": result.get("sfreq_measured"),
        "staging_channel": result.get("staging_channel"),
        "n_segments": result.get("n_segments") or 1,
        "gap_minutes": result.get("gap_minutes") or 0.0,
        "segments_json": json.dumps(result.get("segments") or []),
        "channels_json": json.dumps(result.get("channels") or []),
        "assets_json": json.dumps(result.get("outputs") or {}),
        "asset_dir": asset_dir,
    }
    for key, col in STAT_COLUMNS.items():
        row[col] = stats.get(key)

    cols = list(row)
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "source")
    conn.execute(
        f"INSERT INTO nights ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(source) DO UPDATE SET {updates}",
        row,
    )
    conn.commit()


def all_nights(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # start_time breaks ties: several recordings can share a night_date (naps,
    # restarts, test runs), and without it "last night" picks one arbitrarily.
    return conn.execute(
        "SELECT * FROM nights "
        "ORDER BY COALESCE(night_date, analyzed_at) DESC, "
        "         COALESCE(start_time, analyzed_at) DESC"
    ).fetchall()


def get_night(conn: sqlite3.Connection, source: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM nights WHERE source = ?", (source,)).fetchone()
