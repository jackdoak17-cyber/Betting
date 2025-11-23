#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Player Value Bets V2 — Passes with possession-aware filters.

Flow
- Use team possession ranks (computed via compute_team_possession_ranks.py) to
  identify top-5 and top-10 possession sides per league.
- Remove historical games played against top-5 possession opponents when
  calculating hit-rates.
- Flag players who clear their pass line in 5/5 or 7/10 (after filtering),
  price >= MIN_DEC_PRICE, and upcoming opponent is not possession-elite
  (block vs top-5, and block away vs top-10).

Inputs
- data/player_passes/by_league/{league_id}.json
- data/fixtures/{league_id}.json                      (for participants & ids)
- data/odds/b365/{league_id}.json                     (Bet365 odds)
- data/value_bets/Player_value_bets_V2/team_possession_ranks/{league_id}.json

Output
- data/value_bets/Player_value_bets_V2/passes_possession_value_v2.txt

Env (optional)
- MIN_DEC_PRICE  default 1.72
- LEAGUE_IDS     comma-separated list; defaults to standard set
"""
import datetime as dt
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.72"))
DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS: List[int] = [
    int(x)
    for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",")
    if x.strip()
]

ROOT = Path(".")
PP_DIR = ROOT / "data" / "player_passes" / "by_league"
FIX_DIR = ROOT / "data" / "fixtures"
ODDS_DIR = ROOT / "data" / "odds" / "b365"
RANK_DIR = ROOT / "data" / "value_bets" / "Player_value_bets_V2" / "team_possession_ranks"
OUT_DIR = ROOT / "data" / "value_bets" / "Player_value_bets_V2"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "passes_possession_value_v2.txt"

MARKET_PASSES = 290

SUFFIXES = {"jr", "junior", "sr", "senior", "ii", "iii", "iv", "filho", "neto"}
SURNAME_PREFIXES = {"da", "de", "del", "der", "di", "dos", "du", "la", "le", "van", "von", "bin", "al"}
GENERIC_TOK = {
    "fc",
    "cf",
    "afc",
    "sc",
    "cd",
    "ud",
    "ac",
    "as",
    "ss",
    "ssc",
    "us",
    "uc",
    "rc",
    "rcd",
    "ca",
    "the",
    "club",
    "de",
    "del",
    "la",
    "las",
    "los",
    "calcio",
    "united",
    "city",
    "saint",
    "st",
    "bk",
}


def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _as_float(x) -> Optional[float]:
    try:
        return float(str(x))
    except Exception:
        return None


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
    for k in ("name", "player_name", "player", "short_name", "common_name", "display_name", "full_name", "known_as"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            names.append(v.strip())
    seen, uniq = set(), []
    for n in names:
        key = norm(n)
        if key not in seen:
            seen.add(key)
            uniq.append(n)
    out: List[str] = []
    for n in uniq:
        out.extend(name_variants(n))
    seen2, uniq2 = set(), []
    for a in out:
        if a not in seen2:
            seen2.add(a)
            uniq2.append(a)
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


def team_tokens(name: str):
    return {t for t in norm(name).split() if t not in GENERIC_TOK}


def team_names_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta):
        return True
    inter = ta & tb
    union = ta | tb
    return (len(inter) / max(1, len(union)) >= 0.5) or (len(inter) >= 2)


def parse_fixture_teams(fixture_name: str) -> Tuple[str, str]:
    if not fixture_name:
        return "", ""
    for sep in [" vs ", " v ", " VS ", " Vs "]:
        if sep in fixture_name:
            a, b = fixture_name.split(sep, 1)
            return a.strip(), b.strip()
    if " - " in fixture_name:
        a, b = fixture_name.split(" - ", 1)
        return a.strip(), b.strip()
    return "", ""


# ---------- Fixtures & ranks ----------

def fixture_map_for_league(league_id: int) -> Dict[int, dict]:
    blob = _load_json(FIX_DIR / f"{league_id}.json") or {}
    out: Dict[int, dict] = {}
    for fx in blob.get("fixtures", []) or []:
        fid = fx.get("id")
        if not isinstance(fid, int):
            continue
        home_id = away_id = None
        home_name = away_name = ""
        for p in fx.get("participants", []) or []:
            pid = p.get("id")
            loc = (p.get("meta") or {}).get("location")
            name = p.get("name") or ""
            if loc == "home":
                home_id, home_name = pid, name
            elif loc == "away":
                away_id, away_name = pid, name
        out[fid] = {
            "home_id": home_id,
            "away_id": away_id,
            "home_name": home_name,
            "away_name": away_name,
        }
    return out


def possession_ranks(league_id: int) -> Tuple[List[int], List[int]]:
    blob = _load_json(RANK_DIR / f"{league_id}.json") or {}
    rows = blob.get("possession_rank") or []
    top5 = [r.get("team_id") for r in rows[:5] if isinstance(r.get("team_id"), int)]
    top10 = [r.get("team_id") for r in rows[:10] if isinstance(r.get("team_id"), int)]
    return top5, top10


def opponent_for_team(fixture: dict, team_id: int) -> Optional[int]:
    if not fixture:
        return None
    if team_id == fixture.get("home_id"):
        return fixture.get("away_id")
    if team_id == fixture.get("away_id"):
        return fixture.get("home_id")
    return None


def filter_series_vs_top5(series: List[int], fixture_ids: List[int], fixture_map: Dict[int, dict], team_id: int, top5_ids: List[int]) -> List[int]:
    out: List[int] = []
    for val, fid in zip(series, fixture_ids):
        if not isinstance(fid, int):
            continue
        opp = opponent_for_team(fixture_map.get(fid) or {}, team_id)
        if opp is None:
            continue
        if opp in top5_ids:
            continue
        if isinstance(val, (int, float)):
            out.append(int(val))
    return out


# ---------- Odds helpers ----------

def over_rows_for_player(odds_rows: List[dict], player_rec: dict) -> List[dict]:
    aliases = aliases_from_record(player_rec)
    out: List[dict] = []
    for r in odds_rows or []:
        if r.get("bookmaker_id") != 2:
            continue
        try:
            if int(r.get("market_id", 0)) != int(MARKET_PASSES):
                continue
        except Exception:
            continue

        lab = (r.get("label") or r.get("outcome") or "").strip().lower()
        if not re.search(r"\bover\b", lab):
            continue

        cand_fields = [
            r.get("name", ""),
            r.get("original_label", ""),
            r.get("outcome_name", ""),
            r.get("header", ""),
            r.get("description", ""),
        ]
        if not any(cf and label_matches_aliases(str(cf), aliases) for cf in cand_fields):
            continue

        t = _as_float(r.get("total"))
        v = _as_float(r.get("value"))
        if (t is None) or (v is None):
            continue

        out.append(r)
    return out


# ---------- Hit-rate checks ----------

def qualifies_5of5(series: List[int], line_total: float) -> Tuple[bool, List[int]]:
    usable = [int(x) for x in series if isinstance(x, (int, float))][:5]
    if len(usable) < 5:
        return False, usable
    return all(x >= line_total for x in usable), usable


def qualifies_7of10(series: List[int], line_total: float) -> Tuple[bool, List[int], int]:
    usable = [int(x) for x in series if isinstance(x, (int, float))][:10]
    if len(usable) < 10:
        return False, usable, 0
    hits = sum(1 for x in usable if x >= line_total)
    return hits >= 7, usable, hits


# ---------- Main ----------

def main() -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    header = [
        f"Generated at (UTC): {now}",
        f"Criteria: Over line in 5/5 or 7/10 (filtered vs top-5 possession) | Min price ≥ {MIN_PRICE:.2f}",
        "Blocks: skip upcoming vs top-5 possession; skip away vs top-10 possession",
        "Market: Bet365 Player Passes (Over)",
        "",
    ]

    picks: List[dict] = []

    for lid in LEAGUE_IDS:
        pp_blob = _load_json(PP_DIR / f"{lid}.json") or {}
        players = pp_blob.get("players") or pp_blob.get("rows") or pp_blob.get("data") or []
        odds_blob = _load_json(ODDS_DIR / f"{lid}.json") or {}
        fixtures = odds_blob.get("fixtures") or []
        fx_map = fixture_map_for_league(lid)
        top5_ids, top10_ids = possession_ranks(lid)

        for fx in fixtures:
            fname = fx.get("name") or ""
            fixture_id = fx.get("fixture_id")
            odds_rows = fx.get("odds") or []

            fx_info = fx_map.get(fixture_id) or {}
            home_id = fx_info.get("home_id")
            away_id = fx_info.get("away_id")
            if home_id is None or away_id is None:
                home_name, away_name = parse_fixture_teams(fname)
            else:
                home_name, away_name = fx_info.get("home_name"), fx_info.get("away_name")

            for rec in players:
                team_id = rec.get("team_id")
                if not isinstance(team_id, int):
                    continue

                if team_id == home_id:
                    side = "home"
                    opp_id = away_id
                elif team_id == away_id:
                    side = "away"
                    opp_id = home_id
                else:
                    # fallback to name match if ids missing
                    tname = rec.get("team_name") or rec.get("team") or ""
                    if tname and home_name and team_names_match(tname, home_name):
                        side = "home"
                        opp_id = away_id
                    elif tname and away_name and team_names_match(tname, away_name):
                        side = "away"
                        opp_id = home_id
                    else:
                        continue

                if opp_id is None:
                    continue

                if opp_id in top5_ids:
                    continue
                if side == "away" and opp_id in top10_ids:
                    continue

                series = rec.get("passes_last_n") or rec.get("series") or rec.get("passes") or []
                fixture_ids = rec.get("fixture_ids") or []
                filtered_series = filter_series_vs_top5(series, fixture_ids, fx_map, team_id, top5_ids)

                over_rows = over_rows_for_player(odds_rows, {"name": rec.get("name")})
                if not over_rows:
                    continue

                for r in over_rows:
                    price = _as_float(r.get("value"))
                    line = _as_float(r.get("total"))
                    if price is None or line is None:
                        continue
                    if price < MIN_PRICE:
                        continue

                    ok5, used5 = qualifies_5of5(filtered_series, line)
                    ok7, used10, hits10 = qualifies_7of10(filtered_series, line)
                    tag = "5/5" if ok5 else ("7/10" if ok7 else None)
                    if not tag:
                        continue

                    picks.append(
                        {
                            "league_id": lid,
                            "player": rec.get("name"),
                            "team_id": team_id,
                            "fixture": fname,
                            "side": side,
                            "opponent_id": opp_id,
                            "line": float(line),
                            "price": float(price),
                            "tag": tag,
                            "series": filtered_series[:10],
                            "used": used5 if ok5 else used10,
                            "hits": 5 if ok5 else hits10,
                        }
                    )

    picks.sort(key=lambda x: (-x["price"], x["player"].lower()))

    lines: List[str] = header
    if not picks:
        lines.append("No qualifying picks found.")
    else:
        for p in picks:
            lines.append(
                f"L{p['league_id']} | {p['player']} | {p['fixture']} | line {p['line']:.1f} @ {p['price']:.2f} | {p['tag']} | used={p['used']} hits={p['hits']}"
            )

    OUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT_FILE} ({len(picks)} picks)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
