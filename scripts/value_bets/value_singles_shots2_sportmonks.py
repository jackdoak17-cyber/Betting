#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value Singles — Shots 2+ (Over 1.5) — Sportmonks/Bet365

Criteria:
  • Form path A: 5/5  (all of last 5 with ≥2 shots)
  • Form path B: 7/10 (≥7 of last 10 with ≥2 shots)
  • Form path C: 4/5  (≥4 of last 5) AND team ML ≤ FAVORITE_CAP_4OF5 (default 2.50)
  • Odds filter: Bet365 Over 1.5 shots (market_id=268, line=1.5) with price > MIN_DEC_PRICE (default 1.72)
  • Favorites only: remove underdogs (team ML ≤ opponent ML)

Inputs (local):
  • data/player_shots/by_league/{league_id}.json
  • data/predicted_xi/by_league/{league_id}.json  (optional team_id -> name map)
  • data/odds/b365/{league_id}.json               (fixtures[].odds[]; market_id=1,268)

Output:
  • data/value_bets/value_singles_2plus.txt

ENV (optional):
  • MIN_DEC_PRICE        (default "1.72")   # price must be strictly greater than this
  • FAVORITES_ONLY       (default "true")   # remove underdogs by 1X2
  • FAVORITE_CAP_4OF5    (default "2.50")   # extra cap for the 4/5 path
  • LEAGUE_IDS           (default "301,384,387,564,567,600,8,82,9")
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

# -------- Config --------
MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.72"))  # price must be strictly greater than this
FAVORITES_ONLY = (os.getenv("FAVORITES_ONLY", "true").strip().lower() in {"1","true","yes","y"})
FAVORITE_CAP_4OF5 = float(os.getenv("FAVORITE_CAP_4OF5", "2.50"))

DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS = [int(x) for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",") if x.strip()]

ROOT     = Path(".")
PX_DIR   = ROOT / "data" / "predicted_xi" / "by_league"
PS_DIR   = ROOT / "data" / "player_shots" / "by_league"
ODDS_DIR = ROOT / "data" / "odds" / "b365"
OUT_DIR  = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "value_singles_2plus.txt"

MARKET_MATCH_WINNER = 1      # 1X2
MARKET_PLAYER_SHOTS = 268    # Player Shots (O/U)

# -------- String helpers --------
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TOK = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}
def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t not in GENERIC_TOK}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
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

def player_label_matches(player: str, option_name_or_label: str) -> bool:
    """Match 'O. Watkins' / 'Ollie Watkins' / 'Watkins' to Bet365 player strings."""
    if not player or not option_name_or_label: return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(option_name_or_label))
    if not last or last not in label: return False
    if initial:
        fw = label.split()[0][0:1] if label.split() else None
        if fw and fw == initial: return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

