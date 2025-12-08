#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Player Stats V3 — Long shots (higher-price shots/SOT props)
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
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(".")
PS_DIR = ROOT / "data" / "player_shots" / "by_league"
SOT_DIR = ROOT / "data" / "player_shots_on_target" / "by_league"
FIX_DIR = ROOT / "data" / "fixtures"
ODDS_DIR = ROOT / "data" / "odds" / "b365"
RANK_DIR = ROOT / "data" / "value_bets" / "Player_value_bets_V2" / "team_shot_ranks"
OUT_DIR = ROOT / "data" / "value_bets" / "V3 Bets" / "V3 long shots"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "player_stats_v3_long_shots.txt"
SHEET_DIR = ROOT / "data" / "value_bets" / "sheets"
SHEET_DIR.mkdir(parents=True, exist_ok=True)
SHEET_FILE = SHEET_DIR / "player_stats_v3_long_shots.csv"

SHEET_HEADERS = [
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
]

DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS: List[int] = [
    int(x)
    for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",")
    if x.strip()
]

GENERIC_TOK = {
    "fc",
    "cf",
    "calcio",
    "ac",
    "as",
    "bsc",
    "sc",
    "cd",
    "sd",
    "ud",
    "ss",
    "ssc",
    "us",
    "uc",
    "rc",
    "rcd",
    "ca",
    "club",
    "the",
    "del",
    "de",
    "la",
    "las",
    "los",
    "united",
    "city",
    "saint",
    "st",
    "bk",
}

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "vi"}

