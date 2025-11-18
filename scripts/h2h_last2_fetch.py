#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
H2H cache + per-league bundles (Sportmonks v3)

- Reads upcoming fixtures from:
    data/fixtures/latest.json  (preferred)
    or union of data/fixtures/*.json  (fallback)
- For each unique team pair from those fixtures:
    * Fetch/refresh H2H last-N fixtures (TTL-based) into:
        data/h2h/{minId}_{maxId}.json
- ALWAYS writes per-league aggregates built FROM the caches:
    data/h2h/by_league/{league_id}.json

Env:
  SPORTMONKS_TOKEN   (required)
  H2H_MATCHES        (default 5)    # how many past meetings to keep in each pair cache
  H2H_CACHE_HOURS    (default 24)   # TTL before a pair cache is refreshed
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
H2H_BY_LEAGUE_DIR = H2H_DIR / "by_league"
H2H_DIR.mkdir(parents=True, exist_ok=True)
H2H_BY_LEAGUE_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = "https://api.sportmonks.com/v3/football"
TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not TOKEN:
    print("ERROR: SPORTMONKS_TOKEN not set", file=sys.stderr)
    sys.exit(1)

LAST_N = int(os.getenv("H2H_MATCHES", "5"))
CACHE_HOURS = int(os.getenv("H2H_CACHE_HOURS", "24"))
TIMEOUT = int(os.getenv("SM_TIMEOUT", "20"))
SLEEP = float(os.getenv("SM_SLEEP", "0.05"))

# ---------- fixtures IO ----------
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
        if fx:
            return fx
    for p in FIX_DIR.glob("*.json"):
        if p.name == "latest.json":
            continue
        fixtures.extend(load_json(p).get("fixtures") or [])
    return fixtures

def extract_ids_names(fx: dict) -> Tuple[Optional[int], Optional[int], Optional[str], Optional[str], Optional[int], Optional[str]]:
    """
    Return (home_id, away_id, home_name, away_name, league_id, starting_at)
    """
    parts = fx.get("participants") or []
    home_id = away_id = None
    home_name = away_name = None
    for p in parts:
        try:
            tid = int(p.get("id"))
        except Exception:
            continue
        loc = ((p.get("meta") or {}).get("location") or "").lower()
        if loc == "home":
            home_id = tid
            home_name = p.get("name")
        elif loc == "away":
            away_id = tid
            away_name = p.get("name")
    # fallbacks
    def to_int(v):
        try: return int(v)
        except: return None
    if home_id is None:
        for k in ("home_team_id","localteam_id","home_id","localteamid"):
            v = fx.get(k)
            if isinstance(v, (int,str)) and str(v).isdigit():
                home_id = int(v); break
    if away_id is None:
        for k in ("away_team_id","visitorteam_id","away_id","visitorteamid"):
            v = fx.get(k)
            if isinstance(v, (int,str)) and str(v).isdigit():
                away_id = int(v); break
    league_id = to_int(fx.get("league_id"))
    starting_at = fx.get("starting_at")
    # try names from name "A vs B" if missing
    if not (home_name and away_name) and isinstance(fx.get("name"), str):
        name = fx["name"]
        for sep in (" vs ", " v ", " - ", " VS ", " Vs "):
            if sep in name:
                a, b = name.split(sep, 1)
                home_name = home_name or a.strip()
                away_name = away_name or b.strip()
                break
    return home_id, away_id, home_name, away_name, league_id, starting_at

# ---------- cache helpers ----------
def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")

def pair_key(a: int, b: int) -> Tuple[int,int]:
    return (a,b) if a <= b else (b,a)

def cache_path(a: int, b: int) -> Path:
    lo, hi = pair_key(a,b)
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

def write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp.replace(path)

# ---------- API pulling ----------
INCLUDE_TIERS = [
    "scores;statistics.type;participants",
    "scores;statistics.type",
    "scores",
    "",
]

def include_is_invalid(j: dict) -> bool:
    msg = (j or {}).get("message") or ""
    return "include" in msg.lower() and "does not exist" in msg.lower()

def _to_int(v):
    try: return int(v)
    except Exception:
        try:
            s = str(v).replace("%","").strip()
            return int(float(s))
        except Exception:
            return None

def parse_participants_from_item(it: dict) -> Tuple[Optional[int], Optional[int]]:
    hid = it.get("localteam_id") or it.get("home_team_id")
    aid = it.get("visitorteam_id") or it.get("away_team_id")
    hid = _to_int(hid); aid = _to_int(aid)
    if hid and aid: return hid, aid
    parts = it.get("participants") or []
    home = away = None
    for p in parts:
        tid = _to_int(p.get("id"))
        loc = ((p.get("meta") or {}).get("location") or "").lower()
        if loc == "home": home = tid
        elif loc == "away": away = tid
    return home, away

def parse_ft_goals(it: dict) -> Tuple[Optional[int], Optional[int]]:
    scores = it.get("scores") or []
    if isinstance(scores, list) and scores:
        curr = [s for s in scores if str(s.get("description") or "").upper() == "CURRENT"]
        use = curr if curr else scores
        home_g = away_g = None
        for s in use:
            sc = s.get("score") or {}
            g = _to_int(sc.get("goals"))
            part = (sc.get("participant") or "").lower()
            if part == "home" and g is not None:
                home_g = g
            elif part == "away" and g is not None:
                away_g = g
        if home_g is not None or away_g is not None:
            return home_g, away_g
    # lenient fallbacks if provider shape differs
    sdict = it.get("scores") or {}
    if isinstance(sdict, dict):
        for hk, ak in [("home_score","away_score"), ("localteam_score","visitorteam_score")]:
            if hk in sdict or ak in sdict:
                return _to_int(sdict.get(hk)), _to_int(sdict.get(ak))
    return None, None

STAT_MAP = {
    # name in API -> canonical key
    "shots": "shots", "total shots": "shots", "shots total": "shots",
    "shots on target": "sot", "shots on goal": "sot", "on target": "sot",
    "corners": "corners", "corner kicks": "corners", "corner kick": "corners",
    "fouls": "fouls", "fouls committed": "fouls",
    "offsides": "offsides", "offside": "offsides",
    "yellow cards": "yellow", "yellow card": "yellow",
    "red cards": "red", "red card": "red", "yellowredcards": "red",
    "ball possession": "possession", "possession": "possession", "possession %": "possession",
}
WANT_KEYS = ["shots","sot","corners","fouls","offsides","yellow","red","possession"]

def canon_stat_name(s: str) -> Optional[str]:
    return STAT_MAP.get((s or "").strip().lower())

def extract_home_away_stats(it: dict, home_id: Optional[int], away_id: Optional[int]) -> Tuple[Dict[str, Optional[int]], Dict[str, Optional[int]]]:
    home = {k: None for k in WANT_KEYS}
    away = {k: None for k in WANT_KEYS}
    stats = it.get("statistics") or []
    if isinstance(stats, list):
        by_team: Dict[int, Dict[str,int]] = {}
        for st in stats:
            tname = ((st.get("type") or {}).get("name") or "")
            key = canon_stat_name(tname)
            if not key:
                continue
            pid = _to_int(st.get("participant_id"))
            data = st.get("data") or {}
            if key == "possession":
                v = _to_int(data.get("value") if "value" in data else data.get("percentage"))
            else:
                v = _to_int(data.get("value"))
            if v is None:
                continue
            if pid not in by_team: by_team[pid] = {}
            # treat yellow-red as red increment
            if key == "red" and "YellowRed" in tname.replace(" ",""):
                by_team[pid]["red"] = by_team[pid].get("red", 0) + v
            else:
                by_team[pid][key] = by_team[pid].get(key, 0) + v
        if home_id in by_team:
            for k in WANT_KEYS:
                if k in by_team[home_id]:
                    home[k] = by_team[home_id][k]
        if away_id in by_team:
            for k in WANT_KEYS:
                if k in by_team[away_id]:
                    away[k] = by_team[away_id][k]
    return home, away

def h2h_fetch(a: int, b: int, last_n: int) -> List[dict]:
    for x, y in [(a,b), (b,a)]:
        for inc in INCLUDE_TIERS:
            params = {"api_token": TOKEN, "per_page": 25, "sort": "-starting_at"}
            if inc: params["include"] = inc
            url = f"{API_BASE}/fixtures/head-to-head/{x}/{y}"
            try:
                r = requests.get(url, params=params, timeout=TIMEOUT)
            except requests.RequestException:
                continue
            if r.status_code != 200:
                try:
                    j = r.json()
                except Exception:
                    j = {}
                if include_is_invalid(j):
                    continue  # downgrade includes
                else:
                    continue
            try:
                j = r.json()
            except Exception:
                continue
            rows = j.get("data") or []
            rows.sort(key=lambda z: (z.get("starting_at") or ""), reverse=True)
            if rows:
                return rows[:last_n]
            time.sleep(SLEEP)
    return []

# ---------- main ----------
def main():
    fixtures = discover_fixtures()
    if not fixtures:
        print("No fixtures found in data/fixtures. Exiting.")
        return

    # Build per-league list of fixtures + global unique pairs
    league_fixtures: Dict[int, List[dict]] = {}
    pairs: List[Tuple[int,int]] = []
    for fx in fixtures:
        hid, aid, hn, an, lid, starting_at = extract_ids_names(fx)
        if not (isinstance(hid,int) and isinstance(aid,int) and isinstance(lid,int)):
            continue
        lo, hi = pair_key(hid, aid)
        pairs.append((lo, hi))
        league_fixtures.setdefault(lid, []).append({
            "pair_key": f"{lo}_{hi}",
            "home_id": hid, "home_name": hn,
            "away_id": aid, "away_name": an,
            "starting_at": starting_at,
            "fixture_id": fx.get("id") or fx.get("fixture_id"),
        })
    pairs = sorted(set(pairs))

    print(f"Pairs discovered: {len(pairs)}")

    # Refresh caches if stale
    updated = 0
    for lo, hi in pairs:
        outp = cache_path(lo, hi)
        if cache_fresh(outp):
            continue
        rows = h2h_fetch(lo, hi, LAST_N)
        last = []
        for it in rows:
            hid, aid = parse_participants_from_item(it)
            if not (isinstance(hid,int) and isinstance(aid,int)):
                continue
            hg, ag = parse_ft_goals(it)
            home_stats, away_stats = extract_home_away_stats(it, hid, aid)
            last.append({
                "fixture_id": _to_int(it.get("id")),
                "starting_at": it.get("starting_at"),
                "home_id": hid, "away_id": aid,
                "home_goals": hg, "away_goals": ag,
                "total_goals": (hg + ag) if (hg is not None and ag is not None) else None,
                "home": home_stats,
                "away": away_stats,
            })

        def vec(side: str, key: str) -> List[Optional[int]]:
            return [ (r[side] or {}).get(key) for r in last ]

        payload = {
            "pair": [lo, hi],
            "sorted_key": f"{lo}_{hi}",
            "fetched_at": now_iso(),
            "lastN": last,
            "vectors": {
                "home": {
                    "goals":      [r["home_goals"] for r in last],
                    "shots":      vec("home","shots"),
                    "sot":        vec("home","sot"),
                    "corners":    vec("home","corners"),
                    "fouls":      vec("home","fouls"),
                    "offsides":   vec("home","offsides"),
                    "yellow":     vec("home","yellow"),
                    "red":        vec("home","red"),
                    "possession": vec("home","possession"),
                },
                "away": {
                    "goals":      [r["away_goals"] for r in last],
                    "shots":      vec("away","shots"),
                    "sot":        vec("away","sot"),
                    "corners":    vec("away","corners"),
                    "fouls":      vec("away","fouls"),
                    "offsides":   vec("away","offsides"),
                    "yellow":     vec("away","yellow"),
                    "red":        vec("away","red"),
                    "possession": vec("away","possession"),
                }
            }
        }
        write_json_atomic(outp, payload)
        updated += 1
        time.sleep(SLEEP)

    # ---- Build per-league bundles from caches (ALWAYS write) ----
    bundles_written = 0
    for lid, fxs in league_fixtures.items():
        # de-duplicate fixtures per league by pair_key + starting_at (stable)
        seen = set()
        deduped = []
        for row in sorted(fxs, key=lambda r: (r.get("starting_at") or "", r.get("pair_key") or "")):
            key = (row.get("pair_key"), row.get("starting_at"), row.get("fixture_id"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)

        out_fixtures: List[dict] = []
        for row in deduped:
            lo, hi = map(int, row["pair_key"].split("_"))
            cpath = cache_path(lo, hi)
            cache = load_json(cpath) if cpath.exists() else {}
            out_fixtures.append({
                **row,
                "cache_present": bool(cache),
                # keep only what downstream consumers need (vectors; optional lastN meta)
                "vectors": cache.get("vectors") or {},
                "lastN_meta": [
                    {
                        "starting_at": it.get("starting_at"),
                        "home_goals": it.get("home_goals"),
                        "away_goals": it.get("away_goals"),
                    } for it in (cache.get("lastN") or [])
                ],
                "fetched_at": cache.get("fetched_at"),
            })

        league_payload = {
            "generated_at": now_iso(),
            "league_id": lid,
            "match_count": len(out_fixtures),
            "fixtures": out_fixtures,
        }
        outp = H2H_BY_LEAGUE_DIR / f"{lid}.json"
        write_json_atomic(outp, league_payload)
        bundles_written += 1

    print(f"Updated caches: {updated}/{len(pairs)} (TTL={CACHE_HOURS}h, N={LAST_N})")
    print(f"Wrote league bundles: {bundles_written} -> {H2H_BY_LEAGUE_DIR}/<league_id>.json")

if __name__ == "__main__":
    main()
