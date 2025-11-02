#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value — Player Passes (Bet365, Sportmonks data)

Flags value picks where a player has gone OVER a specific passes line in:
  • 5/5 (last 5) OR
  • 7/10 (last 10)

Filters:
  • Price >= MIN_DEC_PRICE (default 1.72)
  • Team moneyline (Bet365 Match Winner) < TEAM_WIN_MAX (default 4.00)  <-- NEW

Inputs (local):
  - data/player_passes/by_league/{league_id}.json
  - data/predicted_xi/by_league/{league_id}.json   (optional team-name map)
  - data/odds/b365/{league_id}.json                (fixtures[].odds[])

Output:
  - data/value_bets/passes_value.txt

ENV (optional):
  - LEAGUE_IDS       (default "301,384,387,564,567,600,8,82,9")
  - WINDOW_DAYS      (default "7")
  - MIN_DEC_PRICE    (default "1.72")
  - TEAM_WIN_MAX     (default "4.00")  # drop picks if team ML >= this
  - MARKET_PASSES_ID (default "290")   # Bet365 Player Passes market id
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Iterable

# -------- Config --------
DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS = [
    int(x)
    for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",")
    if x.strip()
]

WINDOW_DAYS    = int(os.getenv("WINDOW_DAYS", "7"))
MIN_PRICE      = float(os.getenv("MIN_DEC_PRICE", "1.72"))
TEAM_WIN_MAX   = float(os.getenv("TEAM_WIN_MAX", "4.00"))   # NEW: ML must be < this
MARKET_PASSES  = int(os.getenv("MARKET_PASSES_ID", "290"))  # Bet365 Player Passes
MARKET_WINNER  = 1                                          # 1X2

ROOT     = Path(".")
PX_DIR   = ROOT / "data" / "predicted_xi" / "by_league"
PP_DIR   = ROOT / "data" / "player_passes" / "by_league"
ODDS_DIR = ROOT / "data" / "odds" / "b365"
OUT_DIR  = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "passes_value.txt"

# -------- Time helpers --------
def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def parse_dt_utc(s: str) -> Optional[dt.datetime]:
    if not s: return None
    try:
        if "T" not in s:
            return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def within_window(starting_at: str, days: int) -> bool:
    if not days: return True
    dt_k = parse_dt_utc(starting_at)
    if not dt_k: return False
    now = now_utc()
    return now <= dt_k <= (now + dt.timedelta(days=days))

# -------- String & name-matching helpers --------
SUFFIXES = {"jr","junior","sr","senior","ii","iii","iv","filho","neto"}
SURNAME_PREFIXES = {"da","de","del","der","di","dos","du","la","le","van","von","bin","al"}

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
        if ch:
            return ch
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
    if not lab:
        return False
    lab_tokens = set(lab.split())
    for alias in aliases:
        atoks = set(alias.split())
        if alias == lab or (atoks and (atoks.issubset(lab_tokens) or lab_tokens.issubset(atoks))):
            return True
        a_parts = alias.split()
        a_sur = a_parts[-2:] if len(a_parts) >= 2 and a_parts[-2] in SURNAME_PREFIXES else a_parts[-1:]
        if set(a_sur).issubset(lab_tokens):
            if len(a_parts) >= 2 and len(a_parts[0]) == 1:
                if a_parts[0] in lab_tokens or lab.startswith(a_parts[0] + " "):
                    return True
                continue
            return True
    return False

# -------- Teams / fixtures helpers --------
GENERIC_TOK = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}
def team_tokens(name: str):
    return {t for t in norm(name).split() if t not in GENERIC_TOK}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    return (len(inter) / max(1, len(union)) >= 0.5) or (len(inter) >= 2)

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

