#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Find player-bet candidates by combining your collected hit rates with live prices.

Inputs (already produced by your collectors):
  - data/player_shots/by_league/{lid}.json            (key: shots_last_n)
  - data/player_shots_on_target/by_league/{lid}.json  (key: on_target_last_n)
  - data/predicted_xi/by_league/{lid}.json            (for team names / mapping)

External:
  - ODDS API (https://api.odds-api.io) using $ODDS_API_KEY

Outputs:
  - data/bets/player_candidates.json
  - data/bets/player_candidates.txt

Selection logic:
  - Markets: Player Shots, Player Shots on Target
  - "CERTS":  (a) HR >= 1.00 AND n >= 8 AND price >= 1.25
              (b) HR >= 0.90 AND n >= 8 AND price >= 1.25
  - "VALUE":  HR >= 0.80 AND price >= 1.80  (n less important; we still record n)

Special rule:
  - For SHOTS / SOT only, exclude players if their team ML price > 3.50 (big underdog filter).

Notes:
  - Lines are treated as "Over X.5". Hit = integer_series_value > line.
  - Uses robust string matching between book labels and player names.
  - League scope is configurable via LEAGUE_SLUGS (Odds API slugs).
"""

import os
import re
import json
import time
import math
import glob
import unicodedata
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional
import requests

# ------------ Config ------------

API_KEY = os.getenv("ODDS_API_KEY", "")
SPORT = "football"

# High-quality books first (tweak if you like)
BOOKMAKERS = os.getenv("ODDS_BOOKMAKERS", "Bet365,Pinnacle,Unibet,SkyBet,Betfair,WilliamHill")

# Odds API endpoints
EVENTS_API_URL = "https://api.odds-api.io/v3/events"
ODDS_API_URL   = "https://api.odds-api.io/v3/odds"
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"
HTTP_HEADERS = {"accept": "application/json"}

# Leagues scope (Odds API slugs)
LEAGUE_SLUGS = [
    "england-premier-league", "england-championship",
    "italy-serie-a", "italy-serie-b",
    "spain-laliga", "spain-laliga-2",
    "france-ligue-1", "germany-bundesliga",
    "netherlands-eredivisie", "turkiye-super-lig",
    "portugal-primeira-liga", "belgium-pro-league",
    "scotland-premiership",
]

# Selection thresholds
CERT_MIN_PRICE   = 1.25
CERT_MIN_N       = 8
VALUE_MIN_PRICE  = 1.80
UNDERDOG_MAX_ML  = 3.50  # filter: exclude if team ML > this (Shots / SOT only)

# Where your data lives
ROOT = Path(".")
PX_DIR     = ROOT / "data" / "predicted_xi" / "by_league"
SHOTS_DIR  = ROOT / "data" / "player_shots" / "by_league"
SOT_DIR    = ROOT / "data" / "player_shots_on_target" / "by_league"
OUT_JSON   = ROOT / "data" / "bets" / "player_candidates.json"
OUT_TXT    = ROOT / "data" / "bets" / "player_candidates.txt"

# ------------ Small utils ------------

def strip_accents(s: str) -> str:
    if not isinstance(s, str): return ""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

def norm(s: str) -> str:
    s = strip_accents((s or "").lower())
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s

def http_get(url: str, params: dict, retries: int = 3, backoff: float = 1.5, timeout: float = 15.0):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=timeout)
            if r.status_code == 429:
                time.sleep(min(60, backoff ** (i+1)))
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(backoff ** (i+1))
    raise last or RuntimeError("GET failed")

def load_json(p: Path) -> Any:
    if not p.is_file(): return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

# ------------ Load team names (from predicted_xi) ------------

def team_name_index() -> Dict[str, Dict[str, Any]]:
    """
    Return: norm_team_name -> {"team_id": int, "league_id": int, "team_name": str}
    """
    out: Dict[str, Dict[str, Any]] = {}
    for f in PX_DIR.glob("*.json"):
        blob = load_json(f) or {}
        lid = int(blob.get("league_id") or f.stem)
        for fx in (blob.get("fixtures") or []):
            for side in ("home", "away"):
                t = fx.get(side) or {}
                tid, nm = t.get("team_id"), t.get("name")
                if isinstance(tid, int) and isinstance(nm, str) and nm:
                    out.setdefault(norm(nm), {"team_id": tid, "league_id": lid, "team_name": nm})
    return out

# ------------ Load player series (shots / SOT) ------------

def _players(payload: dict) -> List[dict]:
    return [x for x in (payload.get("players") or []) if isinstance(x, dict)]

def _int_series(seq) -> List[int]:
    if not isinstance(seq, list): return []
    out = []
    for v in seq:
        try: out.append(int(v))
        except Exception:
            try: out.append(int(float(v)))
            except Exception: pass
    return out

def load_player_series() -> Dict[int, Dict[int, Dict[str, Any]]]:
    """
    Return: per_league[league_id][team_id] -> list of player dicts:
            {player_id, name, team_id, series_shots, series_sot}
    """
    per_league: Dict[int, Dict[int, List[dict]]] = {}

    # Shots
    for p in SHOTS_DIR.glob("*.json"):
        blob = load_json(p) or {}
        lid = int(blob.get("league_id") or p.stem)
        for r in _players(blob):
            series = _int_series(r.get("shots_last_n") or [])
            per_league.setdefault(lid, {}).setdefault(int(r.get("team_id") or 0), []).append({
                "player_id": int(r.get("player_id") or 0),
                "name": r.get("name") or "",
                "team_id": int(r.get("team_id") or 0),
                "series_shots": series,
                "series_sot": None,  # fill later if present
            })

    # Shots on target (optional but preferred)
    sot_by_lid_tid_pid: Dict[Tuple[int,int,int], List[int]] = {}
    for p in SOT_DIR.glob("*.json"):
        blob = load_json(p) or {}
        lid = int(blob.get("league_id") or p.stem)
        for r in _players(blob):
            seq = _int_series(r.get("on_target_last_n") or [])
            key = (lid, int(r.get("team_id") or 0), int(r.get("player_id") or 0))
            sot_by_lid_tid_pid[key] = seq

    # attach SOT if we have it
    for lid, teams in per_league.items():
        for tid, arr in teams.items():
            for row in arr:
                key = (lid, tid, row["player_id"])
                if key in sot_by_lid_tid_pid:
                    row["series_sot"] = sot_by_lid_tid_pid[key]

    return per_league

# ------------ Odds API helpers ------------

MATCH_WINNER_KEYS = {"1x2", "match result", "match winner", "moneyline", "full time result", "to win", "win/draw/win", "wdw", "ml"}

def market_is_match_winner(name: str) -> bool:
    s = (name or "").strip().lower()
    return bool(s) and any(k in s for k in MATCH_WINNER_KEYS)

def parse_line_value(opt: dict) -> Optional[float]:
    for k in ("hdp", "line"):
        v = opt.get(k)
        if v is None: continue
        try: return float(v)
        except Exception: pass
    return None

def get_events_for_leagues(slugs: List[str]) -> List[dict]:
    all_events = []
    for slug in slugs:
        r = http_get(EVENTS_API_URL, {"apiKey": API_KEY, "sport": SPORT, "league": slug})
        try: data = r.json()
        except Exception: data = None
        if isinstance(data, list):
            all_events.extend(data)
    return all_events

def get_odds_multi(event_ids: List[int]) -> List[dict]:
    if not event_ids: return []
    r = http_get(ODDS_MULTI_API_URL, {
        "apiKey": API_KEY,
        "eventIds": ",".join(map(str, event_ids)),
        "bookmakers": BOOKMAKERS
    })
    try: data = r.json()
    except Exception: return []
    return data if isinstance(data, list) else []

def _bookmakers_iter(bm_payload):
    """Yield (bookmaker_name, market) where market is a dict with name/odds fields."""
    if not isinstance(bm_payload, dict): return
    for bm_name, markets in bm_payload.items():
        if isinstance(markets, list):
            for m in markets:
                if isinstance(m, dict):
                    yield bm_name, m
        elif isinstance(markets, dict):
            # Some endpoints return dict (e.g., aggregated WDW)
            yield bm_name, markets

def _extract_moneyline_prices(market: dict, home_name: str, away_name: str) -> Dict[str, float]:
    """
    Robustly pull ML prices irrespective of schema style in odds payload.
    Returns: {"home": price?, "away": price?}
    """
    res: Dict[str, float] = {}
    odds = market.get("odds")
    # direct dict style: {"home":2.10,"draw":...,"away":3.75}
    if isinstance(odds, dict):
        for side in ("home", "away"):
            try: res[side] = float(odds.get(side))
            except Exception: pass
        return res
    # list with one dict containing home/away/draw
    if isinstance(odds, list) and len(odds) == 1 and isinstance(odds[0], dict):
        entry = odds[0]
        if any(k in entry for k in ("home", "away", "draw")):
            for side in ("home", "away"):
                try: res[side] = float(entry.get(side))
                except Exception: pass
            return res
    # list of options with labels
    if isinstance(odds, list):
        for opt in odds:
            label = (opt.get("label") or "").strip().lower()
            try: price = float(opt.get("over"))
            except Exception: continue
            if label in ("home", "1") or norm(label) in (norm(home_name),):
                res["home"] = min(res.get("home", float("inf")), price)
            elif label in ("away", "2") or norm(label) in (norm(away_name),):
                res["away"] = min(res.get("away", float("inf")), price)
    return res

# ------------ Matching helpers ------------

NEGATIVE_TERMS = {"assist", "goal", "goals", "passes", "tackles", "fouls", "cards", "offsides", "interceptions", "dribbles", "duels", "aerial", "to be fouled", "fouled"}

def parse_player_prop(label: str) -> Tuple[str, str]:
    """
    Returns (player_name, market_type) where market_type in {"shots","sot","other"}.
    """
    s = (label or "").strip()
    if not s: return "", "other"
    # Common label form: "Player Name - Shots" or "Player Name - Shots on Target"
    parts = [p.strip() for p in s.split(" - ", 1)]
    player = parts[0] if parts else s
    rest = parts[1].lower() if len(parts) > 1 else ""
    if "shots on target" in rest:
        return player, "sot"
    if "on target" in rest and "shot" in rest:
        return player, "sot"
    if "shots" in rest and "on target" not in rest:
        return player, "shots"
    # sometimes the word order can be weird—fallbacks
    r = s.lower()
    if "shots on target" in r: return player, "sot"
    if " on target " in r and "shot" in r: return player, "sot"
    if " shots" in r: return player, "shots"
    return player, "other"

def player_label_matches(player_name: str, option_label: str) -> bool:
    """
    Tolerant match: last name must appear; first initial helps disambiguate.
    """
    if not player_name or not option_label: return False
    pl = strip_accents(player_name).replace(".", " ").strip()
    parts = [p for p in pl.split() if p]
    if not parts: return False
    last = norm(parts[-1])
    initial = (parts[0][0:1] or "").lower() if parts else ""
    label = norm(re.sub(r"(?:\s*\([^)]*\))+$","", option_label or ""))
    if last not in label: return False
    if initial and initial not in label.split()[0][:1]:
        # still allow, but favour when initial matches
        return True
    return True

# ------------ Hit rate calc ------------

def hit_rate_for_line(series: List[int], line: float) -> Tuple[float, int]:
    """
    Given an integer series (latest-first) and an Over X.5 line, compute HR.
    Count a hit when value > line (e.g., 2 > 1.5).
    """
    if not series:
        return 0.0, 0
    hits = sum(1 for v in series if v > line)
    n = len(series)
    return (hits / n if n else 0.0), n

# ------------ Main selection ------------

def main():
    if not API_KEY:
        raise SystemExit("Missing ODDS_API_KEY environment variable.")

    # Index team names (from predicted XI)
    tindex = team_name_index()  # norm_name -> {team_id, league_id, team_name}

    # Load player series grouped by league/team
    per_league = load_player_series()

    # Fetch upcoming events for configured leagues + prices
    events = get_events_for_leagues(LEAGUE_SLUGS)
    event_ids = [int(e.get("id")) for e in events if isinstance(e.get("id"), int)]
    odds_payloads = {int(row.get("id")): row for row in get_odds_multi(event_ids)}

    certs = []
    values = []
    audited = []

    for ev in events:
        eid = int(ev.get("id") or 0)
        home_name = ev.get("home") or (ev.get("home_team") or "")
        away_name = ev.get("away") or (ev.get("away_team") or "")
        if not (eid and home_name and away_name): 
            continue

        home_key = norm(home_name); away_key = norm(away_name)
        home_info = tindex.get(home_key); away_info = tindex.get(away_key)
        if not (home_info and away_info):
            # If we don't have these teams in predicted_xi index, skip gracefully
            continue

        lid_home, lid_away = home_info["league_id"], away_info["league_id"]
        # allow cross-check but normally both LIDs equal
        lid = lid_home if lid_home in per_league else lid_away

        if lid not in per_league:
            continue

        # Moneyline filter per team (extract once per event)
        event_odds = odds_payloads.get(eid) or {}
        home_ml, away_ml = None, None
        for bm, mk in _bookmakers_iter(event_odds.get("bookmakers")):
            if market_is_match_winner(mk.get("name")):
                prices = _extract_moneyline_prices(mk, home_name, away_name)
                home_ml = prices.get("home", home_ml)
                away_ml = prices.get("away", away_ml)

        # Build quick lookup of players for both teams
        team_players = {
            "home": [r for r in per_league.get(lid, {}).get(home_info["team_id"], [])],
            "away": [r for r in per_league.get(lid, {}).get(away_info["team_id"], [])],
        }

        # Scan all bookmakers/markets for player props
        for bm, mk in _bookmakers_iter(event_odds.get("bookmakers")):
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
                if mkt not in {"shots", "sot"}:
                    continue

                # Try match against both teams' player pools
                matched_side = None
                matched_row = None

                # prioritise exact-ish name match
                for side in ("home", "away"):
                    for row in team_players[side]:
                        if player_label_matches(row["name"], player_str):
                            matched_side = side
                            matched_row = row
                            break
                    if matched_row:
                        break

                if not matched_row:
                    continue

                # Series for this market
                series = (
                    matched_row["series_sot"] if mkt == "sot" else matched_row["series_shots"]
                ) or []
                hr, n = hit_rate_for_line(series, line)

                # Big-underdog filter (Shots/SOT only)
                if mkt in {"shots", "sot"}:
                    team_ml = home_ml if matched_side == "home" else away_ml
                    if isinstance(team_ml, (int, float)) and team_ml > UNDERDOG_MAX_ML:
                        continue  # drop big underdogs

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
                    "market_label": label,
                    "market_type": mkt,
                    "line": line,
                    "price": over_price,
                    "player_id": matched_row["player_id"],
                    "player_name": matched_row["name"],
                    "team_side": matched_side,
                    "team_ml": home_ml if matched_side == "home" else away_ml,
                    "hit_rate": round(hr, 4),
                    "n": n,
                    "series": series[:10],  # small peek
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
                    "team_ml": home_ml if matched_side == "home" else away_ml,
                    "hit_rate": round(hr, 4),
                    "n": n,
                }

                if tag == "CERT":
                    certs.append(out_row)
                else:
                    values.append(out_row)

    # Output
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "certs": sorted(certs, key=lambda r: (-r["hit_rate"], -r["n"], -r["price"])),
        "value_singles": sorted(values, key=lambda r: (-r["price"], -r["hit_rate"], -r["n"])),
        "audited": audited,  # for debugging
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # Human-readable TXT
    lines: List[str] = []

    def fmt_row(r: dict) -> str:
        band = "CERT" if r["type"] == "CERT" else "VALUE"
        return (f"[{band}] {r['home']} vs {r['away']} — {r['player_name']} "
                f"{r['market']} Over {r['line']:.1f} @ {r['price']:.2f} ({r['bookmaker']}) "
                f"HR={r['hit_rate']*100:.0f}% n={r['n']} ML={r['team_ml'] if r['team_ml'] is not None else '?'}")

    lines.append("=== CERTS ===")
    if payload["certs"]:
        for r in payload["certs"]:
            lines.append(fmt_row({**r, "type": "CERT"}))
    else:
        lines.append("(none)")

    lines.append("")
    lines.append("=== VALUE SINGLES ===")
    if payload["value_singles"]:
        for r in payload["value_singles"]:
            lines.append(fmt_row({**r, "type": "VALUE"}))
    else:
        lines.append("(none)")

    OUT_TXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT_JSON} and {OUT_TXT}")

if __name__ == "__main__":
    main()
