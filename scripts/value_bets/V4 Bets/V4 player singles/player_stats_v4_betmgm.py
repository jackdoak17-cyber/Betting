#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Player Stats V4 — BetMGM Singles (shots & shots on target, overs only)

Weekly analysis using BetMGM odds for shots/SOT overs with V4 criteria:
high hit rates across larger samples (up to 20 games), minimum odds 1.72

Inputs (per league):
  • data/player_shots/by_league/{league_id}.json             -> shots_last_n
  • data/player_shots_on_target/by_league/{league_id}.json   -> on_target_last_n
  • data/fixtures/{league_id}.json                           -> team + fixture metadata
  • data/odds/betmgm/{league_id}.json                        -> BetMGM player markets + moneyline
  • data/value_bets/Player_value_bets_V2/team_shot_ranks/{league_id}.json  -> shot conceded ranks

Outputs:
  • data/value_bets/V4 Bets/V4 BetMGM singles/player_stats_v4_betmgm.txt
  • data/value_bets/V4 Bets/V4 BetMGM singles/notified_players_betmgm.json

Env vars:
  • LEAGUE_IDS          Comma-separated league IDS (defaults to common set)
  • MIN_DEC_PRICE       Minimum price gate (default: 1.72)
  • TELEGRAM_BOT_TOKEN  Telegram bot token for notifications
  • TELEGRAM_CHAT_ID    Telegram chat ID to send messages to
  • ENABLE_TELEGRAM     Set to "1" to enable Telegram notifications (default: 1)
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests

ROOT = Path(".")
PS_DIR = ROOT / "data" / "player_shots" / "by_league"
SOT_DIR = ROOT / "data" / "player_shots_on_target" / "by_league"
FIX_DIR = ROOT / "data" / "fixtures"
ODDS_DIR = ROOT / "data" / "odds" / "betmgm"  # BetMGM odds
RANK_DIR = ROOT / "data" / "value_bets" / "Player_value_bets_V2" / "team_shot_ranks"
OUT_DIR = ROOT / "data" / "value_bets" / "V4 Bets" / "V4 BetMGM singles"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = OUT_DIR / "player_stats_v4_betmgm.txt"
NOTIFIED_FILE = OUT_DIR / "notified_players_betmgm.json"

DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS: List[int] = [
    int(x)
    for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",")
    if x.strip()
]

MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.72"))
MAX_TEAM_ML = 4.0  # skip big underdogs

# Telegram configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ENABLE_TELEGRAM = bool(int(os.getenv("ENABLE_TELEGRAM", "1")))

# V4 Window rules: (window_size, required_hits)
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

# Shot markets to analyze
SHOT_MARKETS = [
    ("O0.5 shots", 268, "0.5", 1),
    ("O1.5 shots", 268, "1.5", 2),
    ("O2.5 shots", 268, "2.5", 3),
]

