#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Find player-bet candidates by combining your collected hit rates with live prices.

Changes vs prior:
  - FIX: events fetched without "league" param (404 avoidance).
  - Scope reduced to fixtures where BOTH teams exist in your collected data (so only leagues you've scraped).
  - Only Bet365 and Kambi (Unibet/888sport/etc.) considered; results split into separate lists.
  - More defensive HTTP + schema handling.

Inputs you already produce:
  - data/player_shots/by_league/{lid}.json            (key: shots_last_n)
  - data/player_shots_on_target/by_league/{lid}.json  (key: on_target_last_n)
  - data/predicted_xi/by_league/{lid}.json            (for team names / mapping)

External:
  - ODDS API (https://api.odds-api.io) using $ODDS_API_KEY

Outputs:
  - data/bets/player_candidates.json
  - data/bets/player_candidates.txt
"""

import os
import re
import json
import time
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional
import unicodedata
import requests

# ------------ Config ------------

API_KEY = os.getenv("ODDS_API_KEY", "")
SPORT = "football"

# Only these two families
BET365_NAMES = {s.strip().lower() for s in os.getenv("BET365_NAMES", "Bet365").split(",") if s.strip()}
# Best-guess defaults for common Kambi brands; override via env if your Odds API spells differ
KAMBI_BRANDS = {s.strip().lower() for s in os.getenv(
    "KAMBI_BRANDS",
    "Kambi,Unibet,888sport,LeoVegas,Mr Green,32Red,Kambi Sportsbook"
).split(",") if s.strip()}

# Markets we care about
MARKET_INCLUDE = {"shots", "sot"}

# Selection thresholds
CERT_MIN_PRICE   = 1.25
CERT_MIN_N       = 8
VALUE_MIN_PRICE  = 1.80
UNDERDOG_MAX_ML  = 3.50  # filter: exclude if team ML > this (Shots / SOT only)

# FS layout
ROOT = Path(".")
PX_DIR     = ROOT / "data" / "predicted_xi" / "by_league"
SHOTS_DIR  = ROOT / "data" / "player_shots" / "by_league"
SOT_DIR    = ROOT / "data" / "player_shots_on_target" / "by_league"
OUT_JSON   = ROOT / "data" / "bets" / "player_candidates.json"
OUT_TXT    = ROOT / "data" / "bets" / "player_candidates.txt"

# Odds API endpoints
EVENTS_API_URL = "https://api.odds-api.io/v3/events"
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"
HTTP_HEADERS = {"accept": "application/json"}


# ------------ Small utils ------------

def strip_accents(s: str) -> str:
    if not isinstance(s, str): return ""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

def norm(s: str) -> str:
    s = strip_accents((s or "").lower())
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s

def http_get(url: str, params: dict, retries: int = 3, backoff: float = 1.5, timeout: float = 20.0):
    err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=timeout)
            if r.status_code >= 500:
                time.sleep(min(60, backoff ** (i+1)))
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            err = e
            time.sleep(backoff ** (i+1))
    raise err or RuntimeError(f"GET {url} failed")

def load_json(p: Path) -> Any:
    if not p.is_file(): return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ------------ Collect team & player indexes from your data ------------

def team_name_index() -> Dict[str, Dict[str, Any]]:
    """norm_team -> {team_id, league_id, team_name} from predicted_xi blobs."""
    out: Dict[str, Dict[str, Any]] = {}
    for f in PX_DIR.glob("*.json"):
        blob = load_json(f) or {}
        lid = int(blob.get("league_id") or f.stem.split(".")[0]) if f.stem else None
        for fx in (blob.get("fixtures") or []):
            for side in ("home", "away"):
                t = fx.get(side) or {}
                tid, nm = t.get("team_id"), t.get("name")
                if isinstance(tid, int) and isinstance(nm, str) and nm:
                    out[norm(nm)] = {"team_id": tid, "league_id": lid, "team_name": nm}
    return out

def _players(payload: dict) -> List[dict]:
    return [x for x in (payload.get("players") or []) if isinstance(x, dict)]

def _int_series(seq) -> List[int]:
    if not isinstance(seq, list): return []
    out = []
    for v in seq:
        try:
            out.append(int(v))
        except Exception:
            try: out.append(int(float(v)))
            except Exception: pass
    return out

def load_player_series() -> Dict[int, Dict[int, List[dict]]]:
    """
    Return: per_league[league_id][team_id] -> list of players dicts with series for shots / sot.
    Only leagues with files present will appear here (=> we only work on leagues you've scraped).
    """
    per_league: Dict[int, Dict[int, List[dict]]] = {}

    # Shots
    for p in SHOTS_DIR.glob("*.json"):
        blob = load_json(p) or {}
        lid = int(blob.get("league_id") or p.stem.split(".")[0])
        for r in _players(blob):
            series = _int_series(r.get("shots_last_n") or [])
            team_id = int(r.get("team_id") or 0)
            if not team_id: continue
            per_league.setdefault(lid, {}).setdefault(team_id, []).append({
                "player_id": int(r.get("player_id") or 0),
                "name": r.get("name") or "",
                "team_id": team_id,
                "series_shots": series,
                "series_sot": None,  # to attach
            })

    # Shots on target (optional)
    sot_map = {}
    for p in SOT_DIR.glob("*.json"):
        blob = load_json(p) or {}
        lid = int(blob.get("league_id") or p.stem.split(".")[0])
        for r in _players(blob):
            key = (lid, int(r.get("team_id") or 0), int(r.get("player_id") or 0))
            sot_map[key] = _int_series(r.get("on_target_last_n") or [])

    # Attach SOT
    for lid, teams in per_league.items():
        for tid, arr in teams.items():
            for row in arr:
                seq = sot_map.get((lid, tid, row["player_id"]))
                if seq is not None:
                    row["series_sot"] = seq

    return per_league


# ------------ Odds helpers ------------

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

def get_events_all_football() -> List[dict]:
    # No league filter (avoids 404s); we filter by teams we know later
    r = http_get(EVENTS_API_URL, {"apiKey": API_KEY, "sport": SPORT})
    try:
        data = r.json()
    except Exception:
        return []
    return data if isinstance(data, list) else []

def get_odds_multi(event_ids: List[int]) -> List[dict]:
    if not event_ids: return []
    r = http_get(ODDS_MULTI_API_URL, {
        "apiKey": API_KEY,
        "eventIds": ",".join(map(str, event_ids)),
        # We’ll receive many books but later we’ll keep only Bet365 + Kambi brands
    })
    try:
        data = r.json()
    except Exception:
        return []
    return data if isinstance(data, list) else []

def _bookmakers_iter(bm_payload):
    """Yield (bookmaker_name, market_dict) across bookmakers payload."""
    if not isinstance(bm_payload, dict): return
    for bm_name, markets in bm_payload.items():
        if isinstance(markets, list):
            for m in markets:
                if isinstance(m, dict):
                    yield bm_name, m
        elif isinstance(markets, dict):
            yield bm_name, markets


# ------------ Player/market parsing ------------

def parse_player_prop(label: str) -> Tuple[str, str]:
    """
    Return (player_name, market_type) where market_type in {"shots","sot","other"}.
    """
    s = (label or "").strip()
    if not s: return "", "other"
    parts = [p.strip() for p in s.split(" - ", 1)]
    player = parts[0] if parts else s
    rest = parts[1].lower() if len(parts) > 1 else ""
    if "shots on target" in rest or ("on target" in rest and "shot" in rest):
        return player, "sot"
    if "shots" in rest and "on target" not in rest:
        return player, "shots"
    r = s.lower()
    if "shots on target" in r: return player, "sot"
    if " on target " in r and "shot" in r: return player, "sot"
    if " shots" in r: return player, "shots"
    return player, "other"

def player_label_matches(player_name: str, option_label: str) -> bool:
    """
    Tolerant match: last name must appear; first initial helps when present.
    """
    if not player_name or not option_label: return False
    pl = strip_accents(player_name).replace(".", " ").strip()
    parts = [p for p in pl.split() if p]
    if not parts: return False
    last = norm(parts[-1])
    initial = (parts[0][0:1] or "").lower()
    label = norm(re.sub(r"(?:\s*\([^)]*\))+$","", option_label or ""))
    if last not in label:
        return False
    # If we have an initial, prefer that it matches—but don’t require it
    return True

def hit_rate_for_line(series: List[int], line: float) -> Tuple[float, int]:
    """
    Over X.5 hit: value > line.
    """
    if not series: return 0.0, 0
    hits = sum(1 for v in series if v > line)
    n = len(series)
    return (hits / n if n else 0.0), n


# ------------ Bookmaker classification ------------

def classify_bookmaker(name: str) -> Optional[str]:
    n = (name or "").strip().lower()
    if not n: return None
    if n in BET365_NAMES:
        return "bet365"
    if n in KAMBI_BRANDS or any(k in n for k in ("kambi",)):
        return "kambi"
    return None


# ------------ Main selection ------------

def main():
    if not API_KEY:
        raise SystemExit("Missing ODDS_API_KEY environment variable.")

    # Build indices from your scraped data (=> scope to “leagues we have collected”)
    tindex = team_name_index()           # known teams from predicted_xi
    per_league = load_player_series()    # known leagues/teams with player series

    # Fetch upcoming football events (no league filter), then keep only games where BOTH teams are in your data
    events = []
    for ev in get_events_all_football():
        eid = ev.get("id")
        home_name = ev.get("home") or ev.get("home_team") or ""
        away_name = ev.get("away") or ev.get("away_team") or ""
        if not (isinstance(eid, int) and home_name and away_name):
            continue
        hk, ak = norm(home_name), norm(away_name)
        hinfo, ainfo = tindex.get(hk), tindex.get(ak)
        if not (hinfo and ainfo):
            continue
        # Only proceed if both teams’ leagues exist in player-series data
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
        # Still write empty outputs
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps({"bet365":{"certs":[],"value_singles":[]},
                                        "kambi":{"certs":[],"value_singles":[]},
                                        "audited":[]}, indent=2), encoding="utf-8")
        OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
        OUT_TXT.write_text("=== BET365 — CERTS ===\n(none)\n\n=== BET365 — VALUE SINGLES ===\n(none)\n\n=== KAMBI — CERTS ===\n(none)\n\n=== KAMBI — VALUE SINGLES ===\n(none)\n", encoding="utf-8")
        return

    event_ids = [e["id"] for e in events]
    odds_payloads = {int(row.get("id")): row for row in get_odds_multi(event_ids)}

    # Output buckets (split by bookmaker family)
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
        # Extract moneyline once for underdog filter
        home_ml, away_ml = None, None
        for bm, mk in _bookmakers_iter(event_odds.get("bookmakers")):
            if market_is_match_winner(mk.get("name")):
                odds = mk.get("odds")
                # odds could be dict or list; try common shapes
                if isinstance(odds, dict):
                    try: home_ml = float(odds.get("home", home_ml))
                    except Exception: pass
                    try: away_ml = float(odds.get("away", away_ml))
                    except Exception: pass
                elif isinstance(odds, list):
                    if len(odds) == 1 and isinstance(odds[0], dict):
                        entry = odds[0]
                        try: home_ml = float(entry.get("home", home_ml))
                        except Exception: pass
                        try: away_ml = float(entry.get("away", away_ml))
                        except Exception: pass
                    else:
                        for opt in odds:
                            label = (opt.get("label") or "").strip().lower()
                            try: price = float(opt.get("over"))
                            except Exception: continue
                            if label in ("home", "1") or norm(label) == norm(home_name):
                                home_ml = min(home_ml, price) if home_ml else price
                            elif label in ("away", "2") or norm(label) == norm(away_name):
                                away_ml = min(away_ml, price) if away_ml else price

        # Build player pools
        team_players = {
            "home": [r for r in per_league.get(lid, {}).get(home_info["team_id"], [])],
            "away": [r for r in per_league.get(lid, {}).get(away_info["team_id"], [])],
        }

        # Walk through bookmaker markets, but keep only Bet365/Kambi
        for bm, mk in _bookmakers_iter(event_odds.get("bookmakers")):
            fam = classify_bookmaker(bm)
            if fam not in ("bet365", "kambi"):
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
                if mkt not in MARKET_INCLUDE:
                    continue

                # Match player to team (home/away)
                matched_side = None
                matched_row = None
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

                series = (matched_row["series_sot"] if mkt == "sot" else matched_row["series_shots"]) or []
                hr, n = hit_rate_for_line(series, line)

                # Big-underdog filter for shots/sot
                team_ml = home_ml if matched_side == "home" else away_ml
                if isinstance(team_ml, (int, float)) and team_ml > UNDERDOG_MAX_ML:
                    continue

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
    for fam in ("bet365", "kambi"):
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
    # BET365
    lines.append("=== BET365 — CERTS ===")
    lines += [fmt_row("CERT", r) for r in out["bet365"]["certs"]] or ["(none)"]
    lines.append("")
    lines.append("=== BET365 — VALUE SINGLES ===")
    lines += [fmt_row("VALUE", r) for r in out["bet365"]["value_singles"]] or ["(none)"]
    lines.append("")
    # KAMBI
    lines.append("=== KAMBI — CERTS ===")
    lines += [fmt_row("CERT", r) for r in out["kambi"]["certs"]] or ["(none)"]
    lines.append("")
    lines.append("=== KAMBI — VALUE SINGLES ===")
    lines += [fmt_row("VALUE", r) for r in out["kambi"]["value_singles"]] or ["(none)"]

    OUT_TXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT_JSON} and {OUT_TXT}")

if __name__ == "__main__":
    main()
