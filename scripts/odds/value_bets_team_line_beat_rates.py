#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Team Line Beat Rates — find current team markets (Shots, SOT, Corners, Tackles),
compute how often the team beats the bookmaker line (Over/Under) based on:
  • Team offense last-N
  • Opponent-allowed last-N
Rank by a conservative combined % = min(team_rate, opp_allowed_rate).
Filter to prices >= MIN_DEC_PRICE.

Inputs:
  - data/team_lines/by_league/*.json  (contains series_used: offense_lastN / opponent_allowed_lastN)

ENV (set by workflow):
  ODDS_API_KEY (required)
  MIN_DEC_PRICE (default 1.20)
  WINDOW_DAYS (default 7)
  BOOKMAKERS (default Bet365)
"""

import os, re, json, math, time, random, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from itertools import islice
import requests

# ===== Config =====
SPORT = "football"
BOOKMAKERS = os.getenv("BOOKMAKERS", "Bet365")
MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.20"))
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "7"))  # 0 = no time filter
TIMEOUT = 25
HTTP_HEADERS = {"accept": "application/json", "user-agent": "team-line-beat/1.0"}

ROOT = Path(".")
LINES_DIR = ROOT / "data" / "team_lines" / "by_league"

EVENTS_API_URL     = "https://api.odds-api.io/v3/events"
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"

# ===== League mapping (SportMonks -> Odds API slug) =====
LEAGUE_SLUG_BY_ID = {
    8:   "england-premier-league",
    9:   "england-championship",
    82:  "germany-bundesliga",
    301: "france-ligue-1",
    384: "italy-serie-a",
    387: "italy-serie-b",
    564: "spain-laliga",
    567: "spain-laliga-2",
    72:  "netherlands-eredivisie",
    600: "turkiye-super-lig",
}

# ===== Team markets we consider =====
TEAM_MARKETS = {
    "team shots": "shots",
    "team shots on target": "shots_on_target",
    "team corners": "corners",
    "team tackles": "tackles",
}

# ===== String helpers =====
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

TEAM_ALIAS = {
    "man city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "spurs": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "west brom": "west bromwich albion",
    "psv": "psv eindhoven",
    "st pauli": "fc st. pauli",
    "inter milan": "inter milano",
    "bayern munich": "bayern munich",
    "forest": "nottingham forest",
    "betis": "real betis",
    "sociedad": "real sociedad",
    "celta": "rc celta de vigo",
    "deportivo alaves": "deportivo alaves",
}
GENERIC_TEAM_TOKENS = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}

def expand_alias(name: str) -> str:
    s = norm(name)
    for k, v in TEAM_ALIAS.items():
        if k in s: return v
    return s

def team_tokens(name: str):
    toks = set(expand_alias(name).split())
    return {t for t in toks if t not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb: return True
    if ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5: return True
    if len(inter) >= 1 and ("madrid" in inter or "milan" in inter or "inter" in inter): return True
    return False

# ===== HTTP helpers =====
def chunked(it, n):
    it = iter(it)
    while True:
        batch = list(islice(it, n))
        if not batch: return
        yield batch

def http_get_with_retries(url: str, params: dict, max_retries=6, base_sleep=1.0, factor=1.8):
    attempt = 0; last_text = ""
    while attempt < max_retries:
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=TIMEOUT)
            if r.status_code == 200: return r
            if r.status_code in (429,500,502,503,504):
                last_text = r.text
                sleep = base_sleep*(factor**attempt)+random.uniform(0,0.4)
                print(f"[RETRY] {url} {r.status_code}; sleeping {sleep:.1f}s...")
                time.sleep(sleep); attempt += 1; continue
            print(f"[HTTP {r.status_code}] {url} :: {r.text[:200]}")
            return None
        except requests.exceptions.RequestException as e:
            sleep = base_sleep*(factor**attempt)+random.uniform(0,0.4)
            print(f"[NET] {url} exception: {e}; sleeping {sleep:.1f}s...")
            time.sleep(sleep); attempt += 1
    if last_text:
        print(f"[ERROR] Retries exhausted for {url}. Last body: {last_text[:220]}")
    else:
        print(f"[ERROR] Retries exhausted for {url}.")
    return None

def get_events_for_league(slug: str, api_key: str) -> List[dict]:
    r = http_get_with_retries(EVENTS_API_URL, {"apiKey": api_key, "sport": SPORT, "league": slug})
    if not (r and r.status_code == 200): return []
    try: data = r.json()
    except: data = None
    return data if isinstance(data, list) else []

def within_next_days(ev: dict, days: int) -> bool:
    if not days: return True
    ds = ev.get("date") or ""
    try:
        dt_utc = dt.datetime.fromisoformat(ds.replace("Z", "+00:00"))
        now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        return now <= dt_utc <= (now + dt.timedelta(days=days))
    except Exception:
        return False

def get_odds_multi(event_ids: List[int], api_key: str) -> List[dict]:
    if not event_ids: return []
    r = http_get_with_retries(
        ODDS_MULTI_API_URL,
        {"apiKey": api_key, "eventIds": ",".join(map(str, event_ids)), "bookmakers": BOOKMAKERS}
    )
    if not (r and r.status_code == 200): return []
    try: data = r.json()
    except: return []
    return data if isinstance(data, list) else []

# ===== Odds parsing =====
def bet365_markets(ev: dict):
    for bm_name, markets in (ev.get("bookmakers") or {}).items():
        if "bet365" not in (bm_name or "").lower(): continue
        for m in markets or []:
            yield m

def market_side_and_stat(name: str) -> Tuple[Optional[str], Optional[str]]:
    s = norm(name)
    side = None
    if s.endswith(" home") or " home" in s:
        side = "home"; s = s.replace(" home", "").strip()
    elif s.endswith(" away") or " away" in s:
        side = "away"; s = s.replace(" away", "").strip()
    # exact stat match
    if s in TEAM_MARKETS:
        return side, TEAM_MARKETS[s]
    return None, None

def parse_line(opt: dict) -> Optional[float]:
    if isinstance(opt, dict) and "hdp" in opt:
        try: return float(opt["hdp"])
        except: return None
    label = (opt.get("label") if isinstance(opt, dict) else None) or ""
    m = re.search(r"\(([-+]?\d+(?:\.\d+)?)\)", label)
    if m:
        try: return float(m.group(1))
        except: return None
    return None

def parse_price(val) -> Optional[float]:
    try:
        if val in (None, "N/A"): return None
        return float(val)
    except Exception:
        return None

# ===== Team-lines loader =====
def iter_team_threshold_series(lines_blob: dict, league_id: int):
    """
    Yield per-team stat context:
      dict(meta, team_side, team_name, opp_name, stat, offense_lastN, opp_allowed_lastN)
    """
    for fx in (lines_blob.get("fixtures") or []):
        meta = {
            "fixture_id": fx.get("fixture_id"),
            "league_id": fx.get("league_id") or league_id,
            "starting_at": fx.get("starting_at"),
            "home_name": fx.get("home_name"),
            "away_name": fx.get("away_name"),
        }
        teams = fx.get("teams") or {}
        for side in ("home","away"):
            t = (teams.get(side) or {})
            opp_side = "away" if side == "home" else "home"
            opp = (teams.get(opp_side) or {})
            team_name = t.get("name") or t.get("team_name") or (meta["home_name"] if side=="home" else meta["away_name"])
            opp_name  = opp.get("name") or opp.get("team_name") or (meta["away_name"] if side=="home" else meta["home_name"])

            stats = (t.get("stats") or {})
            for stat_key, m in stats.items():
                if stat_key not in ("shots","shots_on_target","corners","tackles"):
                    continue
                ser = (m.get("series_used") or {})
                off = ser.get("offense_lastN") or []
                oppA = ser.get("opponent_allowed_lastN") or []
                if not off and not oppA:
                    continue
                yield {
                    "meta": meta,
                    "side": side,
                    "team_name": team_name or "",
                    "opp_name": opp_name or "",
                    "stat": stat_key,
                    "offense_lastN": [x for x in off if isinstance(x, int)],
                    "opp_allowed_lastN": [x for x in oppA if isinstance(x, int)],
                }

# ===== Rates =====
def over_hits(seq: List[int], line: float) -> Tuple[int,int,float]:
    if not seq: return 0,0,0.0
    thr = math.ceil(float(line))  # Over X.5 -> >= ceil(X.5)
    hits = sum(1 for x in seq[:len(seq)] if x >= thr)
    n = len(seq)
    return hits, n, (hits / n) if n else 0.0

def under_hits(seq: List[int], line: float) -> Tuple[int,int,float]:
    if not seq: return 0,0,0.0
    thr = math.floor(float(line))  # Under X.5 -> <= floor(X.5)
    hits = sum(1 for x in seq[:len(seq)] if x <= thr)
    n = len(seq)
    return hits, n, (hits / n) if n else 0.0

# ===== Main =====
def main():
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: ODDS_API_KEY not set.")

    # Load lines files
    leagues_ctx: Dict[int, List[dict]] = {}
    for p in sorted(LINES_DIR.glob("*.json")):
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            lid = int(blob.get("league_id") or re.findall(r"(\d+)", p.stem)[0])
        except Exception:
            continue
        leagues_ctx[lid] = list(iter_team_threshold_series(blob, lid))

    if not leagues_ctx:
        print("No team_lines files found/parsed.")
        return

    # Fetch events
    events_by_league: Dict[int, List[dict]] = {}
    for lid in sorted(leagues_ctx.keys()):
        slug = LEAGUE_SLUG_BY_ID.get(lid)
        if not slug: continue
        evs = get_events_for_league(slug, api_key)
        if WINDOW_DAYS:
            evs = [e for e in evs if within_next_days(e, WINDOW_DAYS)]
        events_by_league[lid] = evs
        print(f"[EVENTS] {slug}: {len(evs)} (next {WINDOW_DAYS}d)")

    # Map (league, team vs opp) to event ids
    def find_event_ids(lid: int, team: str, opp: str) -> List[int]:
        evs = events_by_league.get(lid, [])
        out = []
        for ev in evs:
            h, a = ev.get("home",""), ev.get("away","")
            if team_names_match(team, h) and team_names_match(opp, a):
                if isinstance(ev.get("id"), int): out.append(ev["id"]); continue
            if team_names_match(team, a) and team_names_match(opp, h):
                if isinstance(ev.get("id"), int): out.append(ev["id"]); continue
        return out

    wants: List[dict] = []
    for lid, rows in leagues_ctx.items():
        if lid not in LEAGUE_SLUG_BY_ID: continue
        for r in rows:
            eids = find_event_ids(lid, r["team_name"], r["opp_name"])
            if not eids: continue
            r2 = dict(r); r2["league_id"] = lid; r2["event_ids"] = eids
            wants.append(r2)

    print(f"[MAP] mapped_contexts={len(wants)}")

    # Fetch odds
    event_ids = sorted({eid for w in wants for eid in w["event_ids"]})
    print(f"[ODDS] unique_events={len(event_ids)}")
    odds_payloads: List[dict] = []
    for i, batch in enumerate(chunked(event_ids, 10), start=1):
        print(f"[ODDS] batch {i} — {len(batch)} ids")
        odds_payloads.extend(get_odds_multi(batch, api_key))
    id_to_ev = {o.get("id"): o for o in odds_payloads if isinstance(o.get("id"), int)}
    print(f"[ODDS] payloads_received={len(id_to_ev)}")

    # Scan markets/lines and compute beat % (Over & Under)
    rows_over: List[dict] = []
    rows_under: List[dict] = []

    for w in wants:
        team = w["team_name"]; opp = w["opp_name"]; stat = w["stat"]; ctx_side = w["side"]
        off = w["offense_lastN"]; oppA = w["opp_allowed_lastN"]
        for ev_id in w["event_ids"]:
            ev = id_to_ev.get(ev_id)
            if not ev: continue
            home, away = ev.get("home",""), ev.get("away","")
            ev_side = "home" if team_names_match(team, home) else ("away" if team_names_match(team, away) else ctx_side)

            for m in bet365_markets(ev):
                side, stat_key = market_side_and_stat(m.get("name",""))
                if side != ev_side: continue
                if stat_key != stat: continue
                for opt in (m.get("odds") or []):
                    line = parse_line(opt)
                    if line is None: continue

                    # Prices
                    p_over  = parse_price(opt.get("over"))
                    p_under = parse_price(opt.get("under"))

                    # OVER
                    if p_over is not None and p_over >= MIN_DEC_PRICE:
                        th = line  # use hdp as the X.5 base
                        th_over = math.ceil(th)
                        hits_o_t, n_o_t, rate_o_t = over_hits(off, th)
                        hits_o_a, n_o_a, rate_o_a = over_hits(oppA, th)
                        combo = min(rate_o_t, rate_o_a)
                        rows_over.append({
                            "fixture": f"{home} vs {away}",
                            "team": team, "opp": opp, "side": ev_side,
                            "stat": stat, "hdp": float(line),
                            "price": float(p_over), "pick": "Over",
                            "team_hits": hits_o_t, "team_N": n_o_t, "team_rate": rate_o_t,
                            "opp_allowed_hits": hits_o_a, "opp_allowed_N": n_o_a, "opp_allowed_rate": rate_o_a,
                            "combo_rate": combo, "market": m.get("name",""),
                        })

                    # UNDER
                    if p_under is not None and p_under >= MIN_DEC_PRICE:
                        th = line
                        th_under = math.floor(th)
                        hits_u_t, n_u_t, rate_u_t = under_hits(off, th)
                        hits_u_a, n_u_a, rate_u_a = under_hits(oppA, th)
                        combo = min(rate_u_t, rate_u_a)
                        rows_under.append({
                            "fixture": f"{home} vs {away}",
                            "team": team, "opp": opp, "side": ev_side,
                            "stat": stat, "hdp": float(line),
                            "price": float(p_under), "pick": "Under",
                            "team_hits": hits_u_t, "team_N": n_u_t, "team_rate": rate_u_t,
                            "opp_allowed_hits": hits_u_a, "opp_allowed_N": n_u_a, "opp_allowed_rate": rate_u_a,
                            "combo_rate": combo, "market": m.get("name",""),
                        })

    # Rank — combo_rate desc then price desc
    rows_over.sort(key=lambda r: (-r["combo_rate"], -r["price"], r["fixture"], r["team"], r["stat"], r["hdp"]))
    rows_under.sort(key=lambda r: (-r["combo_rate"], -r["price"], r["fixture"], r["team"], r["stat"], r["hdp"]))

    # Render
    def lab_stat(k: str) -> str:
        return {"shots":"Shots","shots_on_target":"SOT","corners":"Corners","tackles":"Tackles"}.get(k,k)

    def pct(x: float) -> str:
        return f"{x*100:5.1f}%"

    print(f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}")
    print(f"Min decimal price: {MIN_DEC_PRICE:.2f} | Window days: {WINDOW_DAYS} | Bookmakers: {BOOKMAKERS}")
    print("")

    def dump(section: str, rows: List[dict], limit: int = 120):
        print(section)
        if not rows:
            print("  No qualifying lines at/above minimum price.")
            print("")
            return
        for r in rows[:limit]:
            print(f" • {r['team']} — {lab_stat(r['stat'])} {r['pick']} {r['hdp']:.1f} @ {r['price']:.3f} | "
                  f"{r['fixture']} | side={r['side']} | "
                  f"team {r['team_hits']}/{r['team_N']} ({pct(r['team_rate'])}), "
                  f"oppA {r['opp_allowed_hits']}/{r['opp_allowed_N']} ({pct(r['opp_allowed_rate'])}) | "
                  f"combo={pct(r['combo_rate'])} | {r['market']}")
        print("")

    dump("===== TEAM LINES — OVER (ranked by combo % then price) =====", rows_over)
    dump("===== TEAM LINES — UNDER (ranked by combo % then price) =====", rows_under)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