SOT_MARKETS = [
    ("O0.5 SOT", 267, "0.5", 1),
    ("O1.5 SOT", 267, "1.5", 2),
]


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
        NOTIFIED_FILE.parent.mkdir(parents=True, exist_ok=True)
        NOTIFIED_FILE.write_text(
            json.dumps(notified, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"✓ Saved {len(notified)} tracked picks to: {NOTIFIED_FILE}")
    except Exception as e:
        print(f"✗ ERROR: Failed to save notified players to {NOTIFIED_FILE}: {e}")
        raise


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

    lines = [
        f"🎯 <b>V4 BetMGM Singles</b>",
        f"",
        f"⚽ <b>{pick.get('player', 'Unknown')}</b>{pos_label}",
        f"🏟 {pick.get('team', 'Unknown')} vs {pick.get('opponent', 'Unknown')}",
        f"",
        f"📊 <b>{pick.get('market', 'N/A')}</b>",
        f"💰 Odds: <b>{pick.get('price', 0.0):.2f}</b> (BetMGM)",
        f"",
        f"📈 {pick.get('criteria', 'N/A')}",
        f"📊 Hit Rate: {pick.get('hit_rate', 'N/A')}",
        f"📉 Average: {pick.get('average', 'N/A')}",
        f"📋 Sequence: {pick.get('last_sequence', 'N/A')}",
        f"",
        f"🏆 Team ML: {pick.get('team_ml', 'N/A')}",
        f"",
        f"📅 {pick.get('date', 'N/A')} @ {pick.get('ko_time', 'N/A')}",
        f"🆔 Fixture: {pick.get('fixture_id', 'N/A')}",
    ]

    if pick.get('opponent_rank'):
        lines.insert(-2, f"🔝 Opponent rank (shots conceded): {pick.get('opponent_rank')}")

    return "\n".join(lines)


def notify_new_picks(picks: List[dict]) -> int:
    """Send Telegram notifications for new picks. Returns count of notifications sent."""
    if not ENABLE_TELEGRAM or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram notifications disabled (missing credentials or disabled via config)")
        return 0

    notified = load_notified_players()

    print(f"\n{'='*70}")
    print(f"NOTIFICATION TRACKING STATUS (BetMGM)")
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
                "notified_at": datetime.now().isoformat(),
                "odds": pick.get("price"),
            }
        else:
            already_notified.append(player_market)

    print(f"\n📊 BREAKDOWN:")
    print(f"  ✅ New picks to notify: {len(new_picks)}")
    print(f"  ⏭️  Already notified (skipped): {len(already_notified)}")

    if already_notified:
        print(f"\n⏭️  SKIPPED (already notified):")
        for item in already_notified[:10]:
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

    if sent_count > 0:
        summary = f"📬 Sent {sent_count}/{len(new_picks)} V4 BetMGM singles notifications"
        send_telegram_message(summary)
        print(f"\n{summary}")

    print(f"{'='*70}\n")
    return sent_count


# ---- Core Analysis Logic ---------------------------------------------------

