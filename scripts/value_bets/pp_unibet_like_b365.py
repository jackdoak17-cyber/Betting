#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PP/Unibet value finders — like your Bet365 scripts, but reading Paddy Power + Unibet odds.

Modes (choose one with --mode or MODE env):
  • shots_certs  -> replicate shots_certs (O0.5 buckets: 10/10, 9/10, 8/10, 7/7)
  • singles_05   -> replicate value_singles_shots_sportmonks (O0.5, tiers 5/5 or 7/10)
  • singles_15   -> replicate value_singles_shots2_sportmonks (O1.5, tiers 5/5 or 7/10)
  • singles_sot  -> replicate value_singles_sot (SOT O0.5, tiers 5/5 or 7/10, windowed)

Reads local data you already generate:
  • Player shots histories:          data/player_shots/by_league/{league_id}.json
  • Player SOT histories:            data/player_shots_on_target/by_league/{league_id}.json
  • Predicted XI (optional mapping): data/predicted_xi/by_league/{league_id}.json
  • Fixtures (windowing for SOT):    data/fixtures/{league_id}.json

Odds (new sources):
  • PADDY POWER league odds:         data/odds/paddypower/{league_id}.json
  • PADDY fixture odds:              data/odds/paddypower/fixtures/{fixture_id}.json
  • UNIBET league odds:              data/odds/unibet/{league_id}.json
  • UNIBET fixture odds:             data/odds/unibet/fixtures/{fixture_id}.json

Outputs (combined with headers per bookmaker):
  • shots_certs       -> data/value_bets/shots_certs_pp_unibet.txt
  • singles_05        -> data/value_bets/value_singles_pp_unibet.txt
  • singles_15        -> data/value_bets/value_singles_2plus_pp_unibet.txt
  • singles_sot       -> data/value_bets/value_singles_sot_pp_unibet.txt
