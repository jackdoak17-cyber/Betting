#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value Singles — Shots Over 0.5 (Sportmonks/Bet365)

Criteria:
  • Form path A: 7/10 (≥7 of last 10 with ≥1 shot)
  • Form path B: 5/5 (all of last 5 with ≥1 shot)
  • Price: Bet365 Over 0.5 shots ≥ MIN_DEC_PRICE (default 1.72)
  • Remove underdogs: team ML ≤ opponent ML

Inputs (local):
  • data/player_shots/by_league/{league_id}.json
  • data/predicted_xi/by_league/{league_id}.json  (optional, for team names)
  • data/odds/b365/{league_id}.json               (market_id=1,268)

Output:
  • data/value_bets/value_singles.txt
  • Includes player's position tag in output, e.g. [FWD]

ENV (optional):
  • MIN_DEC_PRICE   (default "1.72")
  • FAVORITES_ONLY  (default "true")
  • LEAGUE_IDS      (default "301,384,387,564,567,600,8,82,9")
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Iterable

# -------- Config --------
MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.72"))
FAVORITES_ONLY = (os.getenv("FAVORITES_ONLY", "true").strip().lower() in {"1","true","yes","y"})

DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS = [
    int(x) for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",")
    if x.strip()
]

ROOT     = Path(".")
PX_DIR   = ROOT / "data" / "predicted_xi" / "by_league"
PS_DIR   = ROOT / "data" / "player_shots" / "by_league"
ODDS_DIR = ROOT / "data" / "odds" / "b365"
OUT_DIR  = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "value_singles.txt"

MARKET_MATCH_WINNER = 1      # 1X2
MARKET_PLAYER_SHOTS = 268    # Player Shots (O/U)

# -------- String helpers (robust player matching) --------
SUFFIXES = {"jr","junior","sr","senior","ii","iii","iv","filho","neto"}
SURNAME_PREFIXES = {"da","de","del","der","di","dos","du","la","le","van","von","bin","al"}

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")

def norm_spaces(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^\w\s\.-]", " ", s)
    s = norm_spaces(s)
    return s

def cleanup_label(label: str) -> str:
    # Remove bracketed or parenthetical tails (bookmaker decorations)
    s = re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()
    return s

def person_part_from_option(label: str) -> str:
    """
    Extract the 'person name' portion from an option label that might look like:
      "O. Watkins Over 0.5", "Ollie Watkins - Over 0.5", "O Watkins 0.5+"
    We crop before ' over ' / ' under ' or ' - over ' etc.
    """
    s = cleanup_label(label or "")
    m = re.split(r"\b(?:-?\s*over|-?\s*under|\s+o\/u|\s+o\d+|\s+u\d+)\b", s, flags=re.IGNORECASE)
    return m[0].strip() if m else s

def drop_suffixes(parts: List[str]) -> List[str]:
    out = list(parts)
    while out and re.sub(r"[^\w]+", "", out[-1]).lower() in SUFFIXES:
        out = out[:-1]
    return out

def split_name_tokens(name: str) -> List[str]:
    s = norm(name).replace("-", " ")
    parts = [p for p in s.split() if p]
    return parts

def surname_tokens(parts: List[str]) -> List[str]:
    """Return surname tokens, preserving common prefixes like 'van Dijk'."""
    parts = drop_suffixes(parts)
    if not parts:
        return []
    if len(parts) >= 2 and parts[-2] in SURNAME_PREFIXES:
        return parts[-2:]
    return parts[-1:]

def first_initial(parts: List[str]) -> Optional[str]:
    parts = drop_suffixes(parts)
    if not parts:
        return None
    for p in parts[:-1]:
        ch = p[:1]
        if ch:
            return ch
    return None

def name_variants(full_name: str) -> List[str]:
    """
    Generate robust variants for matching:
      - full name (normalized)
      - surname only
      - first-initial + surname
      - surname-with-prefix (e.g., 'van dijk')
      - hyphen-stripped variants
    """
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
    # also add 'last, first' minimal variant to tolerate odd book names
    out.add(f"{sur} {init or ''}".strip())
    return sorted({norm(o).replace(".", "") for o in out if o})

