#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value Singles — Shots on Target (Sportmonks-only)

Find players to have 1+ SOT (Over 0.5) using your locally stored data:
- Fixtures:            data/fixtures/{league_id}.json
- Per-fixture odds:    data/odds/b365/fixtures/{fixture_id}.json
- Player SOT series:   data/player_shots_on_target/by_league/{league_id}.json

Criteria (kept if ANY tier matches):
  • 5/5 (last 5 all ≥1 SOT), OR
  • 7/10 (last 10 ≥1 SOT in at least 7), OR
  • 4/5 (last 5 ≥1 SOT in at least 4) AND team ML ≤ FAV_MAX (default 2.50)
AND:
  • Bet365 Over 0.5 SOT price ≥ MIN_DEC_PRICE (default 1.72)
  • Team ML < TEAM_UNDERDOG_MAX (default 3.50)
  • Fixture kickoff within WINDOW_DAYS days (default 7) — relative to now (UTC)

Env (all optional):
  LEAGUE_IDS          Comma-separated Sportmonks league IDs; blank = auto-discover from fixtures dir
  WINDOW_DAYS         Default 7
  MIN_DEC_PRICE       Default 1.72
  TEAM_UNDERDOG_MAX   Default 3.50
  FAV_MAX             Default 2.50
  DEBUG_DROPS         1 to print detailed drop reasons (default 0)
  NEAR_MISS_LIMIT     Max near-miss lines to print (default 12)

