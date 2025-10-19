#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value bets — SHOTS certs (1+ in 100% of last 7, min games=7)
Reads your local Sportmonks Bet365 odds and player shots form.

Source data:
  - Player form: data/player_shots/by_league/{league_id}.json (+ predicted XI team names)
  - Odds (Bet365 only): data/odds/b365/{league_id}.json (written by your Bet365 gatherer)

Markets used:
  - Player Shots (market_id = 268)  -> Over 0.5 for the named player
  - Match Winner (market_id = 1)    -> Team ML filter for player's side

Filters:
  - Player qualifies: last 7 matches all >= 1 shot (len(series) >= 7)
  - Price Over 0.5 >= MIN_DEC_PRICE (default 1.30)
  - Team ML (Bet365) for player's side < TEAM_WIN_MAX (default 3.50)

Output:
  data/value_bets/shots_certs.txt + console

ENV (optional):
  MIN_DEC_PRICE  (default 1.30)
  TEAM_WIN_MAX   (default 3.50)
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

# ========= CONFIG =========
MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_WIN_MAX", "3.50"))

# Leagues you’re tracking
LEAGUE_IDS = [301, 384, 387, 564, 567, 600, 8, 82, 9]

# Paths
ROOT      = Path(".")
PX_DIR    = ROOT / "data" / "predicted_xi" / "by_league"   # team_id -> name map (optional)
SHOTS_DIR = ROOT / "data" / "player_shots" / "by_league"   # per-league player shots histories
ODDS_DIR  = ROOT / "data" / "odds" / "b365"                # Sportmonks Bet365 odds by league
OUT_DIR   = ROOT / "data" / "value_bets"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE  = OUT_DIR / "shots_certs.txt"

# ========= STRING + MATCH HELPERS =========
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

def player_label_matches(player: str, option_name_or_label: str) -> bool:
    """Match 'O. Watkins' / 'Ollie Watkins' / 'Watkins' to Bet365 player strings."""
    if not player or not option_name_or_label: return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(option_name_or_label))
    if not last or last not in label: return False
    if initial:
        first_word_initial = label.split()[0][0:1] if label.split() else None
        if first_word_initial and first_word_initial == initial:
            return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

