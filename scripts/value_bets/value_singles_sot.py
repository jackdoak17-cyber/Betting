#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value Singles — Shots on Target (Sportmonks-only)

Find players to have 1+ SOT (Over 0.5) using your locally stored data:
- Fixtures:            data/fixtures/{league_id}.json
- Per-fixture odds:    data/odds/b365/fixtures/{fixture_id}.json
- Player SOT series:   data/player_shots_on_target/by_league/{league_id}.json

Criteria (kept if ANY tier matches):
  • 5/5 (last 5 all ≥1 SOT), OR
  • 7/10 (last 10 ≥1 SOT in at least 7)
AND:
  • Bet365 Over 0.5 SOT price ≥ MIN_DEC_PRICE (default 1.72)
  • Team ML ≤ TEAM_UNDERDOG_MAX (default 3.50); drop big underdogs with team ML > cap
  • Fixture kickoff within WINDOW_DAYS days (default 7) — relative to now (UTC)

Env (all optional):
  LEAGUE_IDS          Comma-separated Sportmonks league IDs; blank = auto-discover from fixtures dir
  WINDOW_DAYS         Default 7
  MIN_DEC_PRICE       Default 1.72
  TEAM_UNDERDOG_MAX   Default 3.50
  DEBUG_DROPS         1 to print detailed drop reasons (default 0)
  NEAR_MISS_LIMIT     Max near-miss lines to print (default 12)

Output: human-readable text to stdout for your workflow to tee into data/value_bets/value_singles_sot.txt
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Iterable

# ========= Config / Paths =========
ROOT = Path(".")
FIX_DIR = ROOT / "data" / "fixtures"
ODDS_PER_FIX_DIR = ROOT / "data" / "odds" / "b365" / "fixtures"
SOT_DIR = ROOT / "data" / "player_shots_on_target" / "by_league"

WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "7"))
MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.72"))
TEAM_UNDERDOG_MAX = float(os.getenv("TEAM_UNDERDOG_MAX", "3.50"))   # drop if team ML > this

LEAGUE_IDS_ENV = os.getenv("LEAGUE_IDS", "").strip()
LEAGUE_IDS: List[int] = [int(x) for x in LEAGUE_IDS_ENV.split(",") if x.strip().isdigit()] if LEAGUE_IDS_ENV else []

DEBUG_DROPS = bool(int(os.getenv("DEBUG_DROPS", "0")))
NEAR_MISS_LIMIT = int(os.getenv("NEAR_MISS_LIMIT", "12"))

# ========= Helpers =========
def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def parse_utc(s: str) -> Optional[dt.datetime]:
    # expects "YYYY-MM-DD HH:MM:SS" (Sportmonks in your fixtures JSON)
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None

def load_json(p: Path) -> Optional[dict]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def debug(msg: str):
    if DEBUG_DROPS:
        print(msg)

def to_float(x) -> Optional[float]:
    try:
        if x is None or x == "N/A":
            return None
        return float(x)
    except Exception:
        return None

# ========= Robust person-name matching (like anytime goalscorer fix) =========
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
    # Remove bookmaker decorations like " (Player SOT)" or other parentheses
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

def person_part_from_option(label: str) -> str:
    """
    Extract the person-name portion from options like:
    "O. Watkins Over 0.5", "Ollie Watkins - Over 0.5", "O Watkins 0.5+"
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
    return [p for p in norm(name).replace("-", " ").split() if p]

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
    for p in parts[:-1]:
        ch = p[:1]
        if ch:
            return ch
    return None

def name_variants(full_name: str) -> List[str]:
    """
    Generate robust variants for matching:
      - full name (normalized)
      - surname only (w/ prefix if applicable)
      - first-initial + surname
      - minimal 'surname initial' variant
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
    # expand to variants
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
        # ensure surname tokens present; enforce initial presence when provided
        a_parts = alias.split()
        a_sur = a_parts[-2:] if len(a_parts) >= 2 and a_parts[-2] in SURNAME_PREFIXES else a_parts[-1:]
        if set(a_sur).issubset(lab_tokens):
            if len(a_parts) >= 2 and len(a_parts[0]) == 1:  # initial
                if a_parts[0] in lab_tokens or lab.startswith(a_parts[0] + " "):
                    return True
                continue
            return True
    return False

# ========= Market classifiers =========
NEGATIVE_SOT_TERMS = {
    "outside", "from outside", "outside the box", "first half", "second half", "1st half", "2nd half",
    "header", "headers", "distance"
}

def market_is_player_sot(desc: str) -> bool:
    d = (desc or "").lower()
    if not d:
        return False
    if "on target" not in d:
        return False
    if "player" not in d:
        return False
    if any(t in d for t in NEGATIVE_SOT_TERMS):
        return False
    return True

