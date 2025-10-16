#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Team Line Beat Rates — Conservative
- Only show picks with combo% >= COMBO_MIN (default 0.50).
- For Shots & Corners OVER: require Team Match Winner (Bet365) <= TEAM_WIN_MAX (default 3.50).
  If ML missing, drop the pick (conservative).
- Keep Unders and SOT/Tackles unaffected by ML filter.
- Only include odds >= MIN_DEC_PRICE (default 1.20).

Data source for series:
  data/team_lines/by_league/*.json  (series_used: offense_lastN, opponent_allowed_lastN)

Odds:
  Bet365 only; team markets + match winner (1x2) to derive ML per side.

ENV:
  ODDS_API_KEY (required)
  MIN_DEC_PRICE (default 1.20)
  TEAM_WIN_MAX  (default 3.50)
  COMBO_MIN     (default 0.50)
  WINDOW_DAYS   (default 7)
  BOOKMAKERS    (default Bet365)
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
TEAM_WIN_MAX  = float(os.getenv("TEAM_WIN_MAX", "3.50"))
COMBO_MIN     = float(os.getenv("COMBO_MIN", "0.50"))
WINDOW_DAYS   = int(os.getenv("WINDOW_DAYS", "7"))  # 0 = no time filter
TIMEOUT = 25
HTTP_HEADERS = {"accept": "application/json", "user-agent": "team-line-beat-cons/1.0"}

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

MATCH_WINNER_KEYS = [
    "1x2","match result","match winner","moneyline","full time result",
    "to win","win/draw/win","wdw","ml","match odds","result","3-way","3 way",
    "90 minutes","regular time result"
]
DNB_KEYS = ["draw no bet","dnb"]

def market_is_match_winner(name: str) -> bool:
    s = (name or "").strip().lower()
    return bool(s) and any(k in s for k in MATCH_WINNER_KEYS)

def market_is_dnb(name: str) -> bool:
    s = (name or "").strip().lower()
    return bool(s) and any(k in s for k in DNB_KEYS)

def min_win_prices(ev: dict) -> Tuple[Optional[float], Optional[float]]:
    """Return (home_ml, away_ml) best Bet365 prices if present; else (None, None)."""
    best_home = None; best_away = None
    for m in bet365_markets(ev):
        if not market_is_match_winner(m.get("name","")): continue
        for row in (m.get("odds") or []):
            try:
                h = float(row.get("home")) if row.get("home") not in (None, "N/A") else None
                a = float(row.get("away")) if row.get("away") not in (None, "N/A") else None
            except: h = a = None
            if isinstance(h, float): best_home = h if (best_home is None or h < best_home) else best_home
            if isinstance(a, float): best_away = a if (best_away is None or a < best_away) else best_away
    # optional DNB fallback if needed
    if best_home is None or best_away is None:
        for m in bet365_markets(ev):
            if not market_is_dnb(m.get("name","")): continue
            for row in (m.get("odds") or []):
                try:
                    h = float(row.get("home")) if row.get("home") not in (None, "N/A") else None
                    a = float(row.get("away")) if row.get("away") not in (None, "N/A") else None
                except: h = a = None
                if isinstance(h, float) and best_home is None: best_home = h
                if isinstance(a, float) and best_away is None: best_away = a
    return best_home, best_away

def market_side_and_stat(name: str) -> Tuple[Optional[str], Optional[str]]:
    s = norm(name)
    side = None
    if s.endswith(" home") or " home" in s:
        side = "home"; s = s.replace(" home", "").strip()
    elif s.endswith(" away") or " away" in s:
        side = "away"; s = s.replace(" away", "").strip()
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

# ===== team_lines loader (series) =====
def iter_series(lines_blob: dict, league_id: int):
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
                if not off and not oppA: continue
                yield {
                    "meta": meta,
                    "side": side,
                    "team_name": team_name or "",
                    "opp_name": opp_name or "",
                    "stat": stat_key,
                    "offense_lastN": [x for x in off if isinstance(x, int)],
                    "opp_allowed_lastN": [x for x in oppA if isinstance(x, int)],
                }

# ===== rates =====
def over_hits(seq: List[int], line: float) -> Tuple[int,int,float]:
    if not seq: return 0,0,0.0
    thr = math.ceil(float(line))
    hits = sum(1 for x in seq if x >= thr)
    n = len(seq)
    return hits, n, (hits/n) if n else 0.0

def under_hits(seq: List[int], line: float) -> Tuple[int,int,float]:
    if not seq: return 0,0,0.0
    thr = math.floor(float(line))
    hits = sum(1 for x in seq if x <= thr)
    n = len(seq)
    return hits, n, (hits/n) if n else 0.0

# ===== main =====
def main():
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: ODDS_API_KEY not set.")

    # Load series
    contexts: List[dict] = []
    for p in sorted(LINES_DIR.glob("*.json")):
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            lid = int(blob.get("league_id") or re.findall(r"(\d+)", p.stem)[0])
        except Exception:
            continue
        for row in iter_series(blob, lid):
            row["league_id"] = lid
            contexts.append(row)

    if not contexts:
        print("No team_lines contexts found.")
        return

    # Fetch events per league
    events_by_league: Dict[int, List[dict]] = {}
    for lid in sorted({c["league_id"] for c in contexts}):
        slug = LEAGUE_SLUG_BY_ID.get(lid)
        if not slug: continue
        r = requests.get(EVENTS_API_URL, params={"apiKey": api_key, "sport": SPORT, "league": slug}, headers=HTTP_HEADERS, timeout=TIMEOUT)
        if not (r.status_code == 200): continue
        try: evs = r.json()
        except: evs = []
        if WINDOW_DAYS:
            filt = []
            for e in evs or []:
                ds = e.get("date") or ""
                try:
                    dt_utc = dt.datetime.fromisoformat(ds.replace("Z","+00:00"))
                    now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
                    if now <= dt_utc <= (now + dt.timedelta(days=WINDOW_DAYS)):
                        filt.append(e)
                except: pass
            evs = filt
        events_by_league[lid] = evs
        print(f"[EVENTS] {slug}: {len(evs)}")

    # Map to event ids
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
    for row in contexts:
        eids = find_event_ids(row["league_id"], row["team_name"], row["opp_name"])
        if not eids: continue
        r2 = dict(row); r2["event_ids"] = eids
        wants.append(r2)
    print(f"[MAP] mapped={len(wants)}/{len(contexts)}")

    # Fetch odds payloads
    event_ids = sorted({eid for w in wants for eid in w["event_ids"]})
    print(f"[ODDS] unique_events={len(event_ids)}")
    odds_payloads: List[dict] = []
    for i, batch in enumerate(chunked(event_ids, 10), start=1):
        r = requests.get(ODDS_MULTI_API_URL, params={
            "apiKey": api_key, "eventIds": ",".join(map(str, batch)), "bookmakers": BOOKMAKERS
        }, headers=HTTP_HEADERS, timeout=TIMEOUT)
        if r.status_code == 200:
            try: odds_payloads.extend(r.json() or [])
            except: pass
        else:
            print(f"[HTTP {r.status_code}] odds/multi batch {i}")
            time.sleep(0.4)
    id_to_ev = {o.get("id"): o for o in odds_payloads if isinstance(o.get("id"), int)}
    print(f"[ODDS] payloads_received={len(id_to_ev)}")

    rows_over: List[dict] = []
    rows_under: List[dict] = []

    for w in wants:
        team = w["team_name"]; opp = w["opp_name"]; stat = w["stat"]; ctx_side = w["side"]
        off = w["offense_lastN"]; oppA = w["opp_allowed_lastN"]
        for ev_id in w["event_ids"]:
            ev = id_to_ev.get(ev_id)
            if not ev: continue
            home, away = ev.get("home",""), ev.get("away","")
            # side for team in this event
            ev_side = "home" if team_names_match(team, home) else ("away" if team_names_match(team, away) else ctx_side)

            # team ML (Bet365)
            home_ml, away_ml = min_win_prices(ev)
            team_ml = home_ml if ev_side == "home" else away_ml

            for m in bet365_markets(ev):
                side, stat_key = market_side_and_stat(m.get("name",""))
                if side != ev_side: continue
                if stat_key != stat: continue
                for opt in (m.get("odds") or []):
                    line = parse_line(opt)
                    if line is None: continue
                    p_over  = parse_price(opt.get("over"))
                    p_under = parse_price(opt.get("under"))

                    # OVER
                    if p_over is not None and p_over >= MIN_DEC_PRICE:
                        _, _, rate_t = over_hits(off, line)
                        _, _, rate_a = over_hits(oppA, line)
                        combo = min(rate_t, rate_a)
                        # Conservative: need combo >= COMBO_MIN
                        if combo < COMBO_MIN: 
                            pass
                        else:
                            # ML filter for Shots/Corners OVER
                            if stat in ("shots","corners"):
                                if (team_ml is None) or (team_ml > TEAM_WIN_MAX):
                                    pass  # drop big underdogs or missing ML
                                else:
                                    rows_over.append({
                                        "fixture": f"{home} vs {away}",
                                        "team": team, "opp": opp, "side": ev_side,
                                        "stat": stat, "hdp": float(line),
                                        "price": float(p_over), "pick": "Over",
                                        "team_rate": rate_t, "opp_allowed_rate": rate_a, "combo_rate": combo,
                                        "team_ml": float(team_ml),
                                        "market": m.get("name",""),
                                    })
                            else:
                                rows_over.append({
                                    "fixture": f"{home} vs {away}",
                                    "team": team, "opp": opp, "side": ev_side,
                                    "stat": stat, "hdp": float(line),
                                    "price": float(p_over), "pick": "Over",
                                    "team_rate": rate_t, "opp_allowed_rate": rate_a, "combo_rate": combo,
                                    "team_ml": float(team_ml) if isinstance(team_ml, float) else None,
                                    "market": m.get("name",""),
                                })

                    # UNDER (no ML filter; still require combo >= COMBO_MIN)
                    if p_under is not None and p_under >= MIN_DEC_PRICE:
                        _, _, rate_t = under_hits(off, line)
                        _, _, rate_a = under_hits(oppA, line)
                        combo = min(rate_t, rate_a)
                        if combo < COMBO_MIN:
                            pass
                        else:
                            rows_under.append({
                                "fixture": f"{home} vs {away}",
                                "team": team, "opp": opp, "side": ev_side,
                                "stat": stat, "hdp": float(line),
                                "price": float(p_under), "pick": "Under",
                                "team_rate": rate_t, "opp_allowed_rate": rate_a, "combo_rate": combo,
                                "team_ml": float(team_ml) if isinstance(team_ml, float) else None,
                                "market": m.get("name",""),
                            })

    # Rank by combo desc then price desc
    rows_over.sort(key=lambda r: (-r["combo_rate"], -r["price"], r["fixture"], r["team"], r["stat"], r["hdp"]))
    rows_under.sort(key=lambda r: (-r["combo_rate"], -r["price"], r["fixture"], r["team"], r["stat"], r["hdp"]))

    def lab_stat(k: str) -> str:
        return {"shots":"Shots","shots_on_target":"SOT","corners":"Corners","tackles":"Tackles"}.get(k,k)
    def pct(x: float) -> str:
        return f"{x*100:5.1f}%"

    print(f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}")
    print(f"Min price: {MIN_DEC_PRICE:.2f} | Combo≥{COMBO_MIN:.2f} | Shots/Corners OVER ML≤{TEAM_WIN_MAX:.2f} | Window={WINDOW_DAYS} | Bookmakers={BOOKMAKERS}")
    print("")
    def dump(title: str, rows: List[dict], limit: int = 120):
        print(title)
        if not rows:
            print("  No qualifying lines after conservative filters.\n")
            return
        for r in rows[:limit]:
            ml = f" | ML={r['team_ml']:.3f}" if isinstance(r.get("team_ml"), float) else ""
            print(f" • {r['team']} — {lab_stat(r['stat'])} {r['pick']} {r['hdp']:.1f} @ {r['price']:.3f} | "
                  f"{r['fixture']} | side={r['side']} | "
                  f"team={pct(r['team_rate'])} oppA={pct(r['opp_allowed_rate'])} combo={pct(r['combo_rate'])}"
                  f"{ml} | {r['market']}")
        print("")
    dump("===== TEAM LINES — OVER (Conservative) =====", rows_over)
    dump("===== TEAM LINES — UNDER (Conservative) =====", rows_under)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass