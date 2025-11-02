#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Player Tackles — Certs & Value (Bet365 via Sportmonks odds)

Outputs (one file, three sections):
  • CERTS — Player 1+ tackles (Over 0.5) with price > 1.80 and strong form tiers (10/10, 9/10, 8/10, 7/7)
  • VALUE SINGLES — Player 1+ tackles (Over 0.5) with price >= 1.72 and form (7/10 OR 5/5)
  • VALUE 2+ — Player 2+ tackles (Over 1.5) with price > 1.80 and form (7/10 OR 5/5)

No money line / match-winner filters are applied.

Reads (local):
  • data/player_tackles/by_league/{league_id}.json
  • data/predicted_xi/by_league/{league_id}.json   (optional; for team name map)
  • data/odds/b365/{league_id}.json                (fixtures[].odds[]; Bet365 bookmaker_id=2)

Writes:
  • data/value_bets/tackles_value.txt

Env (optional):
  • LEAGUE_IDS       (CSV; default "301,384,387,564,567,600,8,82,9")
  • WINDOW_DAYS      (default "7")  — set "0" to disable time window
  • MIN_CERT_PRICE   (default "1.80")  # for Certs O0.5
  • MIN_SINGLE_PRICE (default "1.72")  # for Value Singles O0.5
  • MIN_TWO_PRICE    (default "1.80")  # for Value 2+ O1.5
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Iterable

# -------- Config / IO roots --------
DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS = [
    int(x)
    for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",")
    if x.strip()
]

WINDOW_DAYS      = int(os.getenv("WINDOW_DAYS", "7"))
MIN_CERT_PRICE   = float(os.getenv("MIN_CERT_PRICE",   "1.80"))
MIN_SINGLE_PRICE = float(os.getenv("MIN_SINGLE_PRICE", "1.72"))
MIN_TWO_PRICE    = float(os.getenv("MIN_TWO_PRICE",    "1.80"))

ROOT     = Path(".")
PX_DIR   = ROOT / "data" / "predicted_xi" / "by_league"
TK_DIR   = ROOT / "data" / "player_tackles" / "by_league"
ODDS_DIR = ROOT / "data" / "odds" / "b365"
OUT_DIR  = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "tackles_value.txt"

# -------- basic time helpers --------
def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def parse_dt_utc(s: str) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        if "T" not in s:
            # "YYYY-MM-DD HH:MM:SS"
            return dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def upcoming_within_window(starting_at: str, days: int) -> bool:
    if not days:
        return True
    dt_k = parse_dt_utc(starting_at)
    if not dt_k:
        return False
    now = now_utc()
    return now <= dt_k <= (now + dt.timedelta(days=days))

# -------- String & name-matching helpers (same style as your shots scripts) --------
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
        # surname presence rule with optional initial
        a_parts = alias.split()
        a_sur = a_parts[-2:] if len(a_parts) >= 2 and a_parts[-2] in SURNAME_PREFIXES else a_parts[-1:]
        if set(a_sur).issubset(lab_tokens):
            if len(a_parts) >= 2 and len(a_parts[0]) == 1:
                if a_parts[0] in lab_tokens or lab.startswith(a_parts[0] + " "):
                    return True
                continue
            return True
    return False

# -------- Teams & fixtures helpers --------
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
    for sep in (" vs ", " v ", " VS ", " Vs "):
        if sep in fixture_name:
            a, b = fixture_name.split(sep, 1)
            return a.strip(), b.strip()
    if " - " in fixture_name:
        a, b = fixture_name.split(" - ", 1)
        return a.strip(), b.strip()
    return "", ""

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

# -------- Odds parsing (tackles markets) --------
def to_float(v) -> Optional[float]:
    try:
        if v in (None, "", "N/A"): return None
        return float(v)
    except Exception:
        return None

def _row_text(row: dict) -> str:
    fields = ["label","name","original_label","market_description","outcome","outcome_name","header","description"]
    return " ".join([str(row.get(f, "")) for f in fields]).lower()

def is_tackles_market(desc: str) -> bool:
    s = norm(desc)
    return ("tackle" in s)  # simple and effective; Bet365 strings usually contain "Tackles"

def _line_is(row: dict, target: float) -> bool:
    t = to_float(row.get("total"))
    if t is not None and math.isclose(t, target, abs_tol=1e-6):
        return True
    l = to_float(row.get("label"))
    if l is not None and math.isclose(l, target, abs_tol=1e-6):
        return True
    blob = _row_text(row).replace(",", ".")
    s = f"{target:.1f}"
    return (s in blob)

def _is_over_row(row: dict) -> Optional[bool]:
    txt = _row_text(row)
    if re.search(r"\bunder\b", txt): return False
    if re.search(r"\bover\b",  txt): return True
    # common hints
    if "+0.5" in txt or "0.5+" in txt or "0,5+" in txt: return True
    if "+1.5" in txt or "1.5+" in txt or "1,5+" in txt: return True
    return None

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