def load_json(path: Path) -> Optional[dict]:
    """Load JSON file if it exists."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error loading {path}: {e}")
        return None


def check_window_rules(history: List[int], threshold: int) -> Optional[Tuple[int, int]]:
    """
    Check if player meets any V4 window rule.
    Returns: (window_size, required_hits) or None
    """
    for window_size, required_hits in WINDOW_RULES:
        if len(history) < window_size:
            continue

        recent = history[:window_size]
        hits = sum(1 for x in recent if x >= threshold)

        if hits >= required_hits:
            return (window_size, required_hits)

    return None


def analyze_league(league_id: int) -> List[dict]:
    """Analyze a single league for value bets."""
    print(f"\n{'='*70}")
    print(f"ANALYZING LEAGUE {league_id}")
    print(f"{'='*70}")

    # Load data
    shots_data = load_json(PS_DIR / f"{league_id}.json")
    sot_data = load_json(SOT_DIR / f"{league_id}.json")
    fixtures_data = load_json(FIX_DIR / f"{league_id}.json")
    odds_data = load_json(ODDS_DIR / f"{league_id}.json")
    ranks_data = load_json(RANK_DIR / f"{league_id}.json")

    if not odds_data:
        print(f"⚠️  No BetMGM odds data for league {league_id}")
        return []

    if not shots_data or not sot_data or not fixtures_data:
        print(f"⚠️  Missing player stats or fixtures for league {league_id}")
        return []

    # Index player stats by player_id
    shots_by_player = {p["player_id"]: p for p in shots_data.get("players", [])}
    sot_by_player = {p["player_id"]: p for p in sot_data.get("players", [])}

    # Index fixtures
    fixtures_by_id = {f["fixture_id"]: f for f in fixtures_data.get("fixtures", [])}

    value_bets = []

    # Analyze each fixture
    for fixture in fixtures_data.get("fixtures", []):
        fixture_id = fixture["fixture_id"]
        home_id = fixture.get("home_id")
        away_id = fixture.get("away_id")

        # Get odds for this fixture
        fixture_odds = odds_data.get("fixtures", {}).get(str(fixture_id), {})
        if not fixture_odds:
            continue

        # Analyze shot markets
        for market_name, market_id, line, threshold in SHOT_MARKETS + SOT_MARKETS:
            is_sot = "SOT" in market_name
            player_data = sot_by_player if is_sot else shots_by_player
            stat_key = "on_target_last_n" if is_sot else "shots_last_n"

            # Get player odds for this market
            player_odds = fixture_odds.get("players", {})

            for player_id_str, markets in player_odds.items():
                player_id = int(player_id_str)

                if player_id not in player_data:
                    continue

                player = player_data[player_id]
                history = player.get(stat_key, [])

                if not history:
                    continue

                # Check if this player has odds for this market
                market_odds = markets.get(str(market_id), {})
                odds_value = market_odds.get(line)

                if not odds_value:
                    continue

                try:
                    price = float(odds_value)
                except (ValueError, TypeError):
                    continue

                # Filter by min price
                if price < MIN_PRICE:
                    continue

                # Check window rules
                window_result = check_window_rules(history, threshold)
                if not window_result:
                    continue

                window_size, required_hits = window_result

                # Get team ML odds
                team_id = player.get("team_id")
                team_ml = None

                if team_id == home_id:
                    team_ml = fixture.get("home_ml")
                    home_away = "home"
                    opponent_id = away_id
                elif team_id == away_id:
                    team_ml = fixture.get("away_ml")
                    home_away = "away"
                    opponent_id = home_id
                else:
                    continue

                # Skip if team is too big underdog
                if team_ml and float(team_ml) > MAX_TEAM_ML:
                    continue

                # Calculate stats
                recent = history[:window_size]
                hits = sum(1 for x in recent if x >= threshold)
                avg = sum(recent) / len(recent) if recent else 0

                # Get opponent rank
                opponent_rank = None
                if ranks_data and opponent_id:
                    opponent_rank = ranks_data.get("ranks", {}).get(str(opponent_id))

                # Build pick
                pick = {
                    "league_id": league_id,
                    "fixture_id": fixture_id,
                    "fixture": f"{fixture.get('home_team')} vs {fixture.get('away_team')}",
                    "date": fixture.get("date"),
                    "ko_time": fixture.get("ko_time"),
                    "home_team": fixture.get("home_team"),
                    "away_team": fixture.get("away_team"),
                    "team": fixture.get("home_team") if home_away == "home" else fixture.get("away_team"),
                    "opponent": fixture.get("away_team") if home_away == "home" else fixture.get("home_team"),
                    "home_away": home_away,
                    "player": player.get("name"),
                    "player_id": player_id,
                    "position": player.get("position_tag"),
                    "market": market_name,
                    "line": line,
                    "stat_type": "SOT" if is_sot else "shots",
                    "criteria": f"{window_size} games, {required_hits}+ hits",
                    "bookmaker": "BetMGM",
                    "price": price,
                    "hit_rate": f"{hits}/{window_size}",
                    "average": round(avg, 2),
                    "last_sequence": str(recent[:5]),
                    "team_ml": team_ml,
                    "opponent_rank": opponent_rank,
                }

                value_bets.append(pick)

    print(f"\n✅ Found {len(value_bets)} value bets for league {league_id}")
    return value_bets


def main():
    print("="*70)
    print("V4 BetMGM SINGLES - Shots & SOT")
    print("="*70)
    print(f"Min odds: {MIN_PRICE}")
    print(f"Window rules: {WINDOW_RULES}")
    print(f"Leagues: {LEAGUE_IDS}")
    print()

    all_picks = []

    for league_id in LEAGUE_IDS:
        picks = analyze_league(league_id)
        all_picks.extend(picks)

    # Save results
    if all_picks:
        output_lines = []
        output_lines.append(f"Generated at: {datetime.now().isoformat()}")
        output_lines.append(f"Total picks: {len(all_picks)}")
        output_lines.append(f"Bookmaker: BetMGM")
        output_lines.append("="*70)
        output_lines.append("")

        for i, pick in enumerate(all_picks, 1):
            output_lines.append(f"{i}. {pick['player']} - {pick['market']} @ {pick['price']:.2f}")
            output_lines.append(f"   {pick['fixture']}")
            output_lines.append(f"   Hit Rate: {pick['hit_rate']} | Avg: {pick['average']} | {pick['criteria']}")
            output_lines.append(f"   Team ML: {pick['team_ml']} | Opponent Rank: {pick['opponent_rank']}")
            output_lines.append(f"   Recent: {pick['last_sequence']}")
            output_lines.append("")

        OUT_FILE.write_text("\n".join(output_lines), encoding="utf-8")
        print(f"\n✅ Saved {len(all_picks)} picks to: {OUT_FILE}")

        # Send Telegram notifications
        notify_new_picks(all_picks)
    else:
        print("\n⚠️  No value bets found")

    print(f"\n{'='*70}")
    print(f"COMPLETE")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
