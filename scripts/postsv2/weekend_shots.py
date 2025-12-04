#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build a weekend (Sat/Sun) post of players who have registered 1+ shot
in each of their last 5 matches and have Bet365 odds above MIN_ODDS for
1+ shots (Player Shots market, line 0.5).

Inputs (local files):
  - data/fixtures/by_league/{LEAGUE_ID}.json
  - data/player_shots/by_league/{LEAGUE_ID}.json
  - data/odds/b365/by_league/{LEAGUE_ID}.json

Env vars (optional):
  OUTPUT_PATH  -> Where to write the post (default: posts_v2/weekend_shots.md)
  MIN_ODDS     -> Minimum decimal odds to include (default: 1.4)
"""

import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(".")
FIXTURE_DIR = ROOT / "data" / "fixtures" / "by_league"
SHOTS_DIR = ROOT / "data" / "player_shots" / "by_league"
ODDS_DIR = ROOT / "data" / "odds" / "b365" / "by_league"

OUT_PATH = Path(os.getenv("OUTPUT_PATH", "posts_v2/weekend_shots.md"))
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


def _norm_name(name: str) -> str:
    """Normalize a player name for fuzzy equality checks."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


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


def load_shot_candidates(
    league_id: int, min_per_game: int
) -> Dict[int, List[Tuple[str, str, List[int]]]]:
    """
    Returns {team_id: [(player_name, position, last5_shots)]} for players with
    ``min_per_game`` shots in each of their last 5.
    """
    path = SHOTS_DIR / f"{league_id}.json"
    blob = _load_json(path) or {}
    candidates: Dict[int, List[Tuple[str, List[int]]]] = defaultdict(list)

    for rec in blob.get("players") or []:
        shots = rec.get("shots_last_n") or []
        if not isinstance(shots, list) or len(shots) < 5:
            continue
        last5 = shots[:5]
        if any(not isinstance(x, (int, float)) or x < min_per_game for x in last5):
            continue
        try:
            tid = int(rec.get("team_id"))
        except Exception:
            continue
        position = rec.get("position_tag") or rec.get("position") or ""
        candidates[tid].append((rec.get("name", ""), str(position), [int(x) for x in last5]))

    return candidates


def load_odds_map(
    league_id: int,
) -> Tuple[Dict[int, Dict[str, Dict[str, Tuple[str, float]]]], Optional[str]]:
    """
    Build a mapping {fixture_id: {line_label: {normalized_player_name: (display_name, odds)}}}
    for Player Shots market lines with odds > MIN_ODDS.
    Also returns the league name if present.
    """
    path = ODDS_DIR / f"{league_id}.json"
    blob = _load_json(path) or {}
    league_name = blob.get("league_name")
    mapping: Dict[int, Dict[str, Dict[str, Tuple[str, float]]]] = defaultdict(
        lambda: defaultdict(dict)
    )

    for fx in blob.get("fixtures") or []:
        fid = fx.get("fixture_id") or fx.get("id")
        try:
            fid_int = int(fid)
        except Exception:
            continue
        for o in fx.get("odds") or []:
            if o.get("market_description") not in {"Player Shots", "Player Shots Over/Under"}:
                continue
            label = str(o.get("label"))
            if label not in {"0.5", "1.5"}:
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
            mapping[fid_int][label][_norm_name(name)] = (name, odds_val)
    return mapping, league_name


def build_lines() -> List[str]:
    fixtures = load_weekend_fixtures()
    if not fixtures:
        return ["No weekend fixtures found."]

    lines: List[str] = [
        f"Players with 1+ shot in each of their last 5 matches with odds >{MIN_ODDS} (Bet365)",
        "",
    ]

    fixtures_by_league: Dict[int, List[dict]] = defaultdict(list)
    for fx in fixtures.values():
        fixtures_by_league[int(fx["league_id"])].append(fx)

    entries: List[str] = []
    entries_two_plus: List[str] = []

    for league_id in sorted(fixtures_by_league):
        shot_candidates_1 = load_shot_candidates(league_id, min_per_game=1)
        shot_candidates_2 = load_shot_candidates(league_id, min_per_game=2)
        odds_map, league_name = load_odds_map(league_id)

        for fx in sorted(fixtures_by_league[league_id], key=lambda f: f.get("kickoff")):
            fid = fx.get("fixture_id")
            fx_odds = odds_map.get(fid) or {}

            for team in fx.get("teams") or []:
                team_id = team.get("id")

                for name, position, series in shot_candidates_1.get(team_id, []):
                    odds_entry = (fx_odds.get("0.5") or {}).get(_norm_name(name))
                    if not odds_entry:
                        continue
                    display_name, odds_val = odds_entry
                    team_label = friendly_team_label(team)
                    series_txt = ",".join(str(x) for x in series)
                    entries.append(
                        f"• {display_name} ({team_label} — {position}) — last5: {series_txt} — @ {odds_val:.2f}"
                    )

                for name, position, series in shot_candidates_2.get(team_id, []):
                    odds_entry = (fx_odds.get("1.5") or {}).get(_norm_name(name))
                    if not odds_entry:
                        continue
                    display_name, odds_val = odds_entry
                    team_label = friendly_team_label(team)
                    series_txt = ",".join(str(x) for x in series)
                    entries_two_plus.append(
                        f"• {display_name} ({team_label} — {position}) — last5: {series_txt} — @ {odds_val:.2f}"
                    )

    if entries:
        lines.extend(entries)
    else:
        lines.append("No qualifying players met 1+ shots criteria based on current files.")

    if entries_two_plus:
        lines.append("")
        lines.append(
            f"Players with 2+ shots in each of their last 5 matches with odds >{MIN_ODDS} (Bet365)"
        )
        lines.append("")
        lines.extend(entries_two_plus)
    else:
        lines.append("")
        lines.append("No qualifying players met 2+ shots criteria based on current files.")

    return lines


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = build_lines()
    lines.append("")
    lines.append("⚠️Stat-based shortlist, not tips. Its a good starting point to identify value but we need to consider price, team news, form, and opposition etc. Any value here?")
    text = "\n".join(lines).rstrip() + "\n"
    OUT_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