# -------- IO helpers --------
def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _team_name_map(league_id: int) -> Dict[int, str]:
    """Map team_id -> team name from predicted_xi file (optional)."""
    blob = _load_json(PX_DIR / f"{league_id}.json") or {}
    m: Dict[int, str] = {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

def parse_fixture_teams(fixture_name: str) -> Tuple[str, str]:
    if not fixture_name: return "",""
    for sep in [" vs ", " v ", " VS ", " Vs "]:
        if sep in fixture_name:
            a, b = fixture_name.split(sep, 1)
            return a.strip(), b.strip()
    if " - " in fixture_name:
        a, b = fixture_name.split(" - ", 1)
        return a.strip(), b.strip()
    return "", ""

def side_for_team(team_name: str, home_name: str, away_name: str) -> Optional[str]:
    if team_names_match(team_name, home_name): return "home"
    if team_names_match(team_name, away_name): return "away"
    return None

def as_float(x) -> Optional[float]:
    try:
        return float(str(x))
    except Exception:
        return None

def extract_team_ml_prices(odds_rows: List[dict], home_name: str, away_name: str) -> Tuple[Optional[float], Optional[float]]:
    home_price = None; away_price = None
    for row in odds_rows:
        if int(row.get("market_id", 0)) != MARKET_MATCH_WINNER:
            continue
        label = (row.get("label") or "").strip().lower()
        name  = (row.get("name")  or "").strip().lower()
        val   = as_float(row.get("value"))
        if val is None: 
            continue
        if label in {"1","home"} or team_names_match(home_name, label) or team_names_match(home_name, name):
            home_price = val if (home_price is None or val < home_price) else home_price
        elif label in {"2","away"} or team_names_match(away_name, label) or team_names_match(away_name, name):
            away_price = val if (away_price is None or val < away_price) else away_price
        elif team_names_match(home_name, row.get("name","")):
            home_price = val if (home_price is None or val < home_price) else home_price
        elif team_names_match(away_name, row.get("name","")):
            away_price = val if (away_price is None or val < away_price) else away_price
    return home_price, away_price

def best_over15_player_shots(odds_rows: List[dict], player: str) -> Optional[float]:
    """Find Over 1.5 price for the player in market_id=268."""
    best = None
    for row in odds_rows:
        if int(row.get("market_id", 0)) != MARKET_PLAYER_SHOTS:
            continue
        candidate = row.get("name") or row.get("total") or row.get("original_label") or ""
        if not player_label_matches(player, candidate):
            continue
        line = as_float(row.get("label"))
        if line is None or not math.isclose(line, 1.5, abs_tol=1e-6):
            continue
        price = as_float(row.get("value"))
        if price is None:
            continue
        if best is None or price > best + 1e-12:
            best = price
    return best

# -------- 2+ form filters --------
def q2_5of5(series: List[int]) -> bool:
    seq = [x for x in (series or []) if isinstance(x, int)]
    if len(seq) < 5: return False
    last5 = seq[:5]
    return all(x >= 2 for x in last5)

def q2_7of10(series: List[int]) -> bool:
    seq = [x for x in (series or []) if isinstance(x, int)]
    if len(seq) < 10: return False
    last10 = seq[:10]
    return sum(1 for x in last10 if x >= 2) >= 7

def q2_4of5(series: List[int]) -> bool:
    seq = [x for x in (series or []) if isinstance(x, int)]
    if len(seq) < 5: return False
    last5 = seq[:5]
    return sum(1 for x in last5 if x >= 2) >= 4

def collect_candidates() -> List[dict]:
    """Build candidate pool from player_shots files based on 2+ form only (5/5 OR 7/10 OR 4/5)."""
    out = []
    for lid in LEAGUE_IDS:
        sh_blob = _load_json(PS_DIR / f"{lid}.json") or {}
        team_map = _team_name_map(lid)
        players = sh_blob.get("players") or sh_blob.get("rows") or sh_blob.get("data") or []
        for rec in players:
            series = rec.get("shots_last_n") or rec.get("series") or rec.get("shots") or []
            tag = None
            if q2_5of5(series):
                tag = "5/5"
            elif q2_7of10(series):
                tag = "7/10"
            elif q2_4of5(series):
                tag = "4/5"
            if not tag:
                continue
            player = rec.get("name") or rec.get("player_name") or rec.get("player")
            if not player: 
                continue
            tid = rec.get("team_id")
            team = rec.get("team") or rec.get("team_name") or (team_map.get(int(tid)) if isinstance(tid, int) else None)
            if not team:
                continue
            pos_tag = (rec.get("position_tag") or rec.get("position") or rec.get("pos") or "").upper()
            out.append({
                "league_id": lid,
                "player": player,
                "team": team,
                "position_tag": pos_tag,
                "series": (series or [])[:12],
                "tag": tag,  # "5/5", "7/10", "4/5"
            })
    return out

# -------- Main --------
def main():
    candidates = collect_candidates()
    if not candidates:
        msg = "[RESULT] No value singles 2+ candidates (5/5, 7/10, or 4/5)."
        OUT_FILE.write_text(msg + "\n", encoding="utf-8")
        print(msg)
        return

    odds_by_league: Dict[int, dict] = {lid: (_load_json(ODDS_DIR / f"{lid}.json") or {}) for lid in LEAGUE_IDS}

    picks: List[dict] = []
    seen = set()

    for c in candidates:
        lid, player, team, tag = c["league_id"], c["player"], c["team"], c["tag"]
        key = (lid, team.lower(), player.lower(), tag)
        if key in seen:
            continue

        blob = odds_by_league.get(lid) or {}
        fixtures = blob.get("fixtures") or []
        for fx in fixtures:
            fname = fx.get("name") or ""
            home, away = parse_fixture_teams(fname)
            if not home or not away:
                continue
            side = side_for_team(team, home, away)
            if not side:
                continue

            odds_rows = fx.get("odds") or []
            home_ml, away_ml = extract_team_ml_prices(odds_rows, home, away)
            if home_ml is None or away_ml is None:
                continue

            team_ml = home_ml if side == "home" else away_ml
            opp_ml  = away_ml if side == "home" else home_ml

            # Remove underdogs
            if FAVORITES_ONLY and not (team_ml <= opp_ml):
                continue

            # Extra cap for 4/5 path
            if tag == "4/5" and not (team_ml <= FAVORITE_CAP_4OF5):
                continue

            # Price must be > MIN_PRICE (strictly greater, per your spec)
            price = best_over15_player_shots(odds_rows, player)
            if price is None or not (price > MIN_PRICE):
                continue

            picks.append({
                "league_id": lid,
                "player": player,
                "team": team,
                "position_tag": c.get("position_tag") or "",
                "fixture": fname,
                "kickoff": fx.get("starting_at") or "",
                "price": price,
                "team_ml": team_ml,
                "opp_ml": opp_ml,
                "series": c["series"],
                "tag": tag,
            })
            seen.add(key)
            break

    # Render
    ts = dt.datetime.utcnow().isoformat()
    lines = [
        f"Generated at (UTC): {ts}",
        f"Criteria (2+ shots): 5/5 OR 7/10 OR 4/5(+ team ML ≤ {FAVORITE_CAP_4OF5:.2f}) | O1.5 price > {MIN_PRICE:.2f} | {'Favorites only' if FAVORITES_ONLY else 'Favorites filter OFF'}",
        "Market: Bet365 Player Shots Over 1.5 (market_id=268, line=1.5)",
        "",
    ]

    groups = {"5/5": [], "7/10": [], "4/5": []}
    for p in picks:
        groups[p["tag"]].append(p)

    for tag in ("5/5", "7/10", "4/5"):
        rows = groups[tag]
        rows.sort(key=lambda x: (-x["price"], x["player"]))
        lines.append(f"===== {tag} (count: {len(rows)}) =====")
        if not rows:
            lines.append("  — none —")
            lines.append("")
            continue
        for x in rows:
            ser = ",".join(map(str, x["series"][:10]))
            cap_note = f" | cap≤{FAVORITE_CAP_4OF5:.2f}" if tag == "4/5" else ""
            pos = f"[{x['position_tag']}]" if x.get("position_tag") else ""
            lines.append(
                f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | "
                f"O1.5 @ {x['price']:.2f} | ML {x['team_ml']:.2f} vs {x['opp_ml']:.2f}{cap_note} | series: {ser}"
            )
        lines.append("")

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
