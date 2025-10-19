#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Possible Shot Certs — Sportmonks/Bet365

Criteria:
  • Player has 1+ shot in 100% of their recent games (min games = 3)
  • Position must be MID or FWD (from 'position_tag')
  • Bet365 Player Shots Over 0.5 price >= MIN_DEC_PRICE (default 1.30)
  • Team ML (market_id=1) <= TEAM_ML_MAX (default 3.50) — exclude big underdogs

Inputs (local files):
  • data/player_shots/by_league/{league_id}.json  (has players[], shots_last_n[], position_tag)
  • data/predicted_xi/by_league/{league_id}.json  (optional; maps team_id -> team name)
  • data/odds/b365/{league_id}.json               (from your Bet365 gatherer; has fixtures[].odds[])

Output:
  • data/value_bets/possible_shot_certs.txt  (and prints to console)

ENV (optional):
  • MIN_DEC_PRICE (default "1.30")  — threshold for Over 0.5 price (>=)
  • TEAM_WIN_MAX  (default "3.50")  — max ML for player's team (<=)
  • POS_TAGS_OK   (default "MID,FWD")  — comma list of allowed position_tag values
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

# ---------- Config ----------
MIN_PRICE   = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_WIN_MAX", "3.50"))
POS_TAGS_OK = {p.strip().upper() for p in (os.getenv("POS_TAGS_OK", "MID,FWD").split(",")) if p.strip()}

LEAGUE_IDS = [301, 384, 387, 564, 567, 600, 8, 82, 9]

ROOT      = Path(".")
PX_DIR    = ROOT / "data" / "predicted_xi" / "by_league"
SH_DIR    = ROOT / "data" / "player_shots" / "by_league"
ODDS_DIR  = ROOT / "data" / "odds" / "b365"
OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE  = OUT_DIR / "possible_shot_certs.txt"

MARKET_MATCH_WINNER = 1
MARKET_PLAYER_SHOTS = 268  # Over/Under shots

# ---------- String helpers ----------
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
    """Rough match 'O. Watkins' / 'Ollie Watkins' / 'Watkins' to Bet365 player strings."""
    if not player or not option_name_or_label: return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(option_name_or_label))
    if not last or last not in label: return False
    if initial:
        fw = label.split()[0][0:1] if label.split() else None
        if fw and fw == initial: return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

# ---------- IO helpers ----------
def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _team_name_map(league_id: int) -> Dict[int, str]:
    """Map team_id -> team_name from predicted_xi file."""
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

def best_over05_player_shots(odds_rows: List[dict], player: str) -> Optional[float]:
    """Find player's Over 0.5 price in market_id=268."""
    best = None
    for row in odds_rows:
        if int(row.get("market_id", 0)) != MARKET_PLAYER_SHOTS:
            continue
        candidate_name = row.get("name") or row.get("total") or row.get("original_label") or ""
        if not player_label_matches(player, candidate_name):
            continue
        line = as_float(row.get("label"))
        if line is None or not math.isclose(line, 0.5, abs_tol=1e-6):
            continue
        price = as_float(row.get("value"))
        if price is None:
            continue
        if best is None or price > best + 1e-12:
            best = price
    return best

# ---------- Selection ----------
def meets_shot_cert_100(series_raw: List[int]) -> Tuple[bool, int]:
    """Return (qualifies, n_used). Needs n>=3 and all of the first n are >=1."""
    seq = [x for x in (series_raw or []) if isinstance(x, int)]
    n = len(seq)
    if n < 3:
        return False, n
    # newest -> older; use all available games we have
    return all(x >= 1 for x in seq[:n]), n

def collect_candidates() -> List[dict]:
    out = []
    for lid in LEAGUE_IDS:
        shots_blob = _load_json(SH_DIR / f"{lid}.json") or {}
        team_map   = _team_name_map(lid)
        players = shots_blob.get("players") or shots_blob.get("rows") or shots_blob.get("data") or []
        for rec in players:
            pos = (rec.get("position_tag") or "").upper()
            if pos not in POS_TAGS_OK:
                continue
            series = rec.get("shots_last_n") or rec.get("series") or rec.get("shots") or []
            qualifies, n = meets_shot_cert_100(series)
            if not qualifies:
                continue
            player = rec.get("name") or rec.get("player_name") or rec.get("player")
            if not player:
                continue
            tid = rec.get("team_id")
            team = rec.get("team") or rec.get("team_name") or (team_map.get(int(tid)) if isinstance(tid, int) else None)
            if not team:
                continue
            out.append({
                "league_id": lid,
                "player": player,
                "team": team,
                "position_tag": pos,
                "series": series[:12],  # keep a little history
                "n": n,
            })
    return out

# ---------- Main ----------
def main():
    candidates = collect_candidates()
    if not candidates:
        msg = "[RESULT] No possible shot certs (100% with n>=3 in MID/FWD)."
        OUT_FILE.write_text(msg + "\n", encoding="utf-8")
        print(msg)
        return

    # Load odds once by league
    odds_by_league: Dict[int, dict] = {lid: (_load_json(ODDS_DIR / f"{lid}.json") or {}) for lid in LEAGUE_IDS}

    picks: List[dict] = []
    seen = set()  # avoid dupes (league_id, team, player)

    for c in candidates:
        lid, team, player = c["league_id"], c["team"], c["player"]
        key = (lid, team.lower(), player.lower())
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
            team_ml = home_ml if side == "home" else away_ml
            if team_ml is None or team_ml > TEAM_ML_MAX:
                continue

            price = best_over05_player_shots(odds_rows, player)
            if price is None or price < MIN_PRICE:
                continue

            picks.append({
                "league_id": lid,
                "player": player,
                "team": team,
                "position_tag": c["position_tag"],
                "fixture": fname,
                "kickoff": fx.get("starting_at") or "",
                "price": price,
                "team_ml": team_ml,
                "series": c["series"],
                "n": c["n"],
            })
            seen.add(key)
            break  # found the current fixture; move on

    # Render
    ts = dt.datetime.utcnow().isoformat()
    lines = [
        f"Generated at (UTC): {ts}",
        f"Filters: position in {sorted(POS_TAGS_OK)} | 100% shots in last n>=3 | Over 0.5 >= {MIN_PRICE:.2f} | Team ML <= {TEAM_ML_MAX:.2f}",
        "Market: Bet365 Player Shots Over 0.5 (market_id=268)",
        "",
    ]

    picks.sort(key=lambda x: (-x["price"], x["player"]))
    if not picks:
        lines.append("No matches found.")
    else:
        lines.append(f"===== POSSIBLE SHOT CERTS (count: {len(picks)}) =====")
        for x in picks:
            ser = ",".join(map(str, x["series"][:10]))
            lines.append(
                f" • {x['player']} [{x['position_tag']}] — {x['team']} | {x['fixture']} @ {x['kickoff']} | "
                f"Over 0.5 @ {x['price']:.3f} | Team ML {x['team_ml']:.3f} | n={x['n']} | series: {ser}"
            )

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
