#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Find player-bet candidates (Shots / Shots on Target) by combining your collected hit rates
with live prices from Odds-API.io. Bookmakers restricted to bet365 + kambi and returned
as separate lists.

Scope control:
- Only fixtures where BOTH teams exist in your collected data (predicted_xi + player series)
  are considered. This effectively limits to "leagues we've already scraped".

Selection rules (from spec):
- CERTS: price >= 1.25 and (HR >= 100% or HR >= 90%) with n >= 8
- VALUE SINGLES: price >= 1.80 and HR >= 80% (n recorded but not enforced)
- Big Underdog filter (Shots/SOT only): exclude if team ML > 3.50

Reads (already produced by your collectors):
  - data/predicted_xi/by_league/{lid}.json
  - data/player_shots/by_league/{lid}.json            (players[].shots_last_n)
  - data/player_shots_on_target/by_league/{lid}.json  (players[].on_target_last_n)

Writes:
  - data/bets/player_candidates.json
      {
        "bet365": {"certs": [...], "value_singles": [...]},
        "kambi":  {"certs": [...], "value_singles": [...]},
        "audited": [...]
      }
  - data/bets/player_candidates.txt  # human-friendly sections

Odds API:
  - GET /v3/events?apiKey=...&sport=football
  - GET /v3/odds/multi?apiKey=...&eventIds=...&bookmakers=bet365,kambi
  (chunked to 10 event IDs per call)
