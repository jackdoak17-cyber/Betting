#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scan Bet365 odds for Over 0.5 Shots for players flagged by hit-rate rules:
- Flag if (price >= 1.30) and (10/10 OR 9/10 OR 5/5)
- Otherwise flag if (price >= 1.72)
- Only include if team Moneyline < 3.5
Reads stats from data/latest/shots_stats_*.json
Saves:
  data/YYYY-MM-DD/odds_shots.json
  data/latest/odds_shots.json
"""

import os, json, time, math, random
from typing import Dict, Any, List, Tuple
from common import (
    LEAGUES, league_slug, latest_dir, run_date_dir,
    http_get_json, CACHE_DIR_ODDS, ODDS_API_KEY
)

BOOKMAKERS = "Bet365"
TEAM_WIN_MAX = 3.50

EVENTS_API_URL = "https://api.odds-api.io/v3/events"
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"
HTTP_HEADERS = {"accept": "application/json"}

def get_events(slugs: List[str]) -> List[dict]:
    all_events=[]
    for slug in slugs:
        r = http_get_json(EVENTS_API_URL, {"apiKey": ODDS_API_KEY, "sport": "football", "league": slug},
                          headers=HTTP_HEADERS, cache_dir=CACHE_DIR_ODDS, use_cache=True)
        if isinstance(r, list):
            print(f"[EVENTS] {slug}: {len(r)}")
            all_events.extend(r)
    return all_events

def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

def odds_multi(ids: List[str]) -> List[dict]:
    if not ids: return []
    r = http_get_json(ODDS_MULTI_API_URL, {
        "apiKey": ODDS_API_KEY,
        "eventIds": ",".join(map(str, ids)),
        "bookmakers": BOOKMAKERS
    }, headers=HTTP_HEADERS, cache_dir=CACHE_DIR_ODDS, use_cache=False)
    return r if isinstance(r, list) else []

def norm(s: str) -> str:
    import unicodedata, re
    s = ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","united","city","saint","st"}
def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb: return True
    if ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5: return True
    if len(inter) >= 2: return True
    return False

def extract_last_initial(name: str):
    parts = (name or "").replace(".", " ").split()
    if not parts: return None, None
    last = norm(parts[-1])
    ini = None
    for p in parts[:-1]:
        if p: ini = p[0].lower(); break
    return last, ini

NEGATIVE_TERMS = {"on target","sot","outside","outside box","outside of box","first half","second half","1st","2nd","assist","goal","goals","headers","header","left foot","right foot","half"}
def market_is_player_shots(name: str) -> bool:
    s = (name or "").lower()
    return ("player" in s and "shot" in s and not any(b in s for b in NEGATIVE_TERMS))

def label_matches_player(scraped: str, label: str) -> bool:
    if not scraped or not label: return False
    last, ini = extract_last_initial(scraped)
    lbl = norm(label)
    if not last or last not in lbl: return False
    if ini:
        # "A Last" or "Last, A"
        if lbl.split()[0][:1] == ini: return True
        import re
        return bool(re.search(rf"\b{ini}\w*\b.*\b{last}\b", lbl))
    return True

def parse_line_value(opt: dict):
    for k in ("line","hdp"):
        v = opt.get(k)
        if v is None: continue
        try: return float(v)
        except: pass
    return None

def min_win_prices(event_odds: dict, home: str, away: str):
    best = {"home": None, "away": None}
    bms = event_odds.get("bookmakers") or {}
    for bm_slug, markets in bms.items():
        if "bet365" not in (bm_slug or "").lower(): continue
        for m in markets or []:
            name = (m.get("name") or "").lower()
            if not any(k in name for k in ["1x2","match result","match winner","moneyline","full time result","to win","win/draw/win","wdw","ml"]):
                continue
            odds = m.get("odds")
            if isinstance(odds, list):
                for opt in odds:
                    label = (opt.get("label") or "")
                    try:
                        price = float(opt.get("over"))
                    except:
                        continue
                    if team_names_match(label, home) or label.strip().lower() in {"home","1"}:
                        if best["home"] is None or price < best["home"]:
                            best["home"] = price
                    if team_names_match(label, away) or label.strip().lower() in {"away","2"}:
                        if best["away"] is None or price < best["away"]:
                            best["away"] = price
            elif isinstance(odds, dict):
                for side in ("home","away"):
                    try:
                        price = float(odds.get(side))
                        if best[side] is None or price < best[side]:
                            best[side] = price
                    except:
                        pass
    return best["home"], best["away"]

def load_all_stats_latest() -> Dict[int, List[dict]]:
    base = latest_dir()
    res={}
    for fname in os.listdir(base):
        if not fname.startswith("shots_stats_") or not fname.endswith(".json"): continue
        lid = int(fname.replace("shots_stats_","").replace(".json",""))
        with open(os.path.join(base, fname), "r", encoding="utf-8") as f:
            res[lid] = json.load(f)
    return res

def main():
    if ODDS_API_KEY == "MISSING":
        print("[ERR] ODDS_API_KEY missing (set repo secret)")
        return

    stats = load_all_stats_latest()
    if not stats:
        print("[ERR] no latest shots stats found.")
        return

    # Build candidate map: league -> {player: meta}
    cands_by_league: Dict[int, Dict[str, dict]] = {}
    for lid, arr in stats.items():
        for r in arr:
            # gating by hit-rate buckets
            ok_133 = (
                (r["apps10"]==10 and r["hit10"]>=90.0) or
                (r["apps10"]==10 and r["hit10"]==100.0) or
                (r["apps5"]==5 and r["hit5"]==100.0)
            )
            ok_172 = (r["apps5"]==5 and r["hit5"]>=80.0) or (r["apps10"]==10 and r["hit10"]>=80.0)
            if not (ok_133 or ok_172):
                continue
            cands_by_league.setdefault(lid,{})
            cands_by_league[lid][r["player_name"]] = r

    # Fetch events and odds
    slugs = [league_slug(lid) for lid in stats.keys() if league_slug(lid)]
    events = get_events(slugs)

    # Group by league
    ev_by_slug={}
    for ev in events:
        ev_by_slug.setdefault(ev.get("league"), []).append(ev)

    flagged = []

    # For each league, scan event odds and player markets
    for lid, players in cands_by_league.items():
        slug = league_slug(lid)
        evs = ev_by_slug.get(slug) or []
        if not evs: continue

        ids = [e.get("id") for e in evs if e.get("id")]
        all_odds=[]
        for batch in chunked(ids, 10):
            all_odds.extend(odds_multi(batch))

        by_id = {o.get("id"): o for o in all_odds if o.get("id")}

        for ev in evs:
            eid = ev.get("id"); home = ev.get("home",""); away = ev.get("away","")
            ev_odds = by_id.get(eid) or {}
            best_home, best_away = min_win_prices(ev_odds, home, away)

            bms = ev_odds.get("bookmakers") or {}
            bet365 = None
            for k,v in bms.items():
                if "bet365" in (k or "").lower():
                    bet365 = v; break
            if not bet365: continue

            team_ml_by_side = {"home": best_home, "away": best_away}

            for m in bet365 or []:
                if not market_is_player_shots(m.get("name","")):
                    continue
                for opt in (m.get("odds") or []):
                    label = opt.get("label") or ""
                    line = parse_line_value(opt)
                    if line is None or not math.isclose(line, 0.5, abs_tol=1e-6):
                        continue
                    try:
                        price = float(opt.get("over"))
                    except:
                        continue
                    # Find matching player
                    for pname, meta in players.items():
                        if not label_matches_player(pname, label):
                            continue
                        # team side based on name match
                        side = "home" if team_names_match(label, home) else ("away" if team_names_match(label, away) else None)
                        team_ml = team_ml_by_side.get(side) if side else None
                        if team_ml is None or team_ml >= TEAM_WIN_MAX:
                            continue

                        ok_133 = (
                            (meta["apps10"]==10 and meta["hit10"]>=90.0) or
                            (meta["apps10"]==10 and meta["hit10"]==100.0) or
                            (meta["apps5"]==5 and meta["hit5"]==100.0)
                        )
                        ok_172 = (meta["apps5"]==5 and meta["hit5"]>=80.0) or (meta["apps10"]==10 and meta["hit10"]>=80.0)

                        if (ok_133 and price >= 1.30) or (ok_172 and price >= 1.72):
                            flagged.append({
                                "league_id": lid,
                                "player": pname,
                                "pos": meta["pos"],
                                "over": 0.5,
                                "price": price,
                                "home": home, "away": away,
                                "team_ml": team_ml,
                                "apps10": meta["apps10"], "hit10": meta["hit10"],
                                "apps5": meta["apps5"], "hit5": meta["hit5"],
                            })

    dd = run_date_dir()
    latest = latest_dir()
    out = sorted(flagged, key=lambda r: (-r["price"], r["player"]))
    with open(os.path.join(dd, "odds_shots.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(os.path.join(latest, "odds_shots.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[DONE] odds flagged: {len(out)}")
    if out:
        print("Top 5:")
        for r in out[:5]:
            print(f"  - {r['player']} ({r['pos']}) {r['home']} vs {r['away']} | O0.5 @{r['price']:.3f} | team ML {r['team_ml']:.2f}")

if __name__ == "__main__":
    main()
