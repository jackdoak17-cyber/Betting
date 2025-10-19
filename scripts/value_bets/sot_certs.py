#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SOT certs — Player 1+ SOT (Sportmonks odds + local SOT histories)

Criteria (same as your 1+ SHOTS certs, but for SOT):
  - Player qualifies if last 7 matches are all >= 1 SOT (min games n >= 7).
  - Bet365 price for Over 0.5 SOT >= 1.30.
  - Team Match Winner (ML) < 3.50 for player's side (conservative: drop if ML missing).

Reads:
  - Fixtures:                         data/fixtures/{league_id}.json
  - Player SOT histories:             data/player_shots_on_target/by_league/{league_id}.json
  - Bet365 odds (Sportmonks):         data/odds/b365/{league_id}.json  (or per-fixture files)
Writes:
  - data/value_bets/sot_certs.txt

Env (optional):
  LEAGUE_IDS    CSV list; default = auto from fixtures dir
  MIN_DEC_PRICE default 1.30
  TEAM_WIN_MAX  default 3.50
  WINDOW_DAYS   default 7 (0 = no limit)
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional

ROOT = Path(".")
FIX_DIR  = ROOT / "data" / "fixtures"
PSOT_DIR = ROOT / "data" / "player_shots_on_target" / "by_league"
ODDS_DIR = ROOT / "data" / "odds" / "b365"
OUT_DIR  = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "sot_certs.txt"

MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_WIN_MAX  = float(os.getenv("TEAM_WIN_MAX",  "3.50"))
WINDOW_DAYS   = int(os.getenv("WINDOW_DAYS",     "7"))

# ---------- utils ----------
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
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
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

def parse_fixture_teams(name: str) -> Tuple[str, str]:
    if not name: return "",""
    for sep in (" vs ", " v ", " - ", " VS ", " Vs "):
        if sep in name:
            a,b = name.split(sep,1)
            return a.strip(), b.strip()
    return "",""

def as_float(x) -> Optional[float]:
    try: return float(str(x))
    except Exception: return None

# ---------- SOT rules ----------
def last7_all_one_plus(series: List[int]) -> bool:
    xs = [x for x in (series or []) if isinstance(x, int)]
    if len(xs) < 7: return False
    sub = xs[:7]  # latest_first
    return all(v >= 1 for v in sub)

# ---------- odds helpers (Sportmonks rows) ----------
MATCH_WINNER_ALIASES = {
    "match winner","match result","full time result","fulltime result",
    "1x2","result","win/draw/win","90 minutes","3-way","3 way","regular time result"
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

def is_player_sot_market(row: dict) -> bool:
    md = norm(row.get("market_description") or "")
    # Accept common variants, exclude other player markets
    return ("player" in md and ("shots on target" in md or "sot" in md))

def row_line(row: dict) -> Optional[float]:
    # Prefer explicit handicap
    h = row.get("handicap")
    v = as_float(h) if h is not None else None
    if v is not None: return v
    # Else parse from label/total
    for field in ("label", "total"):
        s = (row.get(field) or "").strip()
        m = re.search(r"([-+]?\d+(?:\.\d+)?)", s)
        if m:
            try: return float(m.group(1))
            except: pass
    return None

def row_player_name(row: dict) -> str:
    # Sportmonks often sets 'name' to the player; sometimes 'total' holds it too.
    for k in ("name","total","original_label"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def player_names_match(player: str, option_player: str) -> bool:
    # tolerant match: last name + optional initial
    if not player or not option_player: return False
    p = norm(player); o = norm(option_player)
    if p == o: return True
    p_parts = p.split()
    o_parts = o.split()
    if not p_parts or not o_parts: return False
    plast = p_parts[-1]
    if plast and plast in o:
        # if first initial matches or single-token last-name containment
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
        # Need player's row
        opt_player = row_player_name(r)
        if not player_names_match(player_name, opt_player):
            continue
        # Need 0.5 line (some books use 1+, treat as 1.0; we require 0.5 exactly)
        line = row_line(r)
        if line is None or not math.isclose(line, 0.5, abs_tol=1e-6):
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
        if not (fx_path.exists() and psot_path.exists() and odds_path.exists()):
            continue

        fixtures = load_json(fx_path).get("fixtures") or []
        psot_blob = load_json(psot_path)
        odds_blob = load_json(odds_path)

        # Index SOT players by team_id
        players_by_team: Dict[int, List[dict]] = {}
        for rec in (psot_blob.get("players") or []):
            tid = rec.get("team_id")
            if isinstance(tid, int):
                players_by_team.setdefault(tid, []).append(rec)

        # Odds by fixture_id
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
            # Find team ids/names from fixture (Sportmonks participants carry official team_id)
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

            if home_tid is None or away_tid is None:
                # Fall back to split
                h, a = parse_fixture_teams(name)
                home_name = home_name or h
                away_name = away_name or a

            odds_fx = odds_by_fixture.get(fid)
            if not odds_fx:
                continue
            rows_odds = odds_fx.get("odds") or []
            if not isinstance(rows_odds, list):
                continue

            # ML filter
            home_ml, away_ml = extract_ml(rows_odds)

            # For each team in this fixture, scan qualifying players
            for side, tid, tname, tml in (
                ("home", home_tid, home_name, home_ml),
                ("away", away_tid, away_name, away_ml),
            ):
                if not isinstance(tid, int):
                    continue

                for rec in players_by_team.get(tid, []):
                    series = rec.get("on_target_last_n") or []
                    n = rec.get("n") or len(series)
                    if not (isinstance(series, list) and last7_all_one_plus(series) and n >= 7):
                        continue

                    player_name = rec.get("name") or ""
                    total_candidates += 1

                    # Require ML present and < TEAM_WIN_MAX
                    if tml is None or tml >= TEAM_WIN_MAX:
                        continue

                    # Best Bet365 price for Over 0.5 SOT for this player in this fixture
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
                        "series7": series[:7],
                        "league_id": lid,
                    })

    # Sort: price desc, then fixture/player
    picks.sort(key=lambda r: (-r["price"], r["fixture"], r["team"], r["player"]))

    # Render
    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}")
    lines.append(f"Criteria: 1+ SOT in last 7 (100% | n>=7) | Over 0.5 SOT >= {MIN_DEC_PRICE:.2f} | Team ML < {TEAM_WIN_MAX:.2f} | Window={WINDOW_DAYS} days")
    lines.append(f"Candidates scanned: {total_candidates}")
    lines.append("")
    if not picks:
        lines.append("No SOT certs found.")
    else:
        lines.append("===== SOT CERTS — Player 1+ SOT =====")
        for r in picks:
            ser = ",".join(map(str, r["series7"]))
            lines.append(
                f" • {r['player']} — {r['team']} | {r['fixture']} @ {r['kickoff']} | "
                f"SOT Over 0.5 @ {r['price']:.3f} | Team ML {r['team_ml']:.3f} | series7: {ser}"
            )

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
