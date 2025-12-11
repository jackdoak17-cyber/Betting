#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Player Stats V3 — Doubles (shots)

Pairs two low-price shot legs (>=1.25) into doubles priced >=1.8 using
form- and opponent-driven criteria.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(".")
PS_DIR = ROOT / "data" / "player_shots" / "by_league"
SOT_DIR = ROOT / "data" / "player_shots_on_target" / "by_league"
FIX_DIR = ROOT / "data" / "fixtures"
ODDS_DIR = ROOT / "data" / "odds" / "b365"
RANK_DIR = ROOT / "data" / "value_bets" / "Player_value_bets_V2" / "team_shot_ranks"
OUT_DIR = ROOT / "data" / "value_bets" / "V3 Bets" / "V3 player doubles"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "player_stats_v3_doubles.txt"
SHEET_DIR = ROOT / "data" / "value_bets" / "sheets"
SHEET_DIR.mkdir(parents=True, exist_ok=True)
SHEET_FILE = SHEET_DIR / "player_stats_v3_doubles.csv"

SHEET_HEADERS = [
    "entry_type",
    "combo_id",
    "leg_index",
    "league_id",
    "league_name",
    "fixture_id",
    "fixture",
    "date",
    "ko_time",
    "home_team",
    "away_team",
    "team",
    "opponent",
    "home_away",
    "team_id",
    "opp_id",
    "player",
    "player_id",
    "position",
    "position_bucket",
    "market",
    "line",
    "stat_type",
    "criteria",
    "bookmaker",
    "odds_taken",
    "closing_odds",
    "clv",
    "stake",
    "hit_rate",
    "average",
    "last_sequence",
    "team_ml",
    "opp_ml",
    "team_shot_line",
    "opponent_rank",
    "result",
    "actual_stat",
    "minutes",
    "starter",
    "final_score",
    "created_at",
    "updated_at",
    "settled_at",
    "profit",
    "running_profit",
    "combo_price",
    "combo_result",
    "leg1_player",
    "leg1_player_id",
    "leg1_fixture_id",
    "leg1_league_id",
    "leg1_market",
    "leg1_line",
    "leg1_stat_type",
    "leg2_player",
    "leg2_player_id",
    "leg2_fixture_id",
    "leg2_league_id",
    "leg2_market",
    "leg2_line",
    "leg2_stat_type",
]

DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS: List[int] = [
    int(x)
    for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",")
    if x.strip()
]

MIN_LEG_PRICE = float(os.getenv("MIN_LEG_PRICE", "1.25"))
MIN_COMBO_PRICE = float(os.getenv("MIN_COMBO_PRICE", "1.8"))
DEBUG_DROPS = bool(int(os.getenv("DEBUG_DROPS", "0")))

# Known league team counts to correct truncated rank files
LEAGUE_TEAM_COUNTS = {
    384: 20,  # Serie A should list 20 teams even if source is short
}


# ---- IO helpers ------------------------------------------------------------

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


