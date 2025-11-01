#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Team win rates (overall + HOME/AWAY splits) for:
  - corners
  - shots_total
  - shots_on_target

Inputs (local only):
  - data/team_stats/by_league/{league_id}.json             (team series; latest -> older)
  - data/team_opponent_stats/by_league/{league_id}.json    (opponent series; latest -> older)
  - data/fixtures/by_league/{league_id}.json  (fallback: data/fixtures/{league_id}.json)
      to map fixture_id -> (home_id, away_id)

Output:
  - data/team_winrates/by_league/{league_id}.json
    {
      "generated_at": "...",
      "league_id": 8,
      "last_n": 10,
      "red_weight": 1.0,
      "teams": [
        {
          "team_name": "Arsenal",
          "last_n": 10,
          "categories": {
            "corners":        { "sequence":[...], "rates":{wins,losses,draws,n,win_rate} },
            "corners_home":   {...}, "corners_away": {...},
            "shots_total":    {...}, "shots_total_home": {...}, "shots_total_away": {...},
            "shots_on_target":{...}, "shots_on_target_home": {...}, "shots_on_target_away": {...}
          }
        }, ...
      ]
    }

ENV (optional):
  LEAGUE_IDS     CSV (default: auto from team_stats dir)
  LAST_N         default 10 (use only latest N fixtures per team when building sequences)
