#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Player Stats V4 — Singles (shots & shots on target, overs only) with Telegram Notifications

Finds "BET RUN 1" value shots/SOT overs by enforcing high hit rates across
larger samples (up to 20 games), a minimum odds gate, and underdog filters.

NEW in V4:
  • Lower odds threshold: 1.72 (vs 1.8 in previous versions)
  • Telegram notifications for new picks
  • Tracks previously notified players to avoid spam

Inputs (per league):
  • data/player_shots/by_league/{league_id}.json             -> shots_last_n
  • data/player_shots_on_target/by_league/{league_id}.json   -> on_target_last_n
  • data/fixtures/{league_id}.json                           -> team + fixture metadata
  • data/odds/b365/{league_id}.json                          -> player markets + moneyline
  • data/value_bets/Player_value_bets_V2/team_shot_ranks/{league_id}.json  -> shot conceded ranks

Outputs:
  • data/value_bets/V4 Bets/V4 player singles/player_stats_v4.txt
  • data/value_bets/V4 Bets/V4 player singles/notified_players.json  (tracking)

Env vars:
  • LEAGUE_IDS          Comma-separated league IDS (defaults to common set)
  • MIN_DEC_PRICE       Minimum price gate (default: 1.72)
  • DEBUG_DROPS         1 = print drop reasons
  • TELEGRAM_BOT_TOKEN  Telegram bot token for notifications
  • TELEGRAM_CHAT_ID    Telegram chat ID to send messages to
  • ENABLE_TELEGRAM     Set to "1" to enable Telegram notifications (default: 1)
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
import requests

ROOT = Path(".")
PS_DIR = ROOT / "data" / "player_shots" / "by_league"
SOT_DIR = ROOT / "data" / "player_shots_on_target" / "by_league"
FIX_DIR = ROOT / "data" / "fixtures"
ODDS_DIR = ROOT / "data" / "odds" / "b365"
RANK_DIR = ROOT / "data" / "value_bets" / "Player_value_bets_V2" / "team_shot_ranks"
OUT_DIR = ROOT / "data" / "value_bets" / "V4 Bets" / "V4 player singles"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "player_stats_v4.txt"
NOTIFIED_FILE = OUT_DIR / "notified_players.json"  # Track notified players
SHEET_DIR = ROOT / "data" / "value_bets" / "sheets"
SHEET_DIR.mkdir(parents=True, exist_ok=True)
SHEET_FILE = SHEET_DIR / "player_stats_v4_singles.csv"

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

MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.72"))  # V4: Lowered from 1.8 to 1.72
DEBUG_DROPS = bool(int(os.getenv("DEBUG_DROPS", "0")))
MAX_TEAM_ML = float(os.getenv("MAX_TEAM_ML", "4.0"))  # skip big underdogs
EDGE_FAV_ML = float(os.getenv("EDGE_FAV_ML", "3.8"))

# Telegram configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ENABLE_TELEGRAM = bool(int(os.getenv("ENABLE_TELEGRAM", "1")))

WINDOW_RULES = [
    (10, 9),
    (10, 8),
    (12, 10),
    (13, 11),
    (14, 12),
    (16, 13),
    (17, 14),
    (18, 15),
    (19, 15),
    (20, 16),
]

SHOT_MARKETS = [
    ("O0.5 shots", 268, "0.5", 1),
    ("O1.5 shots", 268, "1.5", 2),
    ("O2.5 shots", 268, "2.5", 3),
]

SOT_MARKETS = [
    ("O0.5 SOT", 267, "0.5", 1),
    ("O1.5 SOT", 267, "1.5", 2),
]

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
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            return [dict(r) for r in csv.DictReader(f)]
    except Exception:
        return []


def save_sheet(path: Path, rows: List[dict]) -> None:
    if not rows:
        return
    headers = list(SHEET_HEADERS)
    extras = sorted({k for r in rows for k in r.keys()} - set(headers))
    headers.extend(extras)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for r in rows:
            writer.writerow({h: r.get(h, "") for h in headers})


def load_fixture_odds(fixture_id: int) -> List[dict]:
    blob = _load_json(ODDS_DIR / "fixtures" / f"{fixture_id}.json") or {}
    rows = blob.get("odds") or []
    return rows if isinstance(rows, list) else []


# ---- Telegram notification helpers ----------------------------------------

