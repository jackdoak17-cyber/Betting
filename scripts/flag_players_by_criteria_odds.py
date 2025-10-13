#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flags players from data/player_filters/players_by_criteria.{json,txt}
when Bet365 prices for the *specific stat+threshold* are >= MIN_PRICE.

Targets:
  - SHOTS 1+ (line 0.5)
  - SHOTS 2+ (line 1.5)
  - SOT   1+ (line 0.5)

Output:
  data/player_filters/criteria_odds_flags.txt

Env:
  ODDS_API_KEY=...   (required)
  MIN_PRICE=1.80     (optional)
"""

import os, re, json, time, math, random, unicodedata
import requests
from pathlib import Path
from itertools import islice

# ================== CONFIG ==================
API_KEY = os.getenv("ODDS_API_KEY", "").strip()
if not API_KEY:
    raise SystemExit("Set ODDS_API_KEY environment variable.")

SPORT = "football"

# Only Bet365 (param + name guard)
BOOKMAKERS = "Bet365"
ALLOWED_BOOKMAKER_NAMES = {"bet365"}

MIN_PRICE = float(os.getenv("MIN_PRICE", "1.80"))

# Keep this lean; same convention you used elsewhere
LEAGUE_SLUGS = [
    "england-premier-league", "england-championship",
    "italy-serie-a", "italy-serie-b",
    "spain-laliga", "spain-laliga-2",
    "france-ligue-1", "germany-bundesliga", "germany-2-bundesliga",
    "netherlands-eredivisie", "turkiye-super-lig",
    "brazil-brasileiro-serie-a", "brazil-brasileiro-serie-b",
    "usa-mls", "japan-jleague", "saudi-arabia-saudi-pro-league",
]

EVENTS_API_URL = "https://api.odds-api.io/v3/events"      # events listing
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"  # batched odds
HTTP_HEADERS = {"accept": "application/json"}

# ================== UTILS ==================
OUT_TXT = Path("data/player_filters/criteria_odds_flags.txt")

def ensure_dir(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)

def chunked(it, n):
    it = iter(it)
    while True:
        ch = list(islice(it, n))
        if not ch: return
        yield ch

def http_get_with_retries(url, params, max_retries=5, base_sleep=1.0, factor=1.8):
    attempt = 0; last_text = ""
    while attempt < max_retries:
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=25)
            if r.status_code == 200: return r
            if r.status_code in (429,500,502,503,504):
                last_text = r.text
                sleep = base_sleep*(factor**attempt) + random.uniform(0,0.4)
                print(f"[RETRY] {url} {r.status_code}; sleeping {sleep:.1f}s")
                time.sleep(sleep); attempt += 1; continue
            print(f"[ERROR] {url} -> {r.status_code}: {r.text[:200]}")
            return None
        except requests.exceptions.RequestException as e:
            sleep = base_sleep*(factor**attempt) + random.uniform(0,0.4)
            print(f"[NET] {url} exception: {e}; sleep {sleep:.1f}s")
            time.sleep(sleep); attempt += 1
    if last_text:
        print(f"[ERROR] Exhausted retries: {url}. Last: {last_text[:200]}")
    else:
        print(f"[ERROR] Exhausted retries: {url}.")
    return None

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s):
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {
    "fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca",
    "the","club","de","del","la","las","los","calcio","united","city","saint","st"
}
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

def extract_last_init(name):
    name = strip_accents(name).replace(".", " ").strip()
    parts = [p for p in name.split() if p]
    if not parts: return None, None
    last = norm(parts[-1]); initial = None
    for p in parts[:-1]:
        ch = p.strip()[0:1]
        if ch: initial = ch.lower(); break
    return last, initial

def player_label_matches(scraped_name, option_label):
    if not scraped_name or not option_label: return False
    last, initial = extract_last_init(scraped_name)
    label = norm(strip_paren_trail(option_label))
    if not last or last not in label: return False
    if initial:
        first_word_initial = label.split()[0][0:1] if label.split() else None
        if first_word_initial and first_word_initial == initial: return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

def strip_paren_trail(label: str) -> str:
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

# ================== LOAD PLAYER TARGETS ==================
FILTERS_JSON = Path("data/player_filters/players_by_criteria.json")
FILTERS_TXT  = Path("data/player_filters/players_by_criteria.txt")

def load_targets():
    """
    Returns list of dicts:
      { 'player': str, 'team': Optional[str], 'stat': 'shots'|'sot', 'threshold': int }
    We only keep: shots thr in {1,2}; sot thr in {1}.
    """
    targets = []

    if FILTERS_JSON.exists():
        j = json.loads(FILTERS_JSON.read_text(encoding="utf-8"))
        stats = j.get("stats") or {}
        # SHOTS thresholds
        if "shots" in stats:
            tmap = (stats["shots"] or {}).get("thresholds") or {}
            for thr_s in ("1","2",1,2):
                thr = int(thr_s)
                buckets = tmap.get(str(thr)) or tmap.get(thr) or {}
                for bname in ("100pct_all","90pct_all","100pct_last5","4of5_last5"):
                    for r in buckets.get(bname, []):
                        targets.append({
                            "player": r.get("name","").strip(),
                            "team": r.get("team"),
                            "stat": "shots",
                            "threshold": thr,
                        })
        # SOT threshold 1 only
        if "shots_on_target" in stats:
            tmap = (stats["shots_on_target"] or {}).get("thresholds") or {}
            for thr_s in ("1",1):
                thr = int(thr_s)
                buckets = tmap.get(str(thr)) or tmap.get(thr) or {}
                for bname in ("100pct_all","90pct_all","100pct_last5","4of5_last5"):
                    for r in buckets.get(bname, []):
                        targets.append({
                            "player": r.get("name","").strip(),
                            "team": r.get("team"),
                            "stat": "sot",
                            "threshold": thr,
                        })
        # de-dupe
        seen = set(); uniq = []
        for t in targets:
            key = (t["player"], t.get("team"), t["stat"], t["threshold"])
            if key not in seen and t["player"]:
                uniq.append(t); seen.add(key)
        return uniq

    # Fallback: parse TXT sections
    if FILTERS_TXT.exists():
        cur_stat = None
        cur_thr = None
        for line in FILTERS_TXT.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.rstrip()
            m_stat = re.match(r"^\s*===== (SHOTS|SHOTS_ON_TARGET) =====\s*$", line, re.I)
            if m_stat:
                s = m_stat.group(1).lower()
                cur_stat = "shots" if s == "shots" else "sot"
                cur_thr = None
                continue
            m_thr = re.match(r"^\s*-- Threshold:\s*(\d+)\+\s*$", line, re.I)
            if m_thr:
                cur_thr = int(m_thr.group(1))
                continue
            # bullet lines
            if cur_stat and cur_thr and line.strip().startswith("•"):
                # "• Name (Team, Pos): series  [n=...]"
                nm = re.match(r"^\s*•\s*([^()]+)\s*\(([^)]+)\):", line)
                if nm:
                    name = nm.group(1).strip()
                    team = nm.group(2).split(",")[0].strip()
                    if cur_stat == "shots" and cur_thr in (1,2):
                        targets.append({"player": name, "team": team, "stat": "shots", "threshold": cur_thr})
                    if cur_stat == "sot" and cur_thr == 1:
                        targets.append({"player": name, "team": team, "stat": "sot", "threshold": 1})
        # de-dupe
        seen = set(); uniq = []
        for t in targets:
            key = (t["player"], t.get("team"), t["stat"], t["threshold"])
            if key not in seen:
                uniq.append(t); seen.add(key)
        return uniq

    raise FileNotFoundError("players_by_criteria.{json,txt} not found.")

# ================== ODDS API HELPERS ==================
def get_upcoming_events(slugs):
    all_events = []
    for slug in slugs:
        r = http_get_with_retries(EVENTS_API_URL, {"apiKey": API_KEY, "sport": SPORT, "league": slug})
        if r and r.status_code == 200:
            try: data = r.json()
            except: data = None
            if isinstance(data, list):
                all_events.extend(data)
        else:
            print(f"[WARN] events fail for {slug}")
    return all_events

def get_odds_multi(event_ids):
    if not event_ids: return []
    r = http_get_with_retries(ODDS_MULTI_API_URL, {
        "apiKey": API_KEY,
        "eventIds": ",".join(map(str, event_ids)),
        "bookmakers": BOOKMAKERS
    })
    if r and r.status_code == 200:
        try: return r.json() if isinstance(r.json(), list) else []
        except: return []
    return []

def _filter_to_bet365_bookmakers(event_odds):
    out = {}
    for bm_slug, markets in (event_odds.get("bookmakers") or {}).items():
        if any(key in (bm_slug or "").lower() for key in ALLOWED_BOOKMAKER_NAMES):
            out[bm_slug] = markets
    return out

def _bet365_name_from_urls(urls_dict, fallback_slug: str):
    for name, _url in (urls_dict or {}).items():
        try:
            if name and "bet365" in name.lower():
                return name
        except: pass
    return "Bet365" if "bet365" in (fallback_slug or "").lower() else (fallback_slug or "Bet365")

# Market recognisers (avoid halves/periods/combo/foot/etc.)
NEGATIVE_SHOTS_TERMS = {
    "on target","sot","outside","outside box","outside of box","from outside","outside the box",
    "header","headers","head","left foot","right foot","right-foot","left-foot",
    "first half","1st half","2nd half","second half","half",
    "distance","long range","goal","goals","to score","assist","assists","ga","g/a",
    "shots on target","on-target","from corner","from free kick","penalty","penalties"
}
def market_is_player_shots(name: str) -> bool:
    s = re.sub(r"\s+", " ", (name or "")).strip().lower()
    return (bool(s) and "player" in s and "shot" in s and not any(b in s for b in NEGATIVE_SHOTS_TERMS))

NEGATIVE_SOT_TERMS = {
    "header","headed","left foot","right foot","from outside","outside box","outside of box",
    "first half","1st half","second half","2nd half","half",
    "combo","goal","goals","assist","assists",
    "team shots on target", "shots conceded", "keeper saves", "goalkeeper",
}
def market_is_player_sot(name: str) -> bool:
    s = re.sub(r"\s+", " ", (name or "")).strip().lower()
    if not s: return False
    if "shot" not in s and "sot" not in s and "on target" not in s: return False
    if any(bad in s for bad in NEGATIVE_SOT_TERMS): return False
    return ("on target" in s) or ("sot" in s)

def parse_line_value(opt):
    for key in ("line","hdp"):
        v = opt.get(key)
        if v is None: continue
        try: return float(v)
        except: pass
    return None

def _extract_numeric_from_label(label: str):
    # e.g. "(2)" or "2+" patterns
    if not label: return None
    m = re.search(r"\((\d+)\)", label)
    if m: return int(m.group(1))
    m2 = re.search(r"\b(\d+)\s*\+\b", label)
    if m2: return int(m2.group(1))
    return None

def price_from_option_for(threshold: int, opt: dict):
    """Return price if opt corresponds to the exact threshold (1+ -> line 0.5 OR label (1)/1+), else None."""
    # Line-based: over price at line = (threshold - 0.5)
    line = parse_line_value(opt)
    if line is not None and math.isclose(line, threshold - 0.5, abs_tol=1e-6):
        for k in ("over","price","odds","decimal","dec"):
            v = opt.get(k)
            if v in (None, "N/A"): continue
            try: return float(v)
            except: pass
    # Label-based: "(1)" or "1+"
    n = _extract_numeric_from_label(opt.get("label") or "")
    if n == threshold:
        for k in ("over","price","odds","decimal","dec"):
            v = opt.get(k)
            if v in (None, "N/A"): continue
            try: return float(v)
            except: pass
    # Some books encode 1+ as yes/over with no line exposed (rare)
    for k in ("yes","Yes","YES"):
        if k in opt:
            try: return float(opt[k])
            except: pass
    return None

# ================== MONEYLINE (optional display only) ==================
MATCH_WINNER_KEYS = ["1x2","match result","match winner","moneyline","full time result","to win","win/draw/win","wdw","ml"]
def market_is_match_winner(name: str) -> bool:
    s = (name or "").strip().lower()
    return bool(s) and any(k in s for k in MATCH_WINNER_KEYS)

def _extract_moneyline_prices(market, home_name: str, away_name: str):
    res = {}
    odds = market.get("odds")
    if isinstance(odds, dict):
        for side in ("home","away"):
            try: res[side] = float(odds.get(side))
            except: pass
        return res
    if isinstance(odds, list) and len(odds) == 1 and isinstance(odds[0], dict):
        entry = odds[0]
        if any(k in entry for k in ("home","away","draw")):
            for side in ("home","away"):
                try: res[side] = float(entry.get(side))
                except: pass
            return res
    if isinstance(odds, list):
        for opt in odds:
            label = (opt.get("label") or "").strip()
            try: price = float(opt.get("over", opt.get("price", float("inf"))))
            except: continue
            ln = norm(label)
            if team_names_match(label, home_name) or ln in {"home","1"}:
                res["home"] = min(res.get("home", float("inf")), price)
            elif team_names_match(label, away_name) or ln in {"away","2"}:
                res["away"] = min(res.get("away", float("inf")), price)
    return res

def min_win_prices(event_odds):
    home = event_odds.get("home",""); away = event_odds.get("away","")
    best = {"home": None, "away": None}
    bms = _filter_to_bet365_bookmakers(event_odds)
    for _bm, markets in (bms.items()):
        for m in markets or []:
            if not market_is_match_winner(m.get("name","")): continue
            prices = _extract_moneyline_prices(m, home, away)
            for side in ("home","away"):
                p = prices.get(side)
                if isinstance(p, (int,float)):
                    if best[side] is None or p < best[side]:
                        best[side] = p
    return best["home"], best["away"]

# ================== MAIN ==================
def get_events_index():
    events = get_upcoming_events(LEAGUE_SLUGS)
    print(f"[INFO] Events fetched: {len(events)}")
    # simple (home,away) string index
    idx = []
    for ev in events:
        hid = ev.get("home",""); aid = ev.get("away","")
        eid = ev.get("id")
        if eid:
            idx.append({"id": eid, "home": hid, "away": aid, "urls": ev.get("urls", {})})
    return idx

def find_event_for_team(idx, team_name: str):
    for ev in idx:
        if team_names_match(team_name, ev["home"]) or team_names_match(team_name, ev["away"]):
            return ev
    return None

def main():
    targets = load_targets()
    if not targets:
        print("No player targets from criteria files.")
        return

    # Build minimal (team -> event) mapping to cut API calls
    ev_idx = get_events_index()

    # group targets by event id to batch odds fetch
    buckets = {}  # event_id -> list of targets
    unmatched = []
    for t in targets:
        ev = None
        if t.get("team"):
            ev = find_event_for_team(ev_idx, t["team"])
        # if team unknown, leave unmatched for broad scan later (rare)
        if ev:
            buckets.setdefault(ev["id"], []).append({**t, "home": ev["home"], "away": ev["away"]})
        else:
            unmatched.append(t)

    # Fetch odds for matched events
    odds_payloads = []
    event_ids = sorted(buckets.keys())
    for batch in chunked(event_ids, 10):
        odds_payloads.extend(get_odds_multi(batch))

    id_to_ev = {o.get("id"): o for o in odds_payloads if o.get("id")}
    print(f"[INFO] Odds payloads: {len(id_to_ev)} (events matched)")

    flagged = []

    # scan matched events
    for eid, tlist in buckets.items():
        ev = id_to_ev.get(eid)
        if not ev: continue
        home, away = ev.get("home",""), ev.get("away","")
        urls = ev.get("urls", {}) or {}
        bet365 = _filter_to_bet365_bookmakers(ev)
        if not bet365: continue

        # moneylines (display only)
        best_home_ml, best_away_ml = min_win_prices(ev)

        for t in tlist:
            best_price = None; best_book = None; best_market = None
            for bm_slug, markets in bet365.items():
                bm_name = _bet365_name_from_urls(urls, bm_slug)
                for m in markets or []:
                    name = m.get("name","")
                    if t["stat"] == "shots":
                        if not market_is_player_shots(name): continue
                    else:
                        if not market_is_player_sot(name): continue
                    odds = m.get("odds")
                    if isinstance(odds, list):
                        for opt in odds:
                            if not player_label_matches(t["player"], opt.get("label")):
                                continue
                            price = price_from_option_for(t["threshold"], opt)
                            if price is None: continue
                            if price >= MIN_PRICE and (best_price is None or price > best_price + 1e-9):
                                best_price = price; best_book = bm_name; best_market = name
                    elif isinstance(odds, dict):
                        for label, opt in odds.items():
                            if not player_label_matches(t["player"], label):
                                continue
                            if not isinstance(opt, dict):
                                continue
                            p = price_from_option_for(t["threshold"], opt)
                            if p is None: continue
                            if p >= MIN_PRICE and (best_price is None or p > best_price + 1e-9):
                                best_price = p; best_book = bm_name; best_market = name
            if best_price is not None:
                flagged.append({
                    "player": t["player"],
                    "team": t.get("team") or "",
                    "stat": t["stat"],
                    "threshold": t["threshold"],
                    "price": best_price,
                    "book": best_book or "Bet365",
                    "market": best_market or "",
                    "home": home, "away": away,
                    "home_ml": best_home_ml, "away_ml": best_away_ml,
                })

    # OPTIONAL: if some targets couldn't map to a fixture, you could scan all
    # bet365 events/markets to rescue them (omitted by default to keep calls low).

    # Dedup to best price per (player, stat, thr)
    best_map = {}
    for r in flagged:
        key = (r["player"], r["stat"], r["threshold"])
        cur = best_map.get(key)
        if (cur is None) or (r["price"] > cur["price"] + 1e-9):
            best_map[key] = r

    rows = list(best_map.values())
    # group sort: SOT 1+ first, SHOTS 2+, SHOTS 1+, then price desc
    def bucket_key(r):
        if r["stat"] == "sot" and r["threshold"] == 1: return 0
        if r["stat"] == "shots" and r["threshold"] == 2: return 1
        if r["stat"] == "shots" and r["threshold"] == 1: return 2
        return 9
    rows.sort(key=lambda r: (bucket_key(r), -r["price"], r["player"]))

    # Write clean TXT
    lines = []
    lines.append(f"Generated at (UTC): {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    lines.append(f"Min price: {MIN_PRICE:.2f}  |  Bookmaker: Bet365  |  Leagues: {len(LEAGUE_SLUGS)}")
    lines.append("")

    def group_title(stat, thr):
        if stat == "sot": return "SOT 1+"
        if stat == "shots" and thr == 1: return "SHOTS 1+"
        if stat == "shots" and thr == 2: return "SHOTS 2+"
        return f"{stat.upper()} {thr}+"

    current = None
    for r in rows:
        title = group_title(r["stat"], r["threshold"])
        if title != current:
            if current is not None:
                lines.append("")  # blank between groups
            lines.append(f"===== {title} — Bet365 — ≥ {MIN_PRICE:.2f} =====")
            header = f"{'Player':22} {'Team':22} {'Fixture':40} {'Price':>7} {'Market':30} {'HomeML':>7} {'AwayML':>7}"
            lines.append(header)
            lines.append("-"*len(header))
            current = title

        fixture = f"{r['home']} vs {r['away']}"
        home_ml = f"{r['home_ml']:.2f}" if isinstance(r['home_ml'], (int,float)) else "--"
        away_ml = f"{r['away_ml']:.2f}" if isinstance(r['away_ml'], (int,float)) else "--"
        lines.append(f"{r['player'][:22]:22} {r['team'][:22]:22} {fixture[:40]:40} {r['price']:>7.3f} {r['market'][:30]:30} {home_ml:>7} {away_ml:>7}")

    ensure_dir(OUT_TXT)
    content = "\n".join(lines).rstrip() + "\n"
    OUT_TXT.write_text(content, encoding="utf-8")
    print(f"[OK] wrote {OUT_TXT}")

if __name__ == "__main__":
    main()
