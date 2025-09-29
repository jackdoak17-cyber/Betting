#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, math
from typing import Dict, List
from common import DATA_DIR, LEAGUES, LEAGUE_SLUGS, odds_get, EVENTS_API_URL, ODDS_MULTI_API_URL, BOOKMAKERS, team_names_match, strip_accents

REQ10 = 10
REQ5  = 5

def load_rollups(league_id: int) -> List[dict]:
    path = os.path.join(DATA_DIR, f"shots_rollups_{league_id}.jsonl")
    if not os.path.isfile(path): return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try: out.append(json.loads(line))
            except: pass
    return out

def get_events(slug: str) -> List[dict]:
    r = odds_get(EVENTS_API_URL, {"sport":"football","league":slug}, ttl_sec=900)
    return r if isinstance(r, list) else []

def chunked(it, n):
    it = list(it)
    for i in range(0, len(it), n):
        yield it[i:i+n]

def get_odds_multi(ids: List[str]) -> List[dict]:
    if not ids: return []
    r = odds_get(ODDS_MULTI_API_URL, {"eventIds": ",".join(map(str, ids)), "bookmakers": BOOKMAKERS}, ttl_sec=900)
    return r if isinstance(r, list) else []

def market_is_player_shots(name: str) -> bool:
    s = (name or "").lower().strip()
    if not s: return False
    if "player" not in s or "shot" not in s: return False
    bad = ["on target","sot","first half","second half","assist","goal","goals","outside"]
    return not any(b in s for b in bad)

def market_is_match_winner(name: str) -> bool:
    s = (name or "").lower()
    keys = ["1x2","match result","match winner","moneyline","full time result","to win","win/draw/win","wdw","ml"]
    return any(k in s for k in keys)

def parse_line_value(opt) -> float:
    for k in ("line","hdp"):
        v = opt.get(k)
        if v is None: continue
        try: return float(v)
        except: pass
    return float("nan")

def player_label_matches(scraped_name: str, option_label: str) -> bool:
    if not scraped_name or not option_label: return False
    sn = strip_accents(scraped_name).replace(".", " ").strip().split()
    if not sn: return False
    last = sn[-1].lower()
    first_init = sn[0][0].lower() if sn[0] else None
    lab = strip_accents(option_label).lower()
    ok_last = last in lab
    if not ok_last: return False
    if first_init is None: return True
    # try initial somewhere near front
    return first_init in lab.split()[0][:1] or f"{first_init}" in lab

def min_win_prices(event) -> (float, float):
    home = event.get("home",""); away = event.get("away","")
    best = {"home": None, "away": None}
    for bm_slug, markets in (event.get("bookmakers") or {}).items():
        for m in markets or []:
            if not market_is_match_winner(m.get("name","")): continue
            odds = m.get("odds")
            if isinstance(odds, dict):
                for side in ("home","away"):
                    try:
                        p = float(odds.get(side))
                        if best[side] is None or p < best[side]:
                            best[side] = p
                    except: pass
            elif isinstance(odds, list):
                for opt in odds:
                    label = (opt.get("label") or "")
                    try:
                        p = float(opt.get("over"))
                    except:
                        continue
                    if team_names_match(label, home) or label.strip().lower() in ("home","1"):
                        best["home"] = min(best["home"] or p, p)
                    elif team_names_match(label, away) or label.strip().lower() in ("away","2"):
                        best["away"] = min(best["away"] or p, p)
    return best["home"], best["away"]

