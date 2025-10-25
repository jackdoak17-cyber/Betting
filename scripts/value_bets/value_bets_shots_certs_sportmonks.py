#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value bets — SHOTS (buckets: 10/10, 9/10, 8/10, 7/7)
- Uses local Sportmonks Bet365 odds and player shots form.

Buckets:
  - 10/10: last 10 matches all had >=1 shot
  - 9/10 : last 10 matches had >=1 shot in exactly 9 of 10
  - 8/10 : last 10 matches had >=1 shot in exactly 8 of 10
  - 7/7  : last 7  matches all had >=1 shot (only used if <10 matches available)

Filters:
  - Price Over 0.5 >= MIN_DEC_PRICE (default 1.30)
  - Team ML (Bet365, market_id=1) for player's side < TEAM_WIN_MAX (default 3.50)

Output:
  data/value_bets/shots_certs.txt + console

ENV (optional):
  MIN_DEC_PRICE  (default 1.30)
  TEAM_WIN_MAX   (default 3.50)
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Iterable

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
ODDS_FIX  = ODDS_DIR / "fixtures"                          # per-fixture freshest odds
OUT_DIR   = ROOT / "data" / "value_bets"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE  = OUT_DIR / "shots_certs.txt"

# ========= STRING + NAME MATCH HELPERS =========
SUFFIXES = {"jr","junior","sr","senior","ii","iii","iv","filho","neto"}
SURNAME_PREFIXES = {"da","de","del","der","di","dos","du","la","le","van","von","bin","al"}
GENERIC_TEAM_TOKENS = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^\w\s\.-]", " ", s)
    return norm_spaces(s)

def cleanup_label(label: str) -> str:
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

def person_part_from_option(label: str) -> str:
    # Extract the name portion before "Over/Under" bits
    s = cleanup_label(label or "")
    m = re.split(r"\b(?:-?\s*over|-?\s*under|\s+o\/u|\s+o\d+|\s+u\d+)\b", s, flags=re.IGNORECASE)
    return m[0].strip() if m else s

def drop_suffixes(parts: List[str]) -> List[str]:
    out = list(parts)
    while out and re.sub(r"[^\w]+", "", out[-1]).lower() in SUFFIXES:
        out = out[:-1]
    return out

def split_name_tokens(name: str) -> List[str]:
    return [p for p in norm(name).replace("-", " ").split() if p]

def surname_tokens(parts: List[str]) -> List[str]:
    parts = drop_suffixes(parts)
    if not parts:
        return []
    if len(parts) >= 2 and parts[-2] in SURNAME_PREFIXES:
        return parts[-2:]
    return parts[-1:]

def first_initial(parts: List[str]) -> Optional[str]:
    parts = drop_suffixes(parts)
    for p in parts[:-1]:
        ch = p[:1]
        if ch:
            return ch
    return None

def name_variants(full_name: str) -> List[str]:
    if not full_name:
        return []
    parts = split_name_tokens(full_name)
    if not parts:
        return []
    sur = " ".join(surname_tokens(parts))
    init = first_initial(parts)
    full = " ".join(parts)
    out = {full, sur}
    if init:
        out.add(f"{init}. {sur}")
        out.add(f"{init} {sur}")
    out.add(f"{sur} {init or ''}".strip())
    # normalize + remove dots for matching
    return sorted({norm(o).replace(".", "") for o in out if o})

