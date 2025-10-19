#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SOT certs — Player 1+ SOT (Sportmonks odds + local SOT histories)

Criteria (mirrors your Shots certs tiers):
  - Qualify if ANY of these holds (latest-first series):
      • 10/10 (all of last 10 >=1), or
      • 9/10  (>=1 in 9 of last 10), or
      • 8/10  (>=1 in 8 of last 10), or
      • 7/7   (all of last 7 >=1)
  - Bet365 price for Over 0.5 SOT >= MIN_DEC_PRICE (default 1.30).
  - Team Match Winner (ML) for player's side < TEAM_WIN_MAX (default 3.50).
  - Window filter for fixtures (default 7 days; 0 = no limit).

Reads:
  - Fixtures:   data/fixtures/{league_id}.json
  - SOT data:   data/player_shots_on_target/by_league/{league_id}.json
  - Bet365 odds: data/odds/b365/{league_id}.json  (+ fallback: data/odds/b365/fixtures/{fixture_id}.json)

Writes:
  - data/value_bets/sot_certs.txt

Env:
  LEAGUE_IDS    CSV list; default = auto from fixtures dir
  MIN_DEC_PRICE default 1.30
  TEAM_WIN_MAX  default 3.50
  WINDOW_DAYS   default 7
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional

ROOT = Path(".")
FIX_DIR  = ROOT / "data" / "fixtures"
PSOT_DIR = ROOT / "data" / "player_shots_on_target" / "by_league"
ODDS_DIR = ROOT / "data" / "odds" / "b365"
ODDS_FIX = ODDS_DIR / "fixtures"
OUT_DIR  = ROOT / "data" / "value_bets"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "sot_certs.txt"

MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_WIN_MAX  = float(os.getenv("TEAM_WIN_MAX",  "3.50"))
WINDOW_DAYS   = int(os.getenv("WINDOW_DAYS",     "7"))

# ---------- helpers ----------
def load_json(p: Path) -> dict:
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return {}