"""

import os, re, time, math, random, unicodedata, json
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional
import requests

# ================== CONFIG ==================
API_KEY = os.getenv("ODDS_API_KEY", "")
SPORT = "football"

# Odds API: only these two
BOOKMAKERS_PARAM = os.getenv("ODDS_BOOKMAKERS", "bet365,kambi")

# Respect a safe multi limit (10/eventIds per your examples & to avoid 400s)
MULTI_CHUNK_SIZE = int(os.getenv("ODDS_MULTI_CHUNK", "10"))

# Markets & thresholds
CERT_MIN_PRICE   = float(os.getenv("CERT_MIN_PRICE", "1.25"))
CERT_MIN_N       = int(os.getenv("CERT_MIN_N", "8"))
VALUE_MIN_PRICE  = float(os.getenv("VALUE_MIN_PRICE", "1.80"))
UNDERDOG_MAX_ML  = float(os.getenv("UNDERDOG_MAX_ML", "3.50"))

# FS layout
ROOT = Path(".")
PX_DIR     = ROOT / "data" / "predicted_xi" / "by_league"
SHOTS_DIR  = ROOT / "data" / "player_shots" / "by_league"
SOT_DIR    = ROOT / "data" / "player_shots_on_target" / "by_league"
OUT_JSON   = ROOT / "data" / "bets" / "player_candidates.json"
OUT_TXT    = ROOT / "data" / "bets" / "player_candidates.txt"

# API endpoints / headers
EVENTS_API_URL = "https://api.odds-api.io/v3/events"
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"
HTTP_HEADERS = {"accept": "application/json"}

# ================== GENERIC UTILS ==================
def chunked(it, n):
    it = iter(it)
    while True:
        chunk = list([x for _, x in zip(range(n), it)])
        if not chunk:
            return
        yield chunk

def http_get_with_retries(url, params, max_retries=5, base_sleep=1.0, factor=1.8):
    attempt = 0; last_text = ""
    while attempt < max_retries:
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=25)
            if r.status_code == 200: return r
            if r.status_code in (429,500,502,503,504):
                last_text = r.text
                sleep = base_sleep*(factor**attempt)+random.uniform(0,0.4)
                print(f"[RETRY] {url} status {r.status_code}. Sleeping {sleep:.1f}s...")
                time.sleep(sleep); attempt += 1; continue
            print(f"[ERROR] {url} -> {r.status_code}: {r.text[:160]}"); return None
        except requests.exceptions.RequestException as e:
            sleep = base_sleep*(factor**attempt)+random.uniform(0,0.4)
            print(f"[NET] {url} exception: {e}. Sleeping {sleep:.1f}s...")
            time.sleep(sleep); attempt += 1
    if last_text:
        print(f"[ERROR] Exhausted retries for {url}. Last: {last_text[:200]}")
    else:
        print(f"[ERROR] Exhausted retries for {url}.")
    return None

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s):
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def cleanup_label(label: str) -> str:
    if not label: return ""
    # drop trailing parenthetical qualifiers: "Name (X)" -> "Name"
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label).strip()

# ================== DATA LOADERS ==================
def team_name_index() -> Dict[str, Dict[str, Any]]:
    """
    Build a team-name index from predicted_xi (=> "what leagues/teams we have").
    norm(team_name) -> {team_id, league_id, team_name}
    """
    idx: Dict[str, Dict[str, Any]] = {}
    for f in PX_DIR.glob("*.json"):
        try:
            blob = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        lid = blob.get("league_id")
        try:
            lid = int(lid if lid is not None else f.stem.split(".")[0])
        except Exception:
            continue
        for fx in (blob.get("fixtures") or []):
            for side in ("home","away"):
                t = fx.get(side) or {}
                tid, nm = t.get("team_id"), t.get("name")
                if isinstance(tid, int) and isinstance(nm, str) and nm:
                    idx[norm(nm)] = {"team_id": tid, "league_id": lid, "team_name": nm}
    return idx

def _players(payload: dict) -> List[dict]:
    return [x for x in (payload.get("players") or []) if isinstance(x, dict)]

def _ints(seq) -> List[int]:
    if not isinstance(seq, list): return []
    out = []
    for v in seq:
        try: out.append(int(v))
        except Exception:
            try: out.append(int(float(v)))
            except Exception: pass
    return out

def load_player_series() -> Dict[int, Dict[int, List[dict]]]:
    """
    Return: per_league[league_id][team_id] -> list of players with series_shots/series_sot
    Only leagues we *actually* have files for will appear here.
    """
    per_league: Dict[int, Dict[int, List[dict]]] = {}

    # Shots
    for p in SHOTS_DIR.glob("*.json"):
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            lid = int(blob.get("league_id") or p.stem.split(".")[0])
        except Exception:
            continue
        for r in _players(blob):
            team_id = int(r.get("team_id") or 0)
            if not team_id: continue
            per_league.setdefault(lid, {}).setdefault(team_id, []).append({
                "player_id": int(r.get("player_id") or 0),
                "name": r.get("name") or "",
                "team_id": team_id,
                "series_shots": _ints(r.get("shots_last_n") or []),
                "series_sot": None,
            })

    # Shots on target: attach when present
    sot_map = {}
    for p in SOT_DIR.glob("*.json"):
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            lid = int(blob.get("league_id") or p.stem.split(".")[0])
        except Exception:
            continue
        for r in _players(blob):
            key = (lid, int(r.get("team_id") or 0), int(r.get("player_id") or 0))
            sot_map[key] = _ints(r.get("on_target_last_n") or [])

    for lid, teams in per_league.items():
        for tid, arr in teams.items():
            for row in arr:
                seq = sot_map.get((lid, tid, row["player_id"]))
                if seq is not None:
                    row["series_sot"] = seq

    return per_league

# ================== ODDS API HELPERS ==================
def get_events_all_football() -> List[dict]:
    """
    Fetch all upcoming football events (no league param; avoid 404s for some slugs),
    we will *filter* to our collected leagues/teams afterwards.
    """
    r = http_get_with_retries(EVENTS_API_URL, {"apiKey": API_KEY, "sport": SPORT})
    if not (r and r.status_code == 200):
        return []
    try:
        data = r.json()
    except Exception:
        return []
    return data if isinstance(data, list) else []

def get_odds_multi_chunked(event_ids: List[int], chunk_size: int = MULTI_CHUNK_SIZE) -> List[dict]:
    """
    Call /v3/odds/multi in safe chunks with only bet365,kambi.
    """
    all_rows: List[dict] = []
    for chunk in chunked(event_ids, chunk_size):
        r = http_get_with_retries(ODDS_MULTI_API_URL, {
            "apiKey": API_KEY,
            "eventIds": ",".join(map(str, chunk)),
            "bookmakers": BOOKMAKERS_PARAM
        })
        if r and r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                data = None
            if isinstance(data, list):
                all_rows.extend(data)
    return all_rows

MATCH_WINNER_KEYS = {"1x2","match result","match winner","moneyline","full time result","to win","win/draw/win","wdw","ml"}

def market_is_match_winner(name: str) -> bool:
    s = (name or "").strip().lower()
    return bool(s) and any(k in s for k in MATCH_WINNER_KEYS)

def parse_line_value(opt: dict) -> Optional[float]:
    for k in ("hdp","line"):
        if k in opt:
            try: return float(opt[k])
            except Exception: pass
    return None

def classify_bookmaker(name: str) -> Optional[str]:
    n = (name or "").strip().lower()
    if "bet365" in n: return "bet365"
    if "kambi"  in n: return "kambi"
    return None  # shouldn't happen—API is restricted to bet365,kambi

def _bookmakers_iter(bm_payload):
    if not isinstance(bm_payload, dict): return
    for bm_name, markets in bm_payload.items():
        if isinstance(markets, list):
            for m in markets:
                if isinstance(m, dict):
                    yield bm_name, m
        elif isinstance(markets, dict):
            yield bm_name, markets

def extract_moneyline_prices(market: dict, home_name: str, away_name: str) -> Dict[str, float]:
    """
    Robustly pull ML prices across common schema shapes.
    Returns: {"home": price?, "away": price?}
    """
    res: Dict[str, float] = {}
    odds = market.get("odds")
    if isinstance(odds, dict):
        for side in ("home","away"):
            try: res[side] = float(odds.get(side))
            except Exception: pass
        return res
    if isinstance(odds, list) and len(odds) == 1 and isinstance(odds[0], dict):
        entry = odds[0]
        for side in ("home","away"):
            try: res[side] = float(entry.get(side))
            except Exception: pass
        return res
    if isinstance(odds, list):
        for opt in odds:
            label = (opt.get("label") or "").strip().lower()
            try: price = float(opt.get("over"))
            except Exception: continue
            if label in ("home","1") or norm(label) == norm(home_name):
                res["home"] = min(res.get("home", float("inf")), price)
            elif label in ("away","2") or norm(label) == norm(away_name):
                res["away"] = min(res.get("away", float("inf")), price)
    return res

# ================== MARKET PARSING ==================
NEGATIVE_TERMS = {
    "assist","goal","goals","passes","tackles","fouls","cards","offsides","interceptions",
    "dribbles","duels","aerial","to be fouled","fouled"
}

def parse_player_prop(label: str) -> Tuple[str, str]:
    """
    Return (player_name, market_type) where market_type in {"shots","sot","other"}.
    """
    s = (label or "").strip()
    if not s: return "", "other"
    base = cleanup_label(s)
    parts = [p.strip() for p in base.split(" - ", 1)]
    player = parts[0] if parts else base
    rest = parts[1].lower() if len(parts) > 1 else ""
    if "shots on target" in rest or ("on target" in rest and "shot" in rest):
        return player, "sot"
    if "shots" in rest and "on target" not in rest:
        return player, "shots"
    r = base.lower()
    if "shots on target" in r: return player, "sot"
    if " on target " in r and "shot" in r: return player, "sot"
    if " shots" in r: return player, "shots"
    return player, "other"

def player_label_matches(player_name: str, option_label: str) -> bool:
    """
    Tolerant match: require last name, allow flexible first initial.
    """
    if not player_name or not option_label: return False
    pl = strip_accents(player_name).replace(".", " ").strip()
    parts = [p for p in pl.split() if p]
    if not parts: return False
    last = norm(parts[-1])
    label = norm(cleanup_label(option_label))
    return last in label

# ================== HIT RATE ==================
def hit_rate_for_line(series: List[int], line: float) -> Tuple[float, int]:
    """
    Over X.5 hit: value > line.
    """
    if not series: return 0.0, 0
    hits = sum(1 for v in series if v > line)
    n = len(series)
    return (hits / n if n else 0.0), n

# ================== MAIN ==================
def main():
    if not API_KEY:
        raise SystemExit("Missing ODDS_API_KEY environment variable.")

    # Index "what we have" from your data
    tindex = team_name_index()
    per_league = load_player_series()

    # Fetch all events, then keep only those where BOTH teams are in our data
    raw_events = get_events_all_football()
    events = []
    for ev in raw_events:
        eid = ev.get("id")
        home_name = ev.get("home") or ev.get("home_team") or ""
        away_name = ev.get("away") or ev.get("away_team") or ""
        if not (isinstance(eid, int) and home_name and away_name):
            continue
        hk, ak = norm(home_name), norm(away_name)
        hinfo, ainfo = tindex.get(hk), tindex.get(ak)
        if not (hinfo and ainfo):
            continue
        # Only proceed if we also have player-series for the league
        if hinfo["league_id"] not in per_league or ainfo["league_id"] not in per_league:
            continue
        events.append({
            "id": eid,
            "home": home_name,
            "away": away_name,
            "home_info": hinfo,
            "away_info": ainfo,
        })

    if not events:
        print("[WARN] No upcoming events matched your collected leagues/teams.")
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
        empty = {"certs": [], "value_singles": []}
        payload = {"bet365": empty, "kambi": empty, "audited": []}
        OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        OUT_TXT.write_text(
            "=== BET365 — CERTS ===\n(none)\n\n=== BET365 — VALUE SINGLES ===\n(none)\n\n"
            "=== KAMBI — CERTS ===\n(none)\n\n=== KAMBI — VALUE SINGLES ===\n(none)\n",
            encoding="utf-8"
        )
        return

    # Get odds in chunks (bet365,kambi only)
    event_ids = [e["id"] for e in events]
    odds_payloads = {int(row.get("id")): row for row in get_odds_multi_chunked(event_ids)}

    # Output buckets (split per bookmaker family)
    out = {
        "bet365": {"certs": [], "value_singles": []},
        "kambi":  {"certs": [], "value_singles": []},
    }
    audited: List[dict] = []

    for ev in events:
        eid = ev["id"]
        home_name, away_name = ev["home"], ev["away"]
        home_info, away_info = ev["home_info"], ev["away_info"]
        lid = home_info["league_id"] if home_info["league_id"] in per_league else away_info["league_id"]

        event_odds = odds_payloads.get(eid) or {}

        # Extract moneyline for underdog filter
        home_ml, away_ml = None, None
        for bm, mk in _bookmakers_iter(event_odds.get("bookmakers")):
            if market_is_match_winner(mk.get("name")):
                prices = extract_moneyline_prices(mk, home_name, away_name)
                home_ml = prices.get("home", home_ml)
                away_ml = prices.get("away", away_ml)

        # Build player pools
        team_players = {
            "home": [r for r in per_league.get(lid, {}).get(home_info["team_id"], [])],
            "away": [r for r in per_league.get(lid, {}).get(away_info["team_id"], [])],
        }

        for bm, mk in _bookmakers_iter(event_odds.get("bookmakers")):
            fam = classify_bookmaker(bm)
            if fam not in ("bet365","kambi"):
                continue
            odds_list = mk.get("odds")
            if not isinstance(odds_list, list):
                continue
            for opt in odds_list:
                label = opt.get("label") or ""
                try:
                    over_price = float(opt.get("over"))
                except Exception:
                    continue
                line = parse_line_value(opt)
                if line is None:
                    continue

                player_str, mkt = parse_player_prop(label)
                if mkt not in {"shots","sot"}:
                    continue

                # Match player to team (home/away)
                matched_side = None
                matched_row = None
                for side in ("home","away"):
                    for row in team_players[side]:
                        if player_label_matches(row["name"], player_str):
                            matched_side = side
                            matched_row = row
                            break
                    if matched_row: break
                if not matched_row:
                    continue

                series = (matched_row["series_sot"] if mkt == "sot" else matched_row["series_shots"]) or []
                hr, n = hit_rate_for_line(series, line)

                # Big-underdog filter (Shots/SOT only)
                team_ml = home_ml if matched_side == "home" else away_ml
                if isinstance(team_ml, (int,float)) and team_ml > UNDERDOG_MAX_ML:
                    continue

                # Classify
                tag = None
                if n >= CERT_MIN_N and over_price >= CERT_MIN_PRICE and (hr >= 1.0 or hr >= 0.90):
                    tag = "CERT"
                elif over_price >= VALUE_MIN_PRICE and hr >= 0.80:
                    tag = "VALUE"

                audited.append({
                    "event_id": eid,
                    "home": home_info["team_name"],
                    "away": away_info["team_name"],
                    "bookmaker": bm,
                    "family": fam,
                    "market_label": label,
                    "market_type": mkt,
                    "line": line,
                    "price": over_price,
                    "player_id": matched_row["player_id"],
                    "player_name": matched_row["name"],
                    "team_side": matched_side,
                    "team_ml": team_ml,
                    "hit_rate": round(hr, 4),
                    "n": n,
                    "series": series[:10],
                    "classification": tag or "",
                })

                if not tag:
                    continue

                out_row = {
                    "type": tag,
                    "event_id": eid,
                    "home": home_info["team_name"],
                    "away": away_info["team_name"],
                    "bookmaker": bm,
                    "market": "Shots on Target" if mkt == "sot" else "Shots",
                    "line": line,
                    "price": over_price,
                    "player_id": matched_row["player_id"],
                    "player_name": matched_row["name"],
                    "team_side": matched_side,
                    "team_ml": team_ml,
                    "hit_rate": round(hr, 4),
                    "n": n,
                }
                if tag == "CERT":
                    out[fam]["certs"].append(out_row)
                else:
                    out[fam]["value_singles"].append(out_row)

    # Sort for readability
    for fam in ("bet365","kambi"):
        out[fam]["certs"].sort(key=lambda r: (-r["hit_rate"], -r["n"], -r["price"]))
        out[fam]["value_singles"].sort(key=lambda r: (-r["price"], -r["hit_rate"], -r["n"]))

    # Write outputs
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {"bet365": out["bet365"], "kambi": out["kambi"], "audited": audited}
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def fmt_row(band: str, r: dict) -> str:
        return (f"[{band}] {r['home']} vs {r['away']} — {r['player_name']} "
                f"{r['market']} Over {r['line']:.1f} @ {r['price']:.2f} ({r['bookmaker']}) "
                f"HR={r['hit_rate']*100:.0f}% n={r['n']} ML={r['team_ml'] if r['team_ml'] is not None else '?'}")

    lines: List[str] = []
    lines.append("=== BET365 — CERTS ===")
    lines += [fmt_row("CERT", r) for r in out["bet365"]["certs"]] or ["(none)"]
    lines.append("")
    lines.append("=== BET365 — VALUE SINGLES ===")
    lines += [fmt_row("VALUE", r) for r in out["bet365"]["value_singles"]] or ["(none)"]
    lines.append("")
    lines.append("=== KAMBI — CERTS ===")
    lines += [fmt_row("CERT", r) for r in out["kambi"]["certs"]] or ["(none)"]
    lines.append("")
    lines.append("=== KAMBI — VALUE SINGLES ===")
    lines += [fmt_row("VALUE", r) for r in out["kambi"]["value_singles"]] or ["(none)"]

    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT_JSON} and {OUT_TXT}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
