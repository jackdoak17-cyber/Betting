#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — SHOTS & SOT certs (robust discovery + per-threshold gating)

Buckets (each requires n>=7 and last-7 for that stat/threshold):
  • Player Shots: Over 0.5 (1+), Over 1.5 (2+), Over 2.5 (3+)
  • Player Shots On Target: Over 0.5 (1+), Over 1.5 (2+)

Data inputs (scanned recursively; with or without by_league/):
  - Shots:          data/player_shots/**/*.json
  - Shots on target:data/player_shots_on_target/**/*.json
  - Predicted XI:   data/predicted_xi/by_league/{league_id}.json  (optional team_id→name)

Bookmaker / odds filters:
  - Bet365 only
  - Minimum price >= MIN_DEC_PRICE (default 1.30)
  - Team ML (match winner) for player's side must be < TEAM_WIN_MAX (default 3.50)

Output:
  - data/value_bets/shots_certs.txt  (also printed; no KO time in lines)

ENV:
  ODDS_API_KEY   (required)
  MIN_DEC_PRICE  (optional, default 1.30)
  TEAM_WIN_MAX   (optional, default 3.50)
"""

import os, re, json, math, time, random, unicodedata, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Iterable
import requests
from itertools import islice

# ========= CONFIG =========
SPORT = "football"
BOOKMAKERS = "Bet365"
MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_WIN_MAX", "3.50"))

# Sport → league slug mapping (extend if needed)
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

EVENTS_API_URL = "https://api.odds-api.io/v3/events"
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"
HTTP_HEADERS = {"accept": "application/json", "user-agent": "odds-shots-certs/1.3"}
TIMEOUT = 25

ROOT     = Path(".")
PX_DIR   = ROOT / "data" / "predicted_xi" / "by_league"
SH_ROOT  = ROOT / "data" / "player_shots"
SOT_ROOT = ROOT / "data" / "player_shots_on_target"
OUT_DIR  = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "shots_certs.txt"

# ========= MARKET FILTERS =========
NEGATIVE_SHOTS_TERMS = {
    "on target","sot","outside","outside box","outside of box","from outside","outside the box",
    "header","headers","head","left foot","right foot","right-foot","left-foot",
    "first half","1st half","2nd half","second half","half",
    "distance","long range","goal","goals","to score","assist","assists","ga","g/a",
    "shots on target","on-target","from corner","from free kick","penalty","penalties"
}
def market_is_player_shots(name: str) -> bool:
    s = re.sub(r"\s+", " ", (name or "")).strip().lower()
    return bool(s) and "player" in s and "shot" in s and not any(b in s for b in NEGATIVE_SHOTS_TERMS)

NEGATIVE_SOT_TERMS = {
    "outside","outside box","outside of box","from outside","outside the box",
    "header","headers","head",
    "first half","1st half","2nd half","second half","half",
    "distance","long range","from corner","from free kick","penalty","penalties"
}
def market_is_player_sot(name: str) -> bool:
    s = re.sub(r"\s+", " ", (name or "")).strip().lower()
    return bool(s) and "player" in s and "shot" in s and "target" in s and not any(b in s for b in NEGATIVE_SOT_TERMS)

# ========= STRING NORMALISATION =========
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}
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

def cleanup_label(label: str) -> str:
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

def extract_last_name_initial(name: str):
    if not name: return None, None
    name2 = strip_accents(name).replace(".", " ").strip()
    parts = [p for p in name2.split() if p]
    if not parts: return None, None
    last = norm(parts[-1]); initial = None
    for p in parts[:-1]:
        ch = p.strip()[0:1]
        if ch: initial = ch.lower(); break
    return last, initial

def player_label_matches(player: str, option_label: str) -> bool:
    if not player or not option_label: return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(option_label))
    if not last or last not in label: return False
    if initial:
        first_word_initial = label.split()[0][0:1] if label.split() else None
        if first_word_initial and first_word_initial == initial: return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

# ========= IO HELPERS =========
def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _team_name_map(league_id: int) -> Dict[int, str]:
    blob = _load_json(PX_DIR / f"{league_id}.json") or {}
    m: Dict[int, str] = {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

def _iter_json_files(root: Path) -> Iterable[Path]:
    if not root.exists(): return []
    # recursively find *.json under root
    return root.rglob("*.json")

def _digits_in(s: str) -> Optional[int]:
    m = re.search(r"(\d{1,6})", s or "")
    try: return int(m.group(1)) if m else None
    except: return None

def _extract_series(rec: dict, prefer_keys: List[str]) -> Optional[List[int]]:
    for k in prefer_keys + ["series","last_n","values","shots","sot"]:
        v = rec.get(k)
        if isinstance(v, list):
            nums = []
            for x in v:
                try: nums.append(int(x))
                except: nums.append(0)
            return nums
    return None

def last7_all_at_least(series: List[int], k: int) -> bool:
    seq = [x for x in series if isinstance(x, int)]
    if len(seq) < 7: return False
    head7 = seq[:7]
    tail7 = seq[-7:]
    return all(x >= k for x in head7) or all(x >= k for x in tail7)

def collect_stat_rows(root_dir: Path, need_k: int, prefer_keys: List[str]) -> List[dict]:
    """
    Scan any JSON under root_dir for player rows. Tries common shapes:
      - top-level { players|rows|data: [...] }
      - top-level list [...]
      - nested under { stats: { shots|shots_on_target: { players: [...] } } } (fallback)
    Each row must have: player name, team (or team_id), league_id, and a numeric series list.
    """
    out = []
    # group team name map cache per league
    team_maps: Dict[int, Dict[int, str]] = {}

    for fp in _iter_json_files(root_dir):
        blob = _load_json(fp)
        if blob is None: 
            continue

        # Harvest candidate rows array from various layouts
        candidate_arrays = []

        if isinstance(blob, list):
            candidate_arrays.append(blob)
        elif isinstance(blob, dict):
            for key in ("players","rows","data"):
                if isinstance(blob.get(key), list):
                    candidate_arrays.append(blob[key])
            # nested stat shapes
            stats = blob.get("stats") if isinstance(blob.get("stats"), dict) else None
            if stats:
                for sk in ("shots","shots_on_target"):
                    node = stats.get(sk)
                    if isinstance(node, dict):
                        for key in ("players","rows","data"):
                            if isinstance(node.get(key), list):
                                candidate_arrays.append(node[key])

        if not candidate_arrays:
            continue

        for arr in candidate_arrays:
            for rec in arr:
                if not isinstance(rec, dict): 
                    continue
                # player
                player = rec.get("name") or rec.get("player_name") or rec.get("player")
                if not player: 
                    continue
                # series
                series = _extract_series(rec, prefer_keys) or []
                if not isinstance(series, list) or len(series) < 7:
                    continue
                if not last7_all_at_least(series, need_k):
                    continue
                # league id
                lid = rec.get("league_id") or (rec.get("league", {}) or {}).get("id")
                if not isinstance(lid, int):
                    lid = _digits_in(fp.stem)  # fallback to digits in filename
                if not isinstance(lid, int):
                    continue
                # team
                team = rec.get("team") or rec.get("team_name")
                if not team:
                    tid = rec.get("team_id")
                    if isinstance(tid, int):
                        if lid not in team_maps:
                            team_maps[lid] = _team_name_map(lid)
                        team = team_maps[lid].get(tid)
                if not team:
                    continue
                pos = rec.get("position") or rec.get("pos") or ""
                out.append({
                    "league_id": lid, "player": player, "team": team,
                    "position": pos, "series": series[:10]
                })
    return out

# ========= ODDS API =========
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
            if r.status_code == 200:
                return r
            if r.status_code in (429,500,502,503,504):
                last_text = r.text
                sleep = base_sleep * (factor ** attempt) + random.uniform(0, 0.4)
                print(f"[RETRY] {url} {r.status_code}; sleeping {sleep:.1f}s...")
                time.sleep(sleep); attempt += 1; continue
            print(f"[HTTP {r.status_code}] {url} :: {r.text[:200]}")
            return None
        except requests.exceptions.RequestException as e:
            sleep = base_sleep * (factor ** attempt) + random.uniform(0, 0.4)
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

def get_odds_multi(event_ids: List[int], api_key: str) -> List[dict]:
    if not event_ids: return []
    r = http_get_with_retries(ODDS_MULTI_API_URL, {
        "apiKey": api_key, "eventIds": ",".join(map(str, event_ids)), "bookmakers": BOOKMAKERS
    })
    if not (r and r.status_code == 200): return []
    try: data = r.json()
    except: return []
    return data if isinstance(data, list) else []

def bet365_markets(ev: dict):
    for bm_name, markets in (ev.get("bookmakers") or {}).items():
        if "bet365" not in (bm_name or "").lower(): continue
        for m in markets or []:
            yield m

def parse_line(opt: dict) -> Optional[float]:
    if "hdp" in opt:
        try: return float(opt["hdp"])
        except: pass
    m = re.search(r"\(([-+]?\d+(?:\.\d+)?)\)", (opt.get("label") or ""))
    if m:
        try: return float(m.group(1))
        except: return None
    return None

def parse_over_price(opt: dict) -> Optional[float]:
    try:
        val = opt.get("over")
        return float(val) if val is not None else None
    except: return None

MATCH_WINNER_KEYS = ["1x2","match result","match winner","moneyline","full time result","to win","win/draw/win","wdw","ml"]
def market_is_match_winner(name: str) -> bool:
    s = (name or "").strip().lower()
    return bool(s) and any(k in s for k in MATCH_WINNER_KEYS)

def min_win_prices(ev: dict) -> Tuple[Optional[float], Optional[float]]:
    best_home = None; best_away = None
    for m in bet365_markets(ev):
        if not market_is_match_winner(m.get("name","")): continue
        odds = m.get("odds") or []
        for row in odds:
            try:
                h = float(row.get("home")) if row.get("home") not in (None, "N/A") else None
                a = float(row.get("away")) if row.get("away") not in (None, "N/A") else None
            except: h = a = None
            if isinstance(h, float):
                best_home = h if (best_home is None or h < best_home) else best_home
            if isinstance(a, float):
                best_away = a if (best_away is None or a < best_away) else best_away
    return best_home, best_away

# ========= MAIN =========
def main():
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: ODDS_API_KEY not set.")

    # Buckets: (kind, line, need_k, prefer_keys_for_series)
    buckets_cfg = [
        ("shots", 0.5, 1, ["shots","series"]),
        ("shots", 1.5, 2, ["shots","series"]),
        ("shots", 2.5, 3, ["shots","series"]),
        ("sot",   0.5, 1, ["sot","shots_on_target","series"]),
        ("sot",   1.5, 2, ["sot","shots_on_target","series"]),
    ]

    # Gather candidates per bucket (robust scanning)
    candidates_by_bucket: Dict[Tuple[str,float], List[dict]] = {}
    leagues_needed = set()
    for kind, line, need_k, keys in buckets_cfg:
        root = SH_ROOT if kind == "shots" else SOT_ROOT
        rows = collect_stat_rows(root, need_k, keys)
        candidates_by_bucket[(kind, line)] = rows
        leagues_needed.update(r["league_id"] for r in rows)

    # Diagnostics to help if empty
    total_cands = sum(len(v) for v in candidates_by_bucket.values())
    print(f"[CANDIDATES] Total across buckets: {total_cands}")
    for k in candidates_by_bucket:
        print(f"[CANDIDATES] {k}: {len(candidates_by_bucket[k])}")

    if not leagues_needed:
        print("[RESULT] No candidates meet last-7 thresholds for any bucket.")
        print(f"Scanned SH_ROOT={SH_ROOT.resolve()}  SOT_ROOT={SOT_ROOT.resolve()}")
        return

    # Fetch events per league
    events_by_league: Dict[int, List[dict]] = {}
    for lid in sorted(leagues_needed):
        slug = LEAGUE_SLUG_BY_ID.get(lid)
        if not slug:
            events_by_league[lid] = []
            print(f"[WARN] No slug mapping for league_id={lid}; skipping its candidates.")
            continue
        evs = get_events_for_league(slug, api_key)
        events_by_league[lid] = evs
        print(f"[EVENTS] {slug}: {len(evs)}")

    # Map teams -> event ids
    def find_event_ids(lid: int, team: str) -> List[int]:
        evs = events_by_league.get(lid, [])
        out = []
        for ev in evs:
            if team_names_match(team, ev.get("home","")) or team_names_match(team, ev.get("away","")):
                if isinstance(ev.get("id"), int): out.append(ev["id"])
        return out

    for rows in candidates_by_bucket.values():
        for r in rows:
            r["event_ids"] = find_event_ids(r["league_id"], r["team"])

    # Collect unique event ids and fetch odds
    all_event_ids = sorted({eid for rows in candidates_by_bucket.values() for r in rows for eid in (r.get("event_ids") or [])})
    print(f"[ODDS] Unique events to query: {len(all_event_ids)}")

    odds_payloads: List[dict] = []
    for i, batch in enumerate(chunked(all_event_ids, 10), start=1):
        print(f"[ODDS] batch {i} — {len(batch)} ids")
        odds_payloads.extend(get_odds_multi(batch, api_key))
    if not odds_payloads:
        print("[RESULT] No odds payloads; stopping.")
        return
    id_to_ev = {o.get("id"): o for o in odds_payloads if isinstance(o.get("id"), int)}

    # Find best prices per player/fixture/bucket
    results: Dict[Tuple[str,float], List[dict]] = { (k,l): [] for k,l,_,_ in buckets_cfg }

    def upsert_best(best_map, key, row):
        cur = best_map.get(key)
        if (cur is None) or (row["price"] > cur["price"] + 1e-9):
            best_map[key] = row

    for (kind, line, need_k, _keys) in buckets_cfg:
        rows = candidates_by_bucket[(kind, line)]
        best_map = {}
        for c in rows:
            for ev_id in c.get("event_ids") or []:
                ev = id_to_ev.get(ev_id)
                if not ev: continue
                home, away = ev.get("home",""), ev.get("away","")

                # Team ML filter
                home_ml, away_ml = min_win_prices(ev)
                side = "home" if team_names_match(c["team"], home) else ("away" if team_names_match(c["team"], away) else None)
                if not side: 
                    continue
                team_ml = home_ml if side == "home" else away_ml
                if team_ml is None or team_ml >= TEAM_ML_MAX:
                    continue

                # Market scan
                for m in bet365_markets(ev):
                    name = m.get("name","")
                    if kind == "shots":
                        if not market_is_player_shots(name): 
                            continue
                    else:
                        if not market_is_player_sot(name):
                            continue

                    odds = m.get("odds")
                    if isinstance(odds, list):
                        it = ((opt.get("label"), opt) for opt in odds)
                    elif isinstance(odds, dict):
                        it = odds.items()
                    else:
                        continue

                    for label, opt in it:
                        if not player_label_matches(c["player"], label): 
                            continue
                        l = parse_line(opt); price = parse_over_price(opt)
                        if l is None or not math.isclose(l, line, abs_tol=1e-6): 
                            continue
                        if price is None or price < MIN_PRICE:
                            continue
                        key = (c["player"], home, away, kind, float(line))
                        upsert_best(best_map, key, {
                            "player": c["player"], "position": c["position"], "team": c["team"],
                            "fixture": f"{home} vs {away}",
                            "price": float(price), "team_ml": float(team_ml),
                            "series": c["series"], "market": name, "line": float(line),
                        })
        results[(kind, line)] = list(best_map.values())

    # Render
    def section_title(kind: str, line: float) -> str:
        n_plus = {0.5:"1+", 1.5:"2+", 2.5:"3+"}.get(line, f"{line}+")
        base = "Player Shots" if kind == "shots" else "Player Shots On Target"
        return f"===== CERTS — {base} {n_plus} ====="

    header = f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}  |  Min price: {MIN_PRICE:.2f}  |  Team ML < {TEAM_ML_MAX:.2f}"
    sub = "Candidates per bucket are gated on last 7 for that stat/threshold (n>=7). Bet365 only. Strict market filters."
    lines_out = [header, sub, ""]

    order = [("shots",0.5), ("shots",1.5), ("shots",2.5), ("sot",0.5), ("sot",1.5)]
    any_hit = False
    for key in order:
        arr = results.get(key) or []
        if not arr: 
            continue
        any_hit = True
        arr.sort(key=lambda x: (-x["price"], x["player"]))
        lines_out.append(section_title(*key))
        for x in arr:
            ser7 = ",".join(map(str, x["series"][:7]))
            pos = f"[{x['position']}]" if x.get("position") else ""
            stat_name = "Player Shots" if key[0] == "shots" else "Player Shots On Target"
            lines_out.append(
                f" • {x['player']} {pos} — {x['team']} | {x['fixture']} | "
                f"{stat_name} Over {x['line']:.1f} @ {x['price']:.3f} | Team ML {x['team_ml']:.3f} | series7: {ser7}"
            )
        lines_out.append("")

    if not any_hit:
        lines_out.append("No matches found.")

    OUT_FILE.write_text("\n".join(lines_out).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines_out))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
