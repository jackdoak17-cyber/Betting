#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value bets — SHOTS certs (10/10, 9/10, 8/10, 7/7) — Sportmonks/Bet365
Robust detection of 'Over 0.5' and '1+' variants across feed shapes.

Outputs:
  data/value_bets/shots_certs.txt
  data/value_bets/_debug_shots_certs.txt  (why rows were dropped)

ENV (optional):
  MIN_DEC_PRICE  default "1.30"
  TEAM_ML_MAX    default "3.50"   (drop big underdogs)
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

# -------- Config --------
MIN_PRICE   = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_ML_MAX",  "3.50"))

LEAGUE_IDS = [301, 384, 387, 564, 567, 600, 8, 82, 9]

ROOT      = Path(".")
PX_DIR    = ROOT / "data" / "predicted_xi" / "by_league"
SHOTS_DIR = ROOT / "data" / "player_shots" / "by_league"
ODDS_DIR  = ROOT / "data" / "odds" / "b365"
OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE  = OUT_DIR / "shots_certs.txt"
DBG_FILE  = OUT_DIR / "_debug_shots_certs.txt"

MARKET_MATCH_WINNER     = 1
MARKET_PLAYER_SHOTS     = 268   # O/U lines (we want OVER 0.5)
MARKET_NUM_PLAYER_SHOTS = 160   # 1+, 2+, 3+ (we want 1+)

# -------- String helpers --------
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.+-]", " ", s)
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

def player_label_matches(player: str, option_text: str) -> bool:
    if not player or not option_text: return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(option_text))
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
    blob = _load_json(PX_DIR / f"{league_id}.json") or {}
    m: Dict[int, str] = {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home","away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid,int) and isinstance(nm,str) and nm:
                m.setdefault(tid, nm)
    return m

def parse_fixture_teams(fixture_name: str) -> Tuple[str, str]:
    if not fixture_name: return "",""
    for sep in [" vs ", " v ", " VS ", " Vs ", " - "]:
        if sep in fixture_name:
            a, b = fixture_name.split(sep, 1)
            return a.strip(), b.strip()
    return "", ""

def side_for_team(team_name: str, home: str, away: str) -> Optional[str]:
    if team_names_match(team_name, home): return "home"
    if team_names_match(team_name, away): return "away"
    return None

def as_float(x) -> Optional[float]:
    try: return float(str(x))
    except Exception: return None

# -------- ML parsing --------
def is_match_winner_row(row: dict) -> bool:
    if int(row.get("market_id", 0)) == MARKET_MATCH_WINNER:
        return True
    md = norm(row.get("market_description") or "")
    return md in {
        "match winner","match result","full time result","fulltime result","1x2",
        "result","win/draw/win","90 minutes","3-way","3 way","regular time result"
    }

def extract_team_ml_prices(odds_rows: List[dict], home_name: str, away_name: str) -> Tuple[Optional[float], Optional[float]]:
    home_price = None; away_price = None
    for row in odds_rows:
        if not is_match_winner_row(row):
            continue
        label = (row.get("label") or "").strip().lower()
        name  = (row.get("name")  or "").strip().lower()
        val   = as_float(row.get("value"))
        if val is None:
            continue
        if label in {"1","home"} or team_names_match(home_name, label) or team_names_match(home_name, name) or team_names_match(home_name, row.get("name","")):
            home_price = val if (home_price is None or val < home_price) else home_price
        elif label in {"2","away"} or team_names_match(away_name, label) or team_names_match(away_name, name) or team_names_match(away_name, row.get("name","")):
            away_price = val if (away_price is None or val < away_price) else away_price
    return home_price, away_price

# -------- Player Shots detection (robust) --------
NUM_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)")

def combined_text(row: dict) -> str:
    return " | ".join([
        str(row.get("name") or ""),
        str(row.get("original_label") or ""),
        str(row.get("total") or ""),
        str(row.get("label") or ""),
        str(row.get("market_description") or "")
    ])

def parse_line(row: dict) -> Optional[float]:
    if "handicap" in row and row["handicap"] not in (None, ""):
        v = as_float(row["handicap"])
        if v is not None:
            return v
    for k in ("label","total","original_label"):
        s = (row.get(k) or "")
        m = NUM_RE.search(s)
        if m:
            try: return float(m.group(1))
            except: pass
    return None

def is_player_shots_market(row: dict) -> bool:
    mid = int(row.get("market_id", 0))
    if mid in (MARKET_PLAYER_SHOTS, MARKET_NUM_PLAYER_SHOTS):
        return True
    md = norm(row.get("market_description") or "")
    return ("player shots" in md) and ("on target" not in md)

def is_over_selection_ou(row: dict) -> bool:
    joined = " ".join([(row.get("label") or ""), (row.get("total") or ""), (row.get("original_label") or "")]).lower()
    return ("over" in joined) and ("under" not in joined)

ONE_PLUS_PATTERNS = [
    r"\b1\+\b", r"\b1\+\s*shots?\b", r"\b1\s*\+\b", r"\b1\s*or\s*more\b",
    r"\bat\s*least\s*1\b", r"\bto\s*have\s*1\+\b"
]
ONE_PLUS_RE = re.compile("|".join(ONE_PLUS_PATTERNS), re.IGNORECASE)

def is_one_plus_selection(row: dict) -> bool:
    txt = combined_text(row).replace(" ", "")
    if ONE_PLUS_RE.search(combined_text(row)):
        return True
    line = parse_line(row)
    # In some feeds, handicap == 1 for "1+"
    return (line is not None and math.isclose(line, 1.0, abs_tol=1e-6))