def best_over_player_tackles(odds_rows: List[dict], player_rec: dict, line: float) -> Optional[float]:
    """
    Find best 'Over {line}' price for a player in Player Tackles markets.
    Strategy mirrors shots helpers:
      1) Filter to Bet365 rows whose market_description includes 'tackle'
      2) Match the player name robustly
      3) Require the specific line (0.5 or 1.5)
      4) Prefer explicit Over rows; if none, choose min(ambiguous)
    """
    aliases = aliases_from_record(player_rec)
    if not aliases:
        return None

    cands: List[Tuple[Optional[bool], float]] = []
    for row in odds_rows or []:
        if int(row.get("bookmaker_id") or 0) != 2:
            continue  # Bet365 only
        if row.get("stopped"):
            continue
        if not is_tackles_market(row.get("market_description","")):
            continue
        if not _line_is(row, line):
            continue
        if not _row_matches_player(row, aliases):
            continue
        price = to_float(row.get("value"))
        if price is None:
            continue
        over_flag = _is_over_row(row)
        cands.append((over_flag, price))

    if not cands:
        return None

    explicit_over = [p for flag, p in cands if flag is True]
    if explicit_over:
        return min(explicit_over)
    ambiguous = [p for flag, p in cands if flag is None]
    if ambiguous:
        return min(ambiguous)
    return None

# -------- Form logic (>=1 or >=2 tackles) --------
def count_hits(series: List[int], threshold: int, take: int) -> Tuple[int, List[int]]:
    xs = [int(x) for x in (series or []) if isinstance(x, int)]
    window = xs[:take]
    hits = sum(1 for v in window if v >= threshold)
    return hits, window

def tier_1plus(series: List[int]) -> Tuple[Optional[str], List[int]]:
    # Cert tiers: 10/10, 9/10, 8/10, or 7/7 — all using threshold >=1
    xs = [int(x) for x in (series or []) if isinstance(x, int)]
    if len(xs) >= 10:
        h, w = count_hits(xs, 1, 10)
        if h == 10: return "10/10", w
        if h == 9:  return "9/10",  w
        if h == 8:  return "8/10",  w
    if len(xs) >= 7:
        h, w = count_hits(xs, 1, 7)
        if h == 7:  return "7/7",   w
    return None, []

def qual_1plus_value(series: List[int]) -> Optional[str]:
    # Value singles (1+): 7/10 OR 5/5
    xs = [int(x) for x in (series or []) if isinstance(x, int)]
    if len(xs) >= 10 and count_hits(xs, 1, 10)[0] >= 7:
        return "7/10"
    if len(xs) >= 5  and count_hits(xs, 1, 5)[0]  >= 5:
        return "5/5"
    return None

def qual_2plus_value(series: List[int]) -> Optional[str]:
    # Value 2+: 7/10 OR 5/5 at threshold >=2
    xs = [int(x) for x in (series or []) if isinstance(x, int)]
    if len(xs) >= 10 and count_hits(xs, 2, 10)[0] >= 7:
        return "7/10"
    if len(xs) >= 5  and count_hits(xs, 2, 5)[0]  >= 5:
        return "5/5"
    return None

# -------- Candidate building --------
def collect_player_rows(lid: int) -> List[dict]:
    blob = _load_json(TK_DIR / f"{lid}.json") or {}
    players = blob.get("players") or blob.get("rows") or blob.get("data") or []
    return players

def team_name_map(lid: int) -> Dict[int, str]:
    return _team_name_map(lid)

def build_candidates_for_league(lid: int, odds_blob: dict, window_days: int):
    """Yield (fixture, odds_rows, player_record, helper_fields...) for upcoming fixtures only."""
    team_map = team_name_map(lid)
    fixtures = (odds_blob or {}).get("fixtures") or []
    players  = collect_player_rows(lid)

    for rec in players:
        series = rec.get("tackles_last_n") or rec.get("series") or []
        player_name = rec.get("name") or rec.get("player_name") or rec.get("player") or ""
        if not player_name:
            continue
        tid = rec.get("team_id")
        team = rec.get("team_name") or rec.get("team") or (team_map.get(int(tid)) if isinstance(tid, int) else None)

        # try to find a matching upcoming fixture within window that includes this team (if we know team)
        for fx in fixtures:
            starting_at = fx.get("starting_at") or ""
            if window_days and not upcoming_within_window(starting_at, window_days):
                continue
            fname = fx.get("name") or ""
            if team:
                home, away = parse_fixture_teams(fname)
                if not home or not away:
                    continue
                if not (team_names_match(team, home) or team_names_match(team, away)):
                    continue
            yield fx, (fx.get("odds") or []), rec, {
                "league_id": lid,
                "player": player_name,
                "team": team or "",
                "series": series,
                "fixture": fname,
                "kickoff": starting_at.replace("T"," ").replace("Z",""),
                "position_tag": (rec.get("position_tag") or rec.get("position") or rec.get("pos") or "").upper(),
                "_rec_for_alias": dict(rec, **({"name": player_name})),
            }
            break  # one fixture per player is enough

