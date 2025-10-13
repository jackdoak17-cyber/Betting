#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Merge players_by_criteria.json with Bet365 odds (Odds API) for:
  • Player Shots
  • Player Shots On Target

Filters by four criteria:
  "100pct_all (n>=8)", "90pct_all (n>=8)", "100pct_last5", "4of5_last5"
…and by price threshold (>= MIN_DEC_PRICE).

Output: pretty lists grouped by Stat -> Line -> Criterion, sorted by line (asc) then price (desc),
including player position, team, fixture, KO time, and hit info.

Env:
  ODDS_API_KEY   (required)

Files:
  data/player_filters/players_by_criteria.json

Notes:
  - Events fetched per league slug; odds fetched in batches of 10 (odds/multi).
  - Only bookmaker Bet365 is considered.
"""

import os, re, sys, math, time, json, random, unicodedata, datetime as dt
from typing import Dict, List, Tuple, Optional, Any
import requests
from itertools import islice

# ================== CONFIG ==================
MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
SPORT = "football"
BOOKMAKERS = "Bet365"
HTTP_HEADERS = {"accept": "application/json", "user-agent": "value-bets/1.0"}

# League mapping (Sportmonks league_id -> Odds-API slug)
LEAGUE_SLUG_BY_ID = {
    8:   "england-premier-league",
    9:   "england-championship",
    82:  "germany-bundesliga",
    301: "france-ligue-1",
    384: "italy-serie-a",
    387: "italy-serie-b",
    564: "spain-laliga",
    567: "spain-laliga-2",
    600: "turkiye-super-lig",
    72:  "netherlands-eredivisie",
    271: "denmark-superliga",
}

# Criterion ordering (as requested)
CRITERIA_ORDER = [
    "100pct_all (n>=8)",
    "90pct_all (n>=8)",
    "100pct_last5",
    "4of5_last5",
]

# Stats we’ll cover and their market matchers
STATS_MARKETS = {
    "shots": "player_shots",
    "shots_on_target": "player_shots_on_target",
}

EVENTS_API_URL = "https://api.odds-api.io/v3/events"
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"
TIMEOUT = 25

# ================== UTILS ==================
def chunked(it, n):
    it = iter(it)
    while True:
        batch = list(islice(it, n))
        if not batch:
            return
        yield batch

def http_get_with_retries(url: str, params: dict, max_retries=6, base_sleep=1.0, factor=1.8):
    attempt = 0; last_text = ""
    while attempt < max_retries:
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                last_text = r.text
                sleep = base_sleep * (factor ** attempt) + random.uniform(0, 0.4)
                print(f"[RETRY] {url} {r.status_code}; sleeping {sleep:.1f}s...")
                time.sleep(sleep)
                attempt += 1
                continue
            print(f"[HTTP {r.status_code}] {url} :: {r.text[:200]}")
            return None
        except requests.exceptions.RequestException as e:
            sleep = base_sleep * (factor ** attempt) + random.uniform(0, 0.4)
            print(f"[NET] {url} exception: {e}; sleeping {sleep:.1f}s...")
            time.sleep(sleep)
            attempt += 1
    if last_text:
        print(f"[ERROR] Retries exhausted for {url}. Last body: {last_text[:220]}")
    else:
        print(f"[ERROR] Retries exhausted for {url}.")
    return None

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","calcio","united","city","saint","st","mk","bk"}
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

def cleanup_option_label(label: str) -> str:
    if not label: return ""
    # remove trailing (…) brackets often used for team side or ids
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label).strip()

def extract_line_from_label(label: str) -> Optional[float]:
    if not label: return None
    m = re.search(r"\(([-+]?\d+(?:\.\d+)?)\)", label)
    if not m: return None
    try: return float(m.group(1))
    except: return None

def parse_price(val: Any) -> Optional[float]:
    try:
        return float(val)
    except:
        return None

def line_to_threshold(line: float) -> Optional[int]:
    # 0.5 -> 1, 1.5 -> 2, 2.5 -> 3, etc.
    try:
        return int(math.floor(line + 0.5))
    except:
        return None

# ===== Market detectors (robust to book variations) =====
def is_player_sot_market(name: str) -> bool:
    s = norm(name)
    return ("shot" in s and "target" in s) or ("s.o.t" in s) or ("shots on" in s and "target" in s)

def is_player_shots_market(name: str) -> bool:
    s = norm(name)
    if "shot" not in s: return False
    if "target" in s: return False  # keep SOT separate
    # allow generic "player shots", "shots", etc.
    return True

# ================== INPUT: players_by_criteria ==================
def load_players_by_criteria(path="data/player_filters/players_by_criteria.json"):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Missing {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def collect_eligible_players(pbc: dict) -> Dict[str, Dict[int, Dict[str, List[dict]]]]:
    """
    Returns: eligible[stat][threshold][criterion] = [rows...]
    Where each row minimally includes: player_id, name, team, league_id, position, threshold, n, hit_rate, series
    """
    eligible: Dict[str, Dict[int, Dict[str, List[dict]]]] = {}
    stats = pbc.get("stats") or {}
    for stat_key, stat_obj in stats.items():
        # normalize stat key
        key_norm = stat_key.strip().lower().replace(" ", "_")
        if key_norm not in STATS_MARKETS:
            # only keep "shots" and "shots_on_target"
            continue
        thresholds = (stat_obj.get("thresholds") or {})
        for thr_s, buckets in thresholds.items():
            try:
                thr = int(thr_s)
            except:
                continue
            for criterion in CRITERIA_ORDER:
                arr = buckets.get(criterion) or []
                for row in arr:
                    out = {
                        "league_id": row.get("league_id"),
                        "team": row.get("team"),
                        "team_id": row.get("team_id"),
                        "player_id": row.get("player_id"),
                        "name": row.get("name"),
                        "position": row.get("position"),
                        "series": row.get("series"),
                        "n": row.get("n"),
                        "hit_rate": row.get("hit_rate"),
                        "threshold": thr,
                        "criterion": criterion,
                        "stat": key_norm,
                    }
                    eligible.setdefault(key_norm, {}).setdefault(thr, {}).setdefault(criterion, []).append(out)
    return eligible

# ================== ODDS API ==================
def fetch_events_for_league(slug: str, api_key: str) -> List[dict]:
    r = http_get_with_retries(EVENTS_API_URL, {"apiKey": api_key, "sport": SPORT, "league": slug})
    if not (r and r.status_code == 200):
        return []
    try:
        data = r.json()
        return data if isinstance(data, list) else []
    except:
        return []

def fetch_odds_multi(event_ids: List[int], api_key: str) -> List[dict]:
    if not event_ids:
        return []
    r = http_get_with_retries(ODDS_MULTI_API_URL, {
        "apiKey": api_key,
        "eventIds": ",".join(map(str, event_ids)),
        "bookmakers": BOOKMAKERS
    })
    if not (r and r.status_code == 200):
        return []
    try:
        data = r.json()
        return data if isinstance(data, list) else []
    except:
        return []

# ================== MATCHING ==================
def match_events_by_team(events: List[dict], team_name: str) -> List[dict]:
    out = []
    for ev in events:
        if team_names_match(team_name, ev.get("home","")) or team_names_match(team_name, ev.get("away","")):
            out.append(ev)
    return out

def player_label_matches(player_name: str, option_label: str) -> bool:
    """
    Match by last-name (+ first initial tolerant).
    """
    if not player_name or not option_label:
        return False

    def last_and_initial(name: str):
        name = strip_accents(name).replace(".", " ").strip()
        parts = [p for p in name.split() if p]
        if not parts: return None, None
        last = norm(parts[-1])
        ini = None
        for p in parts[:-1]:
            ch = p.strip()[0:1]
            if ch: ini = ch.lower(); break
        return last, ini

    last, ini = last_and_initial(player_name)
    base = norm(cleanup_option_label(option_label))
    if not last or last not in base:
        return False
    if ini:
        first_word_initial = base.split()[0][0:1] if base.split() else None
        if first_word_initial and first_word_initial == ini:
            return True
        return bool(re.search(rf"\b{ini}\w*\b.*\b{last}\b", base))
    return True

def iter_bet365_markets(ev: dict):
    for bm_name, markets in (ev.get("bookmakers") or {}).items():
        if "bet365" not in (bm_name or "").lower():
            continue
        for m in markets or []:
            yield m

def extract_options(market: dict) -> List[dict]:
    odds = market.get("odds")
    if isinstance(odds, list):
        return [o for o in odds if isinstance(o, dict)]
    elif isinstance(odds, dict):
        # flatten dict form to {label, ...}
        out = []
        for label, opt in odds.items():
            if isinstance(opt, dict):
                out.append({"label": label, **opt})
        return out
    return []

def option_line(opt: dict) -> Optional[float]:
    # Prefer numeric 'hdp' (line) if present
    if "hdp" in opt:
        try:
            return float(opt["hdp"])
        except:
            pass
    # Fallback: parse "(0.5)" from label
    return extract_line_from_label(opt.get("label",""))

def option_over_price(opt: dict) -> Optional[float]:
    # standard 'over' field
    if "over" in opt:
        return parse_price(opt.get("over"))
    # 'price' fallback
    return parse_price(opt.get("price"))

# ================== MAIN ==================
def main():
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        print("ERROR: ODDS_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    # 1) Load players_by_criteria
    pbc = load_players_by_criteria()
    eligible = collect_eligible_players(pbc)

    # 2) Fetch events by league
    events_by_slug: Dict[str, List[dict]] = {}
    for lid, slug in LEAGUE_SLUG_BY_ID.items():
        evs = fetch_events_for_league(slug, api_key)
        events_by_slug[slug] = evs
        print(f"[EVENTS] {slug}: {len(evs)}")

    # 3) Build a map from (league_id, team_name) -> event candidates (usually 1)
    candidates: Dict[Tuple[int, str], List[dict]] = {}
    for lid, slug in LEAGUE_SLUG_BY_ID.items():
        evs = events_by_slug.get(slug, [])
        # Collect all unique team names in this league’s events once
        for stat in eligible:
            for thr in eligible[stat]:
                for crit in eligible[stat][thr]:
                    for row in eligible[stat][thr][crit]:
                        if int(row["league_id"] or 0) != lid:
                            continue
                        team = row.get("team")
                        if not team:
                            continue
                        k = (lid, team)
                        if k not in candidates:
                            candidates[k] = match_events_by_team(evs, team)

    # 4) Aggregate unique event ids to fetch odds in batches of 10
    event_ids = sorted({ev["id"] for lst in candidates.values() for ev in lst if isinstance(ev.get("id"), int)})
    print(f"[ODDS] Unique events matched to teams: {len(event_ids)}")

    all_odds_payloads: List[dict] = []
    for i, batch in enumerate(chunked(event_ids, 10), start=1):
        print(f"[ODDS] batch {i} — {len(batch)} ids")
        all_odds_payloads.extend(fetch_odds_multi(batch, api_key))

    odds_by_id = {o.get("id"): o for o in all_odds_payloads if isinstance(o.get("id"), int)}

    # 5) Scan & collect hits per (stat -> line -> criterion)
    results: Dict[str, Dict[float, Dict[str, List[dict]]]] = {"shots": {}, "shots_on_target": {}}

    for stat in ("shots", "shots_on_target"):
        stat_block = eligible.get(stat) or {}
        if not stat_block:
            continue

        for thr, crit_map in stat_block.items():
            for criterion, rows in crit_map.items():
                for r in rows:
                    lid = int(r.get("league_id") or 0)
                    team = r.get("team") or ""
                    pid = r.get("player_id")
                    pname = r.get("name") or ""
                    pos = r.get("position") or ""
                    series = r.get("series") or []
                    n = r.get("n") or 0
                    hit_rate = r.get("hit_rate")

                    slug = LEAGUE_SLUG_BY_ID.get(lid)
                    if not slug:
                        continue
                    evs = candidates.get((lid, team)) or []
                    if not evs:
                        continue

                    # iterate candidate events (normally 1)
                    for ev in evs:
                        ev_id = ev.get("id")
                        ev_odds = odds_by_id.get(ev_id)
                        if not ev_odds:
                            continue

                        # search relevant markets
                        for m in iter_bet365_markets(ev_odds):
                            mname = m.get("name","")
                            if stat == "shots_on_target" and not is_player_sot_market(mname):
                                continue
                            if stat == "shots" and not is_player_shots_market(mname):
                                continue

                            for opt in extract_options(m):
                                label = opt.get("label", "")
                                if not player_label_matches(pname, label):
                                    continue

                                line = option_line(opt)
                                price = option_over_price(opt)
                                if price is None or price < MIN_DEC_PRICE or line is None:
                                    continue

                                # map line to threshold; keep only those matching player threshold bucket
                                thr_from_line = line_to_threshold(line)
                                if thr_from_line != int(r["threshold"]):
                                    # if your filters contain thresholds 1/2/3, we align with 0.5/1.5/2.5…
                                    continue

                                # Passed all filters — pack row
                                start = ev_odds.get("date") or ""  # ISO
                                home = ev_odds.get("home","")
                                away = ev_odds.get("away","")
                                urls = ev_odds.get("urls") or {}
                                bet365_url = None
                                for k, v in urls.items():
                                    if "bet365" in (k or "").lower():
                                        bet365_url = v; break

                                row = {
                                    "player": pname,
                                    "position": pos,
                                    "team": team,
                                    "fixture": f"{home} vs {away}",
                                    "kickoff": start.replace("T", " ").replace("Z",""),
                                    "line": float(line),
                                    "price": float(price),
                                    "criterion": criterion,
                                    "n": int(n or 0),
                                    "hit_rate_pct": int(round(float(hit_rate or 0.0) * 100)),
                                    "series": series,
                                    "bet365_url": bet365_url,
                                }
                                results[stat].setdefault(float(line), {}).setdefault(criterion, []).append(row)

    # 6) Print — grouped by stat -> line (asc) -> criterion (CRITERIA_ORDER), sorted by price desc.
    def print_block(stat_name: str, stat_key: str):
        block = results.get(stat_key) or {}
        if not block:
            print(f"\n===== {stat_name} — no matches (price ≥ {MIN_DEC_PRICE:.2f}) =====")
            return

        print(f"\n===== {stat_name} — Bet365 — price ≥ {MIN_DEC_PRICE:.2f} =====")
        for line in sorted(block.keys()):
            print(f"\n  — Line {line:.1f}")
            for crit in CRITERIA_ORDER:
                arr = list(block[line].get(crit) or [])
                if not arr:
                    continue
                arr.sort(key=lambda x: (-x["price"], x["player"]))
                print(f"    [{crit}]")
                for x in arr:
                    series_short = ",".join(map(str, x["series"][:10])) if isinstance(x["series"], list) else ""
                    print(
                        f"      • {x['player']} [{x['position']}] — {x['team']} | "
                        f"{x['fixture']} @ {x['kickoff']} | "
                        f"{stat_name} Over {line:.1f} @ {x['price']:.3f}  | "
                        f"hit {int(x['hit_rate_pct'])}% of {x['n']}  "
                        f"(series: {series_short})"
                    )

    print_block("Player Shots On Target", "shots_on_target")
    print_block("Player Shots", "shots")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