def best_price_over05_or_1plus(odds_rows: List[dict], player: str, dbg: List[str], fx_name: str) -> Optional[float]:
    best = None
    for row in odds_rows:
        if not is_player_shots_market(row):
            continue

        # Player match
        opt_text = combined_text(row)
        if not player_label_matches(player, opt_text):
            continue

        price = as_float(row.get("value"))
        if price is None:
            dbg.append(f"[no_price] {player} @ {fx_name} :: row={opt_text}")
            continue

        mid = int(row.get("market_id", 0))
        if mid == MARKET_PLAYER_SHOTS:
            line = parse_line(row)
            if not is_over_selection_ou(row) or line is None or not math.isclose(line, 0.5, abs_tol=1e-6):
                dbg.append(f"[skip_268_not_over05] {player} @ {fx_name} :: row={opt_text}")
                continue
            if best is None or price > best + 1e-12:
                best = price
            continue

        if mid == MARKET_NUM_PLAYER_SHOTS:
            if not is_one_plus_selection(row):
                dbg.append(f"[skip_160_not_1plus] {player} @ {fx_name} :: row={opt_text}")
                continue
            if best is None or price > best + 1e-12:
                best = price
            continue

        # In case IDs differ but description matches:
        md = norm(row.get("market_description") or "")
        if "player shots" in md and "on target" not in md:
            # Try both patterns:
            line = parse_line(row)
            over05_ok = ("over" in opt_text.lower()) and line is not None and math.isclose(line, 0.5, abs_tol=1e-6)
            oneplus_ok = is_one_plus_selection(row)
            if not (over05_ok or oneplus_ok):
                dbg.append(f"[skip_desc_match_not_line] {player} @ {fx_name} :: row={opt_text}")
                continue
            if best is None or price > best + 1e-12:
                best = price
    return best

# -------- Buckets --------
def series_counts(series_raw: List[int]) -> Tuple[int, Optional[int], int]:
    seq = [x for x in (series_raw or []) if isinstance(x, int)]
    n = len(seq)
    last7  = seq[:7]  if n >= 7  else []
    last10 = seq[:10] if n >= 10 else []
    shots7  = sum(1 for x in last7  if x >= 1) if last7  else 0
    shots10 = sum(1 for x in last10 if x >= 1) if last10 else None
    return shots7, shots10, n

def bucket_for_series(series_raw: List[int]) -> Optional[str]:
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
            pos = rec.get("position") or rec.get("pos") or rec.get("position_tag")
            out.append({
                "league_id": lid, "player": player, "team": team,
                "position": (pos or ""), "series": series[:12], "bucket": bucket
            })
    return out

# -------- Main --------
def main():
    dbg_lines: List[str] = []

    candidates = collect_candidates()
    odds_by_league: Dict[int, dict] = {lid: (_load_json(ODDS_DIR / f"{lid}.json") or {}) for lid in LEAGUE_IDS}

    buckets = {"10/10": [], "9/10": [], "8/10": [], "7/7": []}
    kept_after_ml = 0
    kept_after_price = 0

    for c in candidates:
        lid, team, plyr, bucket = c["league_id"], c["team"], c["player"], c["bucket"]
        blob = odds_by_league.get(lid) or {}
        fixtures = blob.get("fixtures") or []
        matched_fixture = False

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
                dbg_lines.append(f"[drop_ml] {plyr} ({team}) @ {fname} ML={team_ml}")
                continue
            kept_after_ml += 1

            price = best_price_over05_or_1plus(odds_rows, plyr, dbg_lines, fname)
            if price is None:
                dbg_lines.append(f"[drop_no_price_match] {plyr} ({team}) @ {fname}")
                continue
            if price < MIN_PRICE:
                dbg_lines.append(f"[drop_price_lt_min] {plyr} ({team}) @ {fname} price={price:.3f} < {MIN_PRICE:.2f}")
                continue
            kept_after_price += 1

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
            matched_fixture = True
            break  # only need one matching fixture

        if not matched_fixture:
            dbg_lines.append(f"[no_fixture_match] {plyr} ({team}) — no matching fixture in odds blob for L{lid}")

    # Render
    ts = dt.datetime.utcnow().isoformat()
    header = [
        f"Generated at (UTC): {ts}  |  Min price >= {MIN_PRICE:.2f}  |  Team ML < {TEAM_ML_MAX:.2f}",
        "Buckets: 10/10, 9/10, 8/10 (last-10); 7/7 (last-7 when <10 games available)",
        "Markets: Bet365 Player Shots Over 0.5 (id=268, Over only) + Number of Player Shots 1+ (id=160)",
        "",
        f"Pipeline: candidates={len(candidates)} | kept_after_ML={kept_after_ml} | kept_after_price={kept_after_price}",
        ""
    ]

    def render_bucket(tag: str, rows: List[dict]) -> List[str]:
        rows.sort(key=lambda x: (-x["price"], x["player"]))
        out = [f"===== {tag} =====  (count: {len(rows)})"]
        if not rows:
            out += ["  — none —", ""]
            return out
        for x in rows:
            ser = ",".join(map(str, x["series"][:10]))
            pos = f"[{x['position']}]" if x.get("position") else ""
            out.append(
                f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | "
                f"Over 0.5 @ {x['price']:.3f} | Team ML {x['team_ml']:.3f} | series: {ser}"
            )
        out.append("")
        return out

    lines = header[:]
    for tag in ("10/10","9/10","8/10","7/7"):
        lines += render_bucket(tag, buckets[tag])

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    DBG_FILE.write_text("\n".join(dbg_lines).rstrip() + "\n", encoding="utf-8")

    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
