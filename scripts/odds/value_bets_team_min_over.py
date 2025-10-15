#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — Team MIN (p80 & p100) floors → Over lines

Inputs:
  - data/team_lines/by_league/*.json

Bookmaker: Bet365 only
Markets used (team-level):
  - Team Shots (Home/Away)
  - Team Shots on Target (Home/Away)
  - Team Corners (Home/Away)
  - Team Tackles (Home/Away)

Logic:
  - For each upcoming fixture, read each team’s MIN floors: p80.min and p100.min per stat.
  - For 'Over X.5' lines: covered by MIN floor T if ceil(X.5) <= T. (Generalized: ceil(hdp) <= T)
  - Capture best Over price, filter by MIN_DEC_PRICE (default 1.20).
  - Output two lists: MIN 80 and MIN 100, ranked by odds desc.

ENV:
  ODDS_API_KEY (required)
  MIN_DEC_PRICE=1.20
  CAPTURE_FLOOR=1.10
  WINDOW_DAYS=7
  BOOKMAKERS=Bet365
"""

import os, re, json, math, time, random, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from itertools import islice
import requests

# ========= CONFIG =========
SPORT = "football"
BOOKMAKERS = os.getenv("BOOKMAKERS", "Bet365")
MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.20"))   # final filter
CAPTURE_FLOOR = float(os.getenv("CAPTURE_FLOOR", "1.10"))   # capture for diagnostics
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "7"))            # 0 disables time filter
TIMEOUT = 25
HTTP_HEADERS = {"accept": "application/json", "user-agent": "team-min-over/1.1"}

ROOT = Path(".")
LINES_DIR = ROOT / "data" / "team_lines" / "by_league"
PX_DIR    = ROOT / "data" / "predicted_xi" / "by_league"
OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_TXT   = OUT_DIR / "team_min_over.txt"
OUT_NDJSON= OUT_DIR / "team_min_over.ndjson"

EVENTS_API_URL     = "https://api.odds-api.io/v3/events"
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"

# ========= LEAGUE MAPPING (SportMonks -> Odds-API) =========
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

# ========= TEAM MARKET MAP =========
STAT_MARKETS = {  # allowed team markets
    "shots": "team shots",
    "shots_on_target": "team shots on target",
    "corners": "team corners",
    "tackles": "team tackles",
}
TEAM_MARKET_KEYS = {
    "team shots": "shots",
    "team shots on target": "shots_on_target",
    "team corners": "corners",
    "team tackles": "tackles",
}

# ========= STRING / NAME HELPERS =========
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

# ========= PREDICTED XI MAP (team_id -> name) =========
def team_name_map_from_px(league_id: int) -> Dict[int, str]:
    p = PX_DIR / f"{league_id}.json"
    if not p.exists(): return {}
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    m: Dict[int, str] = {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

# ========= HTTP / RETRIES =========
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

# ========= ODDS API HELPERS =========
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

def bet365_markets(ev: dict):
    for bm_name, markets in (ev.get("bookmakers") or {}).items():
        if "bet365" not in (bm_name or "").lower(): continue
        for m in markets or []:
            yield m

def market_side_and_stat(name: str) -> Tuple[Optional[str], Optional[str]]:
    s = norm(name)
    side = None
    # identify side from suffix/word
    if s.endswith(" home") or " home" in s:
        side = "home"; s = s.replace(" home", "").strip()
    elif s.endswith(" away") or " away" in s:
        side = "away"; s = s.replace(" away", "").strip()
    # exact stat match
    for key, stat in TEAM_MARKET_KEYS.items():
        if key == s:
            return side, stat
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

# ========= TEAM-LINES LOADING =========
class TeamLines:
    def __init__(self, league_id: int, body: dict):
        self.league_id = league_id
        self.fixtures = body.get("fixtures") or []

    def iter_thresholds(self):
        """
        Yield: (meta, side, stat, p80_min, p100_min, team_id, opp_id, team_name, opp_name).
        We pull names from both teams[side] and fixture-level fields for robustness.
        """
        for fx in self.fixtures:
            league_id = fx.get("league_id") or self.league_id
            meta = {
                "fixture_id": fx.get("fixture_id"),
                "league_id": league_id,
                "starting_at": fx.get("starting_at"),
                "home_id": fx.get("home_id"),
                "away_id": fx.get("away_id"),
                "home_name": fx.get("home_name"),
                "away_name": fx.get("away_name"),
            }
            teams = fx.get("teams") or {}
            for side in ("home", "away"):
                t = (teams.get(side) or {})
                opp_side = "away" if side == "home" else "home"
                opp = (teams.get(opp_side) or {})

                # prefer per-team names; fallback to fixture-level
                team_id = t.get("team_id") or (meta["home_id"] if side == "home" else meta["away_id"])
                opp_id  = opp.get("team_id") or (meta["away_id"] if side == "home" else meta["home_id"])
                team_name = t.get("name") or t.get("team_name") \
                            or (meta["home_name"] if side == "home" else meta["away_name"])
                opp_name  = opp.get("name") or opp.get("team_name") \
                            or (meta["away_name"] if side == "home" else meta["home_name"])

                stats = (t.get("stats") or {})
                for stat_key, m in stats.items():
                    if stat_key not in STAT_MARKETS:
                        continue
                    p80 = (m.get("p80") or {}).get("min")
                    p100 = (m.get("p100") or {}).get("min")
                    if p80 is None and p100 is None:
                        continue
                    yield meta, side, stat_key, p80, p100, team_id, opp_id, team_name, opp_name

# ========= CORE =========
def ceil_from_half_line(hdp: float) -> int:
    return math.ceil(float(hdp))

def best_over_price_for_threshold(ev: dict, team_side: str, stat: str, T: int) -> Optional[Tuple[float, float, str]]:
    """Return (price, line, market_name) for best Over with ceil(hdp) <= T."""
    best = None
    for m in bet365_markets(ev):
        side, stat_key = market_side_and_stat(m.get("name", ""))
        if side != team_side or stat_key != stat:
            continue
        odds = m.get("odds") or []
        for opt in odds:
            line = parse_line(opt)
            if line is None: continue
            if ceil_from_half_line(line) > T: continue
            val = opt.get("over")
            try:
                price = float(val) if val not in (None, "N/A") else None
            except:
                price = None
            if price is None or price < CAPTURE_FLOOR:
                continue
            if (best is None) or (price > best[0] + 1e-9):
                best = (price, float(line), m.get("name", ""))
    return best

def main():
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: ODDS_API_KEY not set.")

    # Load team-lines
    leagues: Dict[int, TeamLines] = {}
    files = sorted(LINES_DIR.glob("*.json"))
    for p in files:
        try:
            body = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            print(f"[WARN] Could not parse {p}")
            continue
        try:
            lid = int(body.get("league_id") or re.findall(r"(\d+)", p.stem)[0])
        except Exception:
            print(f"[WARN] Could not infer league_id from {p.name}")
            continue
        leagues[lid] = TeamLines(lid, body)

    if not leagues:
        OUT_TXT.write_text("No team-lines files found.\n", encoding="utf-8")
        print("[RESULT] No team-lines files found.")
        return

    # Collect thresholds
    wants: List[dict] = []
    for lid, TL in leagues.items():
        if lid not in LEAGUE_SLUG_BY_ID:
            continue
        name_map = team_name_map_from_px(lid)
        for meta, side, stat, p80, p100, team_id, opp_id, team_name, opp_name in TL.iter_thresholds():
            if not team_name and isinstance(team_id, int):
                team_name = name_map.get(team_id)
            if not opp_name and isinstance(opp_id, int):
                opp_name = name_map.get(opp_id)
            wants.append({
                "league_id": meta["league_id"],
                "fixture_id": meta["fixture_id"],
                "starting_at": meta["starting_at"],
                "side": side,
                "stat": stat,
                "p80": p80,
                "p100": p100,
                "team": team_name or "",
                "opp": opp_name or "",
            })

    if not wants:
        OUT_TXT.write_text("No thresholds present in team-lines.\n", encoding="utf-8")
        print("[RESULT] No thresholds present.")
        return

    # Fetch events per league
    leagues_needed = sorted({w["league_id"] for w in wants if LEAGUE_SLUG_BY_ID.get(w["league_id"])})
    events_by_league: Dict[int, List[dict]] = {}
    for lid in leagues_needed:
        slug = LEAGUE_SLUG_BY_ID[lid]
        evs = get_events_for_league(slug, api_key)
        if WINDOW_DAYS:
            evs = [e for e in evs if within_next_days(e, WINDOW_DAYS)]
        events_by_league[lid] = evs
        print(f"[EVENTS] {slug}: {len(evs)} (next {WINDOW_DAYS}d)")

    # Map thresholds to events by team names
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

    for w in wants:
        w["event_ids"] = find_event_ids(w["league_id"], w.get("team",""), w.get("opp",""))

    mapped = [w for w in wants if w.get("event_ids")]
    print(f"[MAP] mapped_thresholds={len(mapped)}/{len(wants)}")

    # Fetch odds
    event_ids = sorted({eid for w in mapped for eid in w["event_ids"]})
    print(f"[ODDS] Unique events to query: {len(event_ids)}")
    odds_payloads: List[dict] = []
    for i, batch in enumerate(chunked(event_ids, 10), start=1):
        print(f"[ODDS] batch {i} — {len(batch)} ids")
        odds_payloads.extend(get_odds_multi(batch, api_key))
    id_to_ev = {o.get("id"): o for o in odds_payloads if isinstance(o.get("id"), int)}
    print(f"[ODDS] payloads_received={len(id_to_ev)}")

    # Evaluate candidates
    p80_rows: List[dict] = []
    p100_rows: List[dict] = []

    for w in mapped:
        team = w.get("team") or ""
        opp  = w.get("opp")  or ""
        lid  = w.get("league_id")
        stat = w.get("stat")
        side = w.get("side")
        for ev_id in w["event_ids"]:
            ev = id_to_ev.get(ev_id)
            if not ev: continue
            home, away = ev.get("home",""), ev.get("away","")
            ev_side = "home" if team_names_match(team, home) else ("away" if team_names_match(team, away) else side)

            def maybe_add(T: Optional[int], bucket: str):
                if T is None: return
                got = best_over_price_for_threshold(ev, ev_side, stat, int(T))
                if not got: return
                price, line, market_name = got
                if price < MIN_DEC_PRICE: return
                row = {
                    "league_id": lid,
                    "fixture": f"{home} vs {away}",
                    "team": team,
                    "opp": opp,
                    "side": ev_side,
                    "stat": stat,
                    "safe_min": int(T),
                    "line": float(line),
                    "price": float(price),
                    "market": market_name or "",
                    "book": "Bet365",
                }
                if bucket == "p80": p80_rows.append(row)
                else: p100_rows.append(row)

            maybe_add(w.get("p80"),  "p80")
            maybe_add(w.get("p100"), "p100")

    # Sort and render
    p80_rows.sort(key=lambda r: (-r["price"], r["fixture"], r["team"], r["stat"]))
    p100_rows.sort(key=lambda r: (-r["price"], r["fixture"], r["team"], r["stat"]))

    header = (
        f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}\n"
        f"Min price: {MIN_DEC_PRICE:.2f} | Capture≥{CAPTURE_FLOOR:.2f} | Bookmaker: {BOOKMAKERS} | WINDOW_DAYS={WINDOW_DAYS}\n"
    )

    def stat_label(k: str) -> str:
        return {
            "shots": "Shots",
            "shots_on_target": "SOT",
            "corners": "Corners",
            "tackles": "Tackles",
        }.get(k, k)

    def fmt_row(r: dict) -> str:
        return (f" • {r['team']} — {stat_label(r['stat'])} Over {r['line']:.1f} @ {r['price']:.3f} | "
                f"{r['fixture']} | min={r['safe_min']} | {r['market']}")

    lines = [header]
    lines.append("===== TEAM MIN 80 — Over candidates =====")
    if p80_rows:
        for r in p80_rows: lines.append(fmt_row(r))
    else:
        lines.append("No matches found (no qualifying Over lines at or above min price).")
    lines.append("")
    lines.append("===== TEAM MIN 100 — Over candidates =====")
    if p100_rows:
        for r in p100_rows: lines.append(fmt_row(r))
    else:
        lines.append("No matches found (no qualifying Over lines at or above min price).")
    lines.append("")

    OUT_TXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    with OUT_NDJSON.open("w", encoding="utf-8") as f:
        for r in p80_rows:
            rr = dict(r); rr["bucket"] = "p80"; f.write(json.dumps(rr, ensure_ascii=False) + "\n")
        for r in p100_rows:
            rr = dict(r); rr["bucket"] = "p100"; f.write(json.dumps(rr, ensure_ascii=False) + "\n")

    print(header.strip())
    print(f"[RESULT] p80={len(p80_rows)}  p100={len(p100_rows)}  (written to {OUT_TXT})")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
