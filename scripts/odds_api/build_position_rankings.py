#!/usr/bin/env python3
"""
Build Position Rankings for Fouls/Tackles Value Betting

Analyzes player stats by position to create percentile rankings:
- Fouls committed by position
- Fouls drawn by position
- Tackles by position

Output: data/odds_api/position_rankings.json
"""
import json
import statistics
from pathlib import Path
from typing import Dict, List
from collections import defaultdict

# Paths
ROOT = Path(".")
FOULS_FILE = ROOT / "data" / "player_fouls" / "combined.json"
TACKLES_FILE = ROOT / "data" / "player_tackles" / "combined.json"
FOULS_DRAWN_FILE = ROOT / "data" / "player_fouls_drawn" / "combined.json"
OUTPUT_FILE = ROOT / "data" / "odds_api" / "position_rankings.json"

# Minimum games played to be included in rankings
MIN_GAMES = 5


def load_player_stats(file_path: Path, stat_key: str) -> List[dict]:
    """Load player stats from JSON file."""
    if not file_path.exists():
        print(f"⚠️  File not found: {file_path}")
        return []

    data = json.loads(file_path.read_text())
    players = []

    for player in data.get("players", []):
        position = player.get("position_tag")
        history = player.get(stat_key, [])

        if not position or not history:
            continue

        # Only include players with minimum games played
        if len(history) < MIN_GAMES:
            continue

        # Calculate average (use first 10 games for consistency)
        games_to_use = min(10, len(history))
        avg_stat = sum(history[:games_to_use]) / games_to_use

        players.append({
            "player_id": player.get("player_id"),
            "name": player.get("name"),
            "position": position,
            "avg": avg_stat,
            "games": games_to_use
        })

    return players


def calculate_position_rankings(players: List[dict]) -> Dict[str, dict]:
    """Calculate average and percentile rankings by position."""
    # Group players by position
    by_position = defaultdict(list)
    for player in players:
        by_position[player["position"]].append(player["avg"])

    # Calculate position stats
    position_stats = {}
    all_averages = [p["avg"] for p in players]

    for position, averages in by_position.items():
        if len(averages) < 3:  # Need at least 3 players for meaningful stats
            continue

        position_avg = statistics.mean(averages)

        # Calculate percentile: what % of all players have lower averages?
        lower_count = sum(1 for avg in all_averages if avg < position_avg)
        percentile = (lower_count / len(all_averages)) * 100

        position_stats[position] = {
            "avg": round(position_avg, 2),
            "percentile": round(percentile, 1),
            "player_count": len(averages),
            "median": round(statistics.median(averages), 2),
            "std_dev": round(statistics.stdev(averages), 2) if len(averages) > 1 else 0
        }

    return position_stats


def main():
    print("=" * 70)
    print("BUILDING POSITION RANKINGS")
    print("=" * 70)
    print()

    rankings = {
        "generated_at": Path(FOULS_FILE).stat().st_mtime if FOULS_FILE.exists() else None,
        "min_games": MIN_GAMES,
        "fouls_committed": {},
        "fouls_drawn": {},
        "tackles": {}
    }

    # 1. Fouls Committed
    print("📊 Analyzing fouls committed by position...")
    fouls_players = load_player_stats(FOULS_FILE, "fouls_last_n")
    if fouls_players:
        rankings["fouls_committed"] = calculate_position_rankings(fouls_players)
        print(f"   ✓ Analyzed {len(fouls_players)} players")
        print(f"   ✓ Found {len(rankings['fouls_committed'])} positions")
    else:
        print("   ✗ No fouls data found")

    # 2. Fouls Drawn
    print("\n📊 Analyzing fouls drawn by position...")
    if FOULS_DRAWN_FILE.exists():
        fouls_drawn_players = load_player_stats(FOULS_DRAWN_FILE, "fouls_drawn_last_n")
        if fouls_drawn_players:
            rankings["fouls_drawn"] = calculate_position_rankings(fouls_drawn_players)
            print(f"   ✓ Analyzed {len(fouls_drawn_players)} players")
            print(f"   ✓ Found {len(rankings['fouls_drawn'])} positions")
        else:
            print("   ✗ No fouls drawn data found")
    else:
        print("   ⚠️  Fouls drawn file not found")

    # 3. Tackles
    print("\n📊 Analyzing tackles by position...")
    tackles_players = load_player_stats(TACKLES_FILE, "tackles_last_n")
    if tackles_players:
        rankings["tackles"] = calculate_position_rankings(tackles_players)
        print(f"   ✓ Analyzed {len(tackles_players)} players")
        print(f"   ✓ Found {len(rankings['tackles'])} positions")
    else:
        print("   ✗ No tackles data found")

    # Save rankings
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(rankings, indent=2))

    print(f"\n{'=' * 70}")
    print(f"✅ Rankings saved to: {OUTPUT_FILE}")
    print(f"{'=' * 70}")

    # Print sample rankings
    print("\n📈 TOP POSITIONS FOR FOULS COMMITTED (most fouls):")
    if rankings["fouls_committed"]:
        sorted_fouls = sorted(
            rankings["fouls_committed"].items(),
            key=lambda x: x[1]["avg"],
            reverse=True
        )
        for pos, stats in sorted_fouls[:10]:
            print(f"   {pos:6} → Avg: {stats['avg']:.2f} | Percentile: {stats['percentile']:.1f}% | Players: {stats['player_count']}")

    print("\n📈 TOP POSITIONS FOR FOULS DRAWN (draw most fouls):")
    if rankings["fouls_drawn"]:
        sorted_drawn = sorted(
            rankings["fouls_drawn"].items(),
            key=lambda x: x[1]["avg"],
            reverse=True
        )
        for pos, stats in sorted_drawn[:10]:
            print(f"   {pos:6} → Avg: {stats['avg']:.2f} | Percentile: {stats['percentile']:.1f}% | Players: {stats['player_count']}")

    print("\n📈 TOP POSITIONS FOR TACKLES (most tackles):")
    if rankings["tackles"]:
        sorted_tackles = sorted(
            rankings["tackles"].items(),
            key=lambda x: x[1]["avg"],
            reverse=True
        )
        for pos, stats in sorted_tackles[:10]:
            print(f"   {pos:6} → Avg: {stats['avg']:.2f} | Percentile: {stats['percentile']:.1f}% | Players: {stats['player_count']}")

    print()


if __name__ == "__main__":
    main()
