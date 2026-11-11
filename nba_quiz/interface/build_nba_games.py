"""
Build real NBA game data for the NBA intuition-study app.

Outputs nba_games_data.js containing:
  window.NBA_PRACTICE_GAME = {...};
  window.NBA_GAMES_DATA = [ ...5 game objects... ];

Install:
  pip install nba_api pandas

Run:
  python build_nba_games.py --season 2025-26 --n 5 --seed 123 --out nba_games_data.js

Notes:
  - Samples n+1 within-division games.
  - No team appears more than once across the full n+1 sample.
  - First sampled game is the instruction/example game.
  - Remaining n games are quiz games.
  - Does NOT write opponent guesses.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd
from nba_api.stats.endpoints.leaguegamelog import LeagueGameLog
from nba_api.stats.endpoints.playbyplayv3 import PlayByPlayV3

DIVISIONS = {
    "ATL": "Southeast", "CHA": "Southeast", "MIA": "Southeast", "ORL": "Southeast", "WAS": "Southeast",
    "BOS": "Atlantic", "BKN": "Atlantic", "NYK": "Atlantic", "PHI": "Atlantic", "TOR": "Atlantic",
    "CHI": "Central", "CLE": "Central", "DET": "Central", "IND": "Central", "MIL": "Central",
    "DAL": "Southwest", "HOU": "Southwest", "MEM": "Southwest", "NOP": "Southwest", "SAS": "Southwest",
    "DEN": "Northwest", "MIN": "Northwest", "OKC": "Northwest", "POR": "Northwest", "UTA": "Northwest",
    "GSW": "Pacific", "LAC": "Pacific", "LAL": "Pacific", "PHX": "Pacific", "SAC": "Pacific",
}

ABBR_TO_CITY = {
    "ATL": "Atlanta", "BOS": "Boston", "BKN": "Brooklyn", "CHA": "Charlotte", "CHI": "Chicago",
    "CLE": "Cleveland", "DAL": "Dallas", "DEN": "Denver", "DET": "Detroit", "GSW": "Golden State",
    "HOU": "Houston", "IND": "Indiana", "LAC": "LA", "LAL": "Los Angeles", "MEM": "Memphis",
    "MIA": "Miami", "MIL": "Milwaukee", "MIN": "Minnesota", "NOP": "New Orleans", "NYK": "New York",
    "OKC": "Oklahoma City", "ORL": "Orlando", "PHI": "Philadelphia", "PHX": "Phoenix", "POR": "Portland",
    "SAC": "Sacramento", "SAS": "San Antonio", "TOR": "Toronto", "UTA": "Utah", "WAS": "Washington",
}

ABBR_TO_NAME = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets", "CHA": "Charlotte Hornets",
    "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers", "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons", "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies", "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves", "NOP": "New Orleans Pelicans", "NYK": "New York Knicks",
    "OKC": "Oklahoma City Thunder", "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs", "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}


@dataclass(frozen=True)
class GamePick:
    game_id: str
    game_date: str
    home_abbr: str
    away_abbr: str


def js_string(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def parse_matchup(matchup: str) -> tuple[str, str] | None:
    if " vs. " in matchup:
        home, away = matchup.split(" vs. ")
        return home.strip(), away.strip()
    if " @ " in matchup:
        away, home = matchup.split(" @ ")
        return home.strip(), away.strip()
    return None


def load_within_division_games(season: str) -> list[GamePick]:
    log = LeagueGameLog(
        season=season,
        season_type_all_star="Regular Season",
        player_or_team_abbreviation="T",
        counter=0,
        direction="ASC",
        sorter="DATE",
        league_id="00",
        timeout=60,
    ).get_data_frames()[0]

    seen: dict[str, GamePick] = {}
    for _, row in log.iterrows():
        parsed = parse_matchup(str(row["MATCHUP"]))
        if not parsed:
            continue

        home, away = parsed
        if home not in DIVISIONS or away not in DIVISIONS:
            continue
        if DIVISIONS[home] != DIVISIONS[away]:
            continue

        game_id = str(row["GAME_ID"])
        seen[game_id] = GamePick(
            game_id=game_id,
            game_date=str(row["GAME_DATE"]),
            home_abbr=home,
            away_abbr=away,
        )

    return sorted(seen.values(), key=lambda g: (g.game_date, g.game_id))


def sample_games_no_repeated_teams(
    candidates: list[GamePick],
    total_needed: int,
    rng: random.Random,
    max_tries: int = 10000,
) -> list[GamePick]:
    for _ in range(max_tries):
        shuffled = candidates[:]
        rng.shuffle(shuffled)

        picks: list[GamePick] = []
        used_teams: set[str] = set()

        for game in shuffled:
            if game.home_abbr in used_teams or game.away_abbr in used_teams:
                continue

            picks.append(game)
            used_teams.add(game.home_abbr)
            used_teams.add(game.away_abbr)

            if len(picks) == total_needed:
                return picks

    raise RuntimeError(
        f"Could not sample {total_needed} games with no repeated teams. "
        "Try a different seed or season."
    )


def parse_clock_to_seconds_elapsed(period: int, clock: Any) -> float | None:
    if period < 1 or period > 4 or pd.isna(clock):
        return None

    s = str(clock)
    m = re.search(r"PT(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", s)

    if m:
        minutes_left = float(m.group(1) or 0)
        seconds_left = float(m.group(2) or 0)
    else:
        m2 = re.search(r"(\d+):(\d+(?:\.\d+)?)", s)
        if not m2:
            return None
        minutes_left = float(m2.group(1))
        seconds_left = float(m2.group(2))

    remaining = minutes_left * 60 + seconds_left
    return (period - 1) * 12 * 60 + (12 * 60 - remaining)


def coerce_score(x: Any) -> int | None:
    if x is None or pd.isna(x) or x == "":
        return None
    try:
        return int(float(x))
    except Exception:
        return None


def fetch_regulation_timeline(game: GamePick) -> list[dict[str, int]]:
    pbp = PlayByPlayV3(
        game_id=game.game_id,
        start_period=1,
        end_period=4,
        timeout=60,
    ).get_data_frames()[0]

    rows: list[tuple[float, int, int]] = [(0.0, 0, 0)]
    last_home = 0
    last_away = 0

    for _, row in pbp.iterrows():
        period = int(row.get("period", 0) or 0)
        elapsed = parse_clock_to_seconds_elapsed(period, row.get("clock"))

        if elapsed is None or elapsed < 0 or elapsed > 48 * 60:
            continue

        home_score = coerce_score(row.get("scoreHome"))
        away_score = coerce_score(row.get("scoreAway"))

        if home_score is not None:
            last_home = home_score
        if away_score is not None:
            last_away = away_score

        rows.append((elapsed, last_home, last_away))

    rows.sort(key=lambda x: x[0])

    timeline: list[dict[str, int]] = []
    idx = 0
    current_home = 0
    current_away = 0

    for minute in range(49):
        target = minute * 60
        while idx < len(rows) and rows[idx][0] <= target + 1e-6:
            _, current_home, current_away = rows[idx]
            idx += 1

        timeline.append({"minute": minute, "margin": int(current_home - current_away)})

    return timeline


def build_game_object(game: GamePick) -> dict[str, Any]:
    timeline = fetch_regulation_timeline(game)
    correct = timeline[-1]["margin"]

    return {
        "id": f"{game.game_id}_{js_string(game.away_abbr)}_{js_string(game.home_abbr)}",
        "gameId": game.game_id,
        "gameDate": game.game_date,
        "home": ABBR_TO_NAME[game.home_abbr],
        "away": ABBR_TO_NAME[game.away_abbr],
        "homeCity": ABBR_TO_CITY[game.home_abbr],
        "awayCity": ABBR_TO_CITY[game.away_abbr],
        "homeShort": ABBR_TO_CITY[game.home_abbr],
        "awayShort": ABBR_TO_CITY[game.away_abbr],
        "correct": int(correct),
        "timeline": timeline,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--n", type=int, default=5, help="Number of quiz games; one extra example game is also sampled.")
    parser.add_argument("--seed", type=int, default=123)
    parser.add_argument("--out", default="nba_games_data.js")
    parser.add_argument("--sleep", type=float, default=0.7)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    total_needed = args.n + 1

    candidates = load_within_division_games(args.season)
    if len(candidates) < total_needed:
        raise RuntimeError(f"Only found {len(candidates)} within-division games for {args.season}; need {total_needed}.")

    picks = sample_games_no_repeated_teams(candidates, total_needed, rng)

    objects: list[dict[str, Any]] = []
    for i, pick in enumerate(picks, start=1):
        label = "example" if i == 1 else f"quiz {i - 1}/{args.n}"
        print(f"[{label}] {pick.game_date} {pick.away_abbr} @ {pick.home_abbr} ({pick.game_id})")
        objects.append(build_game_object(pick))
        if i < len(picks):
            time.sleep(args.sleep)

    js = (
        "window.NBA_PRACTICE_GAME = " + json.dumps(objects[0], indent=2) + ";\n\n"
        "window.NBA_GAMES_DATA = " + json.dumps(objects[1:], indent=2) + ";\n"
    )

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(js)

    print(f"Wrote {args.out}: 1 example game + {len(objects) - 1} quiz games")


if __name__ == "__main__":
    main()