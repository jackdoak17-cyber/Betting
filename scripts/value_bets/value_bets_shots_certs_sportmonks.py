#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value bets — SHOTS (buckets: 10/10, 9/10, 8/10, 7/7) — Sportmonks/Bet365

Fixes:
  - Ensure we only take the **Over** selection (not Under) for market_id=268.
  - Also scan the builder market (market_id=160 "Number of Player Shots") and map **1+ ≈ Over 0.5**.
  - Pick the **best price** across 268 Over 0.5 and 160 1+.

Buckets:
  - 10/10: last 10 matches all had >=1 shot
  - 9/10 : last 10 matches had >=1 shot in exactly 9 of 10
  - 8/10 : last 10 matches had >=1 shot in exactly 8 of 10
  - 7/7  : last 7  matches all had >=1 shot (only if <10 matches available)

Filters:
  - Price >= MIN_DEC_PRICE (default 1.30)
  - Team ML (Bet365, market_id=1) for player's side < TEAM_ML_MAX (default 3.50)

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
MIN_PRICE   = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_WIN_MAX", "3.50"))

# Leagues you’re tracking
LEAGUE_IDS = [301, 384, 387, 564, 567, 600, 8, 82, 9]

# Paths
ROOT      = Path(".")
PX_DIR    = ROOT / "data" / "predicted_xi" / "by_league"   # team_id -> name map (optional)
SHOTS_DIR = ROOT / "data" / "player_shots" / "by_league"   # per-league player shots histories
ODDS_DIR  = ROOT / "data" / "odds" / "b365"                # Sportmonks Bet365 odds by league
OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE  = OUT_DIR / "shots_certs.txt"

# ========= STRING + MATCH HELPERS =========
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca",
                       "the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}
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
        fw = label.split()[0][0:1] if label.split() else None
        if fw and fw == initial: return True
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

def series_counts(series_raw: List[int]) -> Tuple[int, Optional[int], int]:
    """
    Returns (shots7, shots10, n_games_available)
    Assumes series is newest -> older and contains ints.
    """
    seq = [x for x in (series_raw or []) if isinstance(x, int)]
    n = len(seq)
    last7  = seq[:7]  if n >= 7  else []
    last10 = seq[:10] if n >= 10 else []
    shots7  = sum(1 for x in last7  if x >= 1) if last7  else 0
    shots10 = sum(1 for x in last10 if x >= 1) if last10 else None
    return shots7, shots10, n

def bucket_for_series(series_raw: List[int]) -> Optional[str]:
    """
    Decide which bucket a player belongs to.
    - Prefer 10-game buckets if we have 10+ games: 10/10 > 9/10 > 8/10.
    - Else if we have 7-9 games: use 7/7 when perfect.
    - Otherwise: no bucket.
    """
    shots7, shots10, n = series_counts(series_raw)
    if shots10 is not None:
        if shots10 == 10: return "10/10"
        if shots10 == 9:  return "9/10"
        if shots10 == 8:  return "8/10"
        return None
    if n >= 7 and shots7 == 7:
        return "7/7"
    return None

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
            if not isinstance(series, list): 
                continue
            bucket = bucket_for_series(series)
            if not bucket: 
                continue
            player = rec.get("name") or rec.get("player_name") or rec.get("player")
            if not player: 
                continue
            tid = rec.get("team_id")
            team = rec.get("team") or rec.get("team_name") or (team_map.get(int(tid)) if isinstance(tid, int) else None)
            if not team: 
                continue
            pos = rec.get("position") or rec.get("position_tag") or rec.get("pos") or ""
            out.append({
                "league_id": lid, "player": player, "team": team,
                "position": pos, "series": series[:12], "bucket": bucket
            })
    return out

# ========= ODDS (Sportmonks Bet365 JSONs) =========
MARKET_MATCH_WINNER = 1      # "Match Winner" / "Full Time Result"
MARKET_PLAYER_SHOTS = 268    # "Player Shots" (Over/Under, we want Over 0.5)
MARKET_NUM_PLAYER_SHOTS = 160# "Number of Player Shots" (builder: 1+,2+,3+)

def load_odds_for_league(league_id: int) -> dict:
    p = ODDS_DIR / f"{league_id}.json"
    return _load_json(p) or {}

def parse_fixture_teams(fixture_name: str) -> Tuple[str, str]:
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
        try:
            if int(row.get("market_id", 0)) != MARKET_MATCH_WINNER:
                continue
        except Exception:
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

# ---- helpers for market 268 (O/U) ----
def is_over_selection(row: dict) -> bool:
    txt = " ".join([str(row.get("total") or ""), str(row.get("original_label") or "")]).lower()
    return ("over" in txt) and ("under" not in txt)