# ========= FORM (player shots histories) =========
def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _team_name_map(league_id: int) -> Dict[int, str]:
    """Map team_id -> team_name from predicted_xi file (optional helper)."""
    blob = _load_json(PX_DIR / f"{league_id}.json") or {}
    m: Dict[int, str] = {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

def last7_all_one_plus(series: List[int]) -> bool:
    seq = [x for x in series if isinstance(x, int)]
    if len(seq) < 7: return False
    sub = seq[:7]  # assume series is newest -> older
    return all(x >= 1 for x in sub)

def collect_candidates() -> List[dict]:
    """
    Expect per-league shots files to include per-player 'series' (or 'shots_last_n').
    Fallback keys supported: 'series', 'shots_last_n', 'shots'.
    """
    out = []
    for lid in LEAGUE_IDS:
        shots_blob = _load_json(SHOTS_DIR / f"{lid}.json") or {}
        team_map = _team_name_map(lid)
        players = shots_blob.get("players") or shots_blob.get("rows") or shots_blob.get("data") or []
        for rec in players:
            series = rec.get("series") or rec.get("shots_last_n") or rec.get("shots") or []
            if not isinstance(series, list): continue
            if not last7_all_one_plus(series): continue
            player = rec.get("name") or rec.get("player_name") or rec.get("player")
            if not player: continue
            tid = rec.get("team_id")
            team = rec.get("team") or rec.get("team_name") or (team_map.get(int(tid)) if isinstance(tid, int) else None)
            if not team: continue
            pos = rec.get("position") or rec.get("pos")
            out.append({
                "league_id": lid, "player": player, "team": team,
                "position": pos or "", "series": series[:10]
            })
    return out

# ========= ODDS (Sportmonks Bet365 JSONs) =========
MARKET_MATCH_WINNER = 1      # "Match Winner" / "Full Time Result"
MARKET_PLAYER_SHOTS = 268    # "Player Shots" (we’ll use line 0.5)

def load_odds_for_league(league_id: int) -> dict:
    """Load per-league Bet365 odds payload written by your gatherer."""
    p = ODDS_DIR / f"{league_id}.json"
    blob = _load_json(p) or {}
    return blob

def parse_fixture_teams(fixture_name: str) -> Tuple[str, str]:
    """
    Parse 'Home vs Away' from fixture.name written by your fixtures job.
    If not present, return ("","").
    """
    if not fixture_name: return "",""
    for sep in [" vs ", " v ", " VS ", " Vs "]:
        if sep in fixture_name:
            home, away = fixture_name.split(sep, 1)
            return home.strip(), away.strip()
    if " - " in fixture_name:
        home, away = fixture_name.split(" - ", 1)
        return home.strip(), away.strip()
    return "", ""

def side_for_team(team_name: str, home_name: str, away_name: str) -> Optional[str]:
    if team_names_match(team_name, home_name):
        return "home"
    if team_names_match(team_name, away_name):
        return "away"
    return None

def as_float(x) -> Optional[float]:
    try:
        return float(str(x))
    except Exception:
        return None

def extract_team_ml_prices(odds_rows: List[dict], home_name: str, away_name: str) -> Tuple[Optional[float], Optional[float]]:
    """
    From market_id == 1 rows, extract Home/Away decimal prices.
    Labels encountered may be 'Home'/'Away'/'Draw', '1'/'2'/'X', or team names.
    """
    home_price = None
    away_price = None
    for row in odds_rows:
        if int(row.get("market_id", 0)) != MARKET_MATCH_WINNER:
            continue
        label = (row.get("label") or "").strip().lower()
        name  = (row.get("name")  or "").strip().lower()
        val   = as_float(row.get("value"))
        if val is None:
            continue
        if label in {"1", "home"} or team_names_match(home_name, label) or team_names_match(home_name, name):
            home_price = val if (home_price is None or val < home_price) else home_price
        elif label in {"2", "away"} or team_names_match(away_name, label) or team_names_match(away_name, name):
            away_price = val if (away_price is None or val < away_price) else away_price
        elif team_names_match(home_name, row.get("name","")):
            home_price = val if (home_price is None or val < home_price) else home_price
        elif team_names_match(away_name, row.get("name","")):
            away_price = val if (away_price is None or val < away_price) else away_price
    return home_price, away_price

def best_over05_player_shots(odds_rows: List[dict], player: str) -> Optional[float]:
    """
    From market_id == 268 rows, find player's Over 0.5 price.
    Interprets 'label' as the shots line (e.g., '0.5', '1.5', ...).
    """
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

# ========= MAIN =========
def main():
    # 1) Build candidate list from your player_shots data
    candidates = collect_candidates()
    if not candidates:
        text = "[RESULT] No player candidates with 1+ in each of last 7."
        OUT_FILE.write_text(text + "\n", encoding="utf-8")
        print(text)
        return

    # 2) Load per-league Bet365 odds once
    odds_by_league: Dict[int, dict] = {lid: load_odds_for_league(lid) for lid in LEAGUE_IDS}

    # 3) For each candidate, find their fixture in the same league and evaluate
    flagged: List[dict] = []
    for c in candidates:
        lid   = c["league_id"]
        team  = c["team"]
        plyr  = c["player"]
        odds_blob = odds_by_league.get(lid) or {}
        fixtures = odds_blob.get("fixtures") or []

        for fx in fixtures:
            fname = fx.get("name") or ""  # "Home vs Away"
            home, away = parse_fixture_teams(fname)
            if not home or not away:
                continue
            side = side_for_team(team, home, away)
            if not side:
                continue

            # Team ML check (market 1)
            odds_rows = fx.get("odds") or []
            home_ml, away_ml = extract_team_ml_prices(odds_rows, home, away)
            team_ml = home_ml if side == "home" else away_ml
            if team_ml is None or team_ml >= TEAM_ML_MAX:
                continue

            # Player Shots Over 0.5 (market 268)
            price = best_over05_player_shots(odds_rows, plyr)
            if price is None or price < MIN_PRICE:
                continue

            flagged.append({
                "player": plyr,
                "position": c.get("position") or "",
                "team": team,
                "fixture": fname,
                "kickoff": fx.get("starting_at") or "",
                "price": price,
                "team_ml": team_ml,
                "series": c["series"],
                "league_id": lid,
                "market": "Player Shots Over 0.5",
            })

    # 4) Render output
    flagged.sort(key=lambda x: (-x["price"], x["player"]))
    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}  |  Min price: {MIN_PRICE:.2f}  |  Team ML < {TEAM_ML_MAX:.2f}")
    lines.append("Criteria: 1+ shot in 100% of last 7 (n>=7)  |  Market: Bet365 Player Shots Over 0.5 (market_id=268)")
    lines.append("")

    if not flagged:
        lines.append("No matches found.")
    else:
        lines.append("===== CERTS — Player Shots 1+ =====")
        for x in flagged:
            ser = ",".join(map(str, x["series"][:7]))
            pos = f"[{x['position']}]" if x.get("position") else ""
            lines.append(
                f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | "
                f"Over 0.5 @ {x['price']:.3f} | Team ML {x['team_ml']:.3f} | series7: {ser}"
            )

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