def aliases_from_record(rec: dict) -> List[str]:
    names: List[str] = []
    for k in ("name","player_name","player","short_name","common_name","display_name","full_name","known_as"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            names.append(v.strip())
    # dedupe raw names
    seen, uniq = set(), []
    for n in names:
        key = norm(n)
        if key not in seen:
            seen.add(key); uniq.append(n)
    # expand to variants
    out: List[str] = []
    for n in uniq:
        out.extend(name_variants(n))
    # dedupe variants
    seen2, uniq2 = set(), []
    for a in out:
        if a not in seen2:
            seen2.add(a); uniq2.append(a)
    return uniq2

def label_matches_aliases(option_label: str, aliases: Iterable[str]) -> Tuple[bool,int]:
    """
    Returns (matched, score). score=2 for exact alias eq; score=1 for loose/subset match.
    """
    lab = norm(person_part_from_option(option_label)).replace(".", "")
    if not lab:
        return (False, 0)
    lab_tokens = set(lab.split())
    for alias in aliases:
        atoks = set(alias.split())
        if alias == lab:
            return (True, 2)
        if atoks and (atoks.issubset(lab_tokens) or lab_tokens.issubset(atoks)):
            return (True, 1)
        # surname + optional initial
        a_parts = alias.split()
        a_sur = a_parts[-2:] if len(a_parts) >= 2 and a_parts[-2] in SURNAME_PREFIXES else a_parts[-1:]
        if set(a_sur).issubset(lab_tokens):
            if len(a_parts) >= 2 and len(a_parts[0]) == 1:  # initial present in alias
                if a_parts[0] in lab_tokens or lab.startswith(a_parts[0] + " "):
                    return (True, 1)
                continue
            return (True, 1)
    return (False, 0)

# ========= TEAM / FIXTURE HELPERS =========
def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5: return True
    if len(inter) >= 2: return True
    return False

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

# ========= MARKET CLASSIFIERS =========
MARKET_MATCH_WINNER = 1      # "Match Winner" / "Full Time Result"
MARKET_PLAYER_SHOTS = 268    # "Player Shots" (we’ll use line 0.5)

NEGATIVE_SHOTS_TERMS = {
    "outside", "from outside", "outside the box", "first half", "second half",
    "1st half", "2nd half", "header", "headers", "distance"
}

def market_is_player_shots(desc: str) -> bool:
    d = (desc or "").lower()
    if not d: return False
    if "player" not in d: return False
    if "shot" not in d: return False
    if any(t in d for t in NEGATIVE_SHOTS_TERMS): return False
    return True

MATCH_WINNER_KEYS = ["match winner", "match result", "1x2", "full time result", "win/draw/win", "to win", "90 minutes", "result"]

def market_is_match_winner(desc: str) -> bool:
    s = (desc or "").lower()
    return any(k in s for k in MATCH_WINNER_KEYS)

# ========= ODDS LOADERS =========
def load_odds_for_league(league_id: int) -> dict:
    p = ODDS_DIR / f"{league_id}.json"
    return _load_json(p) or {}

def load_odds_for_fixture(fid: int) -> dict:
    p = ODDS_FIX / f"{fid}.json"
    return _load_json(p) or {}

def extract_team_ml_prices(odds_rows: List[dict], home_name: str, away_name: str) -> Tuple[Optional[float], Optional[float]]:
    home_price = None; away_price = None
    for row in odds_rows:
        if int(row.get("market_id", 0)) != MARKET_MATCH_WINNER:
            continue
        # Strict Bet365
        if str(row.get("bookmaker_id")) not in ("2", 2):
            continue
        label = (row.get("label") or "").strip().lower()
        name  = (row.get("name")  or "").strip().lower()
        val   = as_float(row.get("value"))
        if val is None: continue
        if label in {"1", "home"} or team_names_match(home_name, label) or team_names_match(home_name, name):
            home_price = val if (home_price is None or val < home_price) else home_price
        elif label in {"2", "away"} or team_names_match(away_name, label) or team_names_match(away_name, name):
            away_price = val if (away_price is None or val < away_price) else away_price
        elif team_names_match(home_name, row.get("name","")):
            home_price = val if (home_price is None or val < home_price) else home_price
        elif team_names_match(away_name, row.get("name","")):
            away_price = val if (away_price is None or val < away_price) else away_price
    return home_price, away_price

# ----- Over 0.5 lookup with Over/Under disambiguation + alias matching (with priority) -----
def _row_text(row: dict) -> str:
    fields = ["label","name","original_label","market_description","outcome","outcome_name","header","description"]
    return " ".join([str(row.get(f, "")) for f in fields]).lower()

def _line_is_point5(row: dict) -> bool:
    t = as_float(row.get("total"))
    if t is not None:
        return math.isclose(t, 0.5, abs_tol=1e-6)
    l = as_float(row.get("label"))
    if l is not None:
        return math.isclose(l, 0.5, abs_tol=1e-6)
    blob = _row_text(row).replace(",", ".")
    return "0.5" in blob or "0,5" in blob

def _is_over_row(row: dict) -> Optional[bool]:
    txt = _row_text(row)
    if re.search(r"\bunder\b", txt):
        return False
    if re.search(r"\bover\b", txt):
        return True
    if "+0.5" in txt or "0.5+" in txt or "0,5+" in txt:
        return True
    return None  # ambiguous

def best_over05_player_shots(odds_rows: List[dict], player_rec: dict) -> Optional[float]:
    """
    Find Over 0.5 price for the player in Player Shots (market_id=268).
    Strategy:
      1) Filter rows to this player and line 0.5 (Bet365 only).
      2) Score matches: exact alias (=2) > loose alias (=1).
      3) Prefer explicit Over; if ambiguous, pick LOWER price as Over.
    """
    aliases = aliases_from_record(player_rec)
    if not aliases:
        return None

    exact: List[Tuple[Optional[bool], float]] = []
    loose: List[Tuple[Optional[bool], float]] = []

    for row in odds_rows:
        if int(row.get("market_id", 0)) != MARKET_PLAYER_SHOTS:
            continue
        if str(row.get("bookmaker_id")) not in ("2", 2):
            continue
        if not market_is_player_shots(row.get("market_description") or row.get("market_name") or ""):
            continue
        if not _line_is_point5(row):
            continue

        # Check name in several fields
        matched, score = False, 0
        for cand in (
            row.get("name",""),
            row.get("original_label",""),
            row.get("label",""),
            row.get("outcome_name",""),
            row.get("header",""),
            row.get("description",""),
        ):
            if not cand: 
                continue
            ok, sc = label_matches_aliases(str(cand), aliases)
            if ok and sc > score:
                matched, score = True, sc
        if not matched:
            continue

        price = as_float(row.get("value"))
        if price is None:
            continue
        over_flag = _is_over_row(row)
        (exact if score == 2 else loose).append((over_flag, price))

    def pick(candidates: List[Tuple[Optional[bool], float]]) -> Optional[float]:
        if not candidates:
            return None
        explicit_over = [p for flag, p in candidates if flag is True]
        if explicit_over:
            return min(explicit_over)
        ambiguous = [p for flag, p in candidates if flag is None]
        if ambiguous:
            return min(ambiguous)
        return None

    # Try exact matches first, then loose
    price = pick(exact)
    if price is not None:
        return price
    return pick(loose)

# ========= FORM (player shots histories) =========
def series_counts(series_raw: List[int]) -> Tuple[int, Optional[int], int]:
    """
    Returns (shots7, shots10, n_games_available)
    Assumes series is newest -> older and contains ints.
    """
    seq = [x for x in series_raw if isinstance(x, int)]
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
            pos = rec.get("position") or rec.get("pos") or rec.get("position_tag") or ""
            # stash a richer record for alias generation in odds matching
            rec_out = dict(rec); rec_out.update({"name": player})
            out.append({
                "league_id": lid, "player": player, "team": team,
                "position": pos, "series": series[:12], "bucket": bucket, "_rec": rec_out
            })
    return out

# ========= MAIN =========
def main():
    # 1) Candidates by bucket based on form
    candidates = collect_candidates()
    if not candidates:
        text = "[RESULT] No player candidates met the 10/10, 9/10, 8/10, or 7/7 buckets."
        OUT_FILE.write_text(text + "\n", encoding="utf-8")
        print(text); return

    # 2) Load odds once per league (for fixture names) and use per-fixture odds where available
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

            # Prefer fresh per-fixture odds; fallback to embedded league odds
            fid = int(fx.get("fixture_id") or fx.get("id") or 0)
            if fid:
                per = load_odds_for_fixture(fid)
                odds_rows = (per.get("odds") or []) if isinstance(per.get("odds"), list) else []
                if not odds_rows:
                    odds_rows = fx.get("odds") or []
            else:
                odds_rows = fx.get("odds") or []

            # Team moneyline (Bet365 only)
            home_ml, away_ml = extract_team_ml_prices(odds_rows, home, away)
            team_ml = home_ml if side == "home" else away_ml
            if team_ml is None or team_ml >= TEAM_ML_MAX:
                continue

            price = best_over05_player_shots(odds_rows, c["_rec"])
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
    lines.append("Market: Bet365 Player Shots Over 0.5 (market_id=268)")
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