def discover_leagues() -> List[int]:
    ids = []
    for p in FIX_DIR.glob("*.json"):
        try: ids.append(int(p.stem))
        except: pass
    return sorted(set(ids))

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.\+\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def within_window(starting_at: str, days: int) -> bool:
    if not days: return True
    try:
        dt_utc = dt.datetime.strptime(starting_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return True
    now = dt.datetime.now(dt.timezone.utc)
    return now <= dt_utc <= (now + dt.timedelta(days=days))

def as_float(x) -> Optional[float]:
    try: return float(str(x))
    except Exception: return None

# ---------- qualification logic (SOT) ----------
def qualifies_tier(series: List[int]) -> Optional[str]:
    """Return a tier label if the player qualifies; else None."""
    xs = [x for x in (series or []) if isinstance(x, int)]
    if len(xs) >= 10:
        last10 = xs[:10]
        c10 = sum(1 for v in last10 if v >= 1)
        if c10 == 10: return "10/10"
        if c10 == 9:  return "9/10"
        if c10 == 8:  return "8/10"
    if len(xs) >= 7:
        last7 = xs[:7]
        if all(v >= 1 for v in last7): return "7/7"
    return None

# ---------- odds parsing ----------
MATCH_WINNER_ALIASES = {
    "match winner","match result","full time result","fulltime result","result",
    "1x2","win/draw/win","90 minutes","regular time result","3-way","3 way"
}
def is_match_winner_row(row: dict) -> bool:
    md = norm(row.get("market_description") or "")
    return md in MATCH_WINNER_ALIASES

def label_to_side(label: Optional[str]) -> Optional[str]:
    s = (label or "").strip().lower()
    if s in {"1","home"}: return "home"
    if s in {"2","away"}: return "away"
    return None

def extract_ml(rows: List[dict]) -> Tuple[Optional[float], Optional[float]]:
    home_ml = None; away_ml = None
    for r in rows:
        if not is_match_winner_row(r): continue
        side = label_to_side(r.get("label"))
        price = as_float(r.get("value"))
        if price is None or side not in {"home","away"}:
            continue
        if side == "home":
            home_ml = price if (home_ml is None or price < home_ml) else home_ml
        else:
            away_ml = price if (away_ml is None or price < away_ml) else away_ml
    return home_ml, away_ml

# SOT market detection: be liberal with naming
def is_player_sot_market(row: dict) -> bool:
    md = norm(row.get("market_description") or "")
    if not md: return False
    if "player" not in md: return False
    return ("shots on target" in md) or ("sot" in md) or ("on target" in md and "shots" in md)

def row_player_name(row: dict) -> str:
    for k in ("name","total","original_label"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def line_is_point5(row: dict) -> bool:
    """
    Treat any of these as Over 0.5:
      - handicap == 0.5
      - label includes '0.5'
      - label like '1+'
    """
    h = row.get("handicap")
    if h is not None:
        try:
            if float(h) == 0.5: return True
        except: pass
    lab = (row.get("label") or "").strip().lower()
    if "0.5" in lab: return True
    # common Bet365 encoding is "1+"
    if re.search(r"\b1\+\b", lab): return True
    # sometimes the "total" field holds the player and label is just threshold; keep check above.
    return False

def player_names_match(player: str, option_player: str) -> bool:
    if not player or not option_player: return False
    p = norm(player); o = norm(option_player)
    if p == o: return True
    p_parts = p.split(); o_parts = o.split()
    if not p_parts or not o_parts: return False
    plast = p_parts[-1]
    if not plast: return False
    if plast in o:
        if len(p_parts) > 1:
            pinitial = p_parts[0][0:1]
            if pinitial and (o.startswith(pinitial) or re.search(rf"\b{pinitial}\w*\b.*\b{plast}\b", o)):
                return True
        return True
    return False

def best_price_over_point5_sot(rows: List[dict], player_name: str) -> Optional[float]:
    best = None
    for r in rows:
        if not is_player_sot_market(r): 
            continue
        if not line_is_point5(r):
            continue
        opt_player = row_player_name(r)
        if not player_names_match(player_name, opt_player):
            continue
        price = as_float(r.get("value"))
        if price is None:
            continue
        if price >= MIN_DEC_PRICE and (best is None or price > best + 1e-12):
            best = price
    return best

# ---------- main ----------
def main():
    if os.getenv("LEAGUE_IDS"):
        league_ids = [int(x) for x in os.getenv("LEAGUE_IDS").split(",") if x.strip()]
    else:
        league_ids = discover_leagues()

    picks = []
    total_candidates = 0

    for lid in league_ids:
        fx_path   = FIX_DIR / f"{lid}.json"
        psot_path = PSOT_DIR / f"{lid}.json"
        odds_path = ODDS_DIR / f"{lid}.json"
        if not (fx_path.exists() and psot_path.exists()):
            continue

        fixtures = load_json(fx_path).get("fixtures") or []
        psot_blob = load_json(psot_path)
        odds_blob = load_json(odds_path) if odds_path.exists() else {}

        # index SOT players by team_id
        players_by_team: Dict[int, List[dict]] = {}
        for rec in (psot_blob.get("players") or []):
            tid = rec.get("team_id")
            if isinstance(tid, int):
                players_by_team.setdefault(tid, []).append(rec)

        # league-level odds by fixture
        odds_by_fixture = {
            int(f.get("fixture_id")): f
            for f in (odds_blob.get("fixtures") or [])
            if isinstance(f.get("fixture_id"), int)
        }

        for fx in fixtures:
            fid = fx.get("id")
            name = fx.get("name") or ""
            starting_at = fx.get("starting_at") or ""
            if not isinstance(fid, int):
                continue
            if WINDOW_DAYS and not within_window(starting_at, WINDOW_DAYS):
                continue

            parts = fx.get("participants") or []
            home_tid = away_tid = None
            home_name = away_name = ""
            for p in parts:
                loc = ((p.get("meta") or {}).get("location") or "").lower()
                if loc == "home":
                    home_tid = p.get("id") if isinstance(p.get("id"), int) else None
                    home_name = p.get("name") or ""
                elif loc == "away":
                    away_tid = p.get("id") if isinstance(p.get("id"), int) else None
                    away_name = p.get("name") or ""

            # odds rows (league blob or per-fixture fallback)
            odds_fx = odds_by_fixture.get(fid)
            rows_odds: List[dict] = []
            if odds_fx:
                rows_odds = odds_fx.get("odds") or []
            if not rows_odds:
                pf = load_json(ODDS_FIX / f"{fid}.json")
                rows_odds = (pf.get("odds") or []) if isinstance(pf, dict) else []

            if not isinstance(rows_odds, list) or not rows_odds:
                continue

            # ML
            home_ml, away_ml = extract_ml(rows_odds)

            for side, tid, tname, tml in (
                ("home", home_tid, home_name, home_ml),
                ("away", away_tid, away_name, away_ml),
            ):
                if not isinstance(tid, int):
                    continue
                for rec in players_by_team.get(tid, []):
                    series = rec.get("on_target_last_n") or []
                    tier = qualifies_tier(series)
                    if not tier:
                        continue
                    player_name = rec.get("name") or ""
                    total_candidates += 1

                    # Team ML guard
                    if tml is None or tml >= TEAM_WIN_MAX:
                        continue

                    price = best_price_over_point5_sot(rows_odds, player_name)
                    if price is None:
                        continue

                    picks.append({
                        "player": player_name,
                        "team": tname,
                        "fixture": name,
                        "kickoff": starting_at,
                        "side": side,
                        "price": float(price),
                        "team_ml": float(tml),
                        "tier": tier,
                        "series": series[:10],
                        "league_id": lid,
                    })

    # order by tier, then price
    tier_rank = {"10/10":0, "9/10":1, "8/10":2, "7/7":3}
    picks.sort(key=lambda r: (tier_rank.get(r["tier"], 9), -r["price"], r["fixture"], r["player"]))

    # render
    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}")
    lines.append(f"Criteria: SOT certs (10/10,9/10,8/10 or 7/7) | Over 0.5 SOT >= {MIN_DEC_PRICE:.2f} | Team ML < {TEAM_WIN_MAX:.2f} | Window={WINDOW_DAYS} days")
    lines.append(f"Candidates scanned (qualified by history before odds/ML): {total_candidates}")
    lines.append("")
    if not picks:
        lines.append("No SOT certs found.")
    else:
        lines.append("===== SOT CERTS — Player 1+ SOT =====")
        for r in picks:
            ser = ",".join(map(str, r["series"][:7]))
            lines.append(
                f" • {r['player']} — {r['team']} | {r['fixture']} @ {r['kickoff']} | "
                f"SOT Over 0.5 @ {r['price']:.3f} | Team ML {r['team_ml']:.3f} | tier {r['tier']} | series7: {ser}"
            )

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