# -------- IO --------
def _read_json(p: Path) -> Optional[dict]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _team_name_map(league_id: int) -> Dict[int, str]:
    blob = _read_json(PX_DIR / f"{league_id}.json") or {}
    m: Dict[int, str] = {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

# -------- Hit-rate evaluation --------
def hits_over_line(series: List[int], line_total: float, take_n: int) -> Tuple[int, List[int]]:
    xs = [x for x in (series or []) if isinstance(x, int)]
    used = xs[:take_n]
    hits = sum(1 for v in used if float(v) > float(line_total))
    return hits, used

def qualifies_5of5_over(series: List[int], line_total: float) -> Tuple[bool, List[int], int]:
    if len([x for x in series if isinstance(x,int)]) < 5:
        return False, [], 0
    hits, used = hits_over_line(series, line_total, 5)
    return (hits == 5), used, hits

def qualifies_7of10_over(series: List[int], line_total: float) -> Tuple[bool, List[int], int]:
    if len([x for x in series if isinstance(x,int)]) < 10:
        return False, [], 0
    hits, used = hits_over_line(series, line_total, 10)
    return (hits >= 7), used, hits

# -------- Odds parsing (Bet365) --------
def is_passes_market(desc: str) -> bool:
    s = norm(desc)
    return ("pass" in s) or ("passes" in s) or ("player passes" in s)

def as_float(x) -> Optional[float]:
    try:
        return float(str(x))
    except Exception:
        return None

def over_rows_for_player(odds_rows: List[dict], player_rec: dict) -> List[dict]:
    aliases = aliases_from_record(player_rec)
    out: List[dict] = []
    for r in (odds_rows or []):
        if r.get("bookmaker_id") != 2:  # Bet365 only
            continue
        mid = r.get("market_id")
        if mid is not None:
            try:
                if int(mid) != int(MARKET_PASSES):
                    continue
            except Exception:
                continue
        else:
            if not is_passes_market(r.get("market_description","")):
                continue

        lab = (r.get("label") or r.get("outcome") or "").strip().lower()
        if not re.search(r"\bover\b", lab):
            continue

        cand_fields = [
            r.get("name",""),
            r.get("original_label",""),
            r.get("outcome_name",""),
            r.get("header",""),
            r.get("description",""),
        ]
        if not any(cf and label_matches_aliases(str(cf), aliases) for cf in cand_fields):
            continue

        t = as_float(r.get("total"))
        v = as_float(r.get("value"))
        if (t is None) or (v is None):
            continue

        out.append(r)
    return out

def extract_team_ml_prices(odds_rows: List[dict], home_name: str, away_name: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Find best (lowest) Bet365 Match Winner prices for Home and Away.
    Accepts usual label schemes: '1','X','2' or 'home','draw','away'.
    """
    home_price = None; away_price = None
    for row in odds_rows or []:
        if row.get("bookmaker_id") != 2:
            continue
        try:
            if int(row.get("market_id", 0)) != MARKET_WINNER:
                continue
        except Exception:
            continue
        label = (row.get("label") or "").strip().lower()
        name  = (row.get("name")  or "").strip().lower()
        val   = as_float(row.get("value"))
        if val is None:
            continue

        if label in {"1","home"}:
            home_price = val if (home_price is None or val < home_price) else home_price
        elif label in {"2","away"}:
            away_price = val if (away_price is None or val < away_price) else away_price
        else:
            # some feeds carry team names instead of 1/2
            if team_names_match(home_name, label) or team_names_match(home_name, name):
                home_price = val if (home_price is None or val < home_price) else home_price
            elif team_names_match(away_name, label) or team_names_match(away_name, name):
                away_price = val if (away_price is None or val < away_price) else away_price
    return home_price, away_price

# -------- Main --------
def main():
    ts = now_utc().isoformat(timespec="seconds")
    header = [
        f"Generated at (UTC): {ts}",
        f"Criteria: Over line in 5/5 OR 7/10 | Min price ≥ {MIN_PRICE:.2f} | Team ML < {TEAM_WIN_MAX:.2f} | Window={WINDOW_DAYS} days",
        "Market: Bet365 Player Passes (Over/Under)",
        "",
    ]

    picks_5of5: List[dict] = []
    picks_7of10: List[dict] = []

    for lid in LEAGUE_IDS:
        pp_blob = _read_json(PP_DIR / f"{lid}.json") or {}
        team_map = _team_name_map(lid)
        players = pp_blob.get("players") or pp_blob.get("rows") or pp_blob.get("data") or []
        odds_blob = _read_json(ODDS_DIR / f"{lid}.json") or {}
        fixtures = odds_blob.get("fixtures") or []

        for fx in fixtures:
            fname = fx.get("name") or ""
            starting_at = fx.get("starting_at") or ""
            if not within_window(starting_at, WINDOW_DAYS):
                continue

            home, away = parse_fixture_teams(fname)
            if not home or not away:
                continue

            odds_rows = fx.get("odds") or []

            # Pre-calc MLs for this fixture
            home_ml, away_ml = extract_team_ml_prices(odds_rows, home, away)

            for rec in players:
                series = rec.get("passes_last_n") or rec.get("series") or rec.get("passes") or []
                player_name = rec.get("name") or rec.get("player_name") or rec.get("player")
                if not player_name:
                    continue

                tid = rec.get("team_id")
                tname = rec.get("team_name") or rec.get("team") or (team_map.get(int(tid)) if isinstance(tid, int) else "")
                if not tname:
                    continue

                # Belongs to this fixture?
                if team_names_match(tname, home):
                    side = "home"
                    team_ml = home_ml
                elif team_names_match(tname, away):
                    side = "away"
                    team_ml = away_ml
                else:
                    continue

                # ML filter (drop if team ML >= TEAM_WIN_MAX)
                if team_ml is None or not (team_ml < TEAM_WIN_MAX):
                    continue

                # Evaluate all Over lines for this player
                over_rows = over_rows_for_player(odds_rows, {"name": player_name})
                if not over_rows:
                    continue

                pos_tag = (rec.get("position_tag") or rec.get("position") or rec.get("pos") or "").upper()

                for r in over_rows:
                    price = as_float(r.get("value"))
                    if price is None or price < MIN_PRICE:
                        continue
                    line = as_float(r.get("total"))
                    if line is None:
                        continue

                    ok5, used5, hits5 = qualifies_5of5_over(series, line)
                    ok7, used10, hits10 = qualifies_7of10_over(series, line)

                    row = {
                        "league_id": lid,
                        "player": player_name,
                        "team": tname,
                        "fixture": fname,
                        "kickoff": starting_at.replace("T"," ").replace("Z",""),
                        "side": side,
                        "line": line,
                        "price": float(price),
                        "team_ml": float(team_ml),
                        "position_tag": pos_tag,
                        "series": list(series)[:10],
                        "used": used5 if ok5 else used10,
                        "hits": hits5 if ok5 else hits10,
                        "tag": "5/5" if ok5 else ("7/10" if ok7 else None),
                    }

                    if ok5:
                        picks_5of5.append(row)
                    elif ok7:
                        picks_7of10.append(row)

    # Sort by price desc, then player
    picks_5of5.sort(key=lambda x: (-x["price"], x["player"]))
    picks_7of10.sort(key=lambda x: (-x["price"], x["player"]))

    lines = list(header)

    lines.append(f"===== 5/5 (count: {len(picks_5of5)}) =====")
    if not picks_5of5:
        lines.append("  — none —")
    else:
        for x in picks_5of5:
            pos = f"[{x['position_tag']}]" if x.get("position_tag") else ""
            ser = ",".join(map(str, x["series"]))
            used = ",".join(map(str, x["used"]))
            lines.append(
                f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | "
                f"Over {x['line']:.1f} @ {x['price']:.2f} | ML {x['team_ml']:.2f} | hits 5/5 (used: {used}) | series10: {ser}"
            )
    lines.append("")

    lines.append(f"===== 7/10 (count: {len(picks_7of10)}) =====")
    if not picks_7of10:
        lines.append("  — none —")
    else:
        for x in picks_7of10:
            pos = f"[{x['position_tag']}]" if x.get("position_tag") else ""
            ser = ",".join(map(str, x["series"]))
            used = ",".join(map(str, x["used"]))
            lines.append(
                f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | "
                f"Over {x['line']:.1f} @ {x['price']:.2f} | ML {x['team_ml']:.2f} | hits {x['hits']}/10 (used: {used}) | series10: {ser}"
            )
    lines.append("")

    out = "\n".join(lines).rstrip() + "\n"
    OUT_FILE.write_text(out, encoding="utf-8")
    print(out, end="")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
