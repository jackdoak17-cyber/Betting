#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
H2H cache builder (Sportmonks v3) — API-only script, repo-friendly.

Reads upcoming fixtures from:
  - data/fixtures/latest.json    (preferred)
  - otherwise, every data/fixtures/*.json (union of fixtures arrays)

For each unique pair (home_id, away_id) found in fixtures, fetch the last N
head-to-head fixtures and cache them in:
  data/h2h/{minId}_{maxId}.json

Each cache contains per-game metrics:
  - goals (home/away), total goals
  - shots, shots on target, corners, fouls, offsides
  - yellow cards, red cards, possession %

Robust includes:
  Tries include tiers in order, downgrading on include errors:
    1) scores;statistics.type;participants
    2) scores;statistics.type
    3) scores
    4) (no include)
Works even if your plan does not allow certain includes.

Env:
  SPORTMONKS_TOKEN   (required)
  H2H_MATCHES        (default 5)
  H2H_CACHE_HOURS    (default 24)
  SM_TIMEOUT         (default 20)
  SM_SLEEP           (default 0.05)
"""

import os, sys, json, time, re
import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests

ROOT = Path(".")
FIX_DIR = ROOT / "data" / "fixtures"
H2H_DIR = ROOT / "data" / "h2h"
H2H_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = "https://api.sportmonks.com/v3/football"
TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not TOKEN:
    print("ERROR: SPORTMONKS_TOKEN not set", file=sys.stderr)
    sys.exit(1)

LAST_N = int(os.getenv("H2H_MATCHES", "5"))
CACHE_HOURS = int(os.getenv("H2H_CACHE_HOURS", "24"))
TIMEOUT = int(os.getenv("SM_TIMEOUT", "20"))
SLEEP = float(os.getenv("SM_SLEEP", "0.05"))

# ---------- helpers ----------
def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")

def cache_path(a: int, b: int) -> Path:
    lo, hi = (a, b) if a <= b else (b, a)
    return H2H_DIR / f"{lo}_{hi}.json"

def cache_fresh(p: Path) -> bool:
    if not p.exists(): return False
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
        ts = j.get("fetched_at")
        if not ts: return False
        t = dt.datetime.fromisoformat(ts.replace("Z","+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - t) < dt.timedelta(hours=CACHE_HOURS)
    except Exception:
        return False

def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp.replace(path)

def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def discover_fixtures() -> List[dict]:
    latest = FIX_DIR / "latest.json"
    fixtures: List[dict] = []
    if latest.exists():
        fx = load_json(latest).get("fixtures") or []
        if fx: return fx
    # Fallback: union of all fixtures arrays
    for p in FIX_DIR.glob("*.json"):
        if p.name == "latest.json": continue
        blob = load_json(p)
        fixtures.extend(blob.get("fixtures") or [])
    return fixtures

def extract_home_away_ids(fx: dict) -> Tuple[Optional[int], Optional[int]]:
    # Prefer participants include
    parts = fx.get("participants") or []
    home_id = away_id = None
    for p in parts:
        try:
            tid = int(p.get("id"))
        except Exception:
            continue
        loc = ((p.get("meta") or {}).get("location") or "").lower()
        if loc == "home": home_id = tid
        elif loc == "away": away_id = tid
    # Fallback common keys
    if home_id is None:
        for k in ("home_team_id","localteam_id","home_id","localteamid"):
            v = fx.get(k)
            if isinstance(v,(int,str)) and str(v).isdigit():
                home_id = int(v); break
    if away_id is None:
        for k in ("away_team_id","visitorteam_id","away_id","visitorteamid"):
            v = fx.get(k)
            if isinstance(v,(int,str)) and str(v).isdigit():
                away_id = int(v); break
    return home_id, away_id

# ---------- API ----------
INCLUDE_TIERS = [
    "scores;statistics.type;participants",
    "scores;statistics.type",
    "scores",
    "",  # no include
]

def _include_error(j: dict) -> bool:
    # Detect include errors like: {"message":"The requested include 'X' does not exist", ...}
    msg = (j or {}).get("message") or ""
    return "include" in msg.lower() and "does not exist" in msg.lower()

def h2h_fetch(team_a: int, team_b: int, last_n: int) -> List[dict]:
    """
    Try both orders and include tiers. Returns normalized fixtures list (newest first).
    """
    orders = [(team_a, team_b), (team_b, team_a)]
    for a, b in orders:
        for inc in INCLUDE_TIERS:
            params = {"api_token": TOKEN, "per_page": 25, "sort": "-starting_at"}
            if inc: params["include"] = inc
            url = f"{API_BASE}/fixtures/head-to-head/{a}/{b}"
            try:
                r = requests.get(url, params=params, timeout=TIMEOUT)
            except requests.RequestException:
                continue
            if r.status_code != 200:
                # Try downgrading on include error payloads
                try:
                    j = r.json()
                except Exception:
                    j = {}
                if _include_error(j):
                    # downgrade includes
                    continue
                else:
                    # other error
                    continue
            try:
                j = r.json()
            except Exception:
                continue
            rows = j.get("data") or []
            if not isinstance(rows, list): rows = []
            # Sort newest first (defensive)
            rows.sort(key=lambda x: (x.get("starting_at") or ""), reverse=True)
            if rows:
                return rows[:last_n]
            time.sleep(SLEEP)
    return []

# ---------- parsing ----------
def _int(v) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        try:
            return int(float(str(v).replace("%","").strip()))
        except Exception:
            return None

def parse_ft_goals(item: dict) -> Tuple[Optional[int], Optional[int]]:
    """Use 'scores' include if present. Prefer items with description 'CURRENT'."""
    scores = item.get("scores") or []
    if isinstance(scores, list) and scores:
        # Try CURRENT records first
        curr = [s for s in scores if (s.get("description") or "").upper() == "CURRENT"]
        use = curr if curr else scores
        home_g = away_g = None
        # Prefer explicit participant markers
        for s in use:
            sc = s.get("score") or {}
            goals = _int((sc.get("goals")))
            part = (sc.get("participant") or "").lower()
            pid = s.get("participant_id")
            if part == "home":
                home_g = goals if goals is not None else home_g
            elif part == "away":
                away_g = goals if goals is not None else away_g
            # Fallback via ids when available in base object
        # Final fallback: look for any two distinct CURRENT entries by participant_id matching locals
        if home_g is not None or away_g is not None:
            return home_g, away_g
    # Fallbacks using any embedded ft_score-like fields (rare on v3 responses)
    s = item.get("scores") or {}
    if isinstance(s, dict):
        for hk, ak in [("home_score","away_score"), ("localteam_score","visitorteam_score")]:
            if hk in s or ak in s:
                return _int(s.get(hk)), _int(s.get(ak))
    return None, None

# map many label variants -> canonical key
STAT_NAME_MAP: Dict[str, str] = {
    # shots
    "shots": "shots",
    "total shots": "shots",
    "shots total": "shots",
    "shots on target": "sot",
    "shots on goal": "sot",
    "on target": "sot",
    # corners
    "corners": "corners",
    "corner kicks": "corners",
    "corner kick": "corners",
    # fouls
    "fouls": "fouls",
    "fouls committed": "fouls",
    # offsides
    "offsides": "offsides",
    "offside": "offsides",
    # cards
    "yellow cards": "yellow",
    "yellow card": "yellow",
    "red cards": "red",
    "red card": "red",
    "yellowredcards": "red",  # count second yellows as red too
    # possession
    "ball possession": "possession",
    "possession": "possession",
    "possession %": "possession",
}

WANT_KEYS = ["shots","sot","corners","fouls","offsides","yellow","red","possession"]

def normalize_stat_name(s: str) -> Optional[str]:
    key = (s or "").strip().lower()
    return STAT_NAME_MAP.get(key)

def extract_stats(item: dict, home_id: Optional[int], away_id: Optional[int]) -> Tuple[Dict[str, Optional[int]], Dict[str, Optional[int]]]:
    """
    Returns (home_stats, away_stats) dicts for WANT_KEYS.
    """
    home = {k: None for k in WANT_KEYS}
    away = {k: None for k in WANT_KEYS}

    # statistics include
    stats = item.get("statistics") or []
    if isinstance(stats, list):
        # Build per-team aggregations
        by_team: Dict[int, Dict[str, int]] = {}
        for st in stats:
            tname = ((st.get("type") or {}).get("name") or "").strip()
            canon = normalize_stat_name(tname)
            if not canon:  # skip unknown
                continue
            pid = st.get("participant_id")
            data = st.get("data") or {}
            # Prefer explicit integer 'value'; if only percentage (possession), handle gracefully
            if canon == "possession":
                v = _int(data.get("value") if "value" in data else data.get("percentage"))
            else:
                v = _int(data.get("value"))
            if v is None:
                continue
            if pid not in by_team: by_team[pid] = {}
            # Sum yellowredcards into red
            if canon == "red" and "YellowRed" in (tname.replace(" ","")):
                by_team[pid]["red"] = by_team[pid].get("red", 0) + v
            else:
                by_team[pid][canon] = by_team[pid].get(canon, 0) + v

        # assign to home/away by ids
        if home_id in by_team:
            for k in WANT_KEYS:
                if k in by_team[home_id]:
                    home[k] = by_team[home_id][k]
        if away_id in by_team:
            for k in WANT_KEYS:
                if k in by_team[away_id]:
                    away[k] = by_team[away_id][k]

    return home, away

def parse_participants(item: dict) -> Tuple[Optional[int], Optional[int]]:
    # Prefer base fields if present
    hid = item.get("localteam_id") or item.get("home_team_id")
    aid = item.get("visitorteam_id") or item.get("away_team_id")
    hid = _int(hid); aid = _int(aid)
    if hid and aid: return hid, aid
    # Try includes
    parts = item.get("participants") or []
    home = away = None
    for p in parts:
        tid = _int(p.get("id"))
        loc = ((p.get("meta") or {}).get("location") or "").lower()
        if loc == "home": home = tid
        elif loc == "away": away = tid
    return home, away

# ---------- main ----------
def main():
    fixtures = discover_fixtures()
    pairs: List[Tuple[int,int]] = []
    for fx in fixtures:
        hid, aid = extract_home_away_ids(fx)
        if isinstance(hid,int) and isinstance(aid,int):
            lo, hi = (hid,aid) if hid <= aid else (aid,hid)
            pairs.append((lo, hi))
    # uniq
    pairs = sorted(set(pairs))
    print(f"Pairs discovered: {len(pairs)}")
    if not pairs:
        print("No pairs found in data/fixtures. Exiting.")
        return

    updated = 0
    for lo, hi in pairs:
        outp = cache_path(lo, hi)
        if cache_fresh(outp):
            continue

        rows = h2h_fetch(lo, hi, LAST_N)
        last = []
        for it in rows:
            hid, aid = parse_participants(it)
            # If still None, skip this record
            if not (isinstance(hid,int) and isinstance(aid,int)):
                continue

            hg, ag = parse_ft_goals(it)
            home_stats, away_stats = extract_stats(it, hid, aid)

            rec = {
                "fixture_id": _int(it.get("id")),
                "starting_at": it.get("starting_at"),
                "home_id": hid, "away_id": aid,
                "home_goals": hg, "away_goals": ag,
                "total_goals": (hg + ag) if (hg is not None and ag is not None) else None,
                "home": home_stats,
                "away": away_stats,
            }
            last.append(rec)

        # build vectors (latest -> older) for easy post-processing
        def vec(side: str, key: str) -> List[Optional[int]]:
            return [ (r[side] or {}).get(key) for r in last ]

        payload = {
            "pair": [lo, hi],
            "sorted_key": f"{lo}_{hi}",
            "fetched_at": now_iso(),
            "lastN": last,
            "vectors": {
                "home": {
                    "goals":        [r["home_goals"] for r in last],
                    "shots":        vec("home","shots"),
                    "sot":          vec("home","sot"),
                    "corners":      vec("home","corners"),
                    "fouls":        vec("home","fouls"),
                    "offsides":     vec("home","offsides"),
                    "yellow":       vec("home","yellow"),
                    "red":          vec("home","red"),
                    "possession":   vec("home","possession"),
                },
                "away": {
                    "goals":        [r["away_goals"] for r in last],
                    "shots":        vec("away","shots"),
                    "sot":          vec("away","sot"),
                    "corners":      vec("away","corners"),
                    "fouls":        vec("away","fouls"),
                    "offsides":     vec("away","offsides"),
                    "yellow":       vec("away","yellow"),
                    "red":          vec("away","red"),
                    "possession":   vec("away","possession"),
                }
            }
        }

        write_json(outp, payload)
        updated += 1
        time.sleep(SLEEP)

    print(f"Updated caches: {updated}/{len(pairs)} (TTL={CACHE_HOURS}h, N={LAST_N})")

if __name__ == "__main__":
    main()
