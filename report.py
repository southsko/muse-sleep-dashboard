#!/usr/bin/env python3
"""Build a static index.html listing every analyzed night.

No server and no external assets — the page is opened straight off the share,
so everything it needs must be inline or a relative link to a sibling PNG.
"""

from __future__ import annotations

import json
from pathlib import Path

CSS = """
*, *::before, *::after { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.5rem;
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #f6f7f9; color: #1c1f23;
}
@media (prefers-color-scheme: dark) {
  body { background: #14161a; color: #e6e8eb; }
  .card { background: #1c1f24 !important; border-color: #2b3038 !important; }
  .meta { color: #98a2b3 !important; }
  th { border-color: #2b3038 !important; }
  td { border-color: #23272e !important; }
  a { color: #7cb7ff !important; }
}
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
.sub { color: #667085; margin: 0 0 2rem; font-size: .9rem; }
.card {
  background: #fff; border: 1px solid #e3e6ea; border-radius: 10px;
  padding: 1.15rem 1.25rem; margin-bottom: 1.25rem;
}
.card h2 { font-size: 1.05rem; margin: 0 0 .15rem; font-family: ui-monospace, monospace; }
.meta { color: #667085; font-size: .85rem; margin: 0 0 .9rem; }
.badge {
  display: inline-block; padding: .1rem .5rem; border-radius: 99px;
  font-size: .75rem; font-weight: 600; vertical-align: middle; margin-left: .4rem;
}
.badge.ok   { background: #d7f5e1; color: #0b6b35; }
.badge.bad  { background: #ffe6d5; color: #8a3a06; }
.badge.error{ background: #ffd9dc; color: #8a0f19; }
.badge.railed { background: #fff0c2; color: #7a5600; }
.badge.flat   { background: #e6e8eb; color: #4a5058; }
img { width: 100%; max-width: 100%; height: auto; border-radius: 6px; display: block; }
.plots { display: grid; grid-template-columns: 1fr; gap: .75rem; }
@media (min-width: 900px) { .plots { grid-template-columns: 1fr 1fr; } }
.stats { overflow-x: auto; margin-top: .9rem; }
table { border-collapse: collapse; font-size: .85rem; min-width: 100%; }
th, td { text-align: left; padding: .3rem .85rem .3rem 0; white-space: nowrap; }
th { border-bottom: 1px solid #e3e6ea; font-weight: 600; }
td { border-bottom: 1px solid #f0f2f4; font-variant-numeric: tabular-nums; }
a { color: #1f6feb; text-decoration: none; }
a:hover { text-decoration: underline; }
.empty { color: #667085; }
"""

# The handful of statistics worth putting on the card; the rest stay in the JSON.
HEADLINE = [
    ("TST", "Total sleep", "min"),
    ("SE", "Efficiency", "%"),
    ("SOL", "Sleep latency", "min"),
    ("Lat_REM", "REM latency", "min"),
    ("%N1", "N1", "%"),
    ("%N2", "N2", "%"),
    ("%N3", "N3", "%"),
    ("%REM", "REM", "%"),
]


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:g}"
    return str(v)


def load_results(out_root: Path) -> list[tuple[str, dict]]:
    """Collect every stats JSON under good/ and failed/, newest first."""
    found = []
    for sub in ("good", "failed"):
        d = out_root / sub
        if not d.is_dir():
            continue
        for j in d.glob("*_stats.json"):
            try:
                found.append((sub, json.loads(j.read_text())))
            except (json.JSONDecodeError, OSError):
                continue
    found.sort(key=lambda t: (t[1].get("start_time") or t[1].get("analyzed_at") or ""),
               reverse=True)
    return found


def render_card(sub: str, r: dict) -> str:
    name = Path(r.get("source", "?")).stem
    status = r.get("status", "ok")
    stats = r.get("stats") or {}
    outs = r.get("outputs") or {}

    bits = []
    if r.get("start_time"):
        bits.append(_esc(r["start_time"].replace("T", " ")[:16]))
    bits.append(f"{r.get('duration_minutes', 0):.0f} min")
    if r.get("staging_channel"):
        bits.append(f"staged on {_esc(r['staging_channel'])}")
    if r.get("reason"):
        bits.append(_esc(r["reason"]))

    chan_badges = "".join(
        f'<span class="badge {c["verdict"]}">{_esc(c["name"])}</span>'
        for c in (r.get("channels") or []) if c.get("verdict") != "usable"
    )

    plots = ""
    for key in ("hypnogram", "proba"):
        if outs.get(key):
            src = f"{sub}/{outs[key]}"
            plots += f'<a href="{_esc(src)}"><img src="{_esc(src)}" alt="{key}" loading="lazy"></a>'
    plots = f'<div class="plots">{plots}</div>' if plots else \
            '<p class="empty">No staging output for this recording.</p>'

    table = ""
    if stats:
        heads, vals = [], []
        for key, label, unit in HEADLINE:
            if key in stats:
                heads.append(f"<th>{_esc(label)}{f' ({unit})' if unit else ''}</th>")
                vals.append(f"<td>{_fmt(stats[key])}</td>")
        if heads:
            table = (f'<div class="stats"><table><tr>{"".join(heads)}</tr>'
                     f'<tr>{"".join(vals)}</tr></table></div>')

    links = []
    for key, label in (("edf", "EDF"), ("stats_txt", "stats.txt"), ("stats_json", "JSON")):
        if outs.get(key):
            links.append(f'<a href="{_esc(sub)}/{_esc(outs[key])}">{label}</a>')
    links_html = f'<p class="meta">{" · ".join(links)}</p>' if links else ""

    return f"""<div class="card">
  <h2>{_esc(name)}<span class="badge {status}">{_esc(status)}</span>{chan_badges}</h2>
  <p class="meta">{" · ".join(bits)}</p>
  {plots}
  {table}
  {links_html}
</div>"""


def write_index(out_root: Path) -> Path:
    results = load_results(out_root)
    ok = sum(1 for s, _ in results if s == "good")

    if results:
        body = "\n".join(render_card(sub, r) for sub, r in results)
    else:
        body = '<p class="empty">No recordings analyzed yet.</p>'

    from datetime import datetime
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Muse Sleep Analysis</title>
<style>{CSS}</style>
</head><body>
<h1>Muse Sleep Analysis</h1>
<p class="sub">{len(results)} night{"s" if len(results) != 1 else ""} analyzed · {ok} usable · generated {generated}</p>
{body}
</body></html>
"""
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / "index.html"
    path.write_text(html)
    return path


if __name__ == "__main__":
    import sys
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/data/output")
    print(write_index(root))