def load_notified_players() -> Dict[str, dict]:
    """Load the tracking of previously notified players."""
    if not NOTIFIED_FILE.exists():
        return {}
    try:
        data = json.loads(NOTIFIED_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_notified_players(notified: Dict[str, dict]) -> None:
    """Save the tracking of notified players."""
    try:
        NOTIFIED_FILE.write_text(
            json.dumps(notified, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"Warning: Failed to save notified players: {e}")


def generate_notification_key(pick: dict) -> str:
    """Generate a unique key for a pick to track notifications."""
    return f"{pick.get('league_id')}:{pick.get('fixture_id')}:{pick.get('player_id')}:{pick.get('market')}"


def send_telegram_message(message: str) -> bool:
    """Send a message via Telegram bot."""
    if not ENABLE_TELEGRAM or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram notification failed: {e}")
        return False


def format_telegram_message(pick: dict) -> str:
    """Format a pick as a Telegram message."""
    pos = pick.get("position") or pick.get("position_bucket") or ""
    pos_label = f" [{pos}]" if pos else ""

    try:
        line_val = float(pick.get("line", 0))
    except Exception:
        line_val = 0.0

    stat_short = "SOT" if "target" in (pick.get("stat_type") or "") else "shots"
    line_part = f"O{line_val:.1f} {stat_short}" if line_val else pick.get("market", "N/A")

    lines = [
        f"🎯 <b>V4 Player Singles</b>",
        f"",
        f"⚽ <b>{pick.get('player', 'Unknown')}</b>{pos_label}",
        f"🏟 {pick.get('team', 'Unknown')} vs {pick.get('opponent', 'Unknown')}",
        f"",
        f"📊 <b>{line_part}</b>",
        f"💰 Odds: <b>{pick.get('price', 0.0):.2f}</b>",
        f"",
        f"📈 {pick.get('criteria', 'N/A')}",
        f"📊 Hit Rate: {pick.get('hit_rate', 'N/A')}",
        f"📉 Average: {pick.get('average', 'N/A')}",
        f"📋 Sequence: {pick.get('last_sequence', 'N/A')}",
        f"",
        f"🏆 Team ML: {pick.get('team_ml', 'N/A')}",
        f"🎲 Opp ML: {pick.get('opp_ml', 'N/A')}",
    ]

    if pick.get('opponent_rank'):
        lines.append(f"🔝 Opponent rank (shots conceded): {pick.get('opponent_rank')}")

    lines.extend([
        f"",
        f"📅 {pick.get('date', 'N/A')} @ {pick.get('ko_time', 'N/A')}",
        f"🆔 Fixture: {pick.get('fixture_id', 'N/A')}",
    ])

    return "\n".join(lines)


def notify_new_picks(picks: List[dict]) -> int:
    """Send Telegram notifications for new picks. Returns count of notifications sent."""
    if not ENABLE_TELEGRAM or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram notifications disabled (missing credentials or disabled via config)")
        return 0

    notified = load_notified_players()

    print(f"\n{'='*70}")
    print(f"NOTIFICATION TRACKING STATUS")
    print(f"{'='*70}")
    print(f"Previously tracked picks: {len(notified)}")
    print(f"Total picks this run: {len(picks)}")

    new_picks = []
    already_notified = []

    for pick in picks:
        key = generate_notification_key(pick)
        player_market = f"{pick.get('player')} - {pick.get('market')} (Fixture {pick.get('fixture_id')})"

        if key not in notified:
            new_picks.append(pick)
            notified[key] = {
                "player": pick.get("player"),
                "market": pick.get("market"),
                "fixture": pick.get("fixture"),
                "fixture_id": pick.get("fixture_id"),
                "notified_at": datetime.utcnow().isoformat(),
                "odds": pick.get("price"),
            }
        else:
            already_notified.append(player_market)

    print(f"\n📊 BREAKDOWN:")
    print(f"  ✅ New picks to notify: {len(new_picks)}")
    print(f"  ⏭️  Already notified (skipped): {len(already_notified)}")

    if already_notified:
        print(f"\n⏭️  SKIPPED (already notified):")
        for item in already_notified[:10]:  # Show first 10
            print(f"    • {item}")
        if len(already_notified) > 10:
            print(f"    ... and {len(already_notified) - 10} more")

    if not new_picks:
        print(f"\n✓ No new picks to notify")
        print(f"{'='*70}\n")
        return 0

    print(f"\n🔔 SENDING NOTIFICATIONS:")
    sent_count = 0

    for pick in new_picks:
        player_info = f"{pick.get('player')} - {pick.get('market')} @ {pick.get('price', 0):.2f}"
        message = format_telegram_message(pick)
        if send_telegram_message(message):
            sent_count += 1
            print(f"  ✓ {player_info}")
        else:
            print(f"  ✗ FAILED: {player_info}")

    save_notified_players(notified)
    print(f"\n💾 Saved {len(notified)} total tracked picks to: {NOTIFIED_FILE}")

    if sent_count > 0:
        summary = f"📬 Sent {sent_count}/{len(new_picks)} V4 player singles notifications"
        send_telegram_message(summary)
        print(f"\n{summary}")

    print(f"{'='*70}\n")
    return sent_count


# ---- string helpers (borrowed from V2 scripts) ----------------------------
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
    target_norm = target_label.replace(",", ".")
    pattern = rf"(^|[^0-9]){re.escape(target_norm)}([^0-9]|$)"
    if re.search(pattern, blob.replace(",", ".")):
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
        price = _as_float(row.get("value"))
        if price is None:
            continue
        label = (row.get("label") or "").strip()
        name = (row.get("name") or "").strip()

        if norm(label) in {"1", "home"} or team_names_match(home_name, label) or team_names_match(home_name, name):
            home_price = price if home_price is None or price < home_price else home_price
        elif norm(label) in {"2", "away"} or team_names_match(away_name, label) or team_names_match(away_name, name):
            away_price = price if away_price is None or price < away_price else away_price
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


def load_rank_info(league_id: int) -> dict:
    blob = _load_json(RANK_DIR / f"{league_id}.json") or {}
    stingy: Dict[int, int] = {}
    leaky: Dict[int, int] = {}
    names: Dict[int, str] = {}
    ranks = blob.get("shots_conceded_rank") or []
    total = len(ranks)
    for idx, row in enumerate(ranks):
        tid = int(row.get("team_id") or 0)
        if not tid:
            continue
        stingy[tid] = idx + 1
        leaky[tid] = total - idx
        names[tid] = row.get("team_name") or ""
    return {"stingy": stingy, "leaky": leaky, "team_names": names, "team_count": total}


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
            sot_series = [
                int(float(x)) if isinstance(x, (int, float, str)) and str(x).strip() else 0
                for x in sot_map[pid].get("on_target_last_n") or []
            ]
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


def best_window(series: List[int], threshold: int) -> Optional[Tuple[int, int, List[int], int, int]]:
    """
    Return the first window that meets the high hit-rate rule along with the
    sample that was used.
    """
    for window, required in WINDOW_RULES:
        seq = take(series, window)
        n = len(seq)
        if n < max(10, required):  # need at least 10 games of data
            continue
        hit_count = sum(1 for v in seq if v >= threshold)
        needed_rate = required / window
        # Accept exact window meeting the hit gate, or shorter samples that still
        # beat the same hit-rate bar.
        if hit_count >= required or (n < window and (hit_count / n) >= needed_rate):
            return n, hit_count, seq, required, window
    return None


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
    if p in {"WB", "RWB", "LWB"}:
        return "WB"
    return p or ""


def split_datetime(starting_at: str) -> Tuple[str, str]:
    try:
        dt_obj = datetime.fromisoformat(starting_at.replace("Z", "+00:00"))
    except Exception:
        return "", ""
    return dt_obj.strftime("%Y-%m-%d"), dt_obj.strftime("%H:%M:%S")


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
    threshold = threshold_from_line(line_val)
    outcome = None
    if line_val.is_integer() and actual == int(line_val):
        outcome = "push"
    elif actual >= threshold:
        outcome = "won"
    else:
        outcome = "lost"
    row["result"] = outcome
    try:
        price = float(row.get("odds_taken"))
    except Exception:
        price = None
    stake = 1.0
    try:
        stake = float(row.get("stake", 1) or 1)
    except Exception:
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
    if row.get("closing_odds") and row.get("odds_taken"):
        try:
            closing = float(row["closing_odds"])
            taken = float(row["odds_taken"])
            row["clv"] = f"{closing - taken:.2f}"
        except Exception:
            pass
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
                    "opponent_rank": pick.get("opponent_rank", ""),
                    "created_at": pick.get("created_at", datetime.utcnow().isoformat()),
                    "updated_at": pick.get("updated_at", datetime.utcnow().isoformat()),
                }
            )
            key_map[key] = row
            rows.append(row)

    settle_sheet(rows, stat_index)
    save_sheet(SHEET_FILE, rows)
    return rows