def main():
    # Collect events once per league
    league_events = {}
    for lid, lname in LEAGUES.items():
        slug = LEAGUE_SLUGS.get(lid)
        evs = get_events(slug) if slug else []
        league_events[lid] = evs
        print(f"[EVENTS] {lname}: {len(evs)}")

    # Build picks per league
    for lid, lname in LEAGUES.items():
        rolls = load_rollups(lid)
        if not rolls: 
            print(f"\n===== {lname} — no rollups, skip =====")
            continue

        # buckets (exclusive priority)
        used = set()
        b10_100, b10_80, b5_100, b5_80, streak3 = [], [], [], [], []

        # helper
        def add_if(rec, bucket):
            b = {
                "player": rec["player_name"],
                "team": rec["team_name"],
                "player_id": rec["player_id"],
                "team_id": rec["team_id"],
                "apps10": rec["apps10"], "hit10": rec["hit10"],
                "apps5": rec["apps5"], "hit5": rec["hit5"],
                "position": rec["position"]
            }
            bucket.append(b)

        # fill buckets
        for r in rolls:
            pid = r["player_id"]
            if pid in used: continue
            if r["apps10"] == REQ10 and r["hit10"] == 100.0:
                add_if(r, b10_100); used.add(pid)
        for r in rolls:
            pid = r["player_id"]
            if pid in used: continue
            if r["apps10"] == REQ10 and r["hit10"] >= 80.0:
                add_if(r, b10_80); used.add(pid)
        for r in rolls:
            pid = r["player_id"]
            if pid in used: continue
            if r["apps5"] == REQ5 and r["hit5"] == 100.0:
                add_if(r, b5_100); used.add(pid)
        for r in rolls:
            pid = r["player_id"]
            if pid in used: continue
            if r["apps5"] == REQ5 and r["hit5"] >= 80.0:
                add_if(r, b5_80); used.add(pid)
        for r in rolls:
            pid = r["player_id"]
            if pid in used: continue
            apps_any = r["apps5"] if r["apps5"] >= 3 else (r["apps10"] if r["apps10"] >= 3 else 0)
            hit_any = r["hit5"] if r["apps5"] >= 3 else (r["hit10"] if r["apps10"] >= 3 else 0.0)
            if apps_any >= 3 and hit_any == 100.0:
                add_if(r, streak3); used.add(pid)

        # map shortlist → events by team names, then scan odds
        evs = league_events.get(lid, [])
        ev_by_match = evs  # list of events that include home/away
        def find_event_for_team(team):
            for ev in ev_by_match:
                if team_names_match(team, ev.get("home","")) or team_names_match(team, ev.get("away","")):
                    return ev
            return None

        # fetch odds payloads for all event ids (in chunks)
        all_ids = sorted({(find_event_for_team(x["team"]) or {}).get("id") for x in (b10_100+b10_80+b5_100+b5_80+streak3)} - {None})
        odds_payloads = []
        for batch in chunked(all_ids, 10):
            odds_payloads.extend(get_odds_multi(batch))
        id2event = {o.get("id"): o for o in odds_payloads if o.get("id")}

        def best_shot_over05_price(player_name: str, event_obj: dict) -> float:
            best = None
            for bm_slug, markets in (event_obj.get("bookmakers") or {}).items():
                for m in markets or []:
                    if not market_is_player_shots(m.get("name","")): continue
                    for opt in (m.get("odds") or []):
                        label = opt.get("label")
                        if not player_label_matches(player_name, label): continue
                        line = parse_line_value(opt)
                        if not (isinstance(line, float) and math.isfinite(line)): continue
                        if abs(line - 0.5) > 1e-6: continue
                        try:
                            price = float(opt.get("over"))
                        except:
                            continue
                        if price in (None, float("nan")): continue
                        if best is None or price > best:
                            best = price
            return best if best is not None else float("nan")

        # price filters
        HI_CONF_MIN = 1.30   # for 10/10 and ≥9/10
        BASE_MIN    = 1.72   # others
        TEAM_WIN_MAX = 3.50

        def team_moneyline_for_team(team: str, ev: dict) -> float:
            h,a = (ev.get("home",""), ev.get("away",""))
            home_ml, away_ml = None, None
            # compute min win
            from math import inf
            best = {"home":inf,"away":inf}
            for _, markets in (ev.get("bookmakers") or {}).items():
                for m in markets or []:
                    if not market_is_match_winner(m.get("name","")): continue
                    odds = m.get("odds")
                    if isinstance(odds, dict):
                        for side in ("home","away"):
                            try:
                                p = float(odds.get(side))
                                best[side] = min(best[side], p)
                            except: pass
                    elif isinstance(odds, list):
                        for opt in odds:
                            label = (opt.get("label") or "")
                            try:
                                p = float(opt.get("over"))
                            except:
                                continue
                            if team_names_match(label, h) or label.strip().lower() in ("home","1"):
                                best["home"] = min(best["home"], p)
                            elif team_names_match(label, a) or label.strip().lower() in ("away","2"):
                                best["away"] = min(best["away"], p)
            if best["home"] == float("inf"): best["home"] = None
            if best["away"] == float("inf"): best["away"] = None
            if team_names_match(team, h): return best["home"]
            if team_names_match(team, a): return best["away"]
            return None

        def print_bucket(title: str, arr: List[dict], hi_conf: bool):
            print(f"\n===== {lname} — {title} =====")
            kept = []
            for rec in arr:
                ev = find_event_for_team(rec["team"])
                if not ev: continue
                ev_full = id2event.get(ev.get("id"))
                if not ev_full: continue
                team_ml = team_moneyline_for_team(rec["team"], ev_full)
                if team_ml is not None and team_ml >= TEAM_WIN_MAX:
                    continue
                price = best_shot_over05_price(rec["player"], ev_full)
                if not (isinstance(price, float) and math.isfinite(price)):
                    continue
                thr = HI_CONF_MIN if hi_conf else BASE_MIN
                if price < thr:
                    continue
                kept.append((rec, ev_full, price, team_ml))
            if not kept:
                print("  (none)")
                return
            kept.sort(key=lambda x: (-x[2], x[0]["player"]))
            for rec, ev_full, price, team_ml in kept:
                h, a = ev_full.get("home",""), ev_full.get("away","")
                print(f"  - {rec['player']} ({rec['position']}) — {h} vs {a}  |  O0.5 @{price:.3f}  |  team ML={team_ml if team_ml is not None else '--'}  |  last10:{rec['apps10']}({rec['hit10']}%), last5:{rec['apps5']}({rec['hit5']}%)")

        # A/B
        print_bucket("Last 10: 10/10 (100%)  (min price>1.30, team ML < 3.50)", b10_100, hi_conf=True)
        print_bucket("Last 10: ≥9/10 (≥90%)  (min price>1.30, team ML < 3.50)", b10_80, hi_conf=True)
        print_bucket("Last 5:  5/5 (100%)  (min price>1.72, team ML < 3.50)", b5_100, hi_conf=False)
        print_bucket("Last 5:  ≥4/5 (≥80%)  (min price>1.72, team ML < 3.50)", b5_80, hi_conf=False)
        print_bucket("Streakers: 100% in ≥3 apps (min price>1.72, team ML < 3.50)", streak3, hi_conf=False)

if __name__ == "__main__":
    main()