def load_sheet(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


def save_sheet(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SHEET_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow({h: row.get(h, "") for h in SHEET_HEADERS})


def load_fixture_odds(fixture_id: int) -> List[dict]:
    blob = _load_json(ODDS_DIR / "fixtures" / f"{fixture_id}.json") or {}
    rows = blob.get("odds") or []
    return rows if isinstance(rows, list) else []


# ---- string helpers --------------------------------------------------------
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


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^\w\s\.-]", " ", s)
    return norm_spaces(s)


def team_tokens(name: str) -> set:
    return {t for t in norm(name).split() if t and t not in GENERIC_TOK}


def team_names_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb:
        return False
    return bool(ta & tb)


def split_name_tokens(name: str) -> List[str]:
    parts = [norm(p) for p in re.split(r"[\s\.-]+", name or "") if norm(p)]
    return [p for p in parts if p not in GENERIC_TOK]


def drop_suffixes(parts: List[str]) -> List[str]:
    while parts and parts[-1] in SUFFIXES:
        parts = parts[:-1]
    return parts


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

    surnames = surname_tokens(parts)
    initials = first_initial(parts)
    variants = {" ".join(parts)}
    if surnames:
        variants.add(" ".join(surnames))
    if initials and surnames:
        variants.add(f"{initials} {' '.join(surnames)}")
    return [v for v in variants if v]


def aliases_from_record(rec: dict) -> List[str]:
    aliases = []
    for key in ("name", "player", "player_name", "display_name", "label"):
        val = rec.get(key)
        if isinstance(val, str) and val.strip():
            aliases.extend(name_variants(val))
    return [norm(a) for a in aliases if a]


def label_matches_aliases(label: str, aliases: Iterable[str]) -> bool:
    lbl = norm(label)
    parts = split_name_tokens(label)
    surn = " ".join(surname_tokens(parts)) if parts else ""
    for alias in aliases or []:
        if not alias:
            continue
        if lbl == alias or lbl.replace(" ", "") == alias.replace(" ", ""):
            return True
        if surn and surn == alias:
            return True
        if alias in lbl and len(alias) >= 3:
            return True
    return False


# ---- odds helpers ---------------------------------------------------------

def _line_matches(row: dict, target_label: str) -> bool:
    """Check that an odds row matches the intended line (e.g., 0.5 shots).

    The previous substring check could match "10.5" when we asked for "0.5",
    pulling in wildly priced alts. Use numeric comparisons first, then a
    bounded regex on the text fallback so we only accept exact line mentions.
    """

    target_val = _as_float(target_label)

    total_val = _as_float(row.get("total"))
    if total_val is not None and target_val is not None and math.isclose(total_val, target_val, abs_tol=1e-6):
        return True

    label_str = str(row.get("label") or "").strip()
    label_val = _as_float(label_str)
    if label_val is not None and target_val is not None and math.isclose(label_val, target_val, abs_tol=1e-6):
        return True

    blob = " ".join(
        str(row.get(k, ""))
        for k in (
            "label",
            "name",
            "original_label",
            "description",
            "header",
            "market_description",
            "outcome_name",
        )
    ).lower()
    norm_blob = blob.replace(",", ".")
    pattern = rf"(?<!\d){re.escape(target_label)}(?!\d)"
    if re.search(pattern, norm_blob):
        return True

    return not label_str and total_val is None and target_val is None


def _row_text(row: dict) -> str:
    fields = [
        "label",
        "name",
        "original_label",
        "description",
        "header",
        "market_description",
        "outcome_name",
    ]
    return " ".join(str(row.get(f, "")) for f in fields).lower()


def _is_over_row(row: dict) -> Optional[bool]:
    txt = _row_text(row)
    if re.search(r"\bunder\b", txt):
        return False
    if re.search(r"\bover\b", txt):
        return True
    if "+" in txt and any(tag in txt for tag in ["0.5", "1.5", "2.5", "3.5", "0,5", "1,5", "2,5", "3,5"]):
        return True
    return None


def best_price_for_line(odds_rows: List[dict], player_rec: dict, market_id: int, target_label: str) -> Optional[float]:
    aliases = aliases_from_record(player_rec)
    over_prices: List[float] = []
    ambiguous: List[float] = []

    for row in odds_rows or []:
        if int(row.get("market_id") or 0) != market_id:
            continue
        if not _line_matches(row, target_label):
            continue
        name_fields = [
            row.get("name", ""),
            row.get("total", ""),
            row.get("original_label", ""),
            row.get("description", ""),
            row.get("header", ""),
        ]
        if not any(label_matches_aliases(str(nf), aliases) for nf in name_fields):
            continue

        flag = _is_over_row(row)
        price = _as_float(row.get("value"))
        if price is None:
            continue
        # Guard against nonsensical mis-parsed prices (e.g., 13.0 for O0.5 shots).
        if target_label in {"0.5", "0,5"} and price > 6:
            continue
        if price <= 1.0:
            continue
        if flag is True:
            over_prices.append(price)
        elif flag is None:
            ambiguous.append(price)

    if over_prices:
        return min(over_prices)
    if ambiguous:
        return min(ambiguous)
    return None


def extract_team_ml_prices(odds_rows: List[dict], home_name: str, away_name: str) -> Tuple[Optional[float], Optional[float]]:
    home_price: Optional[float] = None
    away_price: Optional[float] = None
    for row in odds_rows or []:
        if int(row.get("market_id") or 0) != 1:
            continue
        name = row.get("name") or ""
        price = _as_float(row.get("value"))
        if price is None:
            continue
        label = (row.get("label") or row.get("original_label") or "").strip().lower()
        sort_order = int(row.get("sort_order") or 0)

        if team_names_match(name, home_name) or label == "home" or label == "1" or sort_order == 1:
            home_price = price
        elif team_names_match(name, away_name) or label == "away" or label == "2" or sort_order == 2:
            away_price = price
    return home_price, away_price


# ---- data loading ---------------------------------------------------------

def fixture_map(league_id: int) -> Dict[int, dict]:
    data = _load_json(FIX_DIR / f"{league_id}.json") or {}
    fmap: Dict[int, dict] = {}
    for fx in data.get("fixtures") or []:
        fid = int(fx.get("id") or 0)
        teams = fx.get("participants") or []
        home: Optional[dict] = None
        away: Optional[dict] = None
        for t in teams:
            loc = (t.get("meta") or {}).get("location")
            if loc == "home" and home is None:
                home = t
            elif loc == "away" and away is None:
                away = t
        if not home and teams:
            home = teams[0]
        if not away and len(teams) >= 2:
            away = teams[1]
        if not home or not away:
            continue
        fmap[fid] = {
            "fixture_id": fid,
            "home_id": int(home.get("id") or 0),
            "away_id": int(away.get("id") or 0),
            "home_name": home.get("name") or home.get("short_code") or "Home",
            "away_name": away.get("name") or away.get("short_code") or "Away",
            "name": fx.get("name") or "",
            "starting_at": fx.get("starting_at") or "",
        }
    return fmap


def load_rank_info(
    league_id: int,
    fixture_meta: Optional[Dict[int, dict]] = None,
    players_by_team: Optional[Dict[int, List[dict]]] = None,
) -> dict:
    """Load stingy opponent ranks and recover the full team count.

    Some rank files ship with a truncated team_count (e.g., 16 instead of the
    league's 20). Use fixture teams and player team IDs as fallbacks so we
    describe ranks against the right denominator in writeups.
    """

    blob = _load_json(RANK_DIR / f"{league_id}.json") or {}
    stingy: Dict[int, int] = {}
    names: Dict[int, str] = {}
    ranks = blob.get("shots_conceded_rank") or []
    for idx, row in enumerate(ranks):
        tid = int(row.get("team_id") or 0)
        if not tid:
            continue
        stingy[tid] = idx + 1
        names[tid] = row.get("team_name") or ""

    fixture_team_ids: set[int] = set()
    for meta in (fixture_meta or {}).values():
        for key in ("home_id", "away_id"):
            tid = int(meta.get(key) or 0)
            if tid:
                fixture_team_ids.add(tid)

    player_team_ids: set[int] = {int(k) for k in (players_by_team or {}).keys() if int(k)}

    team_count = max(
        len(ranks),
        int(blob.get("team_count") or 0),
        len(fixture_team_ids),
        len(player_team_ids),
        LEAGUE_TEAM_COUNTS.get(league_id, 0),
    )

    return {"stingy": stingy, "team_names": names, "team_count": team_count}


def load_player_data(league_id: int) -> Dict[int, List[dict]]:
    shots_blob = _load_json(PS_DIR / f"{league_id}.json") or {}

    players_by_team: Dict[int, List[dict]] = defaultdict(list)
    for rec in shots_blob.get("players") or []:
        pid = int(rec.get("player_id") or 0)
        team_id = int(rec.get("team_id") or 0)
        shots = [int(float(x)) if isinstance(x, (int, float, str)) and str(x).strip() else 0 for x in rec.get("shots_last_n") or []]
        players_by_team[team_id].append(
            {
                "player_id": pid,
                "team_id": team_id,
                "name": rec.get("name") or rec.get("player_name") or rec.get("player") or "",
                "position": (rec.get("position_tag") or rec.get("position") or rec.get("pos") or "").upper(),
                "shots": shots,
            }
        )
    return players_by_team


# ---- math helpers ---------------------------------------------------------

def take(series: List[int], n: int) -> List[int]:
    return list((series or [])[:n])


def hits(series: List[int], threshold: int, n: int) -> int:
    return sum(1 for v in take(series, n) if v >= threshold)


def avg(series: List[int], n: int) -> float:
    seq = take(series, n)
    return sum(seq) / len(seq) if seq else 0.0


def position_bucket(pos: str) -> str:
    p = (pos or "").upper()
    if p in {"ST", "FW", "FWD", "CF"}:
        return "ST"
    if p in {"LW", "RW", "WF", "W"}:
        return "WF"
    if p in {"AM", "CAM"}:
        return "AM"
    if p in {"CM", "MID", "MF", "RM", "LM"}:
        return "CM"
    if p in {"DM", "CDM"}:
        return "DM"
    if p in {"CB", "RCB", "LCB"}:
        return "CB"
    if p in {"WB", "RWB", "LWB"}:
        return "WB"
    return p or ""


def split_datetime(iso_str: str) -> Tuple[str, str]:
    if not iso_str:
        return "", ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except Exception:
        return "", ""


def blanked_two_in_row(series: List[int]) -> bool:
    recent = take(series, 2)
    return len(recent) >= 2 and recent[0] == 0 and recent[1] == 0


# ---- settlement helpers ---------------------------------------------------

def build_player_stat_index() -> Dict[int, Dict[int, Dict[int, dict]]]:
    index: Dict[int, Dict[int, Dict[int, dict]]] = defaultdict(lambda: defaultdict(dict))

    def upsert(lid: int, pid: int, fid: int, key: str, value):
        entry = index[lid][pid].setdefault(fid, {})
        entry[key] = value

    for path in PS_DIR.glob("*.json"):
        if not path.stem.isdigit():
            continue
        lid = int(path.stem)
        blob = _load_json(path) or {}
        for rec in blob.get("players") or []:
            pid = int(rec.get("player_id") or 0)
            fids = rec.get("fixture_ids") or []
            mins = rec.get("minutes_last_n") or []
            shots = rec.get("shots_last_n") or []
            for fid, s, m in zip(fids, shots, mins):
                try:
                    fid_int = int(fid)
                except Exception:
                    continue
                try:
                    s_val = int(float(s))
                except Exception:
                    s_val = None
                try:
                    m_val = int(float(m))
                except Exception:
                    m_val = None
                if s_val is not None:
                    upsert(lid, pid, fid_int, "shots", s_val)
                if m_val is not None:
                    upsert(lid, pid, fid_int, "minutes", m_val)

    for path in SOT_DIR.glob("*.json"):
        if not path.stem.isdigit():
            continue
        lid = int(path.stem)
        blob = _load_json(path) or {}
        for rec in blob.get("players") or []:
            pid = int(rec.get("player_id") or 0)
            fids = rec.get("fixture_ids") or []
            mins = rec.get("minutes_last_n") or []
            sots = rec.get("on_target_last_n") or []
            for fid, s, m in zip(fids, sots, mins):
                try:
                    fid_int = int(fid)
                except Exception:
                    continue
                try:
                    s_val = int(float(s))
                except Exception:
                    s_val = None
                try:
                    m_val = int(float(m))
                except Exception:
                    m_val = None
                if s_val is not None:
                    upsert(lid, pid, fid_int, "sot", s_val)
                if m_val is not None:
                    upsert(lid, pid, fid_int, "minutes", m_val)

    return index


def _settle_leg_row(row: dict, stat_index: Dict[int, Dict[int, Dict[int, dict]]]) -> None:
    if (row.get("result") or "").lower() in {"won", "lost", "push"}:
        return
    if row.get("entry_type") not in {"", None, "leg"}:
        return
    try:
        lid = int(row.get("league_id"))
        fid = int(row.get("fixture_id"))
        pid = int(row.get("player_id"))
    except Exception:
        return
    stat_type = (row.get("stat_type") or "shots").lower()
    rec = stat_index.get(lid, {}).get(pid, {}).get(fid)
    if not rec:
        return
    actual = rec.get("sot") if "sot" in stat_type else rec.get("shots")
    if actual is None:
        return
    row["actual_stat"] = str(actual)
    minutes = rec.get("minutes")
    if minutes is not None:
        row["minutes"] = str(minutes)
        row["starter"] = "Y" if minutes >= 60 else "N"
    try:
        line_val = float(row.get("line"))
    except Exception:
        return
    outcome = "won" if actual > line_val else "push" if actual == line_val else "lost"
    row["result"] = outcome
    try:
        price = float(row.get("odds_taken"))
    except Exception:
        price = None
    stake = 1.0
    profit: Optional[float] = None
    if outcome == "won" and price:
        profit = (price - 1.0) * stake
    elif outcome == "lost":
        profit = -stake
    elif outcome == "push":
        profit = 0.0
    if profit is not None:
        row["profit"] = f"{profit:.2f}"
    row["settled_at"] = datetime.utcnow().isoformat()


def _settle_double_row(row: dict, stat_index: Dict[int, Dict[int, Dict[int, dict]]]) -> None:
    if (row.get("combo_result") or row.get("result") or "").lower() in {"won", "lost", "push"}:
        return
    if row.get("entry_type") != "double":
        return

    def leg_outcome(prefix: str) -> Optional[str]:
        try:
            lid = int(row.get(f"{prefix}_league_id"))
            fid = int(row.get(f"{prefix}_fixture_id"))
            pid = int(row.get(f"{prefix}_player_id"))
            line_val = float(row.get(f"{prefix}_line"))
        except Exception:
            return None
        stat_type = (row.get(f"{prefix}_stat_type") or "shots").lower()
        rec = stat_index.get(lid, {}).get(pid, {}).get(fid)
        if not rec:
            return None
        actual = rec.get("sot") if "sot" in stat_type else rec.get("shots")
        if actual is None:
            return None
        row.setdefault(f"{prefix}_actual", str(actual))
        minutes = rec.get("minutes")
        if minutes is not None:
            row.setdefault(f"{prefix}_minutes", str(minutes))
        if actual > line_val:
            return "won"
        if actual == line_val:
            return "push"
        return "lost"

    leg1 = leg_outcome("leg1")
    leg2 = leg_outcome("leg2")
    if not leg1 or not leg2:
        return
    if "lost" in {leg1, leg2}:
        outcome = "lost"
    elif "push" in {leg1, leg2}:
        outcome = "push"
    else:
        outcome = "won"

    row["combo_result"] = outcome
    row["result"] = outcome
    try:
        price = float(row.get("combo_price") or row.get("odds_taken"))
    except Exception:
        price = None
    stake = 1.0
    profit: Optional[float] = None
    if outcome == "won" and price:
        profit = (price - 1.0) * stake
    elif outcome == "lost":
        profit = -stake
    elif outcome == "push":
        profit = 0.0
    if profit is not None:
        row["profit"] = f"{profit:.2f}"
    row["settled_at"] = datetime.utcnow().isoformat()


def recompute_running_profit(rows: List[dict]) -> None:
    total = 0.0
    for row in rows:
        try:
            p = float(row.get("profit"))
        except Exception:
            p = None
        if p is not None:
            total += p
        row["running_profit"] = f"{total:.2f}" if p is not None else row.get("running_profit", "")


def settle_sheet(rows: List[dict], stat_index: Dict[int, Dict[int, Dict[int, dict]]]) -> None:
    for row in rows:
        if row.get("entry_type") == "double":
            _settle_double_row(row, stat_index)
        else:
            _settle_leg_row(row, stat_index)
    recompute_running_profit(rows)


# ---- evaluation -----------------------------------------------------------

def build_leg(
    player: dict,
    team_name: str,
    opponent_name: str,
    team_id: int,
    opponent_id: int,
    fixture_name: str,
    fixture_id: int,
    league_id: int,
    starting_at: str,
    home_away: str,
    price: Optional[float],
    team_ml: Optional[float],
    opp_ml: Optional[float],
    opponent_rank: Optional[int],
    opponent_rank_total: Optional[int],
    criteria: str,
    seq: List[int],
    hit_desc: str,
) -> dict:
    date_str, ko_time = split_datetime(starting_at)
    avg_val = avg(seq, len(seq))
    seq_str = ",".join(str(x) for x in seq)
    pos = player.get("position") or ""
    pos_bucket = position_bucket(pos)
    writeup = [f"{player.get('name')} {hit_desc} = {seq_str}"]
    writeup.append(f"Over his last {len(seq)} games he's averaged {avg_val:.1f} shots per game")
    if opponent_rank is not None:
        if opponent_rank_total:
            writeup.append(
                f"Opponent shots conceded rank (1 = fewest allowed of {opponent_rank_total} teams): {opponent_rank}"
            )
        else:
            writeup.append(
                f"Opponent shots conceded rank (1 = fewest allowed): {opponent_rank}"
            )
    return {
        "player": player.get("name"),
        "player_id": player.get("player_id"),
        "position": pos,
        "position_bucket": pos_bucket,
        "team": team_name,
        "opponent": opponent_name,
        "team_id": team_id,
        "opp_id": opponent_id,
        "fixture": fixture_name,
        "fixture_id": fixture_id,
        "league_id": league_id,
        "starting_at": starting_at,
        "date": date_str,
        "ko_time": ko_time,
        "home_away": home_away,
        "market": "O0.5 shots",
        "line": "0.5",
        "stat_type": "shots",
        "price": price,
        "criteria": criteria,
        "team_ml": team_ml,
        "opp_ml": opp_ml,
        "opponent_rank": opponent_rank if opponent_rank is not None else "",
        "opponent_rank_total": opponent_rank_total if opponent_rank_total is not None else "",
        "hit_rate": f"{hits(seq, 1, len(seq))}/{len(seq)}" if seq else "0/0",
        "average": f"{avg_val:.2f}",
        "last_sequence": seq_str,
        "writeup": "\n".join(writeup),
    }


def evaluate_player(
    player: dict,
    odds_rows: List[dict],
    team_ml: Optional[float],
    opponent_ml: Optional[float],
    team_id: int,
    opponent_id: int,
    opponent_name: str,
    team_name: str,
    rank_info: dict,
    fixture_id: int,
    league_id: int,
    fixture_name: str,
    starting_at: str,
    home_away: str,
    form_candidates: Optional[List[dict]] = None,
) -> List[dict]:
    legs: List[dict] = []
    stingy_rank = rank_info.get("stingy", {}).get(opponent_id, 999)
    rank_total = rank_info.get("team_count") or None
    price_o05 = best_price_for_line(odds_rows, player, 268, "0.5")
    price_ok = price_o05 is not None and price_o05 >= MIN_LEG_PRICE

    shots = player.get("shots") or []
    pos_bucket = position_bucket(player.get("position") or "")

    seq10 = take(shots, 10)
    n10 = len(seq10)

    seen_key_parts = [player.get("id"), player.get("player_id"), norm(player.get("name") or ""), norm(team_name)]

    def add_leg(criteria: str, seq: List[int], hit_desc: str, opp_gate: int, ml_gate: float) -> None:
        if team_ml is None or team_ml > ml_gate:
            return
        if stingy_rank <= opp_gate:
            return
        leg = build_leg(
            player,
            team_name,
            opponent_name,
            team_id,
            opponent_id,
            fixture_name,
            fixture_id,
            league_id,
            starting_at,
            home_away,
            price_o05,
            team_ml,
            opponent_ml,
            stingy_rank if stingy_rank != 999 else None,
            rank_total,
            criteria,
            seq,
            hit_desc,
        )
        key = (
            next((k for k in seen_key_parts if k), norm(player.get("name") or "")),
            fixture_id,
            leg["market"],
        )
        if key in add_leg._seen_keys:
            return
        add_leg._seen_keys.add(key)
        if price_ok:
            legs.append(leg)
        elif form_candidates is not None:
            leg_copy = dict(leg)
            leg_copy["price_reason"] = "below_min" if price_o05 is not None else "missing"
            leg_copy["price_ok"] = False
            form_candidates.append(leg_copy)

    add_leg._seen_keys = set()  # type: ignore[attr-defined]

    # Criteria 1: 1+ shot in 10/10, ML<4, opp not top3, no CB
    if (
        n10 >= 10
        and hits(shots, 1, 10) >= 10
        and team_ml is not None
        and team_ml < 4
        and stingy_rank > 3
        and pos_bucket != "CB"
    ):
        add_leg("C1 (1+ shot 10/10)", seq10, "has had 1+ shot in 10 of his last 10", opp_gate=3, ml_gate=4)

    # Criteria 2: 1+ in 9/10, avg >1.3, 2+ at least 4, ML<3, opp not top3, no CB
    if (
        n10 >= 10
        and hits(shots, 1, 10) >= 9
        and avg(shots, 10) > 1.3
        and hits(shots, 2, 10) >= 4
        and team_ml is not None
        and team_ml < 3
        and stingy_rank > 3
        and pos_bucket != "CB"
    ):
        add_leg("C2 (1+ shot 9/10)", seq10, "has had 1+ shot in 9 of his last 10", opp_gate=3, ml_gate=3)

    # Criteria 3: 1+ in 8/10, avg >1.3, 2+ >=4, ML<3, opp not top8, no CB
    if (
        n10 >= 10
        and hits(shots, 1, 10) >= 8
        and avg(shots, 10) > 1.3
        and hits(shots, 2, 10) >= 4
        and team_ml is not None
        and team_ml < 3
        and stingy_rank > 8
        and pos_bucket != "CB"
    ):
        add_leg("C3 (1+ shot 8/10)", seq10, "has had 1+ shot in 8 of his last 10", opp_gate=8, ml_gate=3)

    # Criteria 4: 1+ in 7/10, avg >1.3, 2+ >=4, only forwards, ML<3, opp not top8, no blanks in last 2
    if (
        n10 >= 10
        and hits(shots, 1, 10) >= 7
        and avg(shots, 10) > 1.3
        and hits(shots, 2, 10) >= 4
        and team_ml is not None
        and team_ml < 3
        and stingy_rank > 8
        and pos_bucket in {"ST", "WF"}
        and not blanked_two_in_row(shots)
    ):
        add_leg("C4 (1+ shot 7/10 forwards)", seq10, "has had 1+ shot in 7 of his last 10", opp_gate=8, ml_gate=3)

    # Criteria 5: 1+ in 7/7, avg >1.3, 2+ >=4, only forwards, ML<3, opp not top8, no blanks in last 2
    seq7 = take(shots, 7)
    if (
        len(seq7) >= 7
        and hits(shots, 1, 7) >= 7
        and avg(shots, 7) > 1.3
        and hits(shots, 2, 7) >= 4
        and team_ml is not None
        and team_ml < 3
        and stingy_rank > 8
        and pos_bucket in {"ST", "WF"}
        and not blanked_two_in_row(shots)
    ):
        add_leg("C5 (1+ shot 7/7 forwards)", seq7, "has had 1+ shot in 7 of his last 7", opp_gate=8, ml_gate=3)

    # Criteria 6: n<=8, 1+ in 5/5, 2+ >=3, only forwards, ML<3, opp not top10, no blanks in last 2
    if len(shots) <= 8:
        seq5 = take(shots, 5)
        if (
            len(seq5) >= 5
            and hits(shots, 1, 5) >= 5
            and hits(shots, 2, 5) >= 3
            and team_ml is not None
            and team_ml < 3
            and stingy_rank > 10
            and pos_bucket in {"ST", "WF"}
            and pos_bucket != "CB"
            and not blanked_two_in_row(shots)
        ):
            add_leg("C6 (1+ shot 5/5 forwards)", seq5, "has had 1+ shot in 5 of his last 5", opp_gate=10, ml_gate=3)

    return legs


def process_league(league_id: int, form_candidates: Optional[List[dict]] = None, drop_reasons: Optional[Dict[str, int]] = None) -> List[dict]:
    def drop(reason: str, ctx: Optional[dict] = None):
        if drop_reasons is None:
            return
        drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
        if DEBUG_DROPS:
            print(f"[drop] {reason} :: {ctx or {}}")

    fmap = fixture_map(league_id)
    players_by_team = load_player_data(league_id)
    rank_info = load_rank_info(league_id, fixture_meta=fmap, players_by_team=players_by_team)

    odds_blob = _load_json(ODDS_DIR / f"{league_id}.json") or {}
    legs: List[dict] = []

    fixtures = odds_blob.get("fixtures") or []
    if not fixtures:
        drop("no league odds fixtures", {"league_id": league_id})
        return legs

    for fx in fixtures:
        fid = int(fx.get("fixture_id") or fx.get("id") or 0)
        meta = fmap.get(fid)
        if not meta:
            drop("fixture missing in fixtures json", {"league_id": league_id, "fixture_id": fid})
            continue
        fixture_name = meta.get("name") or f"{meta['home_name']} vs {meta['away_name']}"
        starting_at = meta.get("starting_at") or ""
        odds_rows = fx.get("odds") or []
        if not odds_rows:
            odds_rows = load_fixture_odds(fid)
        if not odds_rows:
            drop("missing odds rows", {"league_id": league_id, "fixture_id": fid, "fixture": fixture_name})
            continue
        home_ml, away_ml = extract_team_ml_prices(odds_rows, meta["home_name"], meta["away_name"])
        if home_ml is None or away_ml is None:
            drop("missing team ML", {"league_id": league_id, "fixture_id": fid, "fixture": fixture_name})

        for team_id, opp_id, team_name, opp_name, team_ml, opp_ml, home_away in [
            (meta["home_id"], meta["away_id"], meta["home_name"], meta["away_name"], home_ml, away_ml, "home"),
            (meta["away_id"], meta["home_id"], meta["away_name"], meta["home_name"], away_ml, home_ml, "away"),
        ]:
            for player in players_by_team.get(team_id, []):
                legs.extend(
                    evaluate_player(
                        player,
                        odds_rows,
                        team_ml,
                        opp_ml,
                        team_id,
                        opp_id,
                        opp_name,
                        team_name,
                        rank_info,
                        fid,
                        league_id,
                        fixture_name,
                        starting_at,
                        home_away,
                        form_candidates=form_candidates,
                    )
                )

    return legs


def generate_doubles(legs: List[dict]) -> List[dict]:
    doubles: List[dict] = []
    for a, b in combinations(legs, 2):
        if not a.get("price") or not b.get("price"):
            continue
        if a.get("player_id") == b.get("player_id"):
            continue
        combo_price = float(a["price"]) * float(b["price"])
        if combo_price < MIN_COMBO_PRICE:
            continue
        doubles.append(
            {
                "legs": (a, b),
                "price": combo_price,
                "criteria": f"{a['criteria']} + {b['criteria']}",
            }
        )
    doubles.sort(key=lambda r: r.get("price", 0), reverse=True)
    return doubles


def generate_near_misses(legs: List[dict], limit: int = 3) -> List[dict]:
    near_misses: List[dict] = []
    for a, b in combinations(legs, 2):
        if not a.get("price") or not b.get("price"):
            continue
        if a.get("player_id") == b.get("player_id"):
            continue
        combo_price = float(a["price"]) * float(b["price"])
        if combo_price >= MIN_COMBO_PRICE:
            continue
        near_misses.append(
            {
                "legs": (a, b),
                "price": combo_price,
                "criteria": f"{a['criteria']} + {b['criteria']}",
            }
        )

    near_misses.sort(key=lambda r: r.get("price", 0), reverse=True)
    return near_misses[:limit]


def best_form_candidates(candidates: List[dict], limit: Optional[int] = None) -> List[dict]:
    def score(leg: dict) -> Tuple[float, float, float]:
        try:
            hit_num, hit_den = (leg.get("hit_rate", "0/0") or "0/0").split("/")
            hit_rate = float(hit_num) / float(hit_den) if float(hit_den) else 0.0
        except Exception:
            hit_rate = 0.0
        try:
            avg_val = float(leg.get("average") or 0)
        except Exception:
            avg_val = 0.0
        try:
            price_val = float(leg.get("price") or 0)
        except Exception:
            price_val = 0.0
        return (hit_rate, avg_val, price_val)

    ranked = sorted(candidates or [], key=score, reverse=True)
    if limit is None:
        return ranked
    return ranked[:limit]


def make_combo_id(a: dict, b: dict) -> str:
    parts = sorted(
        [
            f"{a.get('league_id')}:{a.get('fixture_id')}:{norm(a.get('player') or '')}:{a.get('market')}",
            f"{b.get('league_id')}:{b.get('fixture_id')}:{norm(b.get('player') or '')}:{b.get('market')}",
        ]
    )
    return "|".join(parts)


def upsert_sheet(legs: List[dict], doubles: List[dict]) -> List[dict]:
    rows = load_sheet(SHEET_FILE)
    stat_index = build_player_stat_index()
    if rows:
        settle_sheet(rows, stat_index)

    leg_key_map = {
        (
            "leg",
            str(r.get("league_id")),
            str(r.get("fixture_id")),
            norm(r.get("player")),
            r.get("market"),
        ): r
        for r in rows
        if (r.get("entry_type") or "leg") == "leg"
    }

    combo_map = {r.get("combo_id"): r for r in rows if r.get("entry_type") == "double"}

    for leg in legs:
        key = (
            "leg",
            str(leg.get("league_id")),
            str(leg.get("fixture_id")),
            norm(leg.get("player")),
            leg.get("market"),
        )
        price_fmt = f"{leg.get('price', 0.0):.2f}" if leg.get("price") is not None else ""
        row = leg_key_map.get(key)
        if row:
            if not row.get("odds_taken"):
                row["odds_taken"] = price_fmt
            row["closing_odds"] = price_fmt or row.get("closing_odds", "")
            row["updated_at"] = datetime.utcnow().isoformat()
            row.setdefault("criteria", leg.get("criteria", ""))
            row.setdefault("hit_rate", leg.get("hit_rate", ""))
            row.setdefault("average", leg.get("average", ""))
            row.setdefault("last_sequence", leg.get("last_sequence", ""))
            row.setdefault("team_ml", leg.get("team_ml", ""))
            row.setdefault("opp_ml", leg.get("opp_ml", ""))
            row.setdefault("opponent_rank", leg.get("opponent_rank", ""))
            row.setdefault("position", leg.get("position", ""))
            row.setdefault("position_bucket", leg.get("position_bucket", ""))
        else:
            row = {h: "" for h in SHEET_HEADERS}
            row.update(
                {
                    "entry_type": "leg",
                    "league_id": leg.get("league_id", ""),
                    "fixture_id": leg.get("fixture_id", ""),
                    "fixture": leg.get("fixture", ""),
                    "date": leg.get("date", ""),
                    "ko_time": leg.get("ko_time", ""),
                    "home_team": leg.get("team") if leg.get("home_away") == "home" else leg.get("opponent"),
                    "away_team": leg.get("opponent") if leg.get("home_away") == "home" else leg.get("team"),
                    "team": leg.get("team", ""),
                    "opponent": leg.get("opponent", ""),
                    "home_away": leg.get("home_away", ""),
                    "team_id": leg.get("team_id", ""),
                    "opp_id": leg.get("opp_id", ""),
                    "player": leg.get("player", ""),
                    "player_id": leg.get("player_id", ""),
                    "position": leg.get("position", ""),
                    "position_bucket": leg.get("position_bucket", ""),
                    "market": leg.get("market", ""),
                    "line": leg.get("line", ""),
                    "stat_type": leg.get("stat_type", ""),
                    "criteria": leg.get("criteria", ""),
                    "bookmaker": leg.get("bookmaker", "bet365"),
                    "odds_taken": price_fmt,
                    "closing_odds": price_fmt,
                    "stake": "1",
                    "hit_rate": leg.get("hit_rate", ""),
                    "average": leg.get("average", ""),
                    "last_sequence": leg.get("last_sequence", ""),
                    "team_ml": leg.get("team_ml", ""),
                    "opp_ml": leg.get("opp_ml", ""),
                    "opponent_rank": leg.get("opponent_rank", ""),
                    "created_at": leg.get("created_at", datetime.utcnow().isoformat()),
                    "updated_at": leg.get("updated_at", datetime.utcnow().isoformat()),
                }
            )
            leg_key_map[key] = row
            rows.append(row)

    for combo in doubles:
        a, b = combo["legs"]
        combo_id = make_combo_id(a, b)
        price_fmt = f"{combo.get('price', 0.0):.2f}" if combo.get("price") is not None else ""
        row = combo_map.get(combo_id)
        if row:
            row["combo_price"] = price_fmt
            row["odds_taken"] = price_fmt or row.get("odds_taken", "")
            row["closing_odds"] = price_fmt or row.get("closing_odds", "")
            row["updated_at"] = datetime.utcnow().isoformat()
            row.setdefault("criteria", combo.get("criteria", ""))
        else:
            row = {h: "" for h in SHEET_HEADERS}
            row.update(
                {
                    "entry_type": "double",
                    "combo_id": combo_id,
                    "combo_price": price_fmt,
                    "odds_taken": price_fmt,
                    "closing_odds": price_fmt,
                    "stake": "1",
                    "criteria": combo.get("criteria", ""),
                    "leg1_player": a.get("player", ""),
                    "leg1_player_id": a.get("player_id", ""),
                    "leg1_fixture_id": a.get("fixture_id", ""),
                    "leg1_league_id": a.get("league_id", ""),
                    "leg1_market": a.get("market", ""),
                    "leg1_line": a.get("line", ""),
                    "leg1_stat_type": a.get("stat_type", ""),
                    "leg2_player": b.get("player", ""),
                    "leg2_player_id": b.get("player_id", ""),
                    "leg2_fixture_id": b.get("fixture_id", ""),
                    "leg2_league_id": b.get("league_id", ""),
                    "leg2_market": b.get("market", ""),
                    "leg2_line": b.get("line", ""),
                    "leg2_stat_type": b.get("stat_type", ""),
                    "created_at": datetime.utcnow().isoformat(),
                    "updated_at": datetime.utcnow().isoformat(),
                }
            )
            combo_map[combo_id] = row
            rows.append(row)

    settle_sheet(rows, stat_index)
    save_sheet(SHEET_FILE, rows)
    return rows


def format_opponent_rank(leg: dict) -> str:
    opp_rank = leg.get("opponent_rank")
    opp_total = leg.get("opponent_rank_total")
    if opp_rank in (None, ""):
        return "Opponent shots conceded rank: N/A"
    if opp_total not in (None, ""):
        return f"Opponent shots conceded rank (1 = fewest allowed of {opp_total} teams): {opp_rank}"
    return f"Opponent shots conceded rank (1 = fewest allowed): {opp_rank}"


def render_output(
    legs: List[dict],
    doubles: List[dict],
    near_misses: List[dict],
    fallback_form: List[dict],
    fallback_pairs: List[dict],
    drop_reasons: Dict[str, int],
) -> str:
    lines: List[str] = []
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    lines.append(f"Generated at (UTC): {ts}")
    lines.append(f"Min leg price: >= {MIN_LEG_PRICE:.2f} | Min double price: >= {MIN_COMBO_PRICE:.2f}")
    lines.append("Opponent gates: skip opps in top-3/top-8/top-10 stingiest as per criteria.")
    lines.append("")

    lines.append(f"Legs ({len(legs)}):")
    legs.sort(key=lambda r: (r.get("criteria", ""), r.get("team", ""), r.get("player", "")))
    for leg in legs:
        pos = leg.get("position") or leg.get("position_bucket") or ""
        pos_label = f" [{pos}]" if pos else ""
        try:
            price_str = f"{float(leg['price']):.2f}"
        except Exception:
            price_str = "N/A"
        lines.append(
            f"{leg['criteria']} — {leg['player']}{pos_label} ({leg['team']} vs {leg['opponent']}) {leg['market']} @ {price_str}"
        )
        lines.append(
            f"  Form: {leg['hit_rate']} | Avg: {leg['average']} | Seq: {leg['last_sequence']} | Team ML: {leg.get('team_ml', '')} | {format_opponent_rank(leg)}"
        )
        lines.append("")

    lines.append(f"Doubles ({len(doubles)}):")
    for combo in doubles:
        a, b = combo["legs"]
        lines.append(f"{combo['criteria']} — Combined @{combo['price']:.2f}")
        for leg in (a, b):
            pos = leg.get("position") or leg.get("position_bucket") or ""
            pos_label = f" [{pos}]" if pos else ""
            try:
                price_str = f"{float(leg['price']):.2f}"
            except Exception:
                price_str = "N/A"
            lines.append(
                f"  • {leg['player']}{pos_label} ({leg['team']} vs {leg['opponent']}) {leg['market']} @ {price_str} | {leg['criteria']}"
            )
        lines.append("")

    if not doubles and near_misses:
        lines.append("Near misses (top combos under minimum price):")
        for combo in near_misses:
            a, b = combo["legs"]
            lines.append(f"{combo['criteria']} — Combined @{combo['price']:.2f}")
            for leg in (a, b):
                pos = leg.get("position") or leg.get("position_bucket") or ""
                pos_label = f" [{pos}]" if pos else ""
                try:
                    price_str = f"{float(leg['price']):.2f}"
                except Exception:
                    price_str = "N/A"
                lines.append(
                    f"  • {leg['player']}{pos_label} ({leg['team']} vs {leg['opponent']}) {leg['market']} @ {price_str} | {leg['criteria']}"
                )
            lines.append("")

    if not legs and fallback_form:
        lines.append("Under-min price legs (criteria met, price < min or unavailable):")
        for leg in fallback_form:
            pos = leg.get("position") or leg.get("position_bucket") or ""
            pos_label = f" [{pos}]" if pos else ""
            try:
                price_str = f"{float(leg['price']):.2f}" if leg.get("price") is not None else "N/A"
            except Exception:
                price_str = "N/A"
            lines.append(
                f"{leg['criteria']} — {leg['player']}{pos_label} ({leg['team']} vs {leg['opponent']}) {leg['market']} @ {price_str}"
            )
            lines.append(
                f"  Form: {leg['hit_rate']} | Avg: {leg['average']} | Seq: {leg['last_sequence']} | Team ML: {leg.get('team_ml', '')} | {format_opponent_rank(leg)}"
            )
            lines.append("")

    if not legs and fallback_pairs:
        lines.append("Suggested doubles from under-min legs (aiming for >= 1.80):")
        for combo in fallback_pairs:
            a, b = combo["legs"]
            lines.append(f"{combo['criteria']} — Combined @{combo['price']:.2f}")
            for leg in (a, b):
                pos = leg.get("position") or leg.get("position_bucket") or ""
                pos_label = f" [{pos}]" if pos else ""
                try:
                    price_str = f"{float(leg['price']):.2f}" if leg.get("price") is not None else "N/A"
                except Exception:
                    price_str = "N/A"
                lines.append(
                    f"  • {leg['player']}{pos_label} ({leg['team']} vs {leg['opponent']}) {leg['market']} @ {price_str} | {leg['criteria']}"
                )
            lines.append("")

    if drop_reasons:
        lines.append("Drop summary (by reason):")
        for k, v in sorted(drop_reasons.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {k}: {v}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    all_legs: List[dict] = []
    form_candidates: List[dict] = []
    drop_reasons: Dict[str, int] = {}
    for lid in LEAGUE_IDS:
        all_legs.extend(process_league(lid, form_candidates, drop_reasons))

    doubles = generate_doubles(all_legs)
    near_misses = generate_near_misses(all_legs) if not doubles else []
    fallback_form: List[dict] = []
    fallback_pairs: List[dict] = []
    if not all_legs and form_candidates:
        under_min = []
        for leg in form_candidates:
            try:
                price_val = float(leg.get("price")) if leg.get("price") is not None else None
            except Exception:
                price_val = None
            if price_val is not None and price_val < MIN_LEG_PRICE:
                under_min.append(leg)
        fallback_form = best_form_candidates(under_min or form_candidates)
        if under_min:
            fallback_pairs = generate_doubles(under_min)
    upsert_sheet(all_legs, doubles)
    output = render_output(all_legs, doubles, near_misses, fallback_form, fallback_pairs, drop_reasons)
    OUT_FILE.write_text(output, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
