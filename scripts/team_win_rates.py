#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Team Win Rates (local only) — Corners / Shots / SOT / Cards

Reads, per league:
  - data/team_stats/by_league/{league_id}.json
  - data/team_opponent_stats/by_league/{league_id}.json
Builds, per team (latest -> older):
  - W/L/D sequences where W = team stat > opponent stat in that match
  - hit-rates: wins / (wins + losses)   (draws ignored in denominator)

Outputs:
  - data/team_winrates/by_league/{league_id}.json
  - data/team_winrates/summary.txt

ENV (optional):
  LEAGUE_IDS   CSV (default: auto-discover from team_stats/by_league/*.json)
  LAST_N       clamp sequences to last N entries (default 10)
  RED_WEIGHT   when cards are split (yellow/red), count cards = YC + RED_WEIGHT * RC  (default 1)
  OUT_DIR      default data/team_winrates
"""

import os, json, re, datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# -------- config --------
LAST_N     = int(os.getenv("LAST_N", "10"))
RED_WEIGHT = float(os.getenv("RED_WEIGHT", "1"))
OUT_DIR    = Path(os.getenv("OUT_DIR", "data/team_winrates"))

TS_DIR   = Path("data/team_stats/by_league")
OPP_DIR  = Path("data/team_opponent_stats/by_league")
OUT_DIR.mkdir(parents=True, exist_ok=True)
(OUT_DIR / "by_league").mkdir(parents=True, exist_ok=True)

# -------- utils --------
def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _discover_league_ids() -> List[int]:
    lids = []
    for p in TS_DIR.glob("*.json"):
        try:
            lids.append(int(p.stem))
        except Exception:
            pass
    return sorted(set(lids))

def _norm_nums(arr) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for x in (arr or []):
        try:
            out.append(float(x))
        except Exception:
            out.append(None)
    return out

def _sum_series(a: List[Optional[float]], b: List[Optional[float]]) -> List[Optional[float]]:
    m = min(len(a), len(b))
    out: List[Optional[float]] = []
    for i in range(m):
        va, vb = a[i], b[i]
        if va is None and vb is None:
            out.append(None)
        elif va is None:
            out.append(None)
        elif vb is None:
            out.append(None)
        else:
            out.append(va + vb)
    return out

def _take_last_n(arr: List[Any], n: int) -> List[Any]:
    return (arr or [])[:n] if n > 0 else (arr or [])

def _cmp_code(a: Optional[float], b: Optional[float]) -> Optional[str]:
    if a is None or b is None:
        return None
    if a > b: return "W"
    if a < b: return "L"
    return "D"

def _rate(seq: List[str]) -> Dict[str, Any]:
    w = sum(1 for x in seq if x == "W")
    l = sum(1 for x in seq if x == "L")
    d = sum(1 for x in seq if x == "D")
    n = w + l  # draws excluded
    rate = (w / n) if n > 0 else 0.0
    return {"wins": w, "losses": l, "draws": d, "n": n, "win_rate": round(rate, 4)}

# -------- key mapping (robust to naming differences) --------
TEAM_KEYS = {
    "corners": ["corners_last_n"],
    "shots_total": ["shots_total_last_n", "shots_last_n"],
    "shots_on_target": ["shots_on_target_last_n", "sot_last_n"],
    # cards: prefer combined, else compose from yellow/red
    "cards_combined": ["cards_last_n"],
    "cards_yellow":   ["yellow_cards_last_n", "bookings_last_n", "yellows_last_n"],
    "cards_red":      ["red_cards_last_n", "reds_last_n"],
}

OPP_KEYS = {
    "corners": ["opp_corners_last_n"],
    "shots_total": ["opp_shots_total_last_n", "opp_shots_last_n"],
    "shots_on_target": ["opp_shots_on_target_last_n", "opp_sot_last_n"],
    "cards_combined": ["opp_cards_last_n"],
    "cards_yellow":   ["opp_yellow_cards_last_n", "opp_bookings_last_n", "opp_yellows_last_n"],
    "cards_red":      ["opp_red_cards_last_n", "opp_reds_last_n"],
}

def _first_series(rec: dict, keys: List[str]) -> Optional[List[Optional[float]]]:
    for k in keys:
        if k in rec and isinstance(rec[k], list) and rec[k]:
            return _norm_nums(rec[k])
    return None

def series_team(rec: dict, cat: str) -> Optional[List[Optional[float]]]:
    if cat == "cards":
        s = _first_series(rec, TEAM_KEYS["cards_combined"])
        if s is not None:
            return s
        yc = _first_series(rec, TEAM_KEYS["cards_yellow"]) or []
        rc = _first_series(rec, TEAM_KEYS["cards_red"]) or []
        if yc or rc:
            if not yc: yc = [None] * len(rc)
            if not rc: rc = [None] * len(yc)
            # cards = YC + RED_WEIGHT * RC
            rcw = [ (x * RED_WEIGHT) if x is not None else None for x in rc ]
            return _sum_series(yc, rcw)
        return None
    elif cat == "shots_total":
        s = _first_series(rec, TEAM_KEYS["shots_total"])
        if s is not None:
            return s
        # as a fallback, total = SOT + SOFF if available
        sot = _first_series(rec, TEAM_KEYS["shots_on_target"]) or []
        soff = _first_series(rec, ["shots_off_target_last_n"]) or []
        if sot or soff:
            if not sot:  sot  = [None] * len(soff)
            if not soff: soff = [None] * len(sot)
            return _sum_series(sot, soff)
        return None
    elif cat == "shots_on_target":
        return _first_series(rec, TEAM_KEYS["shots_on_target"])
    elif cat == "corners":
        return _first_series(rec, TEAM_KEYS["corners"])
    else:
        return None

def series_opp(rec: dict, cat: str) -> Optional[List[Optional[float]]]:
    if cat == "cards":
        s = _first_series(rec, OPP_KEYS["cards_combined"])
        if s is not None:
            return s
        yc = _first_series(rec, OPP_KEYS["cards_yellow"]) or []
        rc = _first_series(rec, OPP_KEYS["cards_red"]) or []
        if yc or rc:
            if not yc: yc = [None] * len(rc)
            if not rc: rc = [None] * len(yc)
            rcw = [ (x * RED_WEIGHT) if x is not None else None for x in rc ]
            return _sum_series(yc, rcw)
        return None
    elif cat == "shots_total":
        s = _first_series(rec, OPP_KEYS["shots_total"])
        if s is not None:
            return s
        sot = _first_series(rec, OPP_KEYS["shots_on_target"]) or []
        soff = _first_series(rec, ["opp_shots_off_target_last_n"]) or []
        if sot or soff:
            if not sot:  sot  = [None] * len(soff)
            if not soff: soff = [None] * len(sot)
            return _sum_series(sot, soff)
        return None
    elif cat == "shots_on_target":
        return _first_series(rec, OPP_KEYS["shots_on_target"])
    elif cat == "corners":
        return _first_series(rec, OPP_KEYS["corners"])
    else:
        return None

def build_team_row(team_rec: dict, opp_rec: dict) -> Optional[dict]:
    team_name = team_rec.get("team_name") or opp_rec.get("team_name")
    if not team_name:
        return None

    out = {"team_name": team_name, "last_n": LAST_N, "categories": {}}
    for cat in ("corners", "shots_total", "shots_on_target", "cards"):
        s_team = series_team(team_rec, cat) or []
        s_opp  = series_opp(opp_rec, cat) or []
        m = min(len(s_team), len(s_opp))
        if m == 0:
            continue
        seq: List[str] = []
        for i in range(m):
            code = _cmp_code(s_team[i], s_opp[i])
            if code is not None:
                seq.append(code)
        seq = _take_last_n(seq, LAST_N)
        out["categories"][cat] = {
            "sequence": seq,                          # e.g. ["W","L","D","W",...]
            "rates": _rate(seq),                      # wins/losses/draws/n/win_rate
        }
    return out

def main():
    # league ids to process
    env = os.getenv("LEAGUE_IDS", "").strip()
    if env:
        league_ids = [int(x) for x in env.split(",") if x.strip()]
    else:
        league_ids = _discover_league_ids()

    summary_lines = [f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}",""]

    for lid in league_ids:
        ts_path  = TS_DIR  / f"{lid}.json"
        opp_path = OPP_DIR / f"{lid}.json"
        ts_blob   = _load_json(ts_path)
        opp_blob  = _load_json(opp_path)
        ts_teams  = ts_blob.get("teams") or []
        opp_teams = opp_blob.get("teams") or []

        # index opponent stats by normalized team name (simple lower/no-space normalization)
        def key(name: str) -> str:
            s = (name or "").lower()
            s = re.sub(r"\s+", " ", s).strip()
            return s

        opp_idx: Dict[str, dict] = { key(t.get("team_name","")): t for t in opp_teams if t.get("team_name") }

        rows: List[dict] = []
        for t in ts_teams:
            nm = t.get("team_name")
            if not nm: 
                continue
            o = opp_idx.get(key(nm))
            if not o:
                # try a loose fallback: remove punctuation
                nm2 = re.sub(r"[^a-z0-9 ]","", key(nm))
                found = None
                for k,v in opp_idx.items():
                    if re.sub(r"[^a-z0-9 ]","", k) == nm2:
                        found = v; break
                o = found
            if not o:
                continue
            row = build_team_row(t, o)
            if row:
                rows.append(row)

        payload = {
            "generated_at": dt.datetime.utcnow().isoformat(),
            "league_id": lid,
            "last_n": LAST_N,
            "red_weight": RED_WEIGHT,
            "teams": rows,
            "count": len(rows),
        }
        out_path = OUT_DIR / "by_league" / f"{lid}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        summary_lines.append(f"League {lid}: {len(rows)} teams written")
    (OUT_DIR / "summary.txt").write_text("\n".join(summary_lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(summary_lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