LEAGUE_TEAM_COUNTS = {384: 20}


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
    rows: List[dict] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def save_sheet(path: Path, rows: List[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SHEET_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


# ---- name helpers ---------------------------------------------------------

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
    if ta == tb or ta.issubset(tb) or tb.issubset(ta):
        return True
    inter = ta & tb
    union = ta | tb
    return (len(inter) / max(1, len(union)) >= 0.5) or (len(inter) >= 2)


def cleanup_label(label: str) -> str:
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()


def person_part_from_option(label: str) -> str:
    s = cleanup_label(label or "")
    parts = re.split(r"\b(?:-?\s*over|-?\s*under|\s+o\/u|\s+o\d+|\s+u\d+)\b", s, flags=re.IGNORECASE)
    return parts[0].strip() if parts else s


def split_name_tokens(name: str) -> List[str]:
    return [p for p in norm(name).replace("-", " ").split() if p]


def drop_suffixes(parts: List[str]) -> List[str]:
    out = list(parts)
    while out and re.sub(r"[^\w]+", "", out[-1]).lower() in SUFFIXES:
        out = out[:-1]
    return out


def surname_tokens(parts: List[str]) -> List[str]:
    if not parts:
        return []
    if len(parts) <= 2:
        return parts[-1:]
    return parts[-2:]


def label_matches_aliases(label: str, aliases: Iterable[str]) -> bool:
    label_parts = split_name_tokens(person_part_from_option(label))
    if not label_parts:
        return False
    label_tokens = set(label_parts)
    label_surnames = surname_tokens(label_parts)
    for alias in aliases:
        alias_parts = split_name_tokens(alias)
        if not alias_parts:
            continue
        alias_tokens = set(alias_parts)
        if alias_tokens == label_tokens:
            return True
        if alias_tokens.issubset(label_tokens):
            return True
        if label_tokens.issubset(alias_tokens):
            return True
        if surname_tokens(alias_parts) and label_surnames:
            if surname_tokens(alias_parts) == label_surnames:
                return True
    return False


# ---- odds helpers ---------------------------------------------------------

def _is_over_row(row: dict) -> Optional[bool]:
    desc = (row.get("market_description") or "").lower()
    opt_name = (row.get("name") or row.get("label") or "").lower()
    header = (row.get("header") or "").lower()
    for text in (desc, opt_name, header):
        if "over" in text:
            return True
        if "under" in text:
            return False
    return None


def _line_matches_target(fields: List[str], target: float) -> bool:
    found_numeric = False
    for field in fields:
        if field is None:
            continue
        if isinstance(field, (int, float)):
            try:
                found_numeric = True
                if math.isclose(float(field), target, rel_tol=0, abs_tol=1e-6):
                    return True
            except Exception:
                continue
        text = str(field)
        try:
            found_numeric = True
            if math.isclose(float(text.replace(",", ".")), target, rel_tol=0, abs_tol=1e-6):
                return True
        except Exception:
            pass
        m = re.search(r"-?\d+(?:[\.,]\d+)?", text)
        if m:
            try:
                found_numeric = True
                if math.isclose(float(m.group(0).replace(",", ".")), target, rel_tol=0, abs_tol=1e-6):
                    return True
            except Exception:
                continue
    return False


def best_price_for_line(
    odds_by_market: Dict[int, List[dict]],
    player_rec: dict,
    market_ids: Iterable[int],
    target_label: str,
) -> Optional[float]:
    aliases = [player_rec.get("name") or ""]
    markets = {int(m) for m in (market_ids or [])}
    if not markets:
        return None
    try:
        target_val = float(str(target_label).replace(",", "."))
    except Exception:
        return None
    over_prices: List[float] = []
    ambiguous: List[float] = []

    for mid in markets:
        for row in odds_by_market.get(mid, []):
            line_fields = [
                row.get("line"),
                row.get("label"),
                row.get("original_label"),
                row.get("price"),
                row.get("header"),
                row.get("handicap"),
                row.get("description"),
            ]
            if not _line_matches_target(line_fields, target_val):
                continue
            name_fields = [
                row.get("name", ""),
                row.get("market", ""),
                row.get("market_description", ""),
                row.get("original_label", ""),
                row.get("description", ""),
                row.get("header", ""),
            ]
            if not any(label_matches_aliases(str(nf), aliases) for nf in name_fields):
                continue
            flag = _is_over_row(row)
            if flag is False:
                continue
            price = _as_float(row.get("value"))
            if price is None:
                continue
            if target_label in {"0.5", "0,5"} and price > 12:
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


def extract_team_ml_prices(
    odds_by_market: Dict[int, List[dict]], home_name: str, away_name: str
) -> Tuple[Optional[float], Optional[float]]:
    home_price: Optional[float] = None
    away_price: Optional[float] = None
    for row in odds_by_market.get(1, []):
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


def index_odds_by_market(odds_rows: List[dict]) -> Dict[int, List[dict]]:
    indexed: Dict[int, List[dict]] = defaultdict(list)
    for row in odds_rows or []:
        try:
            mid = int(row.get("market_id") or 0)
        except Exception:
            continue
        indexed[mid].append(row)
    return indexed


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
    sot_blob = _load_json(SOT_DIR / f"{league_id}.json") or {}
    sot_map = {int(r.get("player_id") or 0): r for r in sot_blob.get("players") or []}

    players_by_team: Dict[int, List[dict]] = defaultdict(list)
    for rec in shots_blob.get("players") or []:
        pid = int(rec.get("player_id") or 0)
        team_id = int(rec.get("team_id") or 0)
        shots = [int(float(x)) if isinstance(x, (int, float, str)) and str(x).strip() else 0 for x in rec.get("shots_last_n") or []]
        sot_series: List[int] = []
        if pid in sot_map:
            sot_series = [int(float(x)) if isinstance(x, (int, float, str)) and str(x).strip() else 0 for x in sot_map[pid].get("on_target_last_n") or []]
        players_by_team[team_id].append(
            {
                "player_id": pid,
                "team_id": team_id,
                "name": rec.get("name") or rec.get("player_name") or rec.get("player") or "",
                "position": (rec.get("position_tag") or rec.get("position") or rec.get("pos") or "").upper(),
                "shots": shots,
                "sot": sot_series,
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


def threshold_from_line(line: float) -> int:
    frac = line - math.floor(line)
    if frac > 1e-6:
        return math.floor(line) + 1
    return int(math.floor(line))


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
    if p in {"WB", "RWB", "LWB", "LB", "RB", "FB"}:
        return "WB"
    if p in {"DF", "DEF"}:
        return "CB"
    return p or ""


def is_defender(bucket: str) -> bool:
    return bucket in {"CB", "WB"}


def is_midfielder(bucket: str) -> bool:
    return bucket in {"DM", "CM", "AM"}


def split_datetime(starting_at: str) -> Tuple[str, str]:
    if not starting_at:
        return "", ""
    try:
        dt_obj = datetime.fromisoformat(starting_at.replace("Z", "+00:00"))
        return dt_obj.strftime("%Y-%m-%d"), dt_obj.strftime("%H:%M")
    except Exception:
        return "", ""


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


def settle_row(row: dict, stat_index: Dict[int, Dict[int, Dict[int, dict]]]) -> None:
    if (row.get("result") or "").lower() in {"won", "lost", "push"}:
        return
    try:
        lid = int(row.get("league_id") or 0)
        pid = int(row.get("player_id") or 0)
        fid = int(row.get("fixture_id") or 0)
    except Exception:
        return
    if not lid or not pid or not fid:
        return
    rec = stat_index.get(lid, {}).get(pid, {}).get(fid)
    if not rec:
        return
    stat_type = row.get("stat_type") or ""
    actual = rec.get("sot" if stat_type == "shots_on_target" else "shots")
    if actual is None:
        return
    try:
        line_val = float(row.get("line") or 0)
    except Exception:
        line_val = 0.0
    threshold = threshold_from_line(line_val)
    outcome = "push"
    if actual > threshold:
        outcome = "won"
    elif actual < threshold:
        outcome = "lost"
    row["actual_stat"] = str(actual)
    row["result"] = outcome
    row["settled_at"] = datetime.utcnow().isoformat()
    try:
        price = float(row.get("odds_taken") or row.get("closing_odds") or 0)
    except Exception:
        price = 0.0
    stake = 1.0
    profit = None
    if outcome == "won" and price:
        profit = (price - 1.0) * stake
    elif outcome == "lost":
        profit = -stake
    elif outcome == "push":
        profit = 0.0
    if profit is not None:
        row["profit"] = f"{profit:.2f}"


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
        settle_row(row, stat_index)
    recompute_running_profit(rows)


def upsert_sheet(picks: List[dict]) -> List[dict]:
    rows = load_sheet(SHEET_FILE)
    stat_index = build_player_stat_index()
    if rows:
        settle_sheet(rows, stat_index)

    key_map = {
        (
            str(r.get("league_id")),
            str(r.get("fixture_id")),
            norm(r.get("player")),
            r.get("market"),
        ): r
        for r in rows
    }

    for pick in picks:
        key = (
            str(pick.get("league_id")),
            str(pick.get("fixture_id")),
            norm(pick.get("player")),
            pick.get("market"),
        )
        row = key_map.get(key)
        price_fmt = f"{pick.get('price', 0.0):.2f}" if pick.get("price") is not None else ""
        if row:
            if not row.get("odds_taken"):
                row["odds_taken"] = price_fmt
            row["closing_odds"] = price_fmt or row.get("closing_odds", "")
            row["updated_at"] = datetime.utcnow().isoformat()
            row.setdefault("criteria", pick.get("criteria", ""))
            row.setdefault("hit_rate", pick.get("hit_rate", ""))
            row.setdefault("average", pick.get("average", ""))
            row.setdefault("last_sequence", pick.get("last_sequence", ""))
            row.setdefault("team_ml", pick.get("team_ml", ""))
            row.setdefault("opp_ml", pick.get("opp_ml", ""))
            row.setdefault("opponent_rank", pick.get("opponent_rank", ""))
            row.setdefault("position", pick.get("position", ""))
            row.setdefault("position_bucket", pick.get("position_bucket", ""))
        else:
            row = {h: "" for h in SHEET_HEADERS}
            row.update(
                {
                    "league_id": pick.get("league_id", ""),
                    "league_name": pick.get("league_name", ""),
                    "fixture_id": pick.get("fixture_id", ""),
                    "fixture": pick.get("fixture", ""),
                    "date": pick.get("date", ""),
                    "ko_time": pick.get("ko_time", ""),
                    "home_team": pick.get("team") if pick.get("home_away") == "home" else pick.get("opponent"),
                    "away_team": pick.get("opponent") if pick.get("home_away") == "home" else pick.get("team"),
                    "team": pick.get("team", ""),
                    "opponent": pick.get("opponent", ""),
                    "home_away": pick.get("home_away", ""),
                    "team_id": pick.get("team_id", ""),
                    "opp_id": pick.get("opp_id", ""),
                    "player": pick.get("player", ""),
                    "player_id": pick.get("player_id", ""),
                    "position": pick.get("position", ""),
                    "position_bucket": pick.get("position_bucket", ""),
                    "market": pick.get("market", ""),
                    "line": pick.get("line", ""),
                    "stat_type": pick.get("stat_type", ""),
                    "criteria": pick.get("criteria", ""),
                    "bookmaker": pick.get("bookmaker", "bet365"),
                    "odds_taken": price_fmt,
                    "closing_odds": price_fmt,
                    "stake": "1",
                    "hit_rate": pick.get("hit_rate", ""),
                    "average": pick.get("average", ""),
                    "last_sequence": pick.get("last_sequence", ""),
                    "team_ml": pick.get("team_ml", ""),
                    "opp_ml": pick.get("opp_ml", ""),
                    "team_shot_line": pick.get("team_shot_line", ""),
                    "opponent_rank": pick.get("opponent_rank", ""),
                    "created_at": datetime.utcnow().isoformat(),
                    "updated_at": datetime.utcnow().isoformat(),
                }
            )
            rows.append(row)
            key_map[key] = row

    save_sheet(SHEET_FILE, rows)
    return rows


# ---- evaluation -----------------------------------------------------------

def build_pick(
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
    market: str,
    line: float,
    stat_type: str,
    price: Optional[float],
    team_ml: Optional[float],
    opp_ml: Optional[float],
    opponent_rank: Optional[int],
    opponent_rank_total: Optional[int],
    criteria: str,
    seq: List[int],
    threshold: int,
) -> dict:
    date_str, ko_time = split_datetime(starting_at)
    avg_val = avg(seq, len(seq))
    seq_str = ",".join(str(x) for x in seq)
    pos = player.get("position") or ""
    pos_bucket = position_bucket(pos)
    stat_label = "shots on target" if stat_type == "shots_on_target" else "shots"
    writeup = [f"{player.get('name')} has had {threshold}+ {stat_label} in {hits(seq, threshold, len(seq))} of his last {len(seq)} = {seq_str}"]
    writeup.append(f"Over his last {len(seq)} games he's averaged {avg_val:.1f} {stat_label} per game")
    if team_ml is not None:
        writeup.append(f"Team ML: {team_ml}")
    if opponent_rank is not None:
        if opponent_rank_total:
            writeup.append(
                f"Opponent shots conceded rank (1 = fewest allowed of {opponent_rank_total} teams): {opponent_rank}"
            )
        else:
            writeup.append(f"Opponent shots conceded rank (1 = fewest allowed): {opponent_rank}")

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
        "market": market,
        "line": f"{line:.2f}",
        "stat_type": stat_type,
        "price": price,
        "criteria": criteria,
        "team_ml": team_ml,
        "opp_ml": opp_ml,
        "opponent_rank": opponent_rank if opponent_rank is not None else "",
        "opponent_rank_total": opponent_rank_total if opponent_rank_total is not None else "",
        "hit_rate": f"{hits(seq, threshold, len(seq))}/{len(seq)}" if seq else "0/0",
        "average": f"{avg_val:.2f}",
        "last_sequence": seq_str,
        "writeup": "\n".join(writeup),
    }


def hit_rate_value(hr: str) -> float:
    try:
        num, den = hr.split("/")
        return float(num) / max(1.0, float(den))
    except Exception:
        return 0.0


def evaluate_player(
    player: dict,
    odds_by_market: Dict[int, List[dict]],
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
    price_o05_sot = best_price_for_line(odds_by_market, player, (267, 284, 291), "0.5")
    price_o15_sot = best_price_for_line(odds_by_market, player, (267, 284, 291), "1.5")
    price_o15_shots = best_price_for_line(odds_by_market, player, (268, 285, 292), "1.5")

    shots = player.get("shots") or []
    sot = player.get("sot") or []
    pos_bucket = position_bucket(player.get("position") or "")

    seen_keys = set()

    def add_leg(
        criteria: str,
        market: str,
        line: float,
        stat_type: str,
        seq: List[int],
        price: Optional[float],
        threshold: int,
        min_price: float,
    ) -> None:
        key = (criteria, market)
        if key in seen_keys:
            return
        if team_ml is None or team_ml >= 4:
            return
        if stingy_rank <= 3:
            return
        if price is None:
            return
        pick = build_pick(
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
            market,
            line,
            stat_type,
            price,
            team_ml,
            opponent_ml,
            stingy_rank if stingy_rank != 999 else None,
            rank_total,
            criteria,
            seq,
            threshold,
        )
        seen_keys.add(key)
        if price >= min_price:
            legs.append(pick)
        elif form_candidates is not None:
            pick["price_ok"] = False
            pick["min_price"] = min_price
            form_candidates.append(pick)

    # Criteria 1: 1+ SOT in 4/5, price > 3, team ML < 4, no CB
    seq5_sot = take(sot, 5)
    if len(seq5_sot) >= 5 and hits(sot, 1, 5) >= 4:
        if not is_defender(pos_bucket):
            add_leg("LC1 (1+ SOT 4/5)", "O0.5 SOT", 0.5, "shots_on_target", seq5_sot, price_o05_sot, 1, 3.0)

    # Criteria 2: 1+ SOT in 6/10, price > 3, team ML < 4, no CB
    seq10_sot = take(sot, 10)
    if len(seq10_sot) >= 10 and hits(sot, 1, 10) >= 6:
        if not is_defender(pos_bucket):
            add_leg("LC2 (1+ SOT 6/10)", "O0.5 SOT", 0.5, "shots_on_target", seq10_sot, price_o05_sot, 1, 3.0)

    # Criteria 3: 2+ SOT in 5/10, price > 4, forwards only
    if len(seq10_sot) >= 10 and hits(sot, 2, 10) >= 5:
        if not is_defender(pos_bucket) and not is_midfielder(pos_bucket):
            add_leg("LC3 (2+ SOT 5/10 forwards)", "O1.5 SOT", 1.5, "shots_on_target", seq10_sot, price_o15_sot, 2, 4.0)

    # Criteria 4: 2+ SOT in 4/7, price > 4, forwards only
    seq7_sot = take(sot, 7)
    if len(seq7_sot) >= 7 and hits(sot, 2, 7) >= 4:
        if not is_defender(pos_bucket) and not is_midfielder(pos_bucket):
            add_leg("LC4 (2+ SOT 4/7 forwards)", "O1.5 SOT", 1.5, "shots_on_target", seq7_sot, price_o15_sot, 2, 4.0)

    # Criteria 5: 2+ shots in 4/5, price > 3, no CB
    seq5_shots = take(shots, 5)
    if len(seq5_shots) >= 5 and hits(shots, 2, 5) >= 4:
        if not is_defender(pos_bucket):
            add_leg("LC5 (2+ shots 4/5)", "O1.5 shots", 1.5, "shots", seq5_shots, price_o15_shots, 2, 3.0)

    # Criteria 6: 2+ shots in 6/10, price > 3, no CB
    seq10_shots = take(shots, 10)
    if len(seq10_shots) >= 10 and hits(shots, 2, 10) >= 6:
        if not is_defender(pos_bucket):
            add_leg("LC6 (2+ shots 6/10)", "O1.5 shots", 1.5, "shots", seq10_shots, price_o15_shots, 2, 3.0)

    # Criteria 7: 2+ shots in 5/10, price > 4, forwards only
    if len(seq10_shots) >= 10 and hits(shots, 2, 10) >= 5:
        if not is_defender(pos_bucket) and not is_midfielder(pos_bucket):
            add_leg("LC7 (2+ shots 5/10 forwards)", "O1.5 shots", 1.5, "shots", seq10_shots, price_o15_shots, 2, 3.0)

    # Criteria 8: 2+ shots in 4/7, price > 4, forwards only
    seq7_shots = take(shots, 7)
    if len(seq7_shots) >= 7 and hits(shots, 2, 7) >= 4:
        if not is_defender(pos_bucket) and not is_midfielder(pos_bucket):
            add_leg("LC8 (2+ shots 4/7 forwards)", "O1.5 shots", 1.5, "shots", seq7_shots, price_o15_shots, 2, 3.0)

    return legs


def process_league(league_id: int, form_candidates: Optional[List[dict]] = None) -> List[dict]:
    fmap = fixture_map(league_id)
    players_by_team = load_player_data(league_id)
    rank_info = load_rank_info(league_id, fixture_meta=fmap, players_by_team=players_by_team)

    odds_blob = _load_json(ODDS_DIR / f"{league_id}.json") or {}
    legs: List[dict] = []

    for fx in odds_blob.get("fixtures") or []:
        fid = int(fx.get("fixture_id") or fx.get("id") or 0)
        meta = fmap.get(fid)
        if not meta:
            continue
        fixture_name = meta.get("name") or f"{meta['home_name']} vs {meta['away_name']}"
        starting_at = meta.get("starting_at") or ""
        odds_rows = fx.get("odds") or []
        odds_by_market = index_odds_by_market(odds_rows)
        home_ml, away_ml = extract_team_ml_prices(odds_by_market, meta["home_name"], meta["away_name"])

        for team_id, opp_id, team_name, opp_name, team_ml, opp_ml, home_away in [
            (meta["home_id"], meta["away_id"], meta["home_name"], meta["away_name"], home_ml, away_ml, "home"),
            (meta["away_id"], meta["home_id"], meta["away_name"], meta["home_name"], away_ml, home_ml, "away"),
        ]:
            for player in players_by_team.get(team_id, []):
                legs.extend(
                    evaluate_player(
                        player,
                        odds_by_market,
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


def dedup_records(records: List[dict]) -> List[dict]:
    best: Dict[Tuple[str, int, str], dict] = {}

    def make_key(rec: dict) -> Tuple[str, int, str]:
        pid = str(rec.get("player_id") or "").strip()
        pname = norm(rec.get("player") or "")
        fid = int(rec.get("fixture_id") or 0)
        market = rec.get("market") or ""
        return (pid or pname, fid, market)

    for rec in records:
        key = make_key(rec)
        price = _as_float(rec.get("price"))
        hr = hit_rate_value(rec.get("hit_rate") or "0/0")
        current = best.get(key)
        if current is None:
            best[key] = rec
            continue
        current_price = _as_float(current.get("price"))
        current_hr = hit_rate_value(current.get("hit_rate") or "0/0")
        if (price or -math.inf, hr) > (current_price or -math.inf, current_hr):
            best[key] = rec

    return list(best.values())


# ---- output ---------------------------------------------------------------

def render_output(picks: List[dict], fallback: Optional[List[dict]] = None) -> str:
    lines = [
        f"Generated at (UTC): {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%SZ')}",
        "Opponent gate: skip opponents ranked top-3 stingiest for shots conceded.",
        "",
        f"Legs ({len(picks)}):",
    ]

    for pick in picks:
        pos = pick.get("position_bucket") or pick.get("position") or ""
        try:
            price_str = f"{float(pick.get('price')):.2f}"
        except Exception:
            price_str = str(pick.get("price") or "")
        opp_rank_total = pick.get("opponent_rank_total")
        opp_rank_label = ""
        if pick.get("opponent_rank"):
            if opp_rank_total:
                opp_rank_label = f" | Opponent shots conceded rank (1 = fewest allowed of {opp_rank_total} teams): {pick['opponent_rank']}"
            else:
                opp_rank_label = f" | Opponent shots conceded rank (1 = fewest allowed): {pick['opponent_rank']}"
        lines.append(
            f"{pick['criteria']} — {pick['player']} [{pos}] ({pick['team']} vs {pick['opponent']}) {pick['market']} @ {price_str}"
        )
        lines.append(
            f"  Form: {pick['hit_rate']} | Avg: {pick['average']} | Seq: {pick['last_sequence']} | Team ML: {pick.get('team_ml', '')}{opp_rank_label}"
        )
        lines.append(f"  {pick.get('writeup', '').replace(chr(10), ' ')}")
        lines.append("")

    if fallback:
        lines.append("Top 5 near misses (criteria met; odds below threshold):")
        for pick in fallback:
            pos = pick.get("position_bucket") or pick.get("position") or ""
            min_price = pick.get("min_price") or ""
            try:
                price_str = f"{float(pick.get('price')):.2f}" if pick.get("price") is not None else "N/A"
            except Exception:
                price_str = str(pick.get("price") or "")
            lines.append(
                f"{pick['criteria']} — {pick['player']} [{pos}] ({pick['team']} vs {pick['opponent']}) {pick['market']} @ {price_str} (needs >= {min_price})"
            )
            lines.append(
                f"  Form: {pick['hit_rate']} | Avg: {pick['average']} | Seq: {pick['last_sequence']} | Team ML: {pick.get('team_ml', '')}"
            )
            lines.append("")
    elif not picks:
        lines.append("No candidates met the long-shot criteria with current data (price, form, or opponent/ML gates).")

    return "\n".join(lines).rstrip() + "\n"


# ---- main -----------------------------------------------------------------

def main() -> None:
    all_picks: List[dict] = []
    form_candidates: List[dict] = []
    for lid in LEAGUE_IDS:
        all_picks.extend(process_league(lid, form_candidates=form_candidates))
    all_picks = dedup_records(all_picks)
    form_candidates = dedup_records(form_candidates)
    all_picks.sort(key=lambda r: r.get("price") or 0, reverse=True)
    fallback_sorted: List[dict] = []
    if form_candidates:
        best_by_criteria: Dict[str, dict] = {}
        for rec in form_candidates:
            crit = rec.get("criteria") or ""
            price = _as_float(rec.get("price"))
            if price is None:
                continue
            current = best_by_criteria.get(crit)
            if current is None or (_as_float(current.get("price")) or -math.inf) < price:
                best_by_criteria[crit] = rec
        fallback_sorted = sorted(best_by_criteria.values(), key=lambda r: r.get("price") or 0, reverse=True)[:5]
    if all_picks:
        upsert_sheet(all_picks)
    elif not SHEET_FILE.exists():
        save_sheet(SHEET_FILE, [])
    output = render_output(all_picks, fallback_sorted)
    OUT_FILE.write_text(output, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