Output: human-readable text to stdout for your workflow to tee into data/value_bets/value_singles_sot.txt
"""

import os, re, json, math, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

# ========= Config / Paths =========
ROOT = Path(".")
FIX_DIR = ROOT / "data" / "fixtures"
ODDS_PER_FIX_DIR = ROOT / "data" / "odds" / "b365" / "fixtures"
SOT_DIR = ROOT / "data" / "player_shots_on_target" / "by_league"

WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "7"))
MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.72"))
TEAM_UNDERDOG_MAX = float(os.getenv("TEAM_UNDERDOG_MAX", "3.50"))   # drop if >= this
FAV_MAX = float(os.getenv("FAV_MAX", "2.50"))                       # allow 4/5 tier only if ML ≤ this

LEAGUE_IDS_ENV = os.getenv("LEAGUE_IDS", "").strip()
LEAGUE_IDS: List[int] = [int(x) for x in LEAGUE_IDS_ENV.split(",") if x.strip().isdigit()] if LEAGUE_IDS_ENV else []

DEBUG_DROPS = bool(int(os.getenv("DEBUG_DROPS", "0")))
NEAR_MISS_LIMIT = int(os.getenv("NEAR_MISS_LIMIT", "12"))

# ========= Helpers =========
def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def parse_utc(s: str) -> Optional[dt.datetime]:
    # expects "YYYY-MM-DD HH:MM:SS" (Sportmonks in your fixtures JSON)
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None

def load_json(p: Path) -> Optional[dict]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def debug(msg: str):
    if DEBUG_DROPS:
        print(msg)

# robust player name match (last name + optional first initial)
import unicodedata
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower().strip()

def extract_last_and_initial(full: str) -> Tuple[Optional[str], Optional[str]]:
    if not full:
        return None, None
    s = strip_accents(full).replace(".", " ").strip()
    parts = [p for p in s.split() if p]
    if not parts:
        return None, None
    last = parts[-1].lower()
    initial = None
    for p in parts[:-1]:
        if p:
            initial = p[0].lower()
            break
    return last, initial

def player_label_matches(player_name: str, book_name: str) -> bool:
    if not player_name or not book_name:
        return False
    last, initial = extract_last_and_initial(player_name)
    b = strip_accents(book_name).lower()
    if not last or last not in b:
        return False
    if initial:
        # accept if initial appears somewhere before the last name (loose)
        return bool(re.search(rf"\b{initial}\w*\b.*\b{re.escape(last)}\b", b)) or b.startswith(initial)
    return True

NEGATIVE_SOT_TERMS = {
    "outside", "from outside", "outside the box", "first half", "second half", "1st half", "2nd half",
    "header", "headers", "distance"
}

def market_is_player_sot(desc: str) -> bool:
    d = (desc or "").lower()
    if not d:
        return False
    if "on target" not in d:
        return False
    if "player" not in d:
        return False
    if any(t in d for t in NEGATIVE_SOT_TERMS):
        return False
    return True

MATCH_WINNER_KEYS = [
    "match winner", "match result", "1x2", "full time result", "win/draw/win", "to win", "90 minutes", "result"
]

def market_is_match_winner(desc: str) -> bool:
    s = (desc or "").lower()
    return any(k in s for k in MATCH_WINNER_KEYS)

def to_float(x) -> Optional[float]:
    try:
        if x is None or x == "N/A":
            return None
        return float(x)
    except Exception:
        return None

# ========= Load upcoming fixtures =========
def discover_leagues_from_fixtures() -> List[int]:
    out = []
    for p in sorted(FIX_DIR.glob("*.json")):
        try:
            out.append(int(p.stem))
        except Exception:
            pass
    return out

def upcoming_fixtures_for_league(lid: int, window_days: int) -> List[dict]:
    blob = load_json(FIX_DIR / f"{lid}.json") or {}
    fixtures = blob.get("fixtures") or []
    if not window_days:
        return fixtures
    now = utc_now()
    end = now + dt.timedelta(days=window_days)
    kept = []
    for fx in fixtures:
        t = parse_utc(fx.get("starting_at") or "")
        if t and now <= t <= end:
            kept.append(fx)
    return kept

def team_maps_from_fixtures(fixtures: List[dict]) -> Tuple[Dict[int, str], Dict[str, dict]]:
    """
    Returns:
      team_id_to_name: {team_id -> name}
      team_name_to_next_fixture: {name_lower -> info{fixture_id, side, opp_name, kickoff_dt}}
    """
    team_id_to_name: Dict[int, str] = {}
    team_name_to_next_fixture: Dict[str, dict] = {}
    for fx in fixtures:
        fid = int(fx.get("id"))
        start_s = fx.get("starting_at")
        t = parse_utc(start_s or "")
        parts = fx.get("participants") or []
        home_name = away_name = None
        home_id = away_id = None
        for p in parts:
            nm = p.get("name"); pid = p.get("id")
            loc = ((p.get("meta") or {}).get("location") or "").lower()
            if isinstance(pid, int) and isinstance(nm, str):
                team_id_to_name[pid] = nm
            if loc == "home":
                home_name, home_id = nm, pid
            elif loc == "away":
                away_name, away_id = nm, pid
        if not (home_name and away_name and t):
            continue
        # pick the earliest upcoming for each team
        for nm, side, opp in ((home_name, "home", away_name), (away_name, "away", home_name)):
            key = nm.lower()
            prev = team_name_to_next_fixture.get(key)
            if (not prev) or (t < prev["kickoff_dt"]):
                team_name_to_next_fixture[key] = {
                    "fixture_id": fid,
                    "side": side,
                    "opp_name": opp,
                    "kickoff_dt": t,
                    "home_name": home_name,
                    "away_name": away_name,
                }
    return team_id_to_name, team_name_to_next_fixture

# ========= Pull team ML from per-fixture odds =========
def get_team_match_prices(fid: int, home_name: str, away_name: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (home_ml, away_ml) decimal if found in Bet365 'Match Winner/Result' market.
    Uses multiple fallbacks: by exact team name, or by label ('1','2'), or name 'Home'/'Away'.
    """
    blob = load_json(ODDS_PER_FIX_DIR / f"{fid}.json") or {}
    rows = blob.get("odds") or []
    if not isinstance(rows, list):
        return (None, None)

    home_ml = None
    away_ml = None

    for r in rows:
        if str(r.get("bookmaker_id")) != "2":
            continue
        desc = r.get("market_description") or r.get("market_name") or ""
        if not market_is_match_winner(desc):
            continue
        label = (r.get("label") or "").strip().lower()
        name = (r.get("name") or "").strip()
        price = to_float(r.get("value"))
        if price is None:
            continue

        nlow = name.lower()
        # map by name first
        if nlow == (home_name or "").lower():
            home_ml = price
            continue
        if nlow == (away_name or "").lower():
            away_ml = price
            continue
        # common aliases
        if nlow in ("home", "1") or label in ("home", "1"):
            home_ml = price
            continue
        if nlow in ("away", "2") or label in ("away", "2"):
            away_ml = price
            continue

    return (home_ml, away_ml)