"""

import os, json, datetime as dt, re, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(".")
TS_DIR   = ROOT / "data" / "team_stats" / "by_league"
OPP_DIR  = ROOT / "data" / "team_opponent_stats" / "by_league"
FIX_DIR1 = ROOT / "data" / "fixtures" / "by_league"
FIX_DIR2 = ROOT / "data" / "fixtures"
OUT_DIR  = ROOT / "data" / "team_winrates" / "by_league"
OUT_DIR.mkdir(parents=True, exist_ok=True)

LAST_N = int(os.getenv("LAST_N", "10"))

# --------- util ----------
def load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def discover_league_ids() -> List[int]:
    lids = []
    for p in TS_DIR.glob("*.json"):
        try: lids.append(int(p.stem))
        except: pass
    return sorted(set(lids))

def now_utc_iso() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")

def clamp_list(xs: List[int], n: int) -> List[int]:
    return xs[:n] if isinstance(xs, list) else []

# --------- team-name matching helpers (robust) ----------
GENERIC_TEAM_TOKENS = {
    "fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc",
    "rc","rcd","ca","the","club","de","del","la","las","los","calcio",
    "united","city","saint","st","bk"
}
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')
def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s
def team_tokens(name: str):
    return {t for t in norm(name).split() if t not in GENERIC_TEAM_TOKENS}
def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; uni = ta | tb
    return (len(inter) / max(1, len(uni)) >= 0.5) or (len(inter) >= 2)

# --------- fixture home/away map ----------
def read_fixtures(league_id: int) -> List[dict]:
    blob = load_json(FIX_DIR1 / f"{league_id}.json")
    if not blob:
        blob = load_json(FIX_DIR2 / f"{league_id}.json")
    return blob.get("fixtures") or (blob.get("data") or {}).get("fixtures") or []

def build_home_away_index(fixtures: List[dict]) -> Dict[int, Tuple[Optional[int], Optional[int]]]:
    """
    Return {fixture_id: (home_id, away_id)}
    """
    idx: Dict[int, Tuple[Optional[int], Optional[int]]] = {}
    for fx in fixtures:
        fid = fx.get("id") or fx.get("fixture_id")
        if not isinstance(fid, int): continue
        parts = fx.get("participants") or fx.get("teams") or []
        home_id = away_id = None
        if isinstance(parts, list) and len(parts) >= 2:
            # try to detect by 'meta.location' if present
            for p in parts:
                try:
                    pid = int(p.get("id") or p.get("team_id"))
                except Exception:
                    continue
                loc = ((p.get("meta") or {}).get("location") or "").lower()
                if loc == "home": home_id = pid
                elif loc == "away": away_id = pid
            # fallback: assume order [home, away]
            if home_id is None or away_id is None:
                try:
                    home_id = home_id or int(parts[0].get("id") or parts[0].get("team_id"))
                    away_id = away_id or int(parts[1].get("id") or parts[1].get("team_id"))
                except Exception:
                    pass
        else:
            # some shapes store explicit "home"/"away"
            for key in ("home","localteam"):
                t = fx.get(key)
                if isinstance(t, dict):
                    try: home_id = int(t.get("id") or t.get("team_id"))
                    except Exception: pass
            for key in ("away","visitorteam"):
                t = fx.get(key)
                if isinstance(t, dict):
                    try: away_id = int(t.get("id") or t.get("team_id"))
                    except Exception: pass
        idx[int(fid)] = (home_id, away_id)
    return idx

# --------- core win-rate calc ----------
StatPair = Tuple[str, str]  # (team_key, opp_key)
STAT_PAIRS: Dict[str, StatPair] = {
    "corners": ("corners_last_n", "opp_corners_last_n"),
    "shots_total": ("shots_total_last_n", "opp_shots_total_last_n"),
    "shots_on_target": ("shots_on_target_last_n", "opp_shots_on_target_last_n"),
}

def build_index_by_fixture(team_entry: dict) -> Dict[int, int]:
    """
    Map fixture_id -> index in series for this team entry (latest -> older).
    """
    out: Dict[int, int] = {}
    fids = team_entry.get("fixture_ids") or []
    for i, fid in enumerate(fids):
        try: out[int(fid)] = i
        except Exception: pass
    return out

def outcome(a: Optional[int], b: Optional[int]) -> Optional[str]:
    if a is None or b is None: return None
    if a > b:  return "W"
    if a < b:  return "L"
    return "D"

def sequences_for(team_entry: dict,
                  opp_entry: dict,
                  fixtures_idx: Dict[int, Tuple[Optional[int], Optional[int]]],
                  last_n: int) -> Dict[str, Dict[str, List[str]]]:
    """
    Return sequences per stat:
      {"corners": {"all":[...], "home":[...], "away":[...]}, ...}
    (latest -> older, capped to last_n)
    """
    seqs: Dict[str, Dict[str, List[str]]] = {k: {"all": [], "home": [], "away": []} for k in STAT_PAIRS.keys()}

    idx_team = build_index_by_fixture(team_entry)
    idx_opp  = build_index_by_fixture(opp_entry)

    # iterate team fixtures in latest->older order; cap by last_n additions
    used_counts = {k: 0 for k in STAT_PAIRS.keys()}

    for fid in team_entry.get("fixture_ids") or []:
        try: fid = int(fid)
        except Exception: continue
        if fid not in idx_opp:  # need both sides present
            continue
        home_id, away_id = fixtures_idx.get(fid, (None, None))
        t_id = team_entry.get("team_id")
        side = "home" if (isinstance(t_id, int) and t_id == home_id) else ("away" if (isinstance(t_id, int) and t_id == away_id) else None)

        for stat, (k_team, k_opp) in STAT_PAIRS.items():
            if used_counts[stat] >= last_n:
                continue
            series_team = team_entry.get(k_team) or []
            series_opp  = opp_entry.get(k_opp)  or []
            i_t = idx_team.get(fid); i_o = idx_opp.get(fid)
            if i_t is None or i_o is None: 
                continue
            try:
                a = int(series_team[i_t])
                b = int(series_opp[i_o])
            except Exception:
                continue

            res = outcome(a, b)
            if not res: 
                continue

            seqs[stat]["all"].append(res)
            if side == "home": seqs[stat]["home"].append(res)
            if side == "away": seqs[stat]["away"].append(res)
            used_counts[stat] += 1

        # Early exit if every stat hit last_n
        if all(used_counts[s] >= last_n for s in STAT_PAIRS.keys()):
            break

    # clamp (already in order latest->older)
    for stat in seqs:
        for bucket in ("all","home","away"):
            seqs[stat][bucket] = seqs[stat][bucket][:last_n]
    return seqs

def rates_from_sequence(seq: List[str]) -> Dict[str, float]:
    w = sum(1 for x in seq if x == "W")
    l = sum(1 for x in seq if x == "L")
    d = sum(1 for x in seq if x == "D")
    n = w + l + d
    win_rate = (w / n) if n else 0.0
    return {"wins": w, "losses": l, "draws": d, "n": n, "win_rate": round(win_rate, 4)}

# --------- main ----------
def main():
    env = os.getenv("LEAGUE_IDS", "").strip()
    league_ids = [int(x) for x in env.split(",") if x.strip()] if env else discover_league_ids()

    for lid in league_ids:
        ts_blob  = load_json(TS_DIR / f"{lid}.json")
        opp_blob = load_json(OPP_DIR / f"{lid}.json")
        fixtures = read_fixtures(lid)
        fx_idx   = build_home_away_index(fixtures)

        ts_map: Dict[int, dict] = {int(t["team_id"]): t for t in (ts_blob.get("teams") or []) if isinstance(t.get("team_id"), int)}
        opp_map: Dict[int, dict] = {int(t["team_id"]): t for t in (opp_blob.get("teams") or []) if isinstance(t.get("team_id"), int)}

        out_teams: List[dict] = []

        for tid, team_entry in ts_map.items():
            opp_entry = opp_map.get(tid)
            if not opp_entry:
                continue

            # ensure arrays are capped to LAST_N for safety (we still match by fixture_id)
            for k_team, _ in STAT_PAIRS.values():
                team_entry[k_team] = clamp_list(team_entry.get(k_team) or [], LAST_N)
            for _, k_opp in STAT_PAIRS.values():
                opp_entry[k_opp] = clamp_list(opp_entry.get(k_opp) or [], LAST_N)

            seqs = sequences_for(team_entry, opp_entry, fx_idx, LAST_N)

            cats = {}
            # overall + splits
            for stat in ("corners", "shots_total", "shots_on_target"):
                overall = seqs[stat]["all"]
                home    = seqs[stat]["home"]
                away    = seqs[stat]["away"]
                cats[stat] = {"sequence": overall, "rates": rates_from_sequence(overall)}
                cats[f"{stat}_home"] = {"sequence": home, "rates": rates_from_sequence(home)}
                cats[f"{stat}_away"] = {"sequence": away, "rates": rates_from_sequence(away)}

            out_teams.append({
                "team_name": team_entry.get("team_name") or "",
                "team_id": tid,
                "last_n": LAST_N,
                "categories": cats,
            })

        payload = {
            "generated_at": now_utc_iso(),
            "league_id": lid,
            "last_n": LAST_N,
            "red_weight": 1.0,  # kept for backward compatibility
            "teams": sorted(out_teams, key=lambda r: r["team_name"].lower()),
        }
        (OUT_DIR / f"{lid}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {OUT_DIR / f'{lid}.json'}  (teams={len(out_teams)})")

if __name__ == "__main__":
    main()
