#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build a weekend (Sat/Sun) post of players who have registered 1+ shot
on target streaks and have Bet365 odds above MIN_ODDS for the 1+ shots
on target (Player Shots On Target market, line 0.5).

Inputs (local files):
  - data/fixtures/by_league/{LEAGUE_ID}.json
  - data/player_shots_on_target/by_league/{LEAGUE_ID}.json
  - data/odds/b365/by_league/{LEAGUE_ID}.json

Env vars (optional):
  OUTPUT_PATH  -> Where to write the post (default: posts_v2/weekend_sot.md)
  MIN_ODDS     -> Minimum decimal odds to include (default: 1.4)
"""

import json
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(".")
FIXTURE_DIR = ROOT / "data" / "fixtures" / "by_league"
SOT_DIR = ROOT / "data" / "player_shots_on_target" / "by_league"
ODDS_DIR = ROOT / "data" / "odds" / "b365" / "by_league"

OUT_PATH = Path(os.getenv("OUTPUT_PATH", "posts_v2/weekend_sot.md"))
MIN_ODDS = float(os.getenv("MIN_ODDS", "1.4"))

# Friendlier team labels for copy/paste posts. Keys are normalized to lowercase.
TEAM_LABEL_OVERRIDES = {
    "manchester united": "Man Utd",
    "manchester city": "Man City",
    "nottingham forest": "Nottm Forest",
    "newcastle united": "Newcastle",
    "tottenham hotspur": "Spurs",
    "tottenham": "Spurs",
    "wolverhampton wanderers": "Wolves",
    "wolverhampton": "Wolves",
    "west ham united": "West Ham",
    "west ham": "West Ham",
    "brighton and hove albion": "Brighton",
    "brighton & hove albion": "Brighton",
    "sheffield united": "Sheff Utd",
    "sheffield wednesday": "Sheff Wed",
    "queens park rangers": "QPR",
    "leeds united": "Leeds",
    "leicester city": "Leicester",
    "crystal palace": "Crystal Palace",
    "aston villa": "Aston Villa",
    "afc bournemouth": "Bournemouth",
    "ipswich town": "Ipswich",
}


def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def _norm_name(name: str) -> str:
    """Normalize a player name for fuzzy equality checks."""
    text = _strip_accents(name or "").strip().lower()
    return re.sub(r"\s+", " ", text)


def _parse_dt(raw: str) -> Optional[datetime]:
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def friendly_team_label(team: dict) -> str:
    raw_name = (team.get("name") or "").strip()
    short = (team.get("short") or "").strip()

    for key in (raw_name.lower(), short.lower()):
        if key in TEAM_LABEL_OVERRIDES:
            return TEAM_LABEL_OVERRIDES[key]

    if raw_name:
        processed = re.sub(r"\bManchester\b", "Man", raw_name, flags=re.IGNORECASE)
        processed = re.sub(r"\bNottingham\b", "Nottm", processed, flags=re.IGNORECASE)
        processed = re.sub(r"\bUnited\b", "Utd", processed, flags=re.IGNORECASE)
        processed = re.sub(r"\s+", " ", processed).strip()
        if processed:
            return processed

    if short and len(short) > 3:
        return short

    return raw_name or short or "Team"


def load_weekend_fixtures() -> Dict[int, dict]:
    """Return {fixture_id: info} for Sat/Sun fixtures across all leagues."""
    weekend: Dict[int, dict] = {}
    for fp in FIXTURE_DIR.glob("*.json"):
        blob = _load_json(fp) or {}
        league_id = blob.get("league_id") or blob.get("id")
        try:
            league_id = int(league_id) if league_id is not None else int(fp.stem)
        except Exception:
            continue

        for fx in blob.get("fixtures") or []:
            dt = _parse_dt(fx.get("starting_at", ""))
            if not dt or dt.weekday() not in (5, 6):
                continue

            participants = fx.get("participants") or fx.get("teams") or []
            if isinstance(participants, dict):
                participants = list(participants.values())

            teams = []
            for p in participants or []:
                tid = p.get("id") or p.get("team_id")
                try:
                    tid_int = int(tid)
                except Exception:
                    continue
                teams.append(
                    {
                        "id": tid_int,
                        "name": p.get("name") or p.get("short_code") or "",
                        "short": p.get("short_code") or p.get("name") or "",
                        "loc": (p.get("meta") or {}).get("location") or "",
                    }
                )

            teams.sort(key=lambda t: 0 if t.get("loc") == "home" else 1)
            weekend[int(fx.get("id"))] = {
                "fixture_id": int(fx.get("id")),
                "league_id": league_id,
                "name": fx.get("name") or "",
                "kickoff": dt,
                "teams": teams,
            }
    return weekend


def load_sot_candidates(
    league_id: int,
) -> Dict[int, List[Tuple[str, str, List[int]]]]:
    """
    Returns {team_id: [(player_name, position, last_n_sot)]} for players with
    on-target histories.
    """
    path = SOT_DIR / f"{league_id}.json"
    blob = _load_json(path) or {}
    candidates: Dict[int, List[Tuple[str, str, List[int]]]] = defaultdict(list)

    for rec in blob.get("players") or []:
        series = rec.get("on_target_last_n") or []
        if not isinstance(series, list) or len(series) < 5:
            continue
        try:
            tid = int(rec.get("team_id"))
        except Exception:
            continue
        position = rec.get("position_tag") or rec.get("position") or ""
        candidates[tid].append((rec.get("name", ""), str(position), [int(x) for x in series]))

    return candidates


def load_sot_odds_map(league_id: int) -> Tuple[Dict[int, Dict[str, Tuple[str, float]]], Optional[str]]:
    """
    Build a mapping {fixture_id: {normalized_player_name: (display_name, odds)}}
    for Player Shots On Target market at line 0.5 with odds > MIN_ODDS.
    Also returns the league name if present.
    """
    path = ODDS_DIR / f"{league_id}.json"
    blob = _load_json(path) or {}
    league_name = blob.get("league_name")
    mapping: Dict[int, Dict[str, Tuple[str, float]]] = defaultdict(dict)

    for fx in blob.get("fixtures") or []:
        fid = fx.get("fixture_id") or fx.get("id")
        try:
            fid_int = int(fid)
        except Exception:
            continue
        for o in fx.get("odds") or []:
            market_desc = (o.get("market_description") or "").lower()
            if "shots on target" not in market_desc:
                continue
            if str(o.get("label")) != "0.5":
                continue
            name = o.get("name") or o.get("total") or ""
            if not name:
                continue
            try:
                odds_val = float(o.get("dp3") or o.get("value"))
            except Exception:
                continue
            if odds_val <= MIN_ODDS:
                continue
            mapping[fid_int][_norm_name(name)] = (name, odds_val)
    return mapping, league_name


def resolve_odds_entry(
    fx_odds: Dict[str, Tuple[str, float]], candidate_name: str
) -> Optional[Tuple[str, float]]:
    """
    Find an odds entry for the given candidate name, allowing for small
    mismatches (e.g. shortened first names or accent differences).
    """

    norm_candidate = _norm_name(candidate_name)
    if norm_candidate in fx_odds:
        return fx_odds[norm_candidate]

    tokens = norm_candidate.split()
    if not tokens:
        return None

    last = tokens[-1]
    first = tokens[0]

    for odds_key, entry in fx_odds.items():
        key_tokens = odds_key.split()
        if not key_tokens:
            continue

        if key_tokens[-1] != last:
            continue

        key_first = key_tokens[0]
        if (
            key_first.startswith(first)
            or first.startswith(key_first)
            or first in key_first
            or key_first in first
        ):
            return entry

    return None


def _series_to_text(series: List[int], n: int) -> str:
    return ",".join(str(x) for x in series[:n])


def build_sections() -> List[str]:
    fixtures = load_weekend_fixtures()
    if not fixtures:
        return ["No weekend fixtures found."]

    lines: List[str] = [
        f"Players with 1+ shot on target streaks with odds >{MIN_ODDS} (Bet365)",
        "",
    ]

    fixtures_by_league: Dict[int, List[dict]] = defaultdict(list)
    for fx in fixtures.values():
        fixtures_by_league[int(fx["league_id"])].append(fx)

    sections = {"5/5": [], "7/7": [], "8/10": []}
    seen_keys = set()

    for league_id in sorted(fixtures_by_league):
        sot_candidates = load_sot_candidates(league_id)
        odds_map, _ = load_sot_odds_map(league_id)

        for fx in sorted(fixtures_by_league[league_id], key=lambda f: f.get("kickoff")):
            fid = fx.get("fixture_id")
            fx_odds = odds_map.get(fid) or {}

            for team in fx.get("teams") or []:
                team_id = team.get("id")
                for name, position, series in sot_candidates.get(team_id, []):
                    odds_entry = resolve_odds_entry(fx_odds, name)
                    if not odds_entry:
                        continue
                    display_name, odds_val = odds_entry
                    team_label = friendly_team_label(team)

                    key = (_norm_name(name), team_id)

                    # Choose the strongest qualifying streak so players appear once,
                    # preferring longer perfect runs over broader 8/10 streaks and
                    # then the 5-match baseline.
                    best_tier = None
                    if len(series) >= 7 and all(int(x) >= 1 for x in series[:7]):
                        best_tier = ("7/7", 7, _series_to_text(series, 7))
                    elif len(series) >= 10 and sum(1 for x in series[:10] if int(x) >= 1) >= 8:
                        best_tier = ("8/10", 10, _series_to_text(series, 10))
                    elif len(series) >= 5 and all(int(x) >= 1 for x in series[:5]):
                        best_tier = ("5/5", 5, _series_to_text(series, 5))

                    if best_tier and key not in seen_keys:
                        tier_key, n, text_series = best_tier
                        label = f"last{n}" if tier_key in ("5/5", "7/7") else "last10"
                        sections[tier_key].append(
                            f"• {display_name} ({team_label} — {position}) — {label}: {text_series} — @ {odds_val:.2f}"
                        )
                        seen_keys.add(key)

    if sections["5/5"]:
        lines.append("1+ SOT in last 5 (5/5)")
        lines.extend(sections["5/5"])
    else:
        lines.append("1+ SOT in last 5 (5/5)")
        lines.append("No qualifying players met all criteria based on current files.")

    lines.append("")

    if sections["7/7"]:
        lines.append("1+ SOT in last 7 (7/7)")
        lines.extend(sections["7/7"])
    else:
        lines.append("1+ SOT in last 7 (7/7)")
        lines.append("No qualifying players met all criteria based on current files.")

    lines.append("")

    if sections["8/10"]:
        lines.append("1+ SOT in 8 of last 10 (8/10)")
        lines.extend(sections["8/10"])
    else:
        lines.append("1+ SOT in 8 of last 10 (8/10)")
        lines.append("No qualifying players met all criteria based on current files.")

    return lines


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = build_sections()
    lines.append("")
    lines.append(
        "⚠️Stat-based shortlist, not tips. Its a good starting point to identify value but we need to consider price, team news, form, and opposition etc. Any value here?"
    )
    text = "\n".join(lines).rstrip() + "\n"
    OUT_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