# -------- Main --------
def main():
    ts = dt.datetime.utcnow().isoformat(timespec="seconds")
    header = [
        f"Generated at (UTC): {ts}",
        f"Window={WINDOW_DAYS} days | CERTS O0.5 > {MIN_CERT_PRICE:.2f} | VALUE O0.5 ≥ {MIN_SINGLE_PRICE:.2f} | VALUE O1.5 > {MIN_TWO_PRICE:.2f}",
        "Markets parsed: Bet365 'Player Tackles' (robust by description + line match)",
        "",
    ]

    odds_by_league: Dict[int, dict] = {lid: (_load_json(ODDS_DIR / f"{lid}.json") or {}) for lid in LEAGUE_IDS}

    certs: List[dict] = []
    singles: List[dict] = []
    two_plus: List[dict] = []

    seen_key = set()

    for lid in LEAGUE_IDS:
        blob = odds_by_league.get(lid) or {}
        for fx, rows, rec, meta in build_candidates_for_league(lid, blob, WINDOW_DAYS):
            key = (lid, meta["team"].lower(), meta["player"].lower())
            if key in seen_key:
                continue

            # --- CERTS (1+) ---
            tier, used_series = tier_1plus(meta["series"])
            if tier:
                price_o05 = best_over_player_tackles(rows, meta["_rec_for_alias"], 0.5)
                if price_o05 is not None and price_o05 > MIN_CERT_PRICE:
                    certs.append({
                        **meta,
                        "tier": tier,
                        "price": float(price_o05),
                        "series_used": used_series,
                    })

            # --- VALUE SINGLES (1+) ---
            tag1 = qual_1plus_value(meta["series"])
            if tag1:
                price_o05 = best_over_player_tackles(rows, meta["_rec_for_alias"], 0.5)
                if price_o05 is not None and price_o05 >= MIN_SINGLE_PRICE:
                    singles.append({
                        **meta,
                        "tag": tag1,
                        "price": float(price_o05),
                    })

            # --- VALUE 2+ (Over 1.5) ---
            tag2 = qual_2plus_value(meta["series"])
            if tag2:
                price_o15 = best_over_player_tackles(rows, meta["_rec_for_alias"], 1.5)
                if price_o15 is not None and price_o15 > MIN_TWO_PRICE:
                    two_plus.append({
                        **meta,
                        "tag": tag2,
                        "price": float(price_o15),
                    })

            seen_key.add(key)

    # sort sections
    tier_rank = {"10/10": 4, "9/10": 3, "8/10": 2, "7/7": 1}
    certs.sort(key=lambda r: (-tier_rank.get(r.get("tier",""), 0), -r["price"], r["player"]))
    singles.sort(key=lambda r: (-r["price"], r["player"]))
    two_plus.sort(key=lambda r: (-r["price"], r["player"]))

    lines = []
    lines.extend(header)

    # CERTS
    lines.append("===== CERTS — Player 1+ Tackles (Over 0.5) =====")
    if not certs:
        lines.append("  — none —")
    else:
        for r in certs:
            ser = ",".join(map(str, r.get("series_used", [])[:10])) or ""
            pos = f"[{r['position_tag']}]" if r.get("position_tag") else ""
            lines.append(
                f" • {r['player']} {pos} — {r['team']} | {r['fixture']} @ {r['kickoff']} | "
                f"O0.5 @ {r['price']:.2f} | tier {r['tier']} | series: {ser}"
            )
    lines.append("")

    # VALUE SINGLES
    lines.append("===== VALUE SINGLES — Player 1+ Tackles (Over 0.5) =====")
    if not singles:
        lines.append("  — none —")
    else:
        for r in singles:
            ser = ",".join(map(str, r["series"][:10]))
            pos = f"[{r['position_tag']}]" if r.get("position_tag") else ""
            lines.append(
                f" • {r['player']} {pos} — {r['team']} | {r['fixture']} @ {r['kickoff']} | "
                f"O0.5 @ {r['price']:.2f} | {r['tag']} | series: {ser}"
            )
    lines.append("")

    # VALUE 2+
    lines.append("===== VALUE 2+ — Player 2+ Tackles (Over 1.5) =====")
    if not two_plus:
        lines.append("  — none —")
    else:
        for r in two_plus:
            ser = ",".join(map(str, r["series"][:10]))
            pos = f"[{r['position_tag']}]" if r.get("position_tag") else ""
            lines.append(
                f" • {r['player']} {pos} — {r['team']} | {r['fixture']} @ {r['kickoff']} | "
                f"O1.5 @ {r['price']:.2f} | {r['tag']} | series: {ser}"
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