def extract_line(row: dict) -> Optional[float]:
    if row.get("handicap") not in (None, ""):
        v = as_float(row.get("handicap"))
        if v is not None: return v
    # fallbacks: look inside label/total/original_label
    for fld in ("label", "original_label", "total"):
        s = str(row.get(fld) or "")
        m = re.search(r"([-+]?\d+(?:\.\d+)?)", s)
        if m:
            try: return float(m.group(1))
            except: pass
    return None

# ---- helpers for market 160 (builder) ----
def builder_threshold_at_least_one(row: dict) -> bool:
    """
    True if the builder selection represents 1+ shots for the player.
    We check label/original_label/total text for '1+' or '1 or more' etc.
    """
    s = " ".join([str(row.get("label") or ""), str(row.get("original_label") or ""), str(row.get("total") or "")]).lower()
    # Common patterns: "1+", "1 or more", "over 0.5" (rare), maybe just "1"
    if re.search(r"\b1\s*\+\b", s): return True
    if re.search(r"\b1\s*or\s*(?:more|over|higher)\b", s): return True
    # exact "1" alone can be ambiguous (exactly 1), so avoid taking it unless explicitly "1+"
    return False

def best_over05_or_builder1plus_price(odds_rows: List[dict], player: str) -> Optional[float]:
    """
    Return the best available price for the player's **1+ shot**:
      - Market 268 (Player Shots, Over 0.5) using only **Over** selections at line 0.5
      - Market 160 (Number of Player Shots), taking **1+** selection
    """
    best = None

    for row in odds_rows:
        try:
            mid = int(row.get("market_id", 0))
        except Exception:
            continue

        # Player name check (works for both markets)
        candidate = row.get("name") or row.get("total") or row.get("original_label") or ""
        if not player_label_matches(player, candidate):
            continue

        # Market 268: Over 0.5
        if mid == MARKET_PLAYER_SHOTS:
            if not is_over_selection(row):
                continue
            line = extract_line(row)
            if line is None or not math.isclose(line, 0.5, abs_tol=1e-6):
                continue
            price = as_float(row.get("value"))
            if price is None:
                continue
            if (best is None) or (price > best + 1e-12):
                best = price

        # Market 160: 1+ (builder)
        elif mid == MARKET_NUM_PLAYER_SHOTS:
            if not builder_threshold_at_least_one(row):
                continue
            price = as_float(row.get("value"))
            if price is None:
                continue
            if (best is None) or (price > best + 1e-12):
                best = price

    return best

# ========= MAIN =========
def main():
    # 1) Candidates by bucket based on form
    candidates = collect_candidates()
    if not candidates:
        text = "[RESULT] No player candidates met the 10/10, 9/10, 8/10, or 7/7 buckets."
        OUT_FILE.write_text(text + "\n", encoding="utf-8")
        print(text); return

    # 2) Load odds once per league
    odds_by_league: Dict[int, dict] = {lid: load_odds_for_league(lid) for lid in LEAGUE_IDS}

    # 3) Evaluate odds filters and build results by bucket
    buckets = {"10/10": [], "9/10": [], "8/10": [], "7/7": []}

    for c in candidates:
        lid, team, plyr, bucket = c["league_id"], c["team"], c["player"], c["bucket"]
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
            if team_ml is None or team_ml >= TEAM_ML_MAX:
                continue

            # --- price: best of Over 0.5 (268) and 1+ (160)
            price = best_over05_or_builder1plus_price(odds_rows, plyr)
            if price is None or price < MIN_PRICE:
                continue

            buckets[bucket].append({
                "player": plyr,
                "position": c.get("position") or "",
                "team": team,
                "fixture": fname,
                "kickoff": fx.get("starting_at") or "",
                "price": price,
                "team_ml": team_ml,
                "series": c["series"],
                "league_id": lid,
            })

    # 4) Render output grouped by bucket
    ts = dt.datetime.utcnow().isoformat()
    lines = []
    lines.append(f"Generated at (UTC): {ts}  |  Min price >= {MIN_PRICE:.2f}  |  Team ML < {TEAM_ML_MAX:.2f}")
    lines.append("Buckets: 10/10, 9/10, 8/10 (last-10); 7/7 (last-7 when <10 games available)")
    lines.append("Markets: Bet365 Player Shots Over 0.5 (id=268, Over only) + Number of Player Shots 1+ (id=160)")
    lines.append("")

    def render_bucket(tag: str, rows: List[dict]):
        rows.sort(key=lambda x: (-x["price"], x["player"]))
        lines.append(f"===== {tag} =====  (count: {len(rows)})")
        if not rows:
            lines.append("  — none —")
            lines.append("")
            return
        for x in rows:
            ser = ",".join(map(str, x["series"][:10]))
            pos = f"[{x['position']}]" if x.get("position") else ""
            lines.append(
                f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | "
                f"Over 0.5 @ {x['price']:.3f} | Team ML {x['team_ml']:.3f} | series: {ser}"
            )
        lines.append("")

    for tag in ("10/10", "9/10", "8/10", "7/7"):
        render_bucket(tag, buckets[tag])

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
