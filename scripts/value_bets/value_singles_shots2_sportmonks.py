#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value Singles — Shots 2+ (Over 1.5) — Sportmonks/Bet365

Changes:
  • Removed the 4/5 tier — only 5/5 and 7/10 are considered.
  • Single gating: keep the pick if Team Match Winner (Bet365) < UNDERDOG_MAX (default 3.50).
  • Robust player/market parsing (aliases + Over/Under handling).

Criteria:
  • Form path A: 5/5  (all of last 5 with ≥2 shots)
  • Form path B: 7/10 (≥7 of last 10 with ≥2 shots)
  • Odds filter: Bet365 Over 1.5 shots (market_id=268, line=1.5) with price > MIN_DEC_PRICE (default 1.72)
  • Not a big underdog: Team ML < UNDERDOG_MAX (default 3.50)

Inputs (local):
  • data/player_shots/by_league/{league_id}.json
  • data/predicted_xi/by_league/{league_id}.json  (optional team_id -> name map)
  • data/odds/b365/{league_id}.json               (fixtures[].odds[]; market_id=1,268)

Output:
  • data/value_bets/value_singles_2plus.txt

ENV (optional):
  • MIN_DEC_PRICE   (default "1.72")  # price must be strictly greater than this
  • UNDERDOG_MAX    (default "3.50")  # keep only if team ML < this
  • LEAGUE_IDS      (default "301,384,387,564,567,600,8,82,9")
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Iterable

# -------- Config --------
MIN_PRICE     = float(os.getenv("MIN_DEC_PRICE", "1.72"))  # price must be strictly greater than this
UNDERDOG_MAX  = float(os.getenv("UNDERDOG_MAX", "3.50"))   # keep only if team ML < this

DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS = [
    int(x)
    for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",")
    if x.strip()
]

ROOT     = Path(".")
PX_DIR   = ROOT / "data" / "predicted_xi" / "by_league"
PS_DIR   = ROOT / "data" / "player_shots" / "by_league"
ODDS_DIR = ROOT / "data" / "odds" / "b365"
OUT_DIR  = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "value_singles_2plus.txt"

MARKET_MATCH_WINNER = 1      # 1X2
MARKET_PLAYER_SHOTS = 268    # Player Shots (O/U)

# -------- String & name-matching helpers (robust) --------
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
    # Remove bookmaker decorations like " (Player Shots)" etc.
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

def person_part_from_option(label: str) -> str:
    # take the "name" section before "Over/Under" bits
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
    return sorted({norm(o).replace(".", "") for o in out if o})