# ---- criteria evaluation --------------------------------------------------

def opponent_flags(opponent_id: int, rank_info: dict) -> Tuple[bool, bool]:
    stingy_rank = rank_info.get("stingy", {}).get(opponent_id, 999)
    leaky_rank = rank_info.get("leaky", {}).get(opponent_id, 999)
    return stingy_rank <= 5, leaky_rank <= 7


def format_series(seq: List[int], n: int) -> Tuple[List[int], int]:
    vals = take(seq, n)
    return vals, len(vals)


def build_writeup(
    player: str,
    team: str,
    opponent: str,
    stat_label: str,
    line_val: float,
    series: List[int],
    hit_count: int,
    total_games: int,
    avg_val: float,
    opponent_rank: Optional[int],
) -> str:
    seq_display = ",".join(str(x) for x in series)
    stat_short = "SOT" if "target" in stat_label else "shots"
    line_desc = f"O{float(line_val):.1f} {stat_short}"
    lines = [f"{player} has hit {line_desc} in {hit_count} of his last {total_games} = {seq_display}"]
    lines.append(f"Averaging {avg_val:.2f} per game over that span.")
    if opponent_rank is not None:
        lines.append(f"Over their last 10 games {opponent} rank {opponent_rank} for shots conceded.")
    return "\n".join(lines)


