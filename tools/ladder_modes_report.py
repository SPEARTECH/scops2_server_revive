#!/usr/bin/env python3
"""Per-mode leaderboards from state/ladder_stats.json.
Modes are recorded by the server in separate keys (NEU_/SAB_/EXT_ + SCORE/GAMES/WON/TIME); the in-game ranking screen
cannot show them (it asks for the same data on every tab), so this report is the per-mode view.
Usage: python tools/ladder_modes_report.py   -> prints and writes state/ladder_modes.txt"""
import json, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
db = json.loads((root / "state" / "ladder_stats.json").read_text(encoding="utf-8-sig"))
names = {"NEU": "Neutralisation", "SAB": "Sabotage", "EXT": "Extraction", "UNK": "Mode not recorded"}
out = []
for k, title in names.items():
    rows = [(n, int(v.get(k + "_SCORE", 0) or 0), int(v.get(k + "_GAMES", 0) or 0), int(v.get(k + "_WON", 0) or 0))
            for n, v in db.items() if int(v.get(k + "_GAMES", 0) or 0) > 0]
    if not rows and k == "UNK":
        continue
    rows.sort(key=lambda r: (-r[1], r[0].upper()))
    out.append(f"== {title} ==")
    out.append(f"{'Rank':<5}{'Player':<20}{'Points':>8}{'Games':>7}{'Won':>5}")
    for i, (n, sc, g, w) in enumerate(rows, 1):
        out.append(f"{i:<5}{n:<20}{sc:>8}{g:>7}{w:>5}")
    if not rows:
        out.append("(no matches recorded yet)")
    out.append("")
text = "\n".join(out)
print(text)
(root / "state" / "ladder_modes.txt").write_text(text, encoding="utf-8")