# ========= Find Over 0.5 SOT price for a given player in a fixture =========
def find_player_sot_over_point5(fid: int, player_name: str) -> Optional[float]:
    blob = load_json(ODDS_PER_FIX_DIR / f"{fid}.json") or {}
    rows = blob.get("odds") or []
    if not isinstance(rows, list):
        return None

    best = None
    for r in rows:
        if str(r.get("bookmaker_id")) != "2":
            continue
        desc = r.get("market_description") or r.get("market_name") or ""
        if not market_is_player_sot(desc):
            continue
        label = (r.get("label") or "").strip()
        try:
            line = float(label)
        except Exception:
            # sometimes label could be "0.5" cleanly, otherwise skip
            continue
        if not math.isclose(line, 0.5, abs_tol=1e-9):
            continue
        # In Sportmonks player name is generally in r["name"]
        book_player = r.get("name") or ""
        if not player_label_matches(player_name, book_player):
            continue
        price = to_float(r.get("value"))
        if price is None:
            continue
        if (best is None) or (price > best + 1e-9):
            best = price
    return best

# ========= History tiers =========
def hits(seq: List[int]) -> List[int]:
    return [1 if (isinstance(x, (int, float)) and x >= 1) else 0 for x in (seq or [])]

def compute_tier(series: List[int], team_ml: Optional[float]) -> Optional[str]:
    """
    Returns one of: "5/5", "7/10", "4/5" (fav only) or None.
    """
    h = hits(series)
    last5 = h[:5]
    last10 = h[:10]

    five_of_five = len(last5) >= 5 and sum(last5) >= 5
    seven_of_ten = len(last10) >= 10 and sum(last10) >= 7
    four_of_five = len(last5) >= 5 and sum(last5) >= 4

    if five_of_five:
        return "5/5"
    if seven_of_ten:
        return "7/10"
    if four_of_five and (team_ml is not None) and (team_ml <= FAV_MAX):
        return "4/5"
    return None

