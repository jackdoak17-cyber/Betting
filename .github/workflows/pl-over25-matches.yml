name: Posts — Over 2.5 (All Leagues, Combined%)

on:
  # Run after fixtures job has refreshed local fixture files
  schedule:
    - cron: "25 7 * * *"   # Daily at 07:25 UTC
  workflow_dispatch: {}

permissions:
  contents: write

concurrency:
  group: over25-matches-all
  cancel-in-progress: false

jobs:
  build-and-post:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        league:
          # Exclude cups: 24 (FA Cup), 27 (Carabao Cup), 390 (Coppa Italia), 570 (Copa del Rey)
          - { id: 8,   name: "Premier League" }
          - { id: 9,   name: "Championship" }
          - { id: 72,  name: "Eredivisie" }
          - { id: 82,  name: "Bundesliga" }
          - { id: 181, name: "Admiral Bundesliga" }
          - { id: 208, name: "Pro League" }
          - { id: 244, name: "1. HNL" }
          - { id: 271, name: "Superliga" }
          - { id: 301, name: "Ligue 1" }
          - { id: 384, name: "Serie A" }
          - { id: 387, name: "Serie B" }
          - { id: 444, name: "Eliteserien" }
          - { id: 453, name: "Ekstraklasa" }
          - { id: 462, name: "Liga Portugal" }
          - { id: 486, name: "Premier League" }
          - { id: 501, name: "Premiership" }
          - { id: 564, name: "La Liga" }
          - { id: 567, name: "La Liga 2" }
          - { id: 573, name: "Allsvenskan" }
          - { id: 591, name: "Super League" }
          - { id: 600, name: "Super Lig" }

    env:
      PYTHONUNBUFFERED: "1"
      LAST_N: "10"
      MIN_GAMES: "6"
      MAX_ROWS: "20"
      LOOKBACK_DAYS: "140"
      SPORTMONKS_TOKEN: ${{ secrets.SPORTMONKS_TOKEN }}

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          pip install requests

      - name: Guard — ensure local fixtures exist for this league
        id: guard
        run: |
          set -e
          LID="${{ matrix.league.id }}"
          if [ -f "data/fixtures/by_league/${LID}.json" ]; then
            echo "ok=true" >> "$GITHUB_OUTPUT"
          else
            echo "No fixtures file for league ${LID}; skipping."
            echo "ok=false" >> "$GITHUB_OUTPUT"
          fi

      - name: Generate Over 2.5 post
        if: steps.guard.outputs.ok == 'true'
        env:
          LEAGUE_ID: ${{ matrix.league.id }}
          OUTPUT_PATH: posts/over25_matches_L${{ matrix.league.id }}.md
        run: |
          python scripts/posts/over25_matches.py

      - name: Show output (first 80 lines)
        if: steps.guard.outputs.ok == 'true'
        run: |
          echo "----- posts/over25_matches_L${{ matrix.league.id }}.md -----"
          head -n 80 "posts/over25_matches_L${{ matrix.league.id }}.md" || true
          echo "-----------------------------------------------------------"

      - name: Commit & push if changed
        if: steps.guard.outputs.ok == 'true'
        run: |
          set -euo pipefail
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

          git add "posts/over25_matches_L${{ matrix.league.id }}.md"

          if git diff --staged --quiet; then
            echo "No changes to commit for ${{ matrix.league.name }} (L${{ matrix.league.id }})."
            exit 0
          fi

          git commit -m "Update: Over 2.5 Candidates — ${{ matrix.league.name }} (L${{ matrix.league.id }}) [skip ci]"
          BRANCH="${GITHUB_REF_NAME:-main}"
          git pull --rebase --autostash origin "$BRANCH" || true
          git push origin HEAD:"$BRANCH"