def aliases_from_record(rec: dict) -> List[str]:
    names: List[str] = []
    for k in ("name","player_name","player","short_name","common_name","display_name","full_name","known_as"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            names.append(v.strip())
    # Deduplicate while preserving order
    seen = set(); uniq = []
    for n in names:
        key = norm(n)
        if key not in seen:
            seen.add(key); uniq.append(n)
    # Expand to variants
    out: List[str] = []
    for n in uniq:
        out.extend(name_variants(n))
    # unique again
    seen2 = set(); uniq2 = []
    for n in out:
        if n not in seen2:
            seen2.add(n); uniq2.append(n)
    return uniq2

def label_matches_aliases(option_label: str, aliases: Iterable[str]) -> bool:
    lab = norm(person_part_from_option(option_label)).replace(".", "")
    if not lab:
        return False
    # Token-based containment to reduce false positives
    lab_tokens = set(lab.split())
    for alias in aliases:
        a = alias.strip()
        if not a:
            continue
        atoks = set(a.split())
        # Direct equality or subset/superset logic
        if a == lab:
            return True
        if atoks and (atoks.issubset(lab_tokens) or lab_tokens.issubset(atoks)):
            return True
        # Loose: ensure surname token(s) present + (first initial present if available)
        a_parts = a.split()
        a_sur = a_parts[-2:] if len(a_parts) >= 2 and a_parts[-2] in SURNAME_PREFIXES else a_parts[-1:]
        if set(a_sur).issubset(lab_tokens):
            # If alias has an initial token, require it's present or lab starts with it
            if len(a_parts) >= 2:
                a_first = a_parts[0]
                if len(a_first) == 1:  # initial
                    if a_first in lab_tokens or lab.startswith(a_first + " "):
                        return True
                    continue
            return True
    return False

# -------- Team name helpers (unchanged) --------
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
        label = (row.get("label") or "").strip()
        name  = (row.get("name")  or "").strip()
        val   = as_float(row.get("value"))
        if val is None: 
            continue
        labn = norm(label)
        namen = norm(name)
        if labn in {"1","home"} or team_names_match(home_name, label) or team_names_match(home_name, name):
            home_price = val if (home_price is None or val < home_price) else home_price
        elif labn in {"2","away"} or team_names_match(away_name, label) or team_names_match(away_name, name):
            away_price = val if (away_price is None or val < away_price) else away_price
        elif team_names_match(home_name, name):
            home_price = val if (home_price is None or val < home_price) else home_price
        elif team_names_match(away_name, name):
            away_price = val if (away_price is None or val < away_price) else away_price
    return home_price, away_price

def best_over05_player_shots(odds_rows: List[dict], player_rec: dict) -> Optional[float]:
    """Find Over 0.5 price for the player in market_id=268 using robust alias matching."""
    aliases = aliases_from_record(player_rec)
    if not aliases:
        return None
    best = None
    # Pre-filter all player-shots rows once
    rows = [r for r in odds_rows if int(r.get("market_id", 0)) == MARKET_PLAYER_SHOTS]
    for row in rows:
        candidate = row.get("name") or row.get("total") or row.get("original_label") or ""
        if not candidate:
            continue
        if not label_matches_aliases(candidate, aliases):
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

# -------- Form filters --------
def qualifies_5of5(series: List[int]) -> bool:
    seq = [x for x in (series or []) if isinstance(x, int)]
    if len(seq) < 5: return False
    last5 = seq[:5]
    return all(x >= 1 for x in last5)

def qualifies_7of10(series: List[int]) -> bool:
    seq = [x for x in (series or []) if isinstance(x, int)]
    if len(seq) < 10: return False
    last10 = seq[:10]
    return sum(1 for x in last10 if x >= 1) >= 7

def collect_candidates() -> List[dict]:
    """Build candidate pool from player_shots files based on form only (7/10 OR 5/5)."""
    out = []
    for lid in LEAGUE_IDS:
        shots_blob = _load_json(PS_DIR / f"{lid}.json") or {}
        team_map = _team_name_map(lid)
        players = shots_blob.get("players") or shots_blob.get("rows") or shots_blob.get("data") or []
        for rec in players:
            series = rec.get("shots_last_n") or rec.get("series") or rec.get("shots") or []
            tag = None
            if qualifies_5of5(series):
                tag = "5/5"
            elif qualifies_7of10(series):
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
            rec_out = dict(rec)  # keep original fields for alias extraction
            rec_out.update({"name": player})
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
        msg = "[RESULT] No value singles candidates (5/5 or 7/10)."
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

            price = best_over05_player_shots(odds_rows, c["_rec"])
            if price is None or price < MIN_PRICE:
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
    fav_str = "Favorites only" if FAVORITES_ONLY else "Favorites filter OFF"
    lines = [
        f"Generated at (UTC): {ts}",
        f"Criteria: 5/5 OR 7/10 | Over 0.5 ≥ {MIN_PRICE:.2f} | {fav_str}",
        "Market: Bet365 Player Shots Over 0.5 (market_id=268)",
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
                f"O0.5 @ {x['price']:.2f} | ML {x['team_ml']:.2f} vs {x['opp_ml']:.2f} | series: {ser}"
            )
        lines.append("")

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
