#!/usr/bin/env python3
"""Build-time verification that the pipeline actually runs in this image.

Run during `docker build` against the synthetic fixtures. Its real job is to
catch a bad dependency resolution — yasa's classifiers are LightGBM models
pickled against particular lightgbm/scikit-learn/joblib versions, and a
mismatch fails when the model is loaded. Better here than unattended at 09:00.
"""

import json
import sys
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/smoke/out")


def load(sub: str, name: str) -> dict:
    p = OUT / sub / f"{name}_stats.json"
    assert p.exists(), f"missing {p}"
    return json.loads(p.read_text())


def check(cond, msg, ctx=None):
    if not cond:
        raise AssertionError(f"{msg}\n{json.dumps(ctx, indent=2, default=str) if ctx else ''}")


# A normal night: stages cleanly on the preferred frontal channel.
d = load("good", "synthetic_clean")
check(d["status"] == "ok", "clean fixture should be ok", d)
# Staging ensembles bipolar frontal-to-ear derivations (AF7-TP10 + AF8-TP9),
# or falls back to plain frontals when no ear reference exists.
check("AF7" in d["staging_channel"] and "AF8" in d["staging_channel"],
      "clean fixture should stage on both frontal derivations", d)
check(d["stats"].get("TST") is not None, "clean fixture should have TST", d)
for f in ("synthetic_clean.edf", "synthetic_clean_hypnogram.png",
          "synthetic_clean_proba.png"):
    check((OUT / "good" / f).exists(), f"missing output {f}")

# Ear electrodes lost contact — the common real failure. Must still stage.
r = load("good", "synthetic_railed_ears")
check(r["status"] == "ok", "railed-ears fixture should still stage", r)
check("AF7" in r["staging_channel"] and r["staging_channel"],
      "railed-ears fixture should still stage on a frontal derivation", r)
verdicts = {c["name"]: c["verdict"] for c in r["channels"]}
check(verdicts.get("TP9") == "railed", "TP9 should be flagged railed", verdicts)
check(verdicts.get("TP10") == "railed", "TP10 should be flagged railed", verdicts)

# Nothing usable at all: rejected, but the EDF is still archived.
f = load("failed", "synthetic_flat")
check(f["status"] == "bad", "flat fixture should be marked bad", f)
check(f["outputs"].get("edf"), "flat fixture should still produce an EDF", f)
check((OUT / "failed" / "synthetic_flat.edf").exists(), "flat EDF missing")

# Band on the nightstand for the first and last 5 minutes of a 25-minute file.
# Scoring must be restricted to the worn window rather than discarding the file:
# quality is judged inside that window, not across the whole recording.
w = load("good", "synthetic_edges_not_worn")
check(w["status"] == "ok", "edges-not-worn fixture should still score", w)
check(w["wear_minutes"] is not None, "wear window not detected", w)
check(abs(w["wear_minutes"] - 15.0) <= 1.0,
      f"expected ~15 min worn, got {w['wear_minutes']}", w)
check(abs(w["wear_start_min"] - 5.0) <= 1.0,
      f"expected worn window to start ~5 min in, got {w['wear_start_min']}", w)
check(w["duration_minutes"] > w["wear_minutes"],
      "worn window should be shorter than the whole recording", w)
check(w["stats"].get("SE_recording") is not None
      and w["stats"].get("SE_worn") is not None,
      "both recording-based and worn-window efficiency should be reported", w)

# Three segments spanning one night must merge into ONE scored night, not three
# fragments. Without this the first real night with any dropout would render as
# several disconnected short entries with meaningless statistics.
seg = load("good", "seg_night_a")
check(seg["n_segments"] == 3, f"expected 3 segments merged, got {seg['n_segments']}", seg)
check(len(seg["segments"]) == 3, "segment filenames not recorded", seg)
check(seg["gap_minutes"] > 0, "inter-segment gaps should be counted", seg)
# ~45 min of recording spread over ~50 min of wall clock.
check(seg["stats"]["TIB"] > 40,
      f"merged night should span >40 min, got TIB={seg['stats']['TIB']}", seg)
# Gaps must be unscored, never silently counted as sleep or wake.
check(seg["stage_counts"].get("UNS", 0) > 0,
      "gap epochs should be marked UNS", seg["stage_counts"])
for other in ("seg_night_b", "seg_night_c"):
    check(not (OUT / "good" / f"{other}_stats.json").exists()
          and not (OUT / "failed" / f"{other}_stats.json").exists(),
          f"{other} should have been merged into the night, not scored separately")

# Raw CSVs must be archived to durable storage — the Pi SD card is not a
# system of record, and one failed taking every raw file with it.
raw = OUT / "raw"
check(raw.is_dir(), "raw CSV archive directory was not created")
n_archived = len(list(raw.glob("*.csv")))
check(n_archived >= 4, f"expected the fixtures archived, found {n_archived}")

check((OUT / "index.html").exists(), "index.html was not generated")

# Band power must be produced for a stageable night.
check((OUT / "good" / "synthetic_clean_bandpower.png").exists(),
      "band-power plot missing for the clean fixture")

# The database is what the dashboard reads; verify it is populated and that the
# three-state quality badge distinguishes ear-contact-lost from outright failed.
import sqlite3

dbp = OUT / "sleep.db"
check(dbp.exists(), "sleep.db was not created")
conn = sqlite3.connect(str(dbp))
conn.row_factory = sqlite3.Row
rows = {r["source"]: r for r in conn.execute("SELECT * FROM nights")}
check(len(rows) == 5, f"expected 5 nights in db, found {len(rows)}", list(rows))

clean = rows["synthetic_clean.csv"]
check(clean["quality"] == "good", f"clean should be good, got {clean['quality']}")
check(clean["TST"] is not None, "clean night missing TST in db")
check(clean["night_date"], "night_date not derived")

ears = rows["synthetic_railed_ears.csv"]
check(ears["quality"] == "ear-contact-lost",
      f"railed ears should be ear-contact-lost, got {ears['quality']}")

flat = rows["synthetic_flat.csv"]
check(flat["quality"] == "failed", f"flat should be failed, got {flat['quality']}")

worn = rows["synthetic_edges_not_worn.csv"]
check(worn["wear_minutes"] is not None and worn["duration_minutes"] > worn["wear_minutes"],
      "wear window not persisted to db")

# The lifestyle-annotation seam must exist and start empty.
cols = {c[1] for c in conn.execute("PRAGMA table_info(nights)")}
check({"notes", "tags"} <= cols, "notes/tags annotation seam missing from schema")
conn.close()

print(f"smoke test OK — synthetic night: TST={d['stats']['TST']} min, "
      f"SE={d['stats'].get('SE')}%")