"""

import os, re, json, math, argparse, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Iterable

# ------------------ Config / Paths ------------------
ROOT = Path(".")
OUT_DIR = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)

# Where we read odds for each bookmaker
BOOKS = {
    "paddypower": {
        "label": "PADDY POWER",
        "base": ROOT / "data" / "odds" / "paddypower",
        "fixtures": ROOT / "data" / "odds" / "paddypower" / "fixtures",
    },
    "unibet": {
        "label": "UNIBET",
        "base": ROOT / "data" / "odds" / "unibet",
        "fixtures": ROOT / "data" / "odds" / "unibet" / "fixtures",
    },
}

# Other local data
PX_DIR    = ROOT / "data" / "predicted_xi" / "by_league"                 # team_id -> name map (optional)
SHOTS_DIR = ROOT / "data" / "player_shots" / "by_league"                 # per-league player shots histories
SOT_DIR   = ROOT / "data" / "player_shots_on_target" / "by_league"       # per-league player SOT histories
FIX_DIR   = ROOT / "data" / "fixtures"                                   # for SOT windowing + team mapping

DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS = [
    int(x) for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",")
    if x.strip()
]

# Thresholds (default align with your Bet365 scripts)
MIN_PRICE_05      = float(os.getenv("MIN_DEC_PRICE_05", os.getenv("MIN_DEC_PRICE", "1.72")))
MIN_PRICE_CERTS   = float(os.getenv("MIN_DEC_PRICE_CERTS", "1.30"))     # shots certs
MIN_PRICE_15      = float(os.getenv("MIN_DEC_PRICE_15", "1.72"))        # singles_15
TEAM_ML_CAP_CERTS = float(os.getenv("TEAM_WIN_MAX", "3.50"))            # certs cap
TEAM_ML_CAP_05    = float(os.getenv("TEAM_UNDERDOG_MAX", "3.50"))       # singles_05 cap
TEAM_ML_CAP_15    = float(os.getenv("UNDERDOG_MAX", "3.50"))            # singles_15 cap

# SOT-only extras
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "7"))
TEAM_ML_CAP_SOT = float(os.getenv("TEAM_UNDERDOG_MAX_SOT", os.getenv("TEAM_UNDERDOG_MAX", "3.50")))
MIN_PRICE_SOT   = float(os.getenv("MIN_DEC_PRICE_SOT", os.getenv("MIN_DEC_PRICE", "1.72")))
DEBUG_DROPS     = bool(int(os.getenv("DEBUG_DROPS", "0")))
NEAR_MISS_LIMIT = int(os.getenv("NEAR_MISS_LIMIT", "12"))

# Market ids (Sportmonks)
MARKET_MATCH_WINNER = 1
MARKET_PLAYER_SHOTS = 268

# ------------------ IO helpers ------------------
def load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def as_float(x) -> Optional[float]:
    try:
        return float(str(x))
    except Exception:
        return None

# ------------------ Name helpers ------------------
SUFFIXES = {"jr","junior","sr","senior","ii","iii","iv","filho","neto"}
SURNAME_PREFIXES = {"da","de","del","der","di","dos","du","la","le","van","von","bin","al"}
GENERIC_TOK = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")

def norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^\w\s\.-]", " ", s)
    return norm_spaces(s)

def cleanup_label(label: str) -> str:
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

def person_part_from_option(label: str) -> str:
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
    if not parts: return []
    if len(parts) >= 2 and parts[-2] in SURNAME_PREFIXES:
        return parts[-2:]
    return parts[-1:]

def first_initial(parts: List[str]) -> Optional[str]:
    parts = drop_suffixes(parts)
    for p in parts[:-1]:
        ch = p[:1]
        if ch: return ch
    return None

def name_variants(full_name: str) -> List[str]:
    if not full_name: return []
    parts = split_name_tokens(full_name)
    if not parts: return []
    sur = " ".join(surname_tokens(parts))
    init = first_initial(parts)
    full = " ".join(parts)
    out = {full, sur}
    if init:
        out.add(f"{init}. {sur}")
        out.add(f"{init} {sur}")
    out.add(f"{sur} {init or ''}".strip())
    return sorted({norm(o).replace(".", "") for o in out if o})

def aliases_from_record(rec: dict) -> List[str]:
    names: List[str] = []
    for k in ("name","player_name","player","short_name","common_name","display_name","full_name","known_as"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            names.append(v.strip())
    seen, uniq = set(), []
    for n in names:
        key = norm(n)
        if key not in seen:
            seen.add(key); uniq.append(n)
    out: List[str] = []
    for n in uniq:
        out.extend(name_variants(n))
    seen2, uniq2 = set(), []
    for a in out:
        if a not in seen2:
            seen2.add(a); uniq2.append(a)
    return uniq2

def label_matches_aliases(option_label: str, aliases: Iterable[str]) -> bool:
    lab = norm(person_part_from_option(option_label)).replace(".", "")
    if not lab: return False
    lab_tokens = set(lab.split())
    for alias in aliases:
        atoks = set(alias.split())
        if alias == lab or (atoks and (atoks.issubset(lab_tokens) or lab_tokens.issubset(atoks))):
            return True
        # surname presence + optional initial rule
        a_parts = alias.split()
        a_sur = a_parts[-2:] if len(a_parts) >= 2 and a_parts[-2] in SURNAME_PREFIXES else a_parts[-1:]
        if set(a_sur).issubset(lab_tokens):
            if len(a_parts) >= 2 and len(a_parts[0]) == 1:
                if a_parts[0] in lab_tokens or lab.startswith(a_parts[0] + " "):  # initial
                    return True
                continue
            return True
    return False

# ------------------ Team / fixture name helpers ------------------
def team_tokens(name: str):
    return {t for t in norm(name).split() if t not in GENERIC_TOK}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; uni = ta | tb
    return (len(inter) / max(1, len(uni)) >= 0.5) or (len(inter) >= 2)

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

def _team_name_map(league_id: int) -> Dict[int, str]:
    blob = load_json(PX_DIR / f"{league_id}.json") or {}
    m: Dict[int, str] = {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

# ------------------ Market classifiers ------------------
NEGATIVE_SHOTS_TERMS = {"outside", "from outside", "outside the box", "first half", "second half",
                        "1st half", "2nd half", "header", "headers", "distance"}
NEGATIVE_SOT_TERMS = NEGATIVE_SHOTS_TERMS

def market_is_player_shots(desc: str) -> bool:
    d = (desc or "").lower()
    if not d: return False
    if "player" not in d or "shot" not in d: return False
    if any(t in d for t in NEGATIVE_SHOTS_TERMS): return False
    return True

def market_is_player_sot(desc: str) -> bool:
    d = (desc or "").lower()
    if not d: return False
    if "player" not in d or "on target" not in d: return False
    if any(t in d for t in NEGATIVE_SOT_TERMS): return False
    return True

MATCH_WINNER_KEYS = ["match winner", "match result", "1x2", "full time result", "win/draw/win", "to win", "90 minutes", "result"]
def market_is_match_winner(desc: str) -> bool:
    s = (desc or "").lower()
    return any(k in s for k in MATCH_WINNER_KEYS)

# ------------------ Odds scanners (per bookmaker) ------------------
def league_blob(book_base: Path, league_id: int) -> dict:
    return load_json(book_base / f"{league_id}.json") or {}

def per_fixture_rows(book_fix_dir: Path, fixture_id: int) -> List[dict]:
    blob = load_json(book_fix_dir / f"{fixture_id}.json") or {}
    rows = blob.get("odds") or []
    return rows if isinstance(rows, list) else []

def extract_team_ml_prices(rows: List[dict], home_name: str, away_name: str) -> Tuple[Optional[float], Optional[float]]:
    home_price = None; away_price = None
    for row in rows:
        if int(row.get("market_id", 0)) != MARKET_MATCH_WINNER:
            continue
        desc = row.get("market_description") or row.get("market_name") or ""
        if not market_is_match_winner(desc):
            continue
        label = (row.get("label") or "").strip().lower()
        name  = (row.get("name")  or "").strip().lower()
        val   = as_float(row.get("value"))
        if val is None: continue
        if label in {"1","home"} or team_names_match(home_name, label) or team_names_match(home_name, name):
            home_price = val if (home_price is None or val < home_price) else home_price
        elif label in {"2","away"} or team_names_match(away_name, label) or team_names_match(away_name, name):
            away_price = val if (away_price is None or val < away_price) else away_price
        elif team_names_match(home_name, row.get("name","")):
            home_price = val if (home_price is None or val < home_price) else home_price
        elif team_names_match(away_name, row.get("name","")):
            away_price = val if (away_price is None or val < away_price) else away_price
    return home_price, away_price

def _row_text(row: dict) -> str:
    fields = ["label","name","original_label","market_description","outcome","outcome_name","header","description"]
    return " ".join([str(row.get(f, "")) for f in fields]).lower()

def line_is(row: dict, target: float) -> bool:
    t = as_float(row.get("total"))
    if t is not None and math.isclose(t, target, abs_tol=1e-6):
        return True
    l = as_float(row.get("label"))
    if l is not None and math.isclose(l, target, abs_tol=1e-6):
        return True
    blob = _row_text(row).replace(",", ".")
    needle = f"{target:.1f}"
    return (needle in blob) or (needle + "+" in blob) or ("+" + needle in blob)

def is_over_row(row: dict, target: float) -> Optional[bool]:
    txt = _row_text(row)
    if re.search(r"\bunder\b", txt): return False
    if re.search(r"\bover\b", txt):  return True
    if f"+{target:.1f}" in txt or f"{target:.1f}+" in txt or f"{str(target).replace('.',',')}+" in txt:
        return True
    return None

def row_matches_player(row: dict, aliases: Iterable[str]) -> bool:
    for cand in (row.get("name",""), row.get("original_label",""), row.get("label",""),
                 row.get("outcome_name",""), row.get("header",""), row.get("description","")):
        if cand and label_matches_aliases(str(cand), aliases):
            return True
    return False

def best_player_over_price(rows: List[dict], is_sot: bool, target: float, player_rec: dict) -> Optional[float]:
    aliases = aliases_from_record(player_rec)
    if not aliases: return None
    cands: List[Tuple[Optional[bool], float]] = []
    for r in rows:
        mid = int(r.get("market_id") or 0)
        desc = r.get("market_description") or r.get("market_name") or ""
        if is_sot:
            if not market_is_player_sot(desc): continue
        else:
            if mid != MARKET_PLAYER_SHOTS or not market_is_player_shots(desc): continue
        if not line_is(r, target): continue
        if not row_matches_player(r, aliases): continue
        price = as_float(r.get("value"))
        if price is None: continue
        cands.append((is_over_row(r, target), price))
    if not cands: return None
    explicit = [p for f, p in cands if f is True]
    if explicit: return min(explicit)
    ambig = [p for f, p in cands if f is None]
    if ambig: return min(ambig)
    return None

# ------------------ Shots histories helpers ------------------
def series_counts_1plus(series_raw: List[int]) -> Tuple[int, Optional[int], int]:
    seq = [x for x in series_raw if isinstance(x, int)]
    n = len(seq)
    s7  = sum(1 for x in seq[:7]  if x >= 1) if n >= 7  else 0
    s10 = sum(1 for x in seq[:10] if x >= 1) if n >= 10 else None
    return s7, s10, n

def bucket_shots_certs(series_raw: List[int]) -> Optional[str]:
    s7, s10, n = series_counts_1plus(series_raw)
    if s10 is not None:
        if s10 == 10: return "10/10"
        if s10 == 9:  return "9/10"
        if s10 == 8:  return "8/10"
        return None
    if n >= 7 and s7 == 7: return "7/7"
    return None

def qualifies_05(series: List[int]) -> Optional[str]:
    # for O0.5 singles: 5/5 or 7/10
    seq = [x for x in (series or []) if isinstance(x, int)]
    if len(seq) >= 5 and all(x >= 1 for x in seq[:5]): return "5/5"
    if len(seq) >= 10 and sum(1 for x in seq[:10] if x >= 1) >= 7: return "7/10"
    return None

def qualifies_15(series: List[int]) -> Optional[str]:
    # for O1.5 singles: 5/5 (>=2) or 7/10 (>=2 in at least 7)
    seq = [x for x in (series or []) if isinstance(x, int)]
    if len(seq) >= 5 and all(x >= 2 for x in seq[:5]): return "5/5"
    if len(seq) >= 10 and sum(1 for x in seq[:10] if x >= 2) >= 7: return "7/10"
    return None

def qualifies_sot(series: List[int]) -> Optional[str]:
    h = [1 if (isinstance(x, (int, float)) and x >= 1) else 0 for x in (series or [])]
    if len(h) >= 5 and sum(h[:5]) >= 5: return "5/5"
    if len(h) >= 10 and sum(h[:10]) >= 7: return "7/10"
    return None

# ------------------ Fixtures helpers (SOT window) ------------------
def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def parse_utc(s: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None

def discover_leagues_from_fixtures() -> List[int]:
    out = []
    for p in sorted(FIX_DIR.glob("*.json")):
        try: out.append(int(p.stem))
        except: pass
    return out

def upcoming_fixtures_for_league(lid: int, window_days: int) -> List[dict]:
    blob = load_json(FIX_DIR / f"{lid}.json") or {}
    fixtures = blob.get("fixtures") or []
    if not window_days: return fixtures
    now = utc_now(); end = now + dt.timedelta(days=window_days)
    kept = []
    for fx in fixtures:
        t = parse_utc(fx.get("starting_at") or "")
        if t and now <= t <= end: kept.append(fx)
    return kept

def team_maps_from_fixtures(fixtures: List[dict]) -> Tuple[Dict[int, str], Dict[str, dict]]:
    team_id_to_name: Dict[int, str] = {}
    team_name_to_next_fixture: Dict[str, dict] = {}
    for fx in fixtures:
        fid = int(fx.get("id") or 0)
        t = parse_utc(fx.get("starting_at") or "")
        parts = fx.get("participants") or []
        home_name = away_name = None
        for p in parts:
            nm = p.get("name"); pid = p.get("id")
            loc = ((p.get("meta") or {}).get("location") or "").lower()
            if isinstance(pid, int) and isinstance(nm, str):
                team_id_to_name[pid] = nm
            if loc == "home": home_name = nm
            elif loc == "away": away_name = nm
        if not (home_name and away_name and t): continue
        for nm, side, opp in ((home_name, "home", away_name), (away_name, "away", home_name)):
            key = (nm or "").lower()
            prev = team_name_to_next_fixture.get(key)
            if (not prev) or (t < prev["kickoff_dt"]):
                team_name_to_next_fixture[key] = {
                    "fixture_id": fid, "side": side, "opp_name": opp,
                    "kickoff_dt": t, "home_name": home_name, "away_name": away_name,
                }
    return team_id_to_name, team_name_to_next_fixture

# ------------------ Core runners per mode & bookmaker ------------------
def run_shots_certs_for_book(book_key: str) -> List[str]:
    """Replicate shots_certs on O0.5 for one bookmaker."""
    base = BOOKS[book_key]["base"]
    lines: List[str] = []
    buckets = {"10/10": [], "9/10": [], "8/10": [], "7/7": []}
    for lid in LEAGUE_IDS:
        shots_blob = load_json(SHOTS_DIR / f"{lid}.json") or {}
        team_map = _team_name_map(lid)
        league = league_blob(base, lid)
        fixtures = league.get("fixtures") or []
        for rec in (shots_blob.get("players") or shots_blob.get("rows") or shots_blob.get("data") or []):
            series = rec.get("series") or rec.get("shots_last_n") or rec.get("shots") or []
            bucket = bucket_shots_certs(series)
            if not bucket: continue
            player = rec.get("name") or rec.get("player_name") or rec.get("player")
            if not player: continue
            tid = rec.get("team_id")
            team = rec.get("team") or rec.get("team_name") or (team_map.get(int(tid)) if isinstance(tid, int) else None)
            if not team: continue
            rec_out = dict(rec); rec_out.update({"name": player})
            for fx in fixtures:
                fname = fx.get("name") or ""
                home, away = parse_fixture_teams(fname)
                if not home or not away: continue
                side = "home" if team_names_match(team, home) else ("away" if team_names_match(team, away) else None)
                if not side: continue
                fid = int(fx.get("fixture_id") or fx.get("id") or 0)
                odds_rows = (load_json(base / "fixtures" / f"{fid}.json") or {}).get("odds") or fx.get("odds") or []
                odds_rows = odds_rows if isinstance(odds_rows, list) else []
                # team ml gate
                hml, aml = extract_team_ml_prices(odds_rows, home, away)
                team_ml = hml if side == "home" else aml
                if team_ml is None or team_ml >= TEAM_ML_CAP_CERTS: continue
                price = best_player_over_price(odds_rows, is_sot=False, target=0.5, player_rec=rec_out)
                if price is None or price < MIN_PRICE_CERTS: continue
                buckets[bucket].append({
                    "player": player, "team": team, "fixture": fname, "kickoff": fx.get("starting_at") or "",
                    "price": price, "team_ml": team_ml, "series": series[:10], "pos": rec.get("position") or rec.get("position_tag") or ""
                })
                break
    # render
    lines.append(f"===== {BOOKS[book_key]['label']} =====")
    lines.append(f"(Over 0.5 shots ≥ {MIN_PRICE_CERTS:.2f}; Team ML < {TEAM_ML_CAP_CERTS:.2f})")
    order = ("10/10","9/10","8/10","7/7")
    for tag in order:
        rows = buckets[tag]
        rows.sort(key=lambda x: (-x["price"], x["player"]))
        lines.append(f"-- {tag} (count: {len(rows)}) --")
        if not rows:
            lines.append("  — none —")
        else:
            for x in rows:
                ser = ",".join(map(str, x["series"]))
                pos = f"[{x['pos']}]" if x.get("pos") else ""
                lines.append(f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | O0.5 @ {x['price']:.3f} | ML {x['team_ml']:.3f} | series: {ser}")
        lines.append("")
    return lines

def run_singles_05_for_book(book_key: str) -> List[str]:
    base = BOOKS[book_key]["base"]
    lines: List[str] = [f"===== {BOOKS[book_key]['label']} =====",
                        f"(Over 0.5 ≥ {MIN_PRICE_05:.2f}; Team ML ≤ {TEAM_ML_CAP_05:.2f}; tiers: 5/5 or 7/10)"]
    groups = {"5/5": [], "7/10": []}
    for lid in LEAGUE_IDS:
        shots_blob = load_json(SHOTS_DIR / f"{lid}.json") or {}
        team_map = _team_name_map(lid)
        league = league_blob(base, lid)
        fixtures = league.get("fixtures") or []
        for rec in (shots_blob.get("players") or shots_blob.get("rows") or shots_blob.get("data") or []):
            series = rec.get("shots_last_n") or rec.get("series") or rec.get("shots") or []
            tag = qualifies_05(series)
            if not tag: continue
            player = rec.get("name") or rec.get("player_name") or rec.get("player")
            if not player: continue
            tid = rec.get("team_id")
            team = rec.get("team") or rec.get("team_name") or (team_map.get(int(tid)) if isinstance(tid, int) else None)
            if not team: continue
            rec_out = dict(rec); rec_out.update({"name": player})
            for fx in fixtures:
                fname = fx.get("name") or ""
                home, away = parse_fixture_teams(fname)
                if not home or not away: continue
                side = "home" if team_names_match(team, home) else ("away" if team_names_match(team, away) else None)
                if not side: continue
                fid = int(fx.get("fixture_id") or fx.get("id") or 0)
                odds_rows = (load_json(base / "fixtures" / f"{fid}.json") or {}).get("odds") or fx.get("odds") or []
                odds_rows = odds_rows if isinstance(odds_rows, list) else []
                hml, aml = extract_team_ml_prices(odds_rows, home, away)
                team_ml = hml if side == "home" else aml
                if team_ml is None or team_ml > TEAM_ML_CAP_05: continue
                price = best_player_over_price(odds_rows, is_sot=False, target=0.5, player_rec=rec_out)
                if price is None or price < MIN_PRICE_05: continue
                groups[tag].append({
                    "player": player, "team": team, "fixture": fname, "kickoff": fx.get("starting_at") or "",
                    "price": price, "team_ml": team_ml, "series": series[:10], "pos": rec.get("position_tag") or rec.get("position") or ""
                })
                break
    for tag in ("5/5","7/10"):
        rows = groups[tag]
        rows.sort(key=lambda x: (-x["price"], x["player"]))
        lines.append(f"-- {tag} (count: {len(rows)}) --")
        if not rows:
            lines.append("  — none —")
        else:
            for x in rows:
                ser = ",".join(map(str, x["series"]))
                pos = f"[{x['pos']}]" if x.get("pos") else ""
                lines.append(f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | O0.5 @ {x['price']:.3f} | ML {x['team_ml']:.3f} | series: {ser}")
        lines.append("")
    return lines

def run_singles_15_for_book(book_key: str) -> List[str]:
    base = BOOKS[book_key]["base"]
    lines: List[str] = [f"===== {BOOKS[book_key]['label']} =====",
                        f"(Over 1.5 > {MIN_PRICE_15:.2f}; Team ML < {TEAM_ML_CAP_15:.2f}; tiers: 5/5 or 7/10)"]
    groups = {"5/5": [], "7/10": []}
    for lid in LEAGUE_IDS:
        shots_blob = load_json(SHOTS_DIR / f"{lid}.json") or {}
        team_map = _team_name_map(lid)
        league = league_blob(base, lid)
        fixtures = league.get("fixtures") or []
        for rec in (shots_blob.get("players") or shots_blob.get("rows") or shots_blob.get("data") or []):
            series = rec.get("shots_last_n") or rec.get("series") or rec.get("shots") or []
            tag = qualifies_15(series)
            if not tag: continue
            player = rec.get("name") or rec.get("player_name") or rec.get("player")
            if not player: continue
            tid = rec.get("team_id")
            team = rec.get("team") or rec.get("team_name") or (team_map.get(int(tid)) if isinstance(tid, int) else None)
            if not team: continue
            rec_out = dict(rec); rec_out.update({"name": player})
            for fx in fixtures:
                fname = fx.get("name") or ""
                home, away = parse_fixture_teams(fname)
                if not home or not away: continue
                side = "home" if team_names_match(team, home) else ("away" if team_names_match(team, away) else None)
                if not side: continue
                fid = int(fx.get("fixture_id") or fx.get("id") or 0)
                odds_rows = (load_json(base / "fixtures" / f"{fid}.json") or {}).get("odds") or fx.get("odds") or []
                odds_rows = odds_rows if isinstance(odds_rows, list) else []
                hml, aml = extract_team_ml_prices(odds_rows, home, away)
                team_ml = hml if side == "home" else aml
                if team_ml is None or not (team_ml < TEAM_ML_CAP_15): continue
                price = best_player_over_price(odds_rows, is_sot=False, target=1.5, player_rec=rec_out)
                if price is None or not (price > MIN_PRICE_15): continue
                groups[tag].append({
                    "player": player, "team": team, "fixture": fname, "kickoff": fx.get("starting_at") or "",
                    "price": price, "team_ml": team_ml, "series": series[:10], "pos": rec.get("position_tag") or rec.get("position") or ""
                })
                break
    for tag in ("5/5","7/10"):
        rows = groups[tag]
        rows.sort(key=lambda x: (-x["price"], x["player"]))
        lines.append(f"-- {tag} (count: {len(rows)}) --")
        if not rows:
            lines.append("  — none —")
        else:
            for x in rows:
                ser = ",".join(map(str, x["series"]))
                pos = f"[{x['pos']}]" if x.get("pos") else ""
                lines.append(f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | O1.5 @ {x['price']:.3f} | ML {x['team_ml']:.3f} | series: {ser}")
        lines.append("")
    return lines

def run_singles_sot_for_book(book_key: str) -> List[str]:
    """SOT O0.5 with windowing; uses bookmaker's fixture odds dir."""
    base = BOOKS[book_key]["base"]; fixdir = BOOKS[book_key]["fixtures"]
    lines: List[str] = [f"===== {BOOKS[book_key]['label']} =====",
                        f"(SOT O0.5 ≥ {MIN_PRICE_SOT:.2f}; Team ML ≤ {TEAM_ML_CAP_SOT:.2f}; window={WINDOW_DAYS}d; tiers 5/5 or 7/10)"]
    kept: List[dict] = []
    leagues = sorted(set(LEAGUE_IDS or discover_leagues_from_fixtures()))
    for lid in leagues:
        fx_list = upcoming_fixtures_for_league(lid, WINDOW_DAYS)
        if not fx_list: continue
        team_id_to_name, team_next = team_maps_from_fixtures(fx_list)
        sot_blob = load_json(SOT_DIR / f"{lid}.json") or {}
        for rec in (sot_blob.get("players") or []):
            tid = rec.get("team_id")
            team_name = team_id_to_name.get(int(tid)) if isinstance(tid, int) else None
            if not team_name: continue
            nxt = team_next.get((team_name or "").lower())
            if not nxt: continue
            fid = int(nxt["fixture_id"])
            home_name, away_name = nxt["home_name"], nxt["away_name"]
            # team ML from bookmaker's fixture odds
            rows = per_fixture_rows(fixdir, fid)
            hml, aml = extract_team_ml_prices(rows, home_name, away_name)
            side = nxt["side"]
            team_ml = hml if side == "home" else aml
            if team_ml is None or team_ml > TEAM_ML_CAP_SOT: 
                if DEBUG_DROPS:
                    print(f"[{book_key} drop ML] {team_name} ML={team_ml}")
                continue
            tier = qualifies_sot(rec.get("on_target_last_n") or [])
            if not tier: 
                continue
            # price lookup (SOT)
            rec_out = dict(rec); rec_out.update({"name": rec.get("name") or ""})
            price = best_player_over_price(rows, is_sot=True, target=0.5, player_rec=rec_out)
            if price is None or price < MIN_PRICE_SOT: 
                continue
            kept.append({
                "player": rec.get("name") or "", "team": team_name,
                "fixture": f"{home_name} vs {away_name}", "kickoff": nxt["kickoff_dt"].strftime("%Y-%m-%d %H:%M:%S"),
                "price": price, "team_ml": team_ml, "tier": tier,
                "pos": rec.get("position_tag") or rec.get("position") or "",
                "series5": (rec.get("on_target_last_n") or [])[:5]
            })
    if not kept:
        lines.append("  — none —")
        lines.append("")
        return lines
    # rank: tier then price
    tier_rank = {"5/5": 2, "7/10": 1}
    kept.sort(key=lambda x: (tier_rank.get(x["tier"],0), x["price"]), reverse=True)
    for x in kept:
        ser5 = ",".join(map(str, x["series5"]))
        pos = f"[{x['pos']}]" if x["pos"] else ""
        lines.append(f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | O0.5 SOT @ {x['price']:.3f} | ML {x['team_ml']:.3f} | tier {x['tier']} | last5:{ser5}")
    lines.append("")
    return lines

# ------------------ Driver ------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["shots_certs","singles_05","singles_15","singles_sot"],
                    default=os.getenv("MODE","shots_certs"))
    args = ap.parse_args()
    ts = dt.datetime.utcnow().isoformat()

    if args.mode == "shots_certs":
        out_path = OUT_DIR / "shots_certs_pp_unibet.txt"
        header = [f"Generated at (UTC): {ts}",
                  f"Buckets: 10/10, 9/10, 8/10 (last-10); 7/7 (last-7 when <10 games available)",
                  f"Market: Player Shots Over 0.5 | Price ≥ {MIN_PRICE_CERTS:.2f} | Team ML < {TEAM_ML_CAP_CERTS:.2f}",
                  ""]
        body = header + run_shots_certs_for_book("paddypower") + [""] + run_shots_certs_for_book("unibet")
        out_path.write_text("\n".join(body).rstrip()+"\n", encoding="utf-8")
        print("\n".join(body)); return

    if args.mode == "singles_05":
        out_path = OUT_DIR / "value_singles_pp_unibet.txt"
        header = [f"Generated at (UTC): {ts}",
                  f"Criteria: 5/5 OR 7/10 | O0.5 ≥ {MIN_PRICE_05:.2f} | Team ML ≤ {TEAM_ML_CAP_05:.2f}",
                  "Market: Player Shots Over 0.5", ""]
        body = header + run_singles_05_for_book("paddypower") + [""] + run_singles_05_for_book("unibet")
        out_path.write_text("\n".join(body).rstrip()+"\n", encoding="utf-8")
        print("\n".join(body)); return

    if args.mode == "singles_15":
        out_path = OUT_DIR / "value_singles_2plus_pp_unibet.txt"
        header = [f"Generated at (UTC): {ts}",
                  f"Criteria: 5/5 OR 7/10 | O1.5 > {MIN_PRICE_15:.2f} | Team ML < {TEAM_ML_CAP_15:.2f}",
                  "Market: Player Shots Over 1.5", ""]
        body = header + run_singles_15_for_book("paddypower") + [""] + run_singles_15_for_book("unibet")
        out_path.write_text("\n".join(body).rstrip()+"\n", encoding="utf-8")
        print("\n".join(body)); return

    if args.mode == "singles_sot":
        out_path = OUT_DIR / "value_singles_sot_pp_unibet.txt"
        header = [f"Generated at (UTC): {ts}",
                  f"Criteria: 5/5 OR 7/10 | SOT O0.5 ≥ {MIN_PRICE_SOT:.2f} | Team ML ≤ {TEAM_ML_CAP_SOT:.2f} | Window={WINDOW_DAYS} days",
                  "Market: Player Shots on Target Over 0.5", ""]
        body = header + run_singles_sot_for_book("paddypower") + [""] + run_singles_sot_for_book("unibet")
        out_path.write_text("\n".join(body).rstrip()+"\n", encoding="utf-8")
        print("\n".join(body)); return

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