def aliases_from_record(rec: dict) -> List[str]:
    names: List[str] = []
    for k in ("name","player_name","player","short_name","common_name","display_name","full_name","known_as"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            names.append(v.strip())
    # dedupe
    seen, uniq = set(), []
    for n in names:
        key = norm(n)
        if key not in seen:
            seen.add(key); uniq.append(n)
    # expand
    out: List[str] = []
    for n in uniq:
        out.extend(name_variants(n))
    # dedupe again
    seen2, uniq2 = set(), []
    for a in out:
        if a not in seen2:
            seen2.add(a); uniq2.append(a)
    return uniq2

def label_matches_aliases(option_label: str, aliases: Iterable[str]) -> bool:
    lab = norm(person_part_from_option(option_label)).replace(".", "")
    if not lab:
        return False
    lab_tokens = set(lab.split())
    for alias in aliases:
        atoks = set(alias.split())
        if alias == lab or (atoks and (atoks.issubset(lab_tokens) or lab_tokens.issubset(atoks))):
            return True
        # ensure surname tokens present; enforce initial when provided
        a_parts = alias.split()
        a_sur = a_parts[-2:] if len(a_parts) >= 2 and a_parts[-2] in SURNAME_PREFIXES else a_parts[-1:]
        if set(a_sur).issubset(lab_tokens):
            if len(a_parts) >= 2 and len(a_parts[0]) == 1:  # initial
                if a_parts[0] in lab_tokens or lab.startswith(a_parts[0] + " "):
                    return True
                continue
            return True
    return False

# -------- Teams / fixtures helpers --------
def team_tokens(name: str):
    return {t for t in norm(name).split() if t not in GENERIC_TOK}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    return (len(inter) / max(1, len(union)) >= 0.5) or (len(inter) >= 2)

def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _team_name_map(league_id: int) -> Dict[int, str]:
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

# -------- Player-price lookup (Over 1.5 only, robust Over/Under handling) --------
def _row_text(row: dict) -> str:
    fields = ["label","name","original_label","market_description","outcome","outcome_name","header","description"]
    return " ".join([str(row.get(f, "")) for f in fields]).lower()

def _line_is_one_point_five(row: dict) -> bool:
    t = as_float(row.get("total"))
    if t is not None:
        return math.isclose(t, 1.5, abs_tol=1e-6)
    l = as_float(row.get("label"))
    if l is not None:
        return math.isclose(l, 1.5, abs_tol=1e-6)
    blob = _row_text(row).replace(",", ".")
    return "1.5" in blob or "1,5" in blob

def _is_over_row(row: dict) -> Optional[bool]:
    txt = _row_text(row)
    if re.search(r"\bunder\b", txt):
        return False
    if re.search(r"\bover\b", txt):
        return True
    if "+1.5" in txt or "1.5+" in txt or "1,5+" in txt:
        return True
    return None  # ambiguous

def _row_matches_player(row: dict, aliases: Iterable[str]) -> bool:
    candidates = [
        row.get("name",""),
        row.get("original_label",""),
        row.get("label",""),
        row.get("outcome_name",""),
        row.get("header",""),
        row.get("description",""),
    ]
    for cand in candidates:
        if cand and label_matches_aliases(str(cand), aliases):
            return True
    return False

def best_over15_player_shots(odds_rows: List[dict], player_rec: dict) -> Optional[float]:
    """
    Find Over 1.5 price for the player in Player Shots (market_id=268).
    Strategy:
      1) Filter rows to this player and line 1.5.
      2) Prefer rows confidently tagged 'Over'.
      3) If ambiguous (no explicit Over/Under), pick the LOWER price as Over (typical pricing).
    """
    aliases = aliases_from_record(player_rec)
    if not aliases:
        return None

    candidates: List[Tuple[Optional[bool], float]] = []
    for row in odds_rows:
        if int(row.get("market_id", 0)) != MARKET_PLAYER_SHOTS:
            continue
        if not _line_is_one_point_five(row):
            continue
        if not _row_matches_player(row, aliases):
            continue
        price = as_float(row.get("value"))
        if price is None:
            continue
        over_flag = _is_over_row(row)  # True / False / None
        candidates.append((over_flag, price))

    if not candidates:
        return None

    explicit_over = [p for flag, p in candidates if flag is True]
    if explicit_over:
        return min(explicit_over)

    ambiguous = [p for flag, p in candidates if flag is None]
    if ambiguous:
        return min(ambiguous)

    return None

# -------- 2+ form filters (5/5 & 7/10 only) --------
def q2_5of5(series: List[int]) -> bool:
    seq = [x for x in (series or []) if isinstance(x, int)]
    return len(seq) >= 5 and all(x >= 2 for x in seq[:5])

def q2_7of10(series: List[int]) -> bool:
    seq = [x for x in (series or []) if isinstance(x, int)]
    return len(seq) >= 10 and sum(1 for x in seq[:10] if x >= 2) >= 7

def collect_candidates() -> List[dict]:
    """Build candidate pool from player_shots files based on 2+ form only (5/5 OR 7/10)."""
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
            rec_out = dict(rec); rec_out.update({"name": player})
            out.append({
                "league_id": lid,
                "player": player,
                "team": team,
                "position_tag": pos_tag,
                "series": (series or [])[:12],
                "tag": tag,  # "5/5" or "7/10"
                "_rec": rec_out,
            })
    return out

# -------- Main --------
def main():
    candidates = collect_candidates()
    if not candidates:
        ts = dt.datetime.utcnow().isoformat()
        msg = f"Generated at (UTC): {ts}\n[RESULT] No value singles 2+ candidates (5/5 or 7/10)."
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

            # Not a big underdog: require team ML < UNDERDOG_MAX (strict)
            if not (isinstance(team_ml, float) and team_ml < UNDERDOG_MAX):
                continue

            # Find Over 1.5 price with robust parsing
            price = best_over15_player_shots(odds_rows, c["_rec"])
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
        f"Criteria (2+ shots): 5/5 OR 7/10 | O1.5 price > {MIN_PRICE:.2f} | Team ML < {UNDERDOG_MAX:.2f} (exclude big underdogs)",
        "Market: Bet365 Player Shots Over 1.5 (market_id=268, line=1.5)",
        "",
    ]

    groups = {"5/5": [], "7/10": []}
    for p in picks:
        groups[p["tag"]].append(p)

    for tag in ("5/5", "7/10"):
        rows = groups[tag]
        rows.sort(key=lambda x: (-x["price"], x["player"]))
        lines.append(f"===== {tag} (count: {len(rows)}) =====")
        if not rows:
            lines.append("  — none —")
            lines.append("")
            continue
        for x in rows:
            ser = ",".join(map(str, x["series"][:10]))
            pos = f"[{x['position_tag']}]" if x.get("position_tag") else ""
            lines.append(
                f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | "
                f"O1.5 @ {x['price']:.2f} | ML {x['team_ml']:.2f} vs {x['opp_ml']:.2f} | series: {ser}"
            )
        lines.append("")

    if not any(groups.values()):
        lines.append("No value singles (2+) found after filters.")

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