def evaluate_player(
    player: dict,
    odds_rows: List[dict],
    team_ml: Optional[float],
    opp_ml: Optional[float],
    opponent_id: int,
    opponent_name: str,
    team_name: str,
    rank_info: dict,
    fixture_id: int,
    league_id: int,
    fixture_name: str,
    starting_at: str,
    home_away: str,
    drop,
) -> List[dict]:
    picks: List[dict] = []
    stingy, _ = opponent_flags(opponent_id, rank_info)
    leaky_rank = rank_info.get("leaky", {}).get(opponent_id, 999)
    opp_rank_desc = leaky_rank if leaky_rank != 999 else None
    shots = player.get("shots") or []
    sot = player.get("sot") or []
    position = player.get("position") or ""
    name = player.get("name") or "Player"
    player_id = player.get("player_id")
    team_id = player.get("team_id")
    pos_bucket = position_bucket(position)
    date_str, ko_time = split_datetime(starting_at)

    ctx_base = {
        "player": name,
        "team": team_name,
        "opponent": opponent_name,
        "fixture_id": fixture_id,
        "league_id": league_id,
    }

    if stingy:
        drop("opponent stingy (few shots conceded)", ctx_base)
        return picks

    if team_ml is not None and team_ml > MAX_TEAM_ML:
        drop("team ML too high (> guard)", {**ctx_base, "team_ml": team_ml})
        return picks

    fav_like = team_ml is not None and opp_ml is not None and team_ml < opp_ml
    leaky_ok = leaky_rank <= 10

    def add_pick(
        market_name: str,
        market_id: int,
        target_label: str,
        threshold: int,
        series: List[int],
        stat_label: str,
    ) -> None:
        win = best_window(series, threshold)
        if not win:
            drop("no high-hit window", {**ctx_base, "market": market_name})
            return
        sample_n, hit_count, seq, required_gate, window_label = win
        price = best_price_for_line(odds_rows, player, market_id, target_label)
        if price is None:
            drop("missing odds row", {**ctx_base, "market": market_name})
            return
        if price < MIN_PRICE:
            drop("price below min", {**ctx_base, "price": price, "market": market_name})
            return
        if not leaky_ok and not fav_like and (team_ml is not None and team_ml > EDGE_FAV_ML):
            drop("no opponent edge", {**ctx_base, "opponent_rank": opp_rank_desc, "team_ml": team_ml})
            return

        seq = seq or []
        avg_val = avg(seq, len(seq))
        seq_str = ",".join(str(x) for x in seq)
        stat_key = "shots_on_target" if "target" in stat_label else "shots"
        now_ts = datetime.utcnow().isoformat()
        hit_rate = f"{hit_count}/{sample_n}"
        gate_label = f"{required_gate}/{window_label}"
        line_val = float(target_label)
        writeup = build_writeup(
            name,
            team_name,
            opponent_name,
            stat_label,
            line_val,
            seq,
            hit_count,
            sample_n,
            avg_val,
            opp_rank_desc,
        )
        picks.append(
            {
                "player": name,
                "player_id": player_id,
                "position": position,
                "position_bucket": pos_bucket,
                "team": team_name,
                "team_id": team_id,
                "opponent": opponent_name,
                "opp_id": opponent_id,
                "market": market_name,
                "line": f"{float(target_label):.2f}",
                "stat_type": stat_key,
                "price": price,
                "criteria": f"V4 HR {hit_count}/{sample_n} (gate {gate_label})",
                "league_id": league_id,
                "fixture_id": fixture_id,
                "fixture": fixture_name,
                "starting_at": starting_at,
                "date": date_str,
                "ko_time": ko_time,
                "home_away": home_away,
                "team_ml": team_ml,
                "opp_ml": opp_ml,
                "opponent_rank": opp_rank_desc if opp_rank_desc is not None else "",
                "hit_rate": hit_rate,
                "average": f"{avg_val:.2f}",
                "last_sequence": seq_str,
                "bookmaker": "bet365",
                "stake": "1",
                "created_at": now_ts,
                "updated_at": now_ts,
                "writeup": writeup,
            }
        )

    # Shots
    for market_name, market_id, target_label, threshold in SHOT_MARKETS:
        add_pick(market_name, market_id, target_label, threshold, shots, "shots")
    # Shots on target
    for market_name, market_id, target_label, threshold in SOT_MARKETS:
        add_pick(market_name, market_id, target_label, threshold, sot, "shots on target")

    return picks