MATCH_WINNER_KEYS = ["match winner", "match result", "1x2", "full time result", "win/draw/win", "to win", "90 minutes", "result"]

def market_is_match_winner(desc: str) -> bool:
    s = (desc or "").lower()
    return any(k in s for k in MATCH_WINNER_KEYS)

# ========= Load upcoming fixtures =========
def discover_leagues_from_fixtures() -> List[int]:
    out = []
    for p in sorted(FIX_DIR.glob("*.json")):
        try:
            out.append(int(p.stem))
        except Exception:
            pass
    return out

def upcoming_fixtures_for_league(lid: int, window_days: int) -> List[dict]:
    blob = load_json(FIX_DIR / f"{lid}.json") or {}
    fixtures = blob.get("fixtures") or []
    if not window_days:
        return fixtures
    now = utc_now()
    end = now + dt.timedelta(days=window_days)
    kept = []
    for fx in fixtures:
        t = parse_utc(fx.get("starting_at") or "")
        if t and now <= t <= end:
            kept.append(fx)
    return kept

def team_maps_from_fixtures(fixtures: List[dict]) -> Tuple[Dict[int, str], Dict[str, dict]]:
    """
    Returns:
      team_id_to_name: {team_id -> name}
      team_name_to_next_fixture: {name_lower -> info{fixture_id, side, opp_name, kickoff_dt, home_name, away_name}}
    """
    team_id_to_name: Dict[int, str] = {}
    team_name_to_next_fixture: Dict[str, dict] = {}
    for fx in fixtures:
        fid = int(fx.get("id"))
        start_s = fx.get("starting_at")
        t = parse_utc(start_s or "")
        parts = fx.get("participants") or []
        home_name = away_name = None
        home_id = away_id = None
        for p in parts:
            nm = p.get("name"); pid = p.get("id")
            loc = ((p.get("meta") or {}).get("location") or "").lower()
            if isinstance(pid, int) and isinstance(nm, str):
                team_id_to_name[pid] = nm
            if loc == "home":
                home_name, home_id = nm, pid
            elif loc == "away":
                away_name, away_id = nm, pid
        if not (home_name and away_name and t):
            continue
        for nm, side, opp in ((home_name, "home", away_name), (away_name, "away", home_name)):
            key = nm.lower()
            prev = team_name_to_next_fixture.get(key)
            if (not prev) or (t < prev["kickoff_dt"]):
                team_name_to_next_fixture[key] = {
                    "fixture_id": fid,
                    "side": side,
                    "opp_name": opp,
                    "kickoff_dt": t,
                    "home_name": home_name,
                    "away_name": away_name,
                }
    return team_id_to_name, team_name_to_next_fixture

