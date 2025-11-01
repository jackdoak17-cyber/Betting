# ========================= scripts/team_win_rates.py =========================
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build per-team win-rate sequences (overall + HOME/AWAY) for key stats.

Inputs (from build-team-series):
  - data/team_stats/by_league/{league_id}.json              (has *_last_n + locations_last_n)
  - data/team_opponent_stats/by_league/{league_id}.json     (has opp_*_last_n + locations_last_n)

Output:
  - data/team_winrates/by_league/{league_id}.json
  - data/team_winrates/summary.txt
"""

import os, json, datetime as dt, re, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional

ROOT = Path(".")
TS_DIR   = ROOT / "data" / "team_stats" / "by_league"
OPP_DIR  = ROOT / "data" / "team_opponent_stats" / "by_league"
OUT_DIR  = ROOT / "data" / "team_winrates"; OUT_DIR.mkdir(parents=True, exist_ok=True)

LEAGUE_IDS = [int(x) for x in os.getenv("LEAGUE_IDS","").split(",") if x.strip()] or None
LAST_N     = int(os.getenv("LAST_N", "10"))
RED_WEIGHT = float(os.getenv("RED_WEIGHT", "1"))

# stats we’ll compute winrates for (overall + home + away)
PAIR_KEYS = [
    ("corners_last_n",       "opp_corners_last_n",       "corners"),
    ("shots_total_last_n",   "opp_shots_total_last_n",   "shots_total"),
    ("shots_on_target_last_n","opp_shots_on_target_last_n","shots_on_target"),
    ("cards_total_last_n",   "opp_cards_total_last_n",   "cards_total"),
]

# ---------- helpers ----------
def read_json(p: Path) -> dict:
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return {}

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def leagues_to_process() -> List[int]:
    if LEAGUE_IDS: return sorted(set(LEAGUE_IDS))
    ids = []
    for p in TS_DIR.glob("*.json"):
        try: ids.append(int(p.stem))
        except: pass
    return sorted(set(ids))

def index_by_id_and_name(blob: dict) -> Tuple[Dict[int,dict], Dict[str,dict]]:
    by_id: Dict[int,dict] = {}
    by_name: Dict[str,dict] = {}
    for t in (blob.get("teams") or []):
        tid = t.get("team_id")
        nm  = t.get("team_name") or ""
        if isinstance(tid, int): by_id[tid] = t
        if nm: by_name[norm(nm)] = t
    return by_id, by_name

def clamp_last_n(arr: List[int], n: int) -> List[int]:
    return list(arr)[:n]

def build_sequences(x: List[int], y: List[int], locs: List[str]):
    """Return overall/home/away W/L/D sequences (as list of 'W'/'L'/'D')."""
    n = min(len(x), len(y), len(locs))
    overall, home, away = [], [], []
    for i in range(n):
        xi, yi = x[i], y[i]
        if xi == yi: mark = 'D'
        elif xi > yi: mark = 'W'
        else: mark = 'L'
        overall.append(mark)
        side = (locs[i] or "").lower()
        if side == "home":
            home.append(mark)
        elif side == "away":
            away.append(mark)
    return overall, home, away

def rates_from_seq(seq: List[str]) -> dict:
    """Compute wins/losses/draws, n (excluding draws), win_rate."""
    w = sum(1 for s in seq if s == 'W')
    l = sum(1 for s in seq if s == 'L')
    d = sum(1 for s in seq if s == 'D')
    n = w + l
    wr = (w / n) if n > 0 else 0.0
    return {"wins": w, "losses": l, "draws": d, "n": n, "win_rate": round(wr, 4)}

# ---------- main ----------
def main():
    lids = leagues_to_process()
    summary_lines = []
    summary_lines.append(f"Time (UTC): {dt.datetime.utcnow().isoformat(timespec='seconds')}")
    total_teams = 0
    for lid in lids:
        ts_blob  = read_json(TS_DIR / f"{lid}.json")
        opp_blob = read_json(OPP_DIR / f"{lid}.json")
        if not ts_blob or not opp_blob:
            continue

        ts_by_id, ts_by_name   = index_by_id_and_name(ts_blob)
        opp_by_id, opp_by_name = index_by_id_and_name(opp_blob)

        out_rows = []

        # iterate teams present in team_stats for this league
        for tid, row in ts_by_id.items():
            tname = row.get("team_name") or ""
            opp_row = opp_by_id.get(tid) or opp_by_name.get(norm(tname))
            if not opp_row:
                continue

            locs = (row.get("locations_last_n") or [])[:LAST_N]
            categories = {}

            for team_key, opp_key, base in PAIR_KEYS:
                xs = [int(v) for v in (row.get(team_key) or []) if isinstance(v, int)]
                ys = [int(v) for v in (opp_row.get(opp_key) or []) if isinstance(v, int)]
                xs = clamp_last_n(xs, LAST_N)
                ys = clamp_last_n(ys, LAST_N)

                overall_seq, home_seq, away_seq = build_sequences(xs, ys, locs)

                categories[base] = {
                    "sequence": overall_seq,
                    "rates": rates_from_seq(overall_seq)
                }
                categories[f"{base}_home"] = {
                    "sequence": home_seq,
                    "rates": rates_from_seq(home_seq)
                }
                categories[f"{base}_away"] = {
                    "sequence": away_seq,
                    "rates": rates_from_seq(away_seq)
                }

            out_rows.append({
                "team_name": tname,
                "team_id": tid,
                "last_n": LAST_N,
                "categories": categories
            })

        out_rows.sort(key=lambda r: (r["team_name"].lower(), r["team_id"]))
        payload = {
            "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            "league_id": lid,
            "last_n": LAST_N,
            "red_weight": RED_WEIGHT,
            "teams": out_rows,
        }
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "by_league").mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / "by_league" / f"{lid}.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        total_teams += len(out_rows)
        summary_lines.append(f"League {lid}: {len(out_rows)} teams")

    summary_lines.insert(1, f"Teams  : {total_teams}")
    (OUT_DIR / "summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    print("\n".join(summary_lines))

if __name__ == "__main__":
    main()