# ========= Main =========
def main():
    now = utc_now()
    print(f"Generated at (UTC): {now.isoformat()}")
    print(f"Criteria: 5/5 OR 7/10 OR 4/5 (fav ≤{FAV_MAX:.2f}) | Over 0.5 SOT ≥ {MIN_DEC_PRICE:.2f} | "
          f"Team ML < {TEAM_UNDERDOG_MAX:.2f} | Window={WINDOW_DAYS} days")

    # Leagues to use
    leagues = LEAGUE_IDS or discover_leagues_from_fixtures()
    leagues = sorted(set(leagues))

    # Build upcoming fixture contexts per league
    upcoming_by_league: Dict[int, List[dict]] = {lid: upcoming_fixtures_for_league(lid, WINDOW_DAYS) for lid in leagues}

    # Prepare results
    kept: List[dict] = []
    near_misses: List[dict] = []
    scanned_candidates = 0

    for lid in leagues:
        fx_list = upcoming_by_league.get(lid) or []
        if not fx_list:
            continue

        team_id_to_name, team_next = team_maps_from_fixtures(fx_list)

        # Load SOT series file for this league
        sot_blob = load_json(SOT_DIR / f"{lid}.json") or {}
        players = sot_blob.get("players") or []
        if not players:
            continue

        for rec in players:
            # derive team name (prefer from fixtures mapping via team_id)
            team_id = rec.get("team_id")
            team_name = team_id_to_name.get(int(team_id)) if isinstance(team_id, int) else None
            # if not found via ID, try to infer later by matching (but SOT file doesn't include team_name)
            if not team_name:
                # no upcoming fixture context; skip early
                continue

            nxt = team_next.get((team_name or "").lower())
            if not nxt:
                # team has no fixture in the window
                continue

            fid = nxt["fixture_id"]
            side = nxt["side"]
            opp_name = nxt["opp_name"]
            kickoff = nxt["kickoff_dt"]
            home_name = nxt["home_name"]
            away_name = nxt["away_name"]

            # get team ML from the fixture odds
            home_ml, away_ml = get_team_match_prices(fid, home_name, away_name)
            team_ml = home_ml if side == "home" else away_ml
            if team_ml is None:
                debug(f"[DROP no-ml] {team_name} vs {opp_name} (fid={fid})")
                near_misses.append({
                    "reason": "no-ml",
                    "player": rec.get("name"),
                    "team": team_name,
                    "fixture": f"{home_name} vs {away_name}",
                    "kickoff": kickoff.strftime("%Y-%m-%d %H:%M:%S"),
                    "price": None,
                    "team_ml": None,
                    "tier": None,
                    "series5": ",".join(map(str, (rec.get("on_target_last_n") or [])[:5])),
                })
                continue

            # drop big underdogs early
            if team_ml >= TEAM_UNDERDOG_MAX:
                debug(f"[DROP underdog {team_ml:.2f}] {team_name} ({rec.get('name')})")
                near_misses.append({
                    "reason": "underdog",
                    "player": rec.get("name"),
                    "team": team_name,
                    "fixture": f"{home_name} vs {away_name}",
                    "kickoff": kickoff.strftime("%Y-%m-%d %H:%M:%S"),
                    "price": None,
                    "team_ml": team_ml,
                    "tier": None,
                    "series5": ",".join(map(str, (rec.get("on_target_last_n") or [])[:5])),
                })
                continue

            # compute tier
            series = rec.get("on_target_last_n") or []
            tier = compute_tier(series, team_ml)
            scanned_candidates += 1
            if not tier:
                debug(f"[DROP history] {rec.get('name')} — no (5/5 or 7/10 or 4/5-fav)")
                continue

            # find Over 0.5 SOT price for this player in this fixture
            player_name = rec.get("name") or ""
            price = find_player_sot_over_point5(fid, player_name)
            if price is None:
                debug(f"[DROP no-price] {player_name} — no Over 0.5 SOT found (fid={fid})")
                near_misses.append({
                    "reason": "no-price",
                    "player": player_name,
                    "team": team_name,
                    "fixture": f"{home_name} vs {away_name}",
                    "kickoff": kickoff.strftime("%Y-%m-%d %H:%M:%S"),
                    "price": None,
                    "team_ml": team_ml,
                    "tier": tier,
                    "series5": ",".join(map(str, series[:5])),
                })
                continue

            if price < MIN_DEC_PRICE:
                debug(f"[DROP low-price {price:.2f}] {player_name}")
                near_misses.append({
                    "reason": "low-price",
                    "player": player_name,
                    "team": team_name,
                    "fixture": f"{home_name} vs {away_name}",
                    "kickoff": kickoff.strftime("%Y-%m-%d %H:%M:%S"),
                    "price": price,
                    "team_ml": team_ml,
                    "tier": tier,
                    "series5": ",".join(map(str, series[:5])),
                })
                continue

            kept.append({
                "player": player_name,
                "position": rec.get("position_tag") or "",
                "team": team_name,
                "fixture": f"{home_name} vs {away_name}",
                "kickoff": kickoff.strftime("%Y-%m-%d %H:%M:%S"),
                "price": price,
                "team_ml": team_ml,
                "tier": tier,
                "series5": series[:5],
                "series10": series[:10],
            })

    print(f"\nCandidates kept after filters: {len(kept)}")
    if not kept:
        print("\nNo SOT value singles found.")
    else:
        print("\n===== SOT VALUE SINGLES =====")
        # rank: higher tier precedence then price desc then name
        tier_rank = {"5/5": 3, "7/10": 2, "4/5": 1}
        kept.sort(key=lambda x: (tier_rank.get(x["tier"], 0), x["price"]), reverse=True)
        for x in kept:
            ser5 = ",".join(map(str, x["series5"]))
            pos = f"[{x['position']}]" if x["position"] else ""
            print(
                f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | "
                f"Over 0.5 SOT @ {x['price']:.3f} | Team ML {x['team_ml']:.3f} | tier {x['tier']} | last5: {ser5}"
            )

    if NEAR_MISS_LIMIT and near_misses:
        print("\n-- Near misses (top likely) --")
        # priced first (higher price first), then underdog/no-ml/no-price reasons
        def nm_key(r):
            priced = r["price"] is not None
            return (1 if priced else 0, r["price"] or 0.0)
        near_misses.sort(key=nm_key, reverse=True)
        for r in near_misses[:NEAR_MISS_LIMIT]:
            price = f"{r['price']:.3f}" if isinstance(r["price"], (int, float)) else "—"
            ml = f"{r['team_ml']:.3f}" if isinstance(r["team_ml"], (int, float)) else "—"
            print(
                f"   · [{r['reason']}] {r['player']} — {r['team']} | {r['fixture']} @ {r['kickoff']} | "
                f"price={price} | ML={ml} | tier={r.get('tier') or '—'} | last5:{r['series5']}"
            )

    print(f"\nRun timestamp (UTC): {utc_now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Params: LEAGUE_IDS='{LEAGUE_IDS_ENV}' MIN_DEC_PRICE={MIN_DEC_PRICE} "
          f"TEAM_UNDERDOG_MAX={TEAM_UNDERDOG_MAX} FAV_MAX={FAV_MAX} WINDOW_DAYS={WINDOW_DAYS}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