# ========= Pull team ML from per-fixture odds =========
def get_team_match_prices(fid: int, home_name: str, away_name: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (home_ml, away_ml) decimal if found in Bet365 'Match Winner/Result' market.
    Uses multiple fallbacks: by exact team name, or by label ('1','2'), or name 'Home'/'Away'.
    """
    blob = load_json(ODDS_PER_FIX_DIR / f"{fid}.json") or {}
    rows = blob.get("odds") or []
    if not isinstance(rows, list):
        return (None, None)

    home_ml = None
    away_ml = None

    for r in rows:
        if str(r.get("bookmaker_id")) != "2":
            continue
        desc = r.get("market_description") or r.get("market_name") or ""
        if not market_is_match_winner(desc):
            continue
        label = (r.get("label") or "").strip().lower()
        name = (r.get("name") or "").strip()
        price = to_float(r.get("value"))
        if price is None:
            continue

        nlow = name.lower()
        # map by name first
        if nlow == (home_name or "").lower():
            home_ml = price
            continue
        if nlow == (away_name or "").lower():
            away_ml = price
            continue
        # common aliases
        if nlow in ("home", "1") or label in ("home", "1"):
            home_ml = price
            continue
        if nlow in ("away", "2") or label in ("away", "2"):
            away_ml = price
            continue

    return (home_ml, away_ml)

# ========= SOT Over 0.5 price lookup (robust Over/Under handling) =========
def _row_text(row: dict) -> str:
    fields = ["label","name","original_label","market_description","outcome","outcome_name","header","description"]
    return " ".join([str(row.get(f, "")) for f in fields]).lower()

def _line_is_point5(row: dict) -> bool:
    t = to_float(row.get("total"))
    if t is not None:
        return math.isclose(t, 0.5, abs_tol=1e-6)
    l = to_float(row.get("label"))
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

def find_player_sot_over_point5(fid: int, player_name_or_rec: Any) -> Optional[float]:
    # Accept either a plain string (name) or a player record with richer name fields
    if isinstance(player_name_or_rec, dict):
        aliases = aliases_from_record(player_name_or_rec)
        display_name = player_name_or_rec.get("name") or ""
    else:
        aliases = name_variants(str(player_name_or_rec))
        display_name = str(player_name_or_rec)

    blob = load_json(ODDS_PER_FIX_DIR / f"{fid}.json") or {}
    rows = blob.get("odds") or []
    if not isinstance(rows, list):
        return None

    candidates: List[Tuple[Optional[bool], float]] = []
    for r in rows:
        if str(r.get("bookmaker_id")) != "2":
            continue
        desc = r.get("market_description") or r.get("market_name") or ""
        if not market_is_player_sot(desc):
            continue
        if not _line_is_point5(r):
            continue
        if not _row_matches_player(r, aliases):
            continue
        price = to_float(r.get("value"))
        if price is None:
            continue
        over_flag = _is_over_row(r)  # True/False/None
        candidates.append((over_flag, price))

    if not candidates:
        return None

    # Prefer explicit Over rows, then ambiguous (choose min price), never choose explicit Unders
    explicit_over = [p for flag, p in candidates if flag is True]
    if explicit_over:
        return min(explicit_over)

    ambiguous = [p for flag, p in candidates if flag is None]
    if ambiguous:
        return min(ambiguous)

    return None

# ========= History tiers =========
def hits(seq: List[int]) -> List[int]:
    return [1 if (isinstance(x, (int, float)) and x >= 1) else 0 for x in (seq or [])]

def compute_tier(series: List[int]) -> Optional[str]:
    """
    Returns "5/5" or "7/10" or None. (4/5 removed)
    """
    h = hits(series)
    last5 = h[:5]
    last10 = h[:10]

    five_of_five = len(last5) >= 5 and sum(last5) >= 5
    seven_of_ten = len(last10) >= 10 and sum(last10) >= 7

    if five_of_five:
        return "5/5"
    if seven_of_ten:
        return "7/10"
    return None

# ========= Main =========
def main():
    now = utc_now()
    print(f"Generated at (UTC): {now.isoformat()}")
    print(f"Criteria: 5/5 OR 7/10 | Over 0.5 SOT ≥ {MIN_DEC_PRICE:.2f} | "
          f"Drop big underdogs (team ML > {TEAM_UNDERDOG_MAX:.2f}) | Window={WINDOW_DAYS} days")

    # Leagues to use
    leagues = LEAGUE_IDS or discover_leagues_from_fixtures()
    leagues = sorted(set(leagues))

    # Build upcoming fixture contexts per league
    upcoming_by_league: Dict[int, List[dict]] = {lid: upcoming_fixtures_for_league(lid, WINDOW_DAYS) for lid in leagues}

    # Prepare results
    kept: List[dict] = []
    near_misses: List[dict] = []
    scanned_candidates = 0

    for lid in leagues:
        fx_list = upcoming_by_league.get(lid) or []
        if not fx_list:
            continue

        team_id_to_name, team_next = team_maps_from_fixtures(fx_list)

        # Load SOT series file for this league
        sot_blob = load_json(SOT_DIR / f"{lid}.json") or {}
        players = sot_blob.get("players") or []
        if not players:
            continue

        for rec in players:
            # derive team name (prefer from fixtures mapping via team_id)
            team_id = rec.get("team_id")
            team_name = team_id_to_name.get(int(team_id)) if isinstance(team_id, int) else None
            if not team_name:
                # No upcoming fixture context; skip early
                continue

            nxt = team_next.get((team_name or "").lower())
            if not nxt:
                # team has no fixture in the window
                continue

            fid = nxt["fixture_id"]
            side = nxt["side"]
            opp_name = nxt["opp_name"]
            kickoff = nxt["kickoff_dt"]
            home_name = nxt["home_name"]
            away_name = nxt["away_name"]

            # get team ML from the fixture odds
            home_ml, away_ml = get_team_match_prices(fid, home_name, away_name)
            team_ml = home_ml if side == "home" else away_ml
            if team_ml is None:
                debug(f"[DROP no-ml] {team_name} vs {opp_name} (fid={fid})")
                near_misses.append({
                    "reason": "no-ml",
                    "player": rec.get("name"),
                    "team": team_name,
                    "fixture": f"{home_name} vs {away_name}",
                    "kickoff": kickoff.strftime("%Y-%m-%d %H:%M:%S"),
                    "price": None,
                    "team_ml": None,
                    "tier": None,
                    "series5": ",".join(map(str, (rec.get("on_target_last_n") or [])[:5])),
                })
                continue

            # drop big underdogs (strictly greater than cap)
            if team_ml > TEAM_UNDERDOG_MAX:
                debug(f"[DROP underdog {team_ml:.2f}] {team_name} ({rec.get('name')})")
                near_misses.append({
                    "reason": "underdog",
                    "player": rec.get("name"),
                    "team": team_name,
                    "fixture": f"{home_name} vs {away_name}",
                    "kickoff": kickoff.strftime("%Y-%m-%d %H:%M:%S"),
                    "price": None,
                    "team_ml": team_ml,
                    "tier": None,
                    "series5": ",".join(map(str, (rec.get("on_target_last_n") or [])[:5])),
                })
                continue

            # compute tier
            series = rec.get("on_target_last_n") or []
            tier = compute_tier(series)
            scanned_candidates += 1
            if not tier:
                debug(f"[DROP history] {rec.get('name')} — no (5/5 or 7/10)")
                continue

            # find Over 0.5 SOT price for this player in this fixture
            price = find_player_sot_over_point5(fid, rec)  # pass whole rec for alias richness
            if price is None:
                debug(f"[DROP no-price] {rec.get('name')} — no Over 0.5 SOT found (fid={fid})")
                near_misses.append({
                    "reason": "no-price",
                    "player": rec.get("name"),
                    "team": team_name,
                    "fixture": f"{home_name} vs {away_name}",
                    "kickoff": kickoff.strftime("%Y-%m-%d %H:%M:%S"),
                    "price": None,
                    "team_ml": team_ml,
                    "tier": tier,
                    "series5": ",".join(map(str, series[:5])),
                })
                continue

            if price < MIN_DEC_PRICE:
                debug(f"[DROP low-price {price:.2f}] {rec.get('name')}")
                near_misses.append({
                    "reason": "low-price",
                    "player": rec.get("name"),
                    "team": team_name,
                    "fixture": f"{home_name} vs {away_name}",
                    "kickoff": kickoff.strftime("%Y-%m-%d %H:%M:%S"),
                    "price": price,
                    "team_ml": team_ml,
                    "tier": tier,
                    "series5": ",".join(map(str, series[:5])),
                })
                continue

            kept.append({
                "player": rec.get("name") or "",
                "position": rec.get("position_tag") or "",
                "team": team_name,
                "fixture": f"{home_name} vs {away_name}",
                "kickoff": kickoff.strftime("%Y-%m-%d %H:%M:%S"),
                "price": price,
                "team_ml": team_ml,
                "tier": tier,
                "series5": series[:5],
                "series10": series[:10],
            })

    print(f"\nCandidates kept after filters: {len(kept)}")
    if not kept:
        print("\nNo SOT value singles found.")
    else:
        print("\n===== SOT VALUE SINGLES =====")
        # rank: higher tier precedence then price desc then name
        tier_rank = {"5/5": 2, "7/10": 1}
        kept.sort(key=lambda x: (tier_rank.get(x["tier"], 0), x["price"]), reverse=True)
        for x in kept:
            ser5 = ",".join(map(str, x["series5"]))
            pos = f"[{x['position']}]" if x["position"] else ""
            print(
                f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | "
                f"Over 0.5 SOT @ {x['price']:.3f} | Team ML {x['team_ml']:.3f} | tier {x['tier']} | last5: {ser5}"
            )

    if NEAR_MISS_LIMIT and near_misses:
        print("\n-- Near misses (top likely) --")
        # priced first (higher price first), then underdog/no-ml/no-price reasons
        def nm_key(r):
            priced = r["price"] is not None
            return (1 if priced else 0, r["price"] or 0.0)
        near_misses.sort(key=nm_key, reverse=True)
        for r in near_misses[:NEAR_MISS_LIMIT]:
            price = f"{r['price']:.3f}" if isinstance(r["price"], (int, float)) else "—"
            ml = f"{r['team_ml']:.3f}" if isinstance(r["team_ml"], (int, float)) else "—"
            print(
                f"   · [{r['reason']}] {r['player']} — {r['team']} | {r['fixture']} @ {r['kickoff']} | "
                f"price={price} | ML={ml} | tier={r.get('tier') or '—'} | last5:{r['series5']}"
            )

    print(f"\nRun timestamp (UTC): {utc_now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Params: LEAGUE_IDS='{LEAGUE_IDS_ENV}' MIN_DEC_PRICE={MIN_DEC_PRICE} "
          f"TEAM_UNDERDOG_MAX={TEAM_UNDERDOG_MAX} WINDOW_DAYS={WINDOW_DAYS}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