# ---- main pipeline --------------------------------------------------------

def process_league(league_id: int, drop_reasons: Dict[str, int]) -> List[dict]:
    def drop(reason: str, ctx: Optional[dict] = None):
        drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
        if DEBUG_DROPS:
            print(f"[drop] {reason} :: {ctx or {}}")

    fmap = fixture_map(league_id)
    rank_info = load_rank_info(league_id)
    players_by_team = load_player_data(league_id)

    odds_blob = _load_json(ODDS_DIR / f"{league_id}.json") or {}
    picks: List[dict] = []

    fixtures = odds_blob.get("fixtures") or []
    if not fixtures:
        drop("no league odds fixtures", {"league_id": league_id})
        return picks

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
                picks.extend(
                    evaluate_player(
                        player,
                        odds_rows,
                        team_ml,
                        opp_ml,
                        opp_id,
                        opp_name,
                        team_name,
                        rank_info,
                        fid,
                        league_id,
                        fixture_name,
                        starting_at,
                        home_away,
                        drop,
                    )
                )

    return picks


def render_output(picks: List[dict], drop_reasons: Dict[str, int]) -> str:
    lines: List[str] = []
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    lines.append(f"Generated at (UTC): {ts}")
    lines.append(f"Price gate: >= {MIN_PRICE:.2f} (decimal); team ML guard > {MAX_TEAM_ML}")
    window_desc = ", ".join([f"{req}/{win}" for win, req in WINDOW_RULES])
    lines.append(f"Windows: {window_desc} (overs only); opponent filter: skip top-5 stingy for shots conceded.")
    lines.append("")

    picks.sort(key=lambda r: (r.get("team", ""), r.get("player", "")))
    for p in picks:
        pos = p.get("position") or p.get("position_bucket") or ""
        pos_label = f" [{pos}]" if pos else ""
        try:
            line_val = float(p.get("line"))
        except Exception:
            line_val = None
        stat_short = "SOT" if "target" in (p.get("stat_type") or "") else "shots"
        line_part = f"O{line_val:.1f} {stat_short}" if line_val is not None else (p.get("market") or "")
        lines.append(f"{p['player']}{pos_label} ({p['team']} vs {p['opponent']}) {line_part} @ {p['price']:.2f}")
        for ln in p["writeup"].splitlines():
            lines.append(f"  {ln}")
        lines.append("")

    if drop_reasons:
        lines.append("Drop summary (by reason):")
        for k, v in sorted(drop_reasons.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {k}: {v}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    drop_reasons: Dict[str, int] = {}
    all_picks: List[dict] = []

    print(f"Player Stats V4 - Telegram Notifications")
    print(f"Odds threshold: {MIN_PRICE:.2f}")
    print(f"Telegram enabled: {ENABLE_TELEGRAM}")
    print("")

    for lid in LEAGUE_IDS:
        all_picks.extend(process_league(lid, drop_reasons))

    print(f"Total picks found: {len(all_picks)}")

    upsert_sheet(all_picks)
    output = render_output(all_picks, drop_reasons)
    OUT_FILE.write_text(output, encoding="utf-8")
    print(output)

    # Send Telegram notifications for new picks
    if all_picks:
        sent_count = notify_new_picks(all_picks)
        print(f"\nTelegram notifications sent: {sent_count}")
    else:
        print("\nNo picks to notify")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
