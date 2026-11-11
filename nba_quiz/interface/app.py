#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from flask import Flask, Response, jsonify, render_template_string, request

# =========================
# CONFIG CONSTANTS
# =========================
PRETEST: int = int(os.environ.get("PRETEST", "0"))
QUESTION_SECONDS: int = int(os.environ.get("QUESTION_SECONDS", "20"))
FIRST_PAGE_WAIT_SECONDS: int = int(os.environ.get("FIRST_PAGE_WAIT_SECONDS", "10"))
BONUS_PER_WIN: float = float(os.environ.get("BONUS_PER_WIN", "0.10"))
CONFIDENCE_BONUS: float = float(os.environ.get("CONFIDENCE_BONUS", "0.25"))
REVIEW_MS_PER_GAME_MINUTE: int = int(os.environ.get("REVIEW_MS_PER_GAME_MINUTE", "250"))
FINAL_CODE: str = os.environ.get("FINAL_CODE", "1234")
MIN_PRETEST_OPPONENTS: int = int(os.environ.get("MIN_PRETEST_OPPONENTS", "2"))

HOST: str = os.environ.get("HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PORT", "8080"))
DATABASE_PATH: str = os.environ.get("DATABASE_PATH", "./data")
DB_FILENAME: str = os.environ.get("DB_FILENAME", "exper.db")
DATA_FILENAME: str = os.environ.get("NBA_DATA_FILENAME", "nba_games_data.js")
PRE_POOL_FILENAME: str = os.environ.get("PRE_POOL_FILENAME", os.path.join("..", "data", "pretest_pool_data.csv"))

# Reset the balanced random-assignment counts by changing this UTC timestamp and redeploying.
# Only qualifying subjects created at or after this instant are included in the balance counts.
BALANCE_START_UTC: str = os.environ.get(
    "BALANCE_START_UTC",
    "2026-08-10T15:48:00+00:00",
)

# An assigned participant remains in the balancing counts while active. If they
# have fewer than all 10 quiz answers and no recorded activity for this many
# minutes, they are treated as having left the study and no longer count.
BALANCE_ABANDONED_AFTER_MINUTES: int = int(
    os.environ.get("BALANCE_ABANDONED_AFTER_MINUTES", "20")
)


# =========================
# UTILITIES
# =========================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path() -> str:
    os.makedirs(DATABASE_PATH, exist_ok=True)
    return os.path.join(DATABASE_PATH, DB_FILENAME)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def distance(answer: Any, correct: int) -> float:
    try:
        return abs(float(answer) - float(correct))
    except Exception:
        return float("inf")


def outcome_for(answer: int, opponent_answer: int, correct: int) -> str:
    yours = distance(answer, correct)
    theirs = distance(opponent_answer, correct)
    if yours < theirs:
        return "won"
    if yours > theirs:
        return "lost"
    return "tied"


def parse_js_assignment(text: str, name: str) -> Any:
    pattern = rf"window\.{re.escape(name)}\s*=\s*(.*?);\s*(?=window\.|\Z)"
    m = re.search(pattern, text, flags=re.S)
    if not m:
        return None
    return json.loads(m.group(1))


def default_timeline(final_margin: int) -> list[dict[str, int]]:
    return [
        {"minute": minute, "margin": int(round(final_margin * (minute / 48.0)))}
        for minute in range(49)
    ]


def fallback_games() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    practice = {
        "id": "example_okc_den",
        "gameId": "example_okc_den",
        "gameDate": "Example",
        "home": "Oklahoma City Thunder",
        "away": "Denver Nuggets",
        "homeCity": "Oklahoma City",
        "awayCity": "Denver",
        "homeShort": "Oklahoma City",
        "awayShort": "Denver",
        "correct": 3,
        "timeline": default_timeline(3),
    }

    specs = [
        ("lac_pho", "LA Clippers", "Phoenix Suns", "LA", "Phoenix", -11),
        ("bos_tor", "Boston Celtics", "Toronto Raptors", "Boston", "Toronto", 14),
        ("mem_sas", "Memphis Grizzlies", "San Antonio Spurs", "Memphis", "San Antonio", 1),
        ("por_uta", "Portland Trail Blazers", "Utah Jazz", "Portland", "Utah", 10),
        ("cho_orl", "Charlotte Hornets", "Orlando Magic", "Charlotte", "Orlando", -16),
    ]

    games = []
    for gid, home, away, home_city, away_city, correct in specs:
        games.append({
            "id": gid,
            "gameId": gid,
            "gameDate": "Fallback",
            "home": home,
            "away": away,
            "homeCity": home_city,
            "awayCity": away_city,
            "homeShort": home_city,
            "awayShort": away_city,
            "correct": correct,
            "timeline": default_timeline(correct),
        })

    return practice, games


def load_game_data() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATA_FILENAME)
    practice_default, games_default = fallback_games()

    if not os.path.exists(path):
        return practice_default, games_default

    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()

        practice = parse_js_assignment(txt, "NBA_PRACTICE_GAME") or practice_default
        games = parse_js_assignment(txt, "NBA_GAMES_DATA") or games_default

        if not isinstance(games, list) or len(games) < 1:
            return practice_default, games_default

        return practice, games[:5]
    except Exception:
        return practice_default, games_default


PRACTICE_GAME, QUIZ_GAMES = load_game_data()
GAME_BY_ID = {str(g["id"]): g for g in QUIZ_GAMES}


def parse_utc_cutoff(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RuntimeError(
            "BALANCE_START_UTC must be ISO-8601, for example "
            "2026-07-23T00:00:00+00:00"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


BALANCE_START_DT = parse_utc_cutoff(BALANCE_START_UTC)
BALANCE_START_ISO = BALANCE_START_DT.isoformat()


def load_pre_pool() -> tuple[dict[str, dict[str, int]], list[str]]:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), PRE_POOL_FILENAME)
    if not os.path.exists(path):
        raise RuntimeError(f"Fixed opponent-pool CSV not found: {path}")

    pool: dict[str, dict[str, int]] = {}
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"pool_subject_id", "game_id", "answer"}
        if set(reader.fieldnames or []) != required:
            raise RuntimeError(
                f"{PRE_POOL_FILENAME} must contain exactly these columns: "
                "pool_subject_id, game_id, answer"
            )

        for row_number, row in enumerate(reader, start=2):
            pool_subject_id = str(row.get("pool_subject_id") or "").strip()
            game_id = str(row.get("game_id") or "").strip()
            if not pool_subject_id:
                raise RuntimeError(f"Missing pool_subject_id on CSV row {row_number}")
            if game_id not in GAME_BY_ID:
                raise RuntimeError(f"Unknown game_id on CSV row {row_number}: {game_id}")
            try:
                answer = int(str(row.get("answer") or "").strip())
            except ValueError as exc:
                raise RuntimeError(f"Non-integer answer on CSV row {row_number}") from exc

            answers = pool.setdefault(pool_subject_id, {})
            if game_id in answers:
                raise RuntimeError(
                    f"Duplicate fixed-pool answer for {pool_subject_id}, {game_id}"
                )
            answers[game_id] = answer

    expected_games = set(GAME_BY_ID)
    for pool_subject_id, answers in pool.items():
        if set(answers) != expected_games:
            missing = sorted(expected_games - set(answers))
            extra = sorted(set(answers) - expected_games)
            raise RuntimeError(
                f"Fixed-pool participant {pool_subject_id} has invalid game coverage; "
                f"missing={missing}, extra={extra}"
            )

    pool_ids = sorted(pool)
    if len(pool_ids) < MIN_PRETEST_OPPONENTS:
        raise RuntimeError(
            f"Fixed opponent pool has {len(pool_ids)} participants; "
            f"at least {MIN_PRETEST_OPPONENTS} are required"
        )
    return pool, pool_ids


PRE_POOL, PRE_POOL_IDS = load_pre_pool()


def stable_fake_guess(subject_id: int, round_id: str, game_id: str, correct: int) -> int:
    seed = f"{subject_id}|{round_id}|{game_id}|fallback-v3"
    rng = random.Random(seed)
    noise = int(round(rng.gauss(0, 11)))
    if noise == 0:
        noise = rng.choice([-6, 6])
    return max(-40, min(40, int(correct + noise)))


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r["name"]) for r in rows}
    except Exception:
        return set()


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    conn = get_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subjects (
              subject_id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_utc TEXT NOT NULL,
              last_activity_utc TEXT,
              pretest INTEGER NOT NULL,
              url_params_json TEXT NOT NULL,
              prolific_pid TEXT,
              qualtrics_id TEXT,
              r1_order_json TEXT NOT NULL,
              r2_order_json TEXT NOT NULL,
              r1_opponent_subject_id INTEGER,
              r2_opponent_subject_id INTEGER,
              r1_pool_opponent_id TEXT,
              r2_pool_opponent_id TEXT,
              r1_opponent_source TEXT,
              r2_opponent_source TEXT,
              review_option_a_game_id TEXT,
              review_option_a_is_win INTEGER,
              review_option_b_game_id TEXT,
              review_choice_game_id TEXT,
              review_choice_reason TEXT,
              assigned_review_game_id TEXT,
              assigned_review_method TEXT,
              balanced_review_condition TEXT,
              balance_win_count_before INTEGER,
              balance_loss_count_before INTEGER,
              balance_cutoff_utc TEXT,
              review_assignment_utc TEXT,
              review_identification_wrong_guesses INTEGER,
              review_identification_sequence_json TEXT,
              review_margin_wrong_guesses INTEGER,
              review_margin_sequence_json TEXT,
              review_margin_used_reopen INTEGER,
              review_checks_completed_utc TEXT,
              comprehension_attempts INTEGER,
              comp_check_history TEXT,
              demo_outcome TEXT,
              comprehension_completed_utc TEXT,
              instructions_3_time_ms INTEGER,
              post_r2_confidence_percent INTEGER,
              post_r2_confidence_time_ms INTEGER,
              post_r2_confidence_game_ids_json TEXT,
              post_r2_confidence_completed_utc TEXT,
              post_r2_confidence_details_clicked INTEGER,
              post_r2_confidence_details_click_count INTEGER,
              post_r2_confidence_details_open_ms INTEGER,
              post_r2_confidence_bonus_threshold_percent INTEGER,
              post_r2_confidence_bonus_branch TEXT,
              post_r2_confidence_performance_wins INTEGER,
              post_r2_confidence_performance_success INTEGER,
              post_r2_confidence_lottery_draw INTEGER,
              post_r2_confidence_bonus_awarded INTEGER,
              post_r2_confidence_bonus_amount REAL,
              post_r2_confidence_bonus_evaluated_utc TEXT,
              final_code TEXT NOT NULL,
              completed_utc TEXT
            );

            CREATE TABLE IF NOT EXISTS responses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              subject_id INTEGER NOT NULL,
              round_id TEXT NOT NULL,
              question_index INTEGER NOT NULL,
              game_id TEXT NOT NULL,
              answer INTEGER NOT NULL,
              timed_out INTEGER NOT NULL,
              response_time_ms INTEGER,
              client_ts TEXT,
              server_utc TEXT NOT NULL,
              correct INTEGER NOT NULL,
              opponent_subject_id INTEGER,
              opponent_pool_id TEXT,
              opponent_answer INTEGER,
              opponent_source TEXT,
              outcome TEXT,
              participant_distance REAL,
              opponent_distance REAL,
              UNIQUE(subject_id, round_id, game_id),
              FOREIGN KEY(subject_id) REFERENCES subjects(subject_id)
            );

            CREATE TABLE IF NOT EXISTS rounds (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              subject_id INTEGER NOT NULL,
              round_id TEXT NOT NULL,
              completed_utc TEXT NOT NULL,
              responses_json TEXT NOT NULL,
              FOREIGN KEY(subject_id) REFERENCES subjects(subject_id)
            );

            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              subject_id INTEGER,
              event_name TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              client_ts TEXT,
              server_utc TEXT NOT NULL,
              FOREIGN KEY(subject_id) REFERENCES subjects(subject_id)
            );
            """
        )

        add_column_if_missing(conn, "subjects", "last_activity_utc", "last_activity_utc TEXT")
        add_column_if_missing(conn, "subjects", "r1_pool_opponent_id", "r1_pool_opponent_id TEXT")
        add_column_if_missing(conn, "subjects", "r2_pool_opponent_id", "r2_pool_opponent_id TEXT")
        add_column_if_missing(conn, "subjects", "review_choice_reason", "review_choice_reason TEXT")
        add_column_if_missing(conn, "subjects", "balanced_review_condition", "balanced_review_condition TEXT")
        add_column_if_missing(conn, "subjects", "balance_win_count_before", "balance_win_count_before INTEGER")
        add_column_if_missing(conn, "subjects", "balance_loss_count_before", "balance_loss_count_before INTEGER")
        add_column_if_missing(conn, "subjects", "balance_cutoff_utc", "balance_cutoff_utc TEXT")
        add_column_if_missing(conn, "subjects", "review_assignment_utc", "review_assignment_utc TEXT")
        add_column_if_missing(conn, "subjects", "review_option_a_is_win", "review_option_a_is_win INTEGER")
        add_column_if_missing(conn, "subjects", "review_identification_wrong_guesses", "review_identification_wrong_guesses INTEGER")
        add_column_if_missing(conn, "subjects", "review_identification_sequence_json", "review_identification_sequence_json TEXT")
        add_column_if_missing(conn, "subjects", "review_margin_wrong_guesses", "review_margin_wrong_guesses INTEGER")
        add_column_if_missing(conn, "subjects", "review_margin_sequence_json", "review_margin_sequence_json TEXT")
        add_column_if_missing(conn, "subjects", "review_margin_used_reopen", "review_margin_used_reopen INTEGER")
        add_column_if_missing(conn, "subjects", "review_checks_completed_utc", "review_checks_completed_utc TEXT")
        add_column_if_missing(conn, "subjects", "comprehension_attempts", "comprehension_attempts INTEGER")
        add_column_if_missing(conn, "subjects", "comp_check_history", "comp_check_history TEXT")
        add_column_if_missing(conn, "subjects", "demo_outcome", "demo_outcome TEXT")
        add_column_if_missing(conn, "subjects", "comprehension_completed_utc", "comprehension_completed_utc TEXT")
        add_column_if_missing(conn, "subjects", "instructions_3_time_ms", "instructions_3_time_ms INTEGER")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_percent", "post_r2_confidence_percent INTEGER")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_time_ms", "post_r2_confidence_time_ms INTEGER")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_game_ids_json", "post_r2_confidence_game_ids_json TEXT")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_completed_utc", "post_r2_confidence_completed_utc TEXT")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_details_clicked", "post_r2_confidence_details_clicked INTEGER")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_details_click_count", "post_r2_confidence_details_click_count INTEGER")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_details_open_ms", "post_r2_confidence_details_open_ms INTEGER")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_bonus_threshold_percent", "post_r2_confidence_bonus_threshold_percent INTEGER")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_bonus_branch", "post_r2_confidence_bonus_branch TEXT")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_performance_wins", "post_r2_confidence_performance_wins INTEGER")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_performance_success", "post_r2_confidence_performance_success INTEGER")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_lottery_draw", "post_r2_confidence_lottery_draw INTEGER")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_bonus_awarded", "post_r2_confidence_bonus_awarded INTEGER")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_bonus_amount", "post_r2_confidence_bonus_amount REAL")
        add_column_if_missing(conn, "subjects", "post_r2_confidence_bonus_evaluated_utc", "post_r2_confidence_bonus_evaluated_utc TEXT")
        add_column_if_missing(conn, "responses", "response_time_ms", "response_time_ms INTEGER")
        add_column_if_missing(conn, "responses", "opponent_pool_id", "opponent_pool_id TEXT")

        if "pretest" in table_columns(conn, "subjects"):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subjects_pretest ON subjects(pretest)")
        if {"subject_id", "round_id"}.issubset(table_columns(conn, "responses")):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_responses_subject_round ON responses(subject_id, round_id)")
        if {"round_id", "game_id"}.issubset(table_columns(conn, "responses")):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_responses_round_game ON responses(round_id, game_id)")
        if {"subject_id", "round_id"}.issubset(table_columns(conn, "rounds")):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rounds_subject_round ON rounds(subject_id, round_id)")
        if "subject_id" in table_columns(conn, "events"):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_subject ON events(subject_id)")

        conn.commit()
    finally:
        conn.close()


def touch_subject(conn: sqlite3.Connection, subject_id: Optional[int]) -> None:
    if subject_id is not None:
        conn.execute(
            "UPDATE subjects SET last_activity_utc=? WHERE subject_id=?",
            (utc_now_iso(), int(subject_id)),
        )


def log_event(
    conn: sqlite3.Connection,
    subject_id: Optional[int],
    event_name: str,
    payload: Any,
    client_ts: Optional[str] = None,
) -> None:
    now = utc_now_iso()
    if subject_id is not None:
        conn.execute(
            "UPDATE subjects SET last_activity_utc=? WHERE subject_id=?",
            (now, int(subject_id)),
        )
    conn.execute(
        "INSERT INTO events(subject_id,event_name,payload_json,client_ts,server_utc) VALUES (?,?,?,?,?)",
        (subject_id, event_name, json.dumps(payload), client_ts, now),
    )


def assign_opponent_if_needed(
    conn: sqlite3.Connection,
    subject_id: int,
    round_id: str,
) -> tuple[Optional[str], str]:
    pool_col = "r1_pool_opponent_id" if round_id == "r1" else "r2_pool_opponent_id"
    source_col = "r1_opponent_source" if round_id == "r1" else "r2_opponent_source"

    subj = conn.execute(
        f"""
        SELECT
            {pool_col} AS pool_opponent_id,
            {source_col} AS source,
            r1_pool_opponent_id
        FROM subjects
        WHERE subject_id=?
        """,
        (subject_id,),
    ).fetchone()

    if subj and subj["source"]:
        existing = subj["pool_opponent_id"]
        return (str(existing) if existing is not None else None, str(subj["source"]))

    candidates = PRE_POOL_IDS[:]
    if round_id == "r2" and subj and subj["r1_pool_opponent_id"] is not None:
        prior = str(subj["r1_pool_opponent_id"])
        candidates = [pool_id for pool_id in candidates if pool_id != prior]

    if not candidates:
        conn.execute(
            f"UPDATE subjects SET {pool_col}=NULL, {source_col}=? WHERE subject_id=?",
            ("fallback_fake", subject_id),
        )
        log_event(
            conn,
            subject_id,
            "opponent_assigned",
            {"round_id": round_id, "source": "fallback_fake", "pool_n": 0},
        )
        conn.commit()
        return None, "fallback_fake"

    pool_opponent_id = random.choice(candidates)
    conn.execute(
        f"UPDATE subjects SET {pool_col}=?, {source_col}=? WHERE subject_id=?",
        (pool_opponent_id, "pilot_csv_pool", subject_id),
    )
    log_event(
        conn,
        subject_id,
        "opponent_assigned",
        {
            "round_id": round_id,
            "source": "pilot_csv_pool",
            "pool_n": len(PRE_POOL_IDS),
            "pool_opponent_id": pool_opponent_id,
            "excluded_r1_pool_opponent_id": (
                str(subj["r1_pool_opponent_id"])
                if round_id == "r2" and subj and subj["r1_pool_opponent_id"] is not None
                else None
            ),
        },
    )
    conn.commit()
    return pool_opponent_id, "pilot_csv_pool"


def opponent_answer_for(
    conn: sqlite3.Connection,
    subject_id: int,
    round_id: str,
    game_id: str,
    correct: int,
) -> tuple[Optional[int], Optional[str], Optional[int], str]:
    if PRETEST == 1:
        return None, None, None, "none_pretest"

    pool_opponent_id, source = assign_opponent_if_needed(conn, subject_id, round_id)
    if source == "pilot_csv_pool" and pool_opponent_id is not None:
        answer = PRE_POOL.get(pool_opponent_id, {}).get(game_id)
        if answer is not None:
            return None, pool_opponent_id, int(answer), "pilot_csv_pool"

    return (
        None,
        None,
        stable_fake_guess(subject_id, round_id, game_id, correct),
        "fallback_fake",
    )


def balanced_assignment_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Count eligible balanced assignments in the current reset window.

    Completed participants always count. An incomplete assigned participant also
    counts unless they have had no server-recorded activity for the configured
    abandonment window.
    """
    inactivity_cutoff = (
        datetime.now(timezone.utc)
        - timedelta(minutes=BALANCE_ABANDONED_AFTER_MINUTES)
    ).isoformat()
    total_answers = len(QUIZ_GAMES) * 2

    rows = conn.execute(
        """
        SELECT s.balanced_review_condition AS condition, COUNT(*) AS n
        FROM subjects s
        WHERE s.pretest = 0
          AND s.created_utc >= ?
          AND s.comprehension_attempts BETWEEN 1 AND 3
          AND s.balanced_review_condition IN ('win', 'loss')
          AND s.assigned_review_method IN ('balanced_random_assignment', 'balanced_no_preference')
          AND EXISTS (
              SELECT 1 FROM responses r
              WHERE r.subject_id = s.subject_id
                AND r.round_id = 'r1'
                AND r.outcome = 'won'
          )
          AND EXISTS (
              SELECT 1 FROM responses r
              WHERE r.subject_id = s.subject_id
                AND r.round_id = 'r1'
                AND r.outcome = 'lost'
          )
          AND (
              (
                  SELECT COUNT(DISTINCT r.round_id || ':' || r.game_id)
                  FROM responses r
                  WHERE r.subject_id = s.subject_id
                    AND r.round_id IN ('r1', 'r2')
              ) = ?
              OR COALESCE(
                  s.last_activity_utc,
                  s.review_assignment_utc,
                  s.created_utc
              ) >= ?
          )
        GROUP BY s.balanced_review_condition
        """,
        (BALANCE_START_ISO, total_answers, inactivity_cutoff),
    ).fetchall()

    counts = {"win": 0, "loss": 0}
    for row in rows:
        counts[str(row["condition"])] = int(row["n"])
    return counts

def choose_balanced_condition(counts: dict[str, int]) -> str:
    if counts["win"] < counts["loss"]:
        return "win"
    if counts["loss"] < counts["win"]:
        return "loss"
    return random.choice(["win", "loss"])

def all_responses_for_round(conn: sqlite3.Connection, subject_id: int, round_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM responses WHERE subject_id=? AND round_id=? ORDER BY question_index ASC, id ASC",
        (subject_id, round_id),
    ).fetchall()
    return [dict(r) for r in rows]


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("NBA_QUIZ_SECRET_KEY", os.urandom(24))
    app.config.update(SESSION_COOKIE_SAMESITE="None", SESSION_COOKIE_SECURE=True)

    HTML = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>NBA Intuition Study</title>
  <style>
    :root {
      --text: #111; --muted: #555; --line: #ddd; --button: #111; --buttonText: #fff;
      --green: #16823a; --red: #b3261e; --gold: #b8860b; --light: #f7f7f7;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: #fff; color: var(--text); font-family: Arial, Helvetica, sans-serif; line-height: 1.55; }
    main { max-width: 1040px; margin: 0 auto; padding: 46px 24px 72px; }
    h1 { font-size: 28px; margin: 0 0 24px; line-height: 1.2; }
    h2 { font-size: 22px; margin: 0 0 18px; line-height: 1.25; }
    p { font-size: 18px; margin: 0 0 18px; }
    ul { font-size: 18px; margin: 18px 0 0 24px; padding: 0; }
    li { margin-bottom: 8px; }
    .word-won, .won { color: var(--green); font-weight: bold; }
    .word-lost, .lost { color: var(--red); font-weight: bold; }
    .tied { color: var(--gold); font-weight: bold; }
    .card { border: 1px solid var(--line); border-radius: 12px; padding: 28px; margin: 24px 0; background: #fff; }
    .practice-question { background: var(--light); border-left: 4px solid #111; padding: 16px 18px; margin: 20px 0; font-size: 17px; }
    button { appearance: none; border: 0; background: var(--button); color: var(--buttonText); font-size: 18px; padding: 12px 20px; border-radius: 8px; cursor: pointer; margin-top: 12px; }
    button:hover { opacity: 0.9; }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    .secondary-button { background: #fff; color: #111; border: 1px solid #111; }
    .question-topline { display: flex; justify-content: space-between; gap: 20px; align-items: center; margin-bottom: 24px; font-size: 20px; color: var(--muted); }
    .timer { font-weight: bold; color: #111; }
    .timer.urgent { color: var(--red); }
    .question-text { font-size: 28px; line-height: 1.5; margin-bottom: 34px; }
    input[type="number"] { width: 225px; font-size: 24px; padding: 10px 12px; border: 2px solid #111; border-radius: 8px; }
    label { display: block; font-size: 22px; margin-bottom: 10px; color: var(--muted); }
    .answer-rules { margin-top: 26px; border-top: 1px solid var(--line); padding-top: 18px; }
    .interstitial { max-width: 760px; margin: 80px auto; text-align: center; }
    .interstitial h1 { margin-bottom: 16px; }
    .interstitial ul { text-align: left; max-width: 720px; margin-left: auto; margin-right: auto; }
    .feedback-grid { display: grid; grid-template-columns: 1fr auto 1fr; gap: 14px; align-items: stretch; margin-top: 20px; }
    .feedback-card { border: 1px solid var(--line); border-radius: 12px; padding: 18px; min-height: 180px; display: flex; flex-direction: column; justify-content: space-between; }
    .no-pref-wrap { display: flex; align-items: center; justify-content: center; }
    .review-choice-button.selected { background: var(--green); color: #fff; border-color: var(--green); opacity: 1; }
    .review-reason-card { padding: 20px; margin-top: 16px; }
    .review-reason-card textarea { min-height: 110px; }
    .result-status { font-size: 22px; font-weight: bold; margin: 14px 0; }
    .chart-wrap { margin-top: 20px; }
    canvas { width: 100%; max-width: 1000px; height: 440px; border: 1px solid var(--line); border-radius: 10px; display: block; background: #fff; }
    .chart-controls { margin-top: 12px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    .hidden { display: none !important; }
    .status-message { font-size: 22px; font-weight: bold; margin-bottom: 18px; }
    .check-grid { display: grid; gap: 14px; margin: 20px 0; }
    .check-row { display: grid; grid-template-columns: 1fr 180px 42px; gap: 12px; align-items: center; }
    .check-row .desc { font-size: 18px; }
    .check-mark { font-size: 28px; font-weight: 800; line-height: 1; }
    .check-mark.good { color: var(--green); }
    .check-mark.bad { color: var(--red); }
    .check-message { font-size: 18px; font-weight: 700; min-height: 28px; margin-top: 8px; }
    .check-message.good { color: var(--green); }
    .check-message.bad { color: var(--red); }
    .comprehension-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 12px; }
    .comprehension-actions button { margin-top: 0; }
    .bonus-eligibility-note { font-size: 16px; font-weight: 700; margin: 0 0 0 4px; }
    .choice-list { display: grid; gap: 12px; margin: 20px 0; }
    .choice-option { display: flex; gap: 10px; align-items: flex-start; border: 1px solid var(--line); border-radius: 10px; padding: 14px; font-size: 18px; cursor: pointer; }
    .choice-option input { margin-top: 5px; }
    textarea { width: 100%; min-height: 150px; resize: vertical; font-size: 18px; padding: 12px; border: 2px solid #111; border-radius: 8px; font-family: Arial, Helvetica, sans-serif; }
    .modal-backdrop { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.55); display: flex; align-items: center; justify-content: center; padding: 22px; z-index: 9999; }
    .modal-card { background: #fff; border-radius: 12px; padding: 20px; width: min(1080px, 100%); max-height: 94vh; overflow: auto; box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35); }
    .modal-card canvas { height: 440px; }
    .instruction-flow { display: grid; grid-template-columns: 1fr 54px 1fr 54px 1fr; gap: 12px; align-items: center; margin: 28px 0 20px; }
    .instruction-step { min-height: 190px; border: 2px solid #111; border-radius: 12px; padding: 20px 18px; display: flex; flex-direction: column; justify-content: center; background: #fff; text-align: center; }
    .instruction-step-number { width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center; margin: 0 auto 12px; background: #111; color: #fff; font-size: 18px; font-weight: 800; }
    .instruction-step p { margin: 0; font-size: 17px; line-height: 1.45; }
    .instruction-arrow { font-size: 38px; font-weight: 800; text-align: center; line-height: 1; }
    .prediction-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin: 24px 0 34px; }
    .prediction-card { border: 1px solid var(--line); border-radius: 12px; padding: 20px; min-height: 165px; display: flex; flex-direction: column; justify-content: space-between; text-align: center; }
    .prediction-card h2 { font-size: 18px; margin-bottom: 16px; }
    .prediction-value { font-size: 18px; line-height: 1.35; font-weight: 700; }
    .confidence-wrap { margin: 28px 0 8px; }
    .confidence-slider {
      --slider-fill: 50%;
      width: 100%;
      margin: 8px 0 0;
      cursor: pointer;
      appearance: none;
      -webkit-appearance: none;
      background: transparent;
    }
    .confidence-slider::-webkit-slider-runnable-track {
      height: 10px;
      border: 1px solid #999;
      border-radius: 999px;
      background: linear-gradient(to right, #0b67d0 0 var(--slider-fill), #fff var(--slider-fill) 100%);
    }
    .confidence-slider.untouched::-webkit-slider-runnable-track { background: #fff; }
    .confidence-slider::-moz-range-track {
      height: 10px;
      border: 1px solid #999;
      border-radius: 999px;
      background: #fff;
    }
    .confidence-slider::-moz-range-progress {
      height: 10px;
      border-radius: 999px;
      background: #0b67d0;
    }
    .confidence-slider.untouched::-moz-range-progress { background: #fff; }
    .confidence-slider::-webkit-slider-thumb {
      appearance: none;
      -webkit-appearance: none;
      width: 20px;
      height: 20px;
      margin-top: -6px;
      border: 2px solid #0b67d0;
      border-radius: 50%;
      background: #fff;
    }
    .confidence-slider::-moz-range-thumb {
      width: 20px;
      height: 20px;
      border: 2px solid #0b67d0;
      border-radius: 50%;
      background: #fff;
    }
    .confidence-slider.untouched::-webkit-slider-thumb { opacity: 0; }
    .confidence-slider.untouched::-moz-range-thumb { opacity: 0; }
    .slider-labels { display: grid; grid-template-columns: repeat(11, 1fr); margin-top: 8px; font-size: 13px; color: var(--muted); }
    .slider-labels span { text-align: center; }
    .slider-value { min-height: 32px; font-size: 22px; font-weight: 800; text-align: center; margin-top: 12px; }
    .confidence-bonus-note { font-size: 18px; font-weight: 400; margin: 8px 0 18px; text-align: center; }
    .confidence-bonus-note a { color: #0b67d0; text-decoration: underline; cursor: pointer; }
    .confidence-details-card { position: relative; width: min(700px, 100%); padding: 34px 30px 28px; }
    .confidence-details-card h2 { padding-right: 48px; }
    .modal-close-x { position: absolute; top: 10px; right: 12px; margin: 0; padding: 4px 11px; min-width: 42px; min-height: 42px; border-radius: 999px; font-size: 26px; line-height: 1; background: #fff; color: #111; border: 1px solid #777; }
    body.modal-open { overflow: hidden; }
    @media (max-width: 760px) {
      main { padding: 32px 18px 56px; }
      .feedback-grid { grid-template-columns: 1fr; }
      .no-pref-wrap { order: 3; }
      p, ul, .question-text { font-size: 19px; }
      canvas { height: 360px; }
      .check-row { grid-template-columns: 1fr; }
      input[type="number"] { width: 100%; }
      .instruction-flow { grid-template-columns: 1fr; }
      .instruction-arrow { transform: rotate(90deg); }
      .instruction-step { min-height: 0; }
      .prediction-grid { grid-template-columns: 1fr; }
      .slider-labels { font-size: 10px; }
      .bonus-eligibility-note { margin-left: 0; flex-basis: 100%; }
    }
  </style>
</head>
<body>
  <main id="app"></main>
  <script>
    const PRETEST = {{ pretest }};
    const QUESTION_SECONDS = {{ question_seconds }};
    const FIRST_PAGE_WAIT_SECONDS = {{ first_page_wait_seconds }};
    const BONUS_PER_WIN = {{ bonus_per_win }};
    const CONFIDENCE_BONUS = {{ confidence_bonus }};
    const REVIEW_MS_PER_GAME_MINUTE = {{ review_ms_per_game_minute }};
    const FINAL_CODE = {{ final_code|tojson }};
    const practiceGame = {{ practice_game|tojson }};
    const games = {{ quiz_games|tojson }};

    const state = {
      subjectId: null,
      round: 1,
      questionIndex: 0,
      orders: { r1: [], r2: [] },
      responses: { r1: [], r2: [] },
      reviewOptions: [],
      assignedReviewGame: null,
      reviewChoiceGameId: null,
      timers: { interval: null, timeout: null },
      chartAnimation: { raf: null },
      practiceGuesses: null,
      comprehensionAttempts: 0,
      pendingReviewAssignmentChoiceGameId: undefined,
      reviewIdentificationSequence: [],
      reviewMarginSequence: [],
      reviewMarginUsedReopen: false,
      instructions3StartedAtMs: null,
      postR2ConfidenceStartedAtMs: null,
      confidenceDetailsClickCount: 0,
      confidenceDetailsOpenMs: 0,
      confidenceDetailsOpenedAtMs: null
    };

    const app = document.getElementById("app");

    function postLayout(scrollToTop = false) {
      try {
        const h = Math.max(
          260,
          app ? Math.ceil(app.getBoundingClientRect().height) : 0,
          app ? Math.ceil(app.scrollHeight) : 0
        ) + 16;

        window.parent.postMessage({
          type: "nba-intuition-layout",
          height: h,
          scroll: Boolean(scrollToTop)
        }, "*");
      } catch (e) {}
    }

    function postHeight() {
      postLayout(false);
    }

    window.addEventListener("load", () => setTimeout(postHeight, 100));
    window.addEventListener("resize", () => setTimeout(postHeight, 100));

    function clearTimers() {
      if (state.timers.interval) clearInterval(state.timers.interval);
      if (state.timers.timeout) clearTimeout(state.timers.timeout);
      state.timers.interval = null;
      state.timers.timeout = null;
    }

    function stopChartAnimation() {
      if (state.chartAnimation.raf) cancelAnimationFrame(state.chartAnimation.raf);
      state.chartAnimation.raf = null;
    }

    function render(html, scrollToTop = true) {
      clearTimers();
      stopChartAnimation();
      app.innerHTML = html;

      if (scrollToTop) {
        window.scrollTo(0, 0);
        document.documentElement.scrollTop = 0;
        document.body.scrollTop = 0;
      }

      setTimeout(() => postLayout(scrollToTop), 0);
      setTimeout(() => postLayout(false), 80);
      setTimeout(() => postLayout(false), 300);
      setTimeout(() => postLayout(false), 700);
    }

    async function api(path, payload) {
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {})
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || "Request failed");
      return data;
    }

    function signedText(n) {
      const x = Number(n);
      if (!Number.isFinite(x)) return "No response";
      if (x > 0) return `+${x}`;
      if (x < 0) return `−${Math.abs(x)}`;
      return "0";
    }

    function predictionSentence(game, answer) {
      const value = Number(answer);
      if (!Number.isFinite(value)) return "No response";
      if (value > 0) return `${game.homeCity} wins by ${Math.abs(value)}`;
      if (value < 0) return `${game.awayCity} wins by ${Math.abs(value)}`;
      return "The game goes to overtime";
    }

    function isIntegerResponse(value) {
      if (value === "" || value === null || value === undefined) return false;
      return Number.isInteger(Number(value));
    }

    function distanceFromCorrect(answer, correct) {
      if (!Number.isFinite(Number(answer))) return Infinity;
      return Math.abs(Number(answer) - Number(correct));
    }

    function participantOutcome(answer, opponentGuess, correct) {
      const yourDistance = distanceFromCorrect(answer, correct);
      const opponentDistance = distanceFromCorrect(opponentGuess, correct);
      if (yourDistance < opponentDistance) return "won";
      if (yourDistance > opponentDistance) return "lost";
      return "tied";
    }

    function outcomeLabel(outcome) {
      if (outcome === "won") return '<span class="won">WON</span>';
      if (outcome === "lost") return '<span class="lost">LOST</span>';
      return '<span class="tied">TIED</span>';
    }

    function questionPrompt(game) {
      return `In a ${game.home} (home) vs. ${game.away} (away) game, what was ${game.homeCity}’s end-of-regulation margin?`;
    }

    function gameTitle(game) {
      return `${game.home} vs. ${game.away}`;
    }

    function makePracticeGuesses(game) {
      const guesses = [-7, 15];
      const flip = Math.random() < 0.5;
      const yourGuess = flip ? guesses[0] : guesses[1];
      const opponentGuess = flip ? guesses[1] : guesses[0];
      return { yourGuess, opponentGuess };
    }

    function roundOneAnswerForGame(gameId) {
      const prior = state.responses.r1.find(r => r.game_id === gameId);
      return prior && Number.isInteger(Number(prior.answer)) ? Number(prior.answer) : null;
    }

    async function ensureSubjectStarted() {
      if (state.subjectId) return;

      const params = Object.fromEntries(new URLSearchParams(window.location.search).entries());

      const data = await api("/api/start", {
        url_params: params,
        demo_outcome: PRETEST === 0
          ? (state.practiceGuesses.yourGuess === -7 ? "win" : "loss")
          : null,
        client_ts: new Date().toISOString()
      });

      state.subjectId = data.subject_id;
      state.orders.r1 = data.r1_order;
      state.orders.r2 = data.r2_order;
    }

    function renderInstructions() {
      if (!state.practiceGuesses) state.practiceGuesses = makePracticeGuesses(practiceGame);

      if (PRETEST === 1) {
        render(`
          <h1>Instructions</h1>
          <p>This is a study about your intuition about NBA games.</p>
          <p>We chose 5 random within-division NBA games from the 2025-2026 season. We are testing your ability to guess the end-of-regulation score margin based on the team names. For instance, here’s an example question:</p>
          <div class="practice-question">
            “${questionPrompt(practiceGame)} Use a positive number if you think ${practiceGame.homeCity} won, a negative number if you think ${practiceGame.homeCity} lost, or zero if you think the game went to overtime.”
          </div>
          <p>You will have ${QUESTION_SECONDS} seconds to answer each question. Do not try to look up the games on the internet. Some teams may have played each other multiple times, so you will not know which specific game we are referencing from the question.</p>
          <p>You will answer 5 questions total.</p>
          <button id="next" disabled>Next available in ${FIRST_PAGE_WAIT_SECONDS}</button>
        `);
        wireTimedNext("next", renderComprehensionCheck, { startSubject: true });
        return;
      }

      render(`
        <h1>Instructions Part 1</h1>
        <p>This is a study about your intuition about NBA games. You will compete against other fans and earn bonus payments when your intuition is better than your opponent’s.</p>
        <p>We chose 5 random within-division NBA games from the 2025-2026 season. We are testing your ability to guess the end-of-regulation score margin based on the team names. For instance, here’s an example question:</p>
        <div class="practice-question">
          “${questionPrompt(practiceGame)} Use a positive number if you think ${practiceGame.homeCity} won, a negative number if you think ${practiceGame.homeCity} lost, or zero if you think the game went to overtime.”
        </div>
        <p>You are competing vs. an opponent, who is another online participant. You earn $${BONUS_PER_WIN.toFixed(2)} for every question where your answer is closer to the correct answer than your opponent’s answer. You will only have ${QUESTION_SECONDS} seconds to answer each question: do not try to look up the games on the internet.</p>
        <button id="next" disabled>Next available in ${FIRST_PAGE_WAIT_SECONDS}</button>
      `);
      wireTimedNext("next", renderComprehensionCheck, { startSubject: true });
    }

    function wireTimedNext(buttonId, callback, options = {}) {
      const nextButton = document.getElementById(buttonId);
      let secondsLeft = FIRST_PAGE_WAIT_SECONDS;

      state.timers.interval = setInterval(() => {
        secondsLeft -= 1;
        if (secondsLeft > 0) {
          nextButton.textContent = `Next available in ${secondsLeft}`;
        } else {
          clearTimers();
          nextButton.disabled = false;
          nextButton.textContent = "Next";
        }
      }, 1000);

      nextButton.addEventListener("click", async () => {
        nextButton.disabled = true;

        try {
          if (options.startSubject) {
            await ensureSubjectStarted();
          }

          callback();
        } catch (e) {
          render(`
            <div class="interstitial">
              <h1>Could not start the study.</h1>
              <p>${e.message}</p>
            </div>
          `);
        }
      });
    }

    function renderReviewInstructions() {
  const pg = state.practiceGuesses;
  const anonymousPracticeGame = {
    ...practiceGame,
    homeShort: "Home Team",
    homeCity: "Home Team",
    home: "Home Team",
    awayShort: "Away Team",
    awayCity: "Away Team",
    away: "Away Team"
  };

  render(`
    <h1>Instructions Part 2</h1>
    <p>On the next screen, we will pick 2 of the 5 questions and tell you whether you <span class="word-won">won</span> or <span class="word-lost">lost</span> to your opponent. We will then ask which question you prefer to review. Your preference will be implemented with 10% probability; otherwise, you will be assigned one option at random.</p>
    <p>After you review, you will get matched with a new opponent for Round 2. In Round 2, you will answer the same 5 questions again. The correct answers will not change. Your answers from Round 1 will be filled in, but you are free to change them.</p>
    <p>Here is how the review page will look. Suppose you guessed ${signedText(pg.yourGuess)} and your opponent guessed ${signedText(pg.opponentGuess)}. <strong>This is a made-up example, not one of the 5 games you just guessed.</strong> As the game develops, the color shows whether your guess is on track to win or lose.</p>
    <div class="chart-wrap">
      <canvas id="practiceChart" width="1000" height="440" aria-label="Practice review chart"></canvas>
      <div class="chart-controls">
        <button id="playPractice">Play</button>
        <button id="start" class="hidden" disabled>Next</button>
      </div>
    </div><br>
  `);

  const playButton = document.getElementById("playPractice");
  const startButton = document.getElementById("start");

  drawReviewChart({
    canvasId: "practiceChart",
    game: anonymousPracticeGame,
    yourGuess: pg.yourGuess,
    opponentGuess: pg.opponentGuess,
    progressMinutes: 0,
    showCorrect: false
  });

  playButton.addEventListener("click", () => {
    playButton.disabled = true;

    playReviewAnimation({
      canvasId: "practiceChart",
      game: anonymousPracticeGame,
      yourGuess: pg.yourGuess,
      opponentGuess: pg.opponentGuess,
      onDone: () => {
        startButton.disabled = false;
        startButton.classList.remove("hidden");
        setTimeout(postHeight, 50);
        setTimeout(postHeight, 400);
      }
    });
  });

  startButton.addEventListener("click", renderReviewChoice);
}

function renderReview() {
  const item = state.assignedReviewGame;
  const game = item.game;
  const response = item.response;

  render(`
    <h1>Review</h1>
    <p>You are reviewing <strong>${game.home} vs. ${game.away}</strong>. Remember, you will have a chance to guess the margin for this game again in Round 2, against a different opponent.</p>
    <p>Your answer was <strong>${signedText(response.answer)}</strong>. Your opponent’s answer was <strong>${signedText(response.opponent_answer)}</strong>.</p>
    <div class="chart-wrap">
      <canvas id="reviewChart" width="1000" height="440" aria-label="Review chart"></canvas>
      <div class="chart-controls">
        <button id="playReview">Play</button>
        <button id="continueRound2" class="hidden" disabled>Continue</button>
      </div>
    </div>
  `);

  drawReviewChart({
    canvasId: "reviewChart",
    game,
    yourGuess: response.answer,
    opponentGuess: response.opponent_answer,
    progressMinutes: 0,
    showCorrect: false
  });

  const playButton = document.getElementById("playReview");
  const continueButton = document.getElementById("continueRound2");

  playButton.addEventListener("click", () => {
    playButton.disabled = true;

    playReviewAnimation({
      canvasId: "reviewChart",
      game,
      yourGuess: response.answer,
      opponentGuess: response.opponent_answer,
      onDone: () => {
        continueButton.disabled = false;
        continueButton.classList.remove("hidden");
        setTimeout(postHeight, 50);
        setTimeout(postHeight, 400);
      }
    });
  });

  continueButton.addEventListener("click", renderPostReviewChecks);
}


function escapedAttr(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderPostReviewChecks() {
  const assigned = state.assignedReviewGame;
  if (!assigned || !assigned.game || !assigned.response) {
    renderGetReady(2);
    return;
  }

  state.reviewIdentificationSequence = [];
  state.reviewMarginSequence = [];
  state.reviewMarginUsedReopen = false;

  const orderedGames = state.orders.r1 && state.orders.r1.length ? state.orders.r1 : games;
  const choicesHtml = orderedGames.map((game, idx) => `
    <label class="choice-option" for="review_game_${idx}">
      <input id="review_game_${idx}" name="reviewed_game" type="radio" value="${escapedAttr(game.id)}" />
      <span>${gameTitle(game)}</span>
    </label>
  `).join("");

  render(`
    <h1>Questions about the review</h1>
    <div id="reviewIdentifyBlock" class="card">
      <h2>Which of the 5 games did you just review?</h2>
      <form id="reviewIdentifyForm">
        <div class="choice-list">${choicesHtml}</div>
        <button id="submitReviewedGame" type="submit">Submit</button>
      </form>
      <div id="reviewIdentifyMessage" class="check-message"></div>
    </div>
    <div id="reviewMarginBlock" class="card hidden">
      <h2>Actual margin</h2>
      <p>For this game, you guessed that ${assigned.game.homeCity}’s end-of-regulation margin was <strong>${signedText(assigned.response.answer)}</strong>. You just reviewed the game. What was ${assigned.game.homeCity}’s actual end-of-regulation margin?</p>
      <form id="reviewMarginForm" autocomplete="off">
        <label for="reviewMarginAnswer">Actual end-of-regulation margin</label>
        <input id="reviewMarginAnswer" type="number" step="1" inputmode="numeric" />
        <br />
        <button id="submitReviewMargin" type="submit">Submit</button>
        <button id="showReviewAgain" type="button" class="secondary-button hidden">See review page again</button>
      </form>
      <div id="reviewMarginMessage" class="check-message"></div>
    </div>
    <div id="reviewPopup" class="modal-backdrop hidden" role="dialog" aria-modal="true" aria-label="Review graph popup">
      <div class="modal-card">
        <h2>${gameTitle(assigned.game)}</h2>
        <canvas id="reviewPopupChart" width="1000" height="440" aria-label="Final review chart"></canvas>
        <button id="closeReviewPopup" type="button">Close</button>
      </div>
    </div>
  `);

  const identifyBlock = document.getElementById("reviewIdentifyBlock");
  const identifyForm = document.getElementById("reviewIdentifyForm");
  const identifyMsg = document.getElementById("reviewIdentifyMessage");
  const identifySubmit = document.getElementById("submitReviewedGame");
  const marginBlock = document.getElementById("reviewMarginBlock");
  const marginForm = document.getElementById("reviewMarginForm");
  const marginInput = document.getElementById("reviewMarginAnswer");
  const marginMsg = document.getElementById("reviewMarginMessage");
  const showAgain = document.getElementById("showReviewAgain");
  const popup = document.getElementById("reviewPopup");
  const closePopup = document.getElementById("closeReviewPopup");

  let identifyRequestPending = false;

  identifyForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (identifyRequestPending) return;

    const selected = identifyForm.querySelector('input[name="reviewed_game"]:checked');
    if (!selected) {
      identifyMsg.textContent = "Please select one game.";
      identifyMsg.className = "check-message bad";
      return;
    }

    identifyRequestPending = true;
    identifyForm.querySelectorAll("input, button").forEach(el => { el.disabled = true; });
    identifySubmit.textContent = "Checking…";
    identifyMsg.textContent = "";
    identifyMsg.className = "check-message";

    const guessedGameId = selected.value;
    state.reviewIdentificationSequence.push(guessedGameId);

    try {
      const result = await api("/api/review_identification_check", {
        subject_id: state.subjectId,
        guessed_game_id: guessedGameId,
        sequence: state.reviewIdentificationSequence,
        correct: guessedGameId === assigned.game.id ? 1 : 0,
        client_ts: new Date().toISOString()
      });

      if (!result.correct) {
        identifyMsg.textContent = "Try again.";
        identifyMsg.className = "check-message bad";
        identifyForm.querySelectorAll("input, button").forEach(el => { el.disabled = false; });
        selected.checked = false;
        identifySubmit.textContent = "Submit";
        identifyRequestPending = false;
        return;
      }

      // Remove the completed question instead of leaving a disabled form visible.
      identifyBlock.classList.add("hidden");
      marginBlock.classList.remove("hidden");
      setTimeout(() => marginInput.focus(), 0);
      setTimeout(postHeight, 50);
    } catch (e) {
      // Remove the attempt that was not successfully recorded.
      state.reviewIdentificationSequence.pop();
      identifyMsg.textContent = "There was a connection problem. Please try again.";
      identifyMsg.className = "check-message bad";
      identifyForm.querySelectorAll("input, button").forEach(el => { el.disabled = false; });
      identifySubmit.textContent = "Submit";
      identifyRequestPending = false;
    }
  });

  showAgain.addEventListener("click", async () => {
    state.reviewMarginUsedReopen = true;
    state.reviewMarginSequence.push("check");

    popup.classList.remove("hidden");
    drawReviewChart({
      canvasId: "reviewPopupChart",
      game: assigned.game,
      yourGuess: assigned.response.answer,
      opponentGuess: assigned.response.opponent_answer,
      progressMinutes: 48,
      showCorrect: true
    });

    try {
      await api("/api/review_margin_check", {
        subject_id: state.subjectId,
        sequence: state.reviewMarginSequence,
        used_reopen: 1,
        correct: 0,
        client_ts: new Date().toISOString()
      });
    } catch (e) {}
  });

  closePopup.addEventListener("click", () => {
    popup.classList.add("hidden");
  });

  popup.addEventListener("click", (event) => {
    if (event.target === popup) popup.classList.add("hidden");
  });

  let marginRequestPending = false;

  marginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (marginRequestPending) return;

    const raw = marginInput.value;
    if (!isIntegerResponse(raw)) {
      marginMsg.textContent = "Please enter a whole number.";
      marginMsg.className = "check-message bad";
      return;
    }

    marginRequestPending = true;
    marginForm.querySelectorAll("input, button").forEach(el => { el.disabled = true; });
    const submitMarginButton = document.getElementById("submitReviewMargin");
    submitMarginButton.textContent = "Checking…";
    marginMsg.textContent = "";
    marginMsg.className = "check-message";

    const guess = Number(raw);
    state.reviewMarginSequence.push(guess);

    try {
      const result = await api("/api/review_margin_check", {
        subject_id: state.subjectId,
        guessed_margin: guess,
        sequence: state.reviewMarginSequence,
        used_reopen: state.reviewMarginUsedReopen ? 1 : 0,
        correct: guess === Number(assigned.game.correct) ? 1 : 0,
        client_ts: new Date().toISOString()
      });

      if (!result.correct) {
        marginMsg.textContent = "Try again. You may look at the review page again before answering.";
        marginMsg.className = "check-message bad";
        marginForm.querySelectorAll("input, button").forEach(el => { el.disabled = false; });
        showAgain.classList.remove("hidden");
        submitMarginButton.textContent = "Submit";
        marginInput.select();
        marginRequestPending = false;
        setTimeout(postHeight, 50);
        return;
      }

      render(`<div class="interstitial"><div class="status-message">Correct. Round 2 is next.</div><button id="continueAfterChecks">Continue</button></div>`);
      document.getElementById("continueAfterChecks").addEventListener("click", () => renderGetReady(2));
    } catch (e) {
      state.reviewMarginSequence.pop();
      marginMsg.textContent = "There was a connection problem. Please try again.";
      marginMsg.className = "check-message bad";
      marginForm.querySelectorAll("input, button").forEach(el => { el.disabled = false; });
      submitMarginButton.textContent = "Submit";
      marginRequestPending = false;
    }
  });
}

function drawReviewChart({ canvasId, game, yourGuess, opponentGuess, progressMinutes, showCorrect }) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;

  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;

  const pad = { left: 72, right: 220, top: 34, bottom: 62 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const pts = normalizeTimeline(game.timeline);

  const allValues = [
    ...pts.map(p => p.margin),
    Number.isFinite(Number(yourGuess)) ? Number(yourGuess) : 0,
    Number.isFinite(Number(opponentGuess)) ? Number(opponentGuess) : 0,
    Number(game.correct || 0)
  ];

  const maxAbs = Math.max(10, Math.ceil(Math.max(...allValues.map(v => Math.abs(v))) / 5) * 5);

  const x = minute => pad.left + (minute / 48) * plotW;
  const y = margin => pad.top + ((maxAbs - margin) / (2 * maxAbs)) * plotH;

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = "#e8e8e8";
  ctx.lineWidth = 1;
  ctx.font = "14px Arial";
  ctx.fillStyle = "#555";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";

  for (let m = -maxAbs; m <= maxAbs; m += 5) {
    const yy = y(m);
    ctx.beginPath();
    ctx.moveTo(pad.left, yy);
    ctx.lineTo(x(48), yy);
    ctx.stroke();
    ctx.fillText(String(m), pad.left - 10, yy);
  }

  ctx.strokeStyle = "#111";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(pad.left, y(0));
  ctx.lineTo(x(48), y(0));
  ctx.stroke();

  ctx.strokeStyle = "#bfbfbf";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top);
  ctx.lineTo(pad.left, height - pad.bottom);
  ctx.lineTo(x(48), height - pad.bottom);
  ctx.stroke();

  ctx.fillStyle = "#555";
  ctx.font = "15px Arial";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";

  [0, 12, 24, 36, 48].forEach(min => {
    ctx.fillText(String(min), x(min), height - pad.bottom + 14);
  });

  ctx.fillText("Minutes of regulation", pad.left + plotW / 2, height - 28);

  ctx.save();
  ctx.translate(24, pad.top + plotH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.font = "15px Arial";
  ctx.fillText(`${game.homeShort || game.homeCity} margin`, 0, 0);
  ctx.restore();

  const endMinute = Math.max(0, Math.min(48, Number(progressMinutes || 0)));
  const step = 0.05;

  let prevMinute = 0;
  let prevMargin = interpolateTimeline(pts, 0);

  for (let minute = step; minute <= endMinute + 0.0001; minute += step) {
    const clippedMinute = Math.min(minute, endMinute);
    const margin = interpolateTimeline(pts, clippedMinute);
    const colorMargin = (prevMargin + margin) / 2;

    ctx.strokeStyle = reviewLineColorAtMargin(colorMargin, yourGuess, opponentGuess);
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(x(prevMinute), y(prevMargin));
    ctx.lineTo(x(clippedMinute), y(margin));
    ctx.stroke();

    prevMinute = clippedMinute;
    prevMargin = margin;
  }

  if (endMinute > 0 && endMinute < 48) {
    const currentMargin = interpolateTimeline(pts, endMinute);
    ctx.fillStyle = reviewLineColorAtMargin(currentMargin, yourGuess, opponentGuess);
    ctx.beginPath();
    ctx.arc(x(endMinute), y(currentMargin), 5, 0, Math.PI * 2);
    ctx.fill();
  }

  drawRightLabelDot({
    ctx,
    dotX: x(48),
    dotY: y(Number(yourGuess)),
    text: `You: ${signedText(yourGuess)}`,
    dotColor: "#111"
  });

  drawRightLabelDot({
    ctx,
    dotX: x(48),
    dotY: y(Number(opponentGuess)),
    text: `Opponent: ${signedText(opponentGuess)}`,
    dotColor: "#111"
  });

    if (showCorrect) {
    const correct = Number(game.correct);
    const finalOutcome = finalOutcomeForChart(yourGuess, opponentGuess, correct);
    const yourDistance = distanceFromCorrect(yourGuess, correct);
    const opponentDistance = distanceFromCorrect(opponentGuess, correct);

    // Correct-answer dot and label.
    drawRightLabelDot({
      ctx,
      dotX: x(48),
      dotY: y(correct),
      text: `Correct: ${signedText(correct)}`,
      dotColor: reviewLineColorAtMargin(correct, yourGuess, opponentGuess),
      bold: true
    });

    // Outcome text near the correct endpoint.
    ctx.font = "bold 24px Arial";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillStyle = finalOutcome.color;

    const outcomeX = x(48) + 12;
    const outcomeY = y(-maxAbs);
    ctx.fillText(finalOutcome.text, outcomeX, outcomeY);

    // Salient final-answer callout box.
    const boxX = pad.left + 18;
    const boxY = pad.top + 16;
    const boxW = 370;
    const boxH = 118;
    const radius = 12;

    ctx.save();

    // Box shadow.
    ctx.shadowColor = "rgba(0, 0, 0, 0.18)";
    ctx.shadowBlur = 14;
    ctx.shadowOffsetX = 0;
    ctx.shadowOffsetY = 4;

    ctx.fillStyle = "#fff";
    ctx.strokeStyle = "#111";
    ctx.lineWidth = 2;

    ctx.beginPath();
    ctx.moveTo(boxX + radius, boxY);
    ctx.lineTo(boxX + boxW - radius, boxY);
    ctx.quadraticCurveTo(boxX + boxW, boxY, boxX + boxW, boxY + radius);
    ctx.lineTo(boxX + boxW, boxY + boxH - radius);
    ctx.quadraticCurveTo(boxX + boxW, boxY + boxH, boxX + boxW - radius, boxY + boxH);
    ctx.lineTo(boxX + radius, boxY + boxH);
    ctx.quadraticCurveTo(boxX, boxY + boxH, boxX, boxY + boxH - radius);
    ctx.lineTo(boxX, boxY + radius);
    ctx.quadraticCurveTo(boxX, boxY, boxX + radius, boxY);
    ctx.closePath();

    ctx.fill();
    ctx.shadowColor = "transparent";
    ctx.stroke();

    ctx.fillStyle = "#111";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";

    ctx.font = "bold 16px Arial";
    ctx.fillText("FINAL SCORE MARGIN", boxX + 18, boxY + 16);

    ctx.font = "bold 34px Arial";
    ctx.fillText(`${game.homeShort || game.homeCity}: ${signedText(correct)}`, boxX + 18, boxY + 40);

    ctx.font = "15px Arial";
    ctx.fillText(`You were ${yourDistance} away.`, boxX + 18, boxY + 84);
    ctx.fillText(`Opponent was ${opponentDistance} away.`, boxX + 185, boxY + 84);

    ctx.restore();
  }
}

    function renderComprehensionCheck() {
      const homeTeam = practiceGame.home;
      const awayTeam = practiceGame.away;
      const homeCity = practiceGame.homeCity;
      const homeShort = practiceGame.homeShort || homeCity;
      const awayShort = practiceGame.awayShort || practiceGame.awayCity;

      render(`
        <h1>Check your understanding</h1>
        <p>Here's the example question again.</p>

        <div class="practice-question">
          “In a ${homeTeam} (home) vs. ${awayTeam} (away) game, what was ${homeCity}’s end-of-regulation margin? Use a positive number if you think ${homeCity} won, a negative number if you think ${homeCity} lost, or zero if you think the game went to overtime.”
        </div>

        <p>For each of the following answers, what is the correct number to enter?</p>

        <div class="check-grid">
          <div class="check-row">
            <div class="desc">${homeShort} won by 4</div>
            <input id="cc1" type="number" step="1" inputmode="numeric" />
            <div id="cc1_mark" class="check-mark"></div>
          </div>

          <div class="check-row">
            <div class="desc">${awayShort} won by 11</div>
            <input id="cc2" type="number" step="1" inputmode="numeric" />
            <div id="cc2_mark" class="check-mark"></div>
          </div>

          <div class="check-row">
            <div class="desc">The game went to overtime</div>
            <input id="cc3" type="number" step="1" inputmode="numeric" />
            <div id="cc3_mark" class="check-mark"></div>
          </div>
        </div>

        <div class="comprehension-actions">
          <button id="checkAnswers">Check</button>
          <button id="ccNext" disabled>Next</button>
        </div>
        <div id="checkMessage" class="check-message"></div>
      `);

      const checkButton = document.getElementById("checkAnswers");
      const nextButton = document.getElementById("ccNext");
      const message = document.getElementById("checkMessage");

      checkButton.addEventListener("click", async () => {
        const values = {
          cc1: document.getElementById("cc1").value,
          cc2: document.getElementById("cc2").value,
          cc3: document.getElementById("cc3").value
        };

        const parsed = {
          cc1: values.cc1 === "" ? null : Number(values.cc1),
          cc2: values.cc2 === "" ? null : Number(values.cc2),
          cc3: values.cc3 === "" ? null : Number(values.cc3)
        };

        const ok1 = Number.isInteger(parsed.cc1) && parsed.cc1 === 4;
        const ok2 = Number.isInteger(parsed.cc2) && parsed.cc2 === -11;
        const ok3 = Number.isInteger(parsed.cc3) && parsed.cc3 === 0;
        const allCorrect = ok1 && ok2 && ok3;

        document.getElementById("cc1_mark").textContent = ok1 ? "✓" : "✕";
        document.getElementById("cc1_mark").className = `check-mark ${ok1 ? "good" : "bad"}`;

        document.getElementById("cc2_mark").textContent = ok2 ? "✓" : "✕";
        document.getElementById("cc2_mark").className = `check-mark ${ok2 ? "good" : "bad"}`;

        document.getElementById("cc3_mark").textContent = ok3 ? "✓" : "✕";
        document.getElementById("cc3_mark").className = `check-mark ${ok3 ? "good" : "bad"}`;

        state.comprehensionAttempts += 1;

        try {
          await api("/api/comprehension_check", {
            subject_id: state.subjectId,
            attempt_number: state.comprehensionAttempts,
            answers: parsed,
            correct: allCorrect ? 1 : 0,
            client_ts: new Date().toISOString()
          });
        } catch (e) {
          message.textContent = "There was a connection problem. Please try again.";
          message.className = "check-message bad";
          return;
        }

        if (allCorrect) {
          message.textContent = "Correct.";
          message.className = "check-message good";
          nextButton.disabled = false;
          checkButton.disabled = true;

          ["cc1", "cc2", "cc3"].forEach(id => {
            document.getElementById(id).disabled = true;
          });
        } else if (state.comprehensionAttempts >= 3) {
          checkButton.disabled = true;
          nextButton.disabled = true;
          ["cc1", "cc2", "cc3"].forEach(id => {
            document.getElementById(id).disabled = true;
          });
          await renderComprehensionFailure();
        } else {
          const attemptsRemaining = 3 - state.comprehensionAttempts;
          message.textContent = `Try again. ${attemptsRemaining} attempt${attemptsRemaining === 1 ? "" : "s"} remaining.`;
          message.className = "check-message bad";
          nextButton.disabled = true;
        }
      });

      nextButton.addEventListener("click", () => renderGetReady(1));
    }


    async function renderComprehensionFailure() {
      try {
        await api("/api/complete", {
          subject_id: state.subjectId,
          client_ts: new Date().toISOString()
        });
      } catch (e) {
        render(`
          <div class="interstitial">
            <div class="status-message">There was a connection problem. Please try again.</div>
            <button id="retryComprehensionFailure">Try again</button>
          </div>
        `);
        document.getElementById("retryComprehensionFailure").addEventListener("click", renderComprehensionFailure);
        return;
      }

      render(`
        <h1>You did not pass the comprehension check.</h1>
        <p>Please scroll down and enter <strong>${FINAL_CODE}</strong> to continue.</p>
      `, false);
    }

    function renderGetReady(round) {
      const totalRounds = PRETEST === 1 ? 1 : 2;
      const extraLine = round === 2
        ? `<p>The games are the same as before, but you have been matched with a new opponent. Your answers from Round 1 will be filled in, but you are free to change them.</p>`
        : "";

      render(`
        <div class="interstitial">
          <h1>Get ready!</h1>
          <p>Remember, you only have ${QUESTION_SECONDS} seconds to enter a number for each game.</p>
          ${extraLine}
          <ul>
            <li>If you think the home team won in regulation, enter a positive number.</li>
            <li>If you think the away team won in regulation, enter a negative number.</li>
            <li>If you think the game went to overtime, enter 0.</li>
          </ul>
          <p style="margin-top: 22px;">If you do not enter a number before time runs out, we will automatically enter 0.</p>
          <button id="beginQuiz">Begin</button>
        </div>
      `);

      document.getElementById("beginQuiz").addEventListener("click", () => {
        state.round = round;
        state.questionIndex = 0;
        renderQuestion();
      });
    }

    function renderQuestion() {
      const roundKey = state.round === 1 ? "r1" : "r2";
      const order = state.orders[roundKey];
      const game = order[state.questionIndex];
      const displayQuestionNumber = state.questionIndex + 1;
      const prefillAnswer = state.round === 2 ? roundOneAnswerForGame(game.id) : null;
      const prefillValue = prefillAnswer === null ? "" : String(prefillAnswer);
      const roundTwoReminder = state.round === 2
        ? `<p>Your Round 1 answer is filled in below. You are free to change it before submitting.</p>`
        : "";

      const reviewedReminder = (
        state.round === 2 &&
        state.assignedReviewGame &&
        state.assignedReviewGame.game &&
        state.assignedReviewGame.game.id === game.id
      )
        ? `<p style="color: #064fdb; font-weight: 800; margin: 0 0 18px;">This is the game you reviewed.</p>`
        : "";

      render(`
        <div class="question-topline">
          <div>${PRETEST === 1 ? "Quiz" : `Round ${state.round}`} · Question ${displayQuestionNumber} of ${order.length}</div>
          <div id="questionTimer" class="timer">Time left: <span id="timeLeft">${QUESTION_SECONDS}</span>s</div>
        </div>
        <div class="card">
          <div class="question-text">${questionPrompt(game)}</div>
          ${roundTwoReminder}
          ${reviewedReminder}
          <form id="answerForm" autocomplete="off">
            <label for="answer">Your answer</label>
            <input id="answer" name="answer" type="number" step="1" inputmode="numeric" value="${prefillValue}" autofocus />
            <br /><button id="submitAnswer" type="submit">Submit</button>
          </form>
          <div class="answer-rules">
            <ul>
              <li>If you think ${game.homeCity} won in regulation, enter a positive number.</li>
              <li>If you think ${game.homeCity} lost in regulation, enter a negative number.</li>
              <li>If you think the game went to overtime, enter 0.</li>
            </ul>
          </div>
        </div>
      `);

      const input = document.getElementById("answer");
      const form = document.getElementById("answerForm");
      const timeLeftEl = document.getElementById("timeLeft");
      const timerEl = document.getElementById("questionTimer");

      input.focus();
      if (state.round === 2 && input.value !== "") {
        // Do not use setSelectionRange() on type="number"; some browsers throw,
        // which prevents the submit handler from being attached and can reload
        // the app back to the instructions. Reassigning the value after focus
        // keeps the caret at the end without highlighting the number.
        const currentValue = input.value;
        setTimeout(() => {
          input.value = "";
          input.value = currentValue;
        }, 0);
      } else {
        input.select();
      }

      const questionStartMs = performance.now();

      let secondsLeft = QUESTION_SECONDS;
      let alreadyRecorded = false;

      function recordCurrentInputOrZero(timedOut) {
        const raw = input.value;
        const answer = isIntegerResponse(raw) ? Number(raw) : 0;
        recordAnswer(answer, timedOut);
      }

      state.timers.interval = setInterval(() => {
        secondsLeft -= 1;
        timeLeftEl.textContent = String(Math.max(0, secondsLeft));
        timerEl.classList.toggle("urgent", secondsLeft <= 5);
        if (secondsLeft <= 0) recordCurrentInputOrZero(true);
      }, 1000);

      state.timers.timeout = setTimeout(() => recordCurrentInputOrZero(true), QUESTION_SECONDS * 1000 + 50);

      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const raw = input.value;
        if (!isIntegerResponse(raw)) return;
        recordAnswer(Number(raw), false);
      });

      async function recordAnswer(answer, timedOut) {
        if (alreadyRecorded) return;
        alreadyRecorded = true;
        clearTimers();

        try {
          const data = await api("/api/answer", {
            subject_id: state.subjectId,
            round_id: roundKey,
            question_index: state.questionIndex,
            game_id: game.id,
            answer,
            timed_out: timedOut ? 1 : 0,
            response_time_ms: Math.round(performance.now() - questionStartMs),
            client_ts: new Date().toISOString()
          });

          state.responses[roundKey].push(data.response);
          renderQuestionSubmitted({ timedOut });
        } catch (e) {
          render(`<div class="interstitial"><div class="status-message">There was a connection problem. Please reload the page.</div><p>${e.message}</p></div>`);
        }
      }
    }

    function renderQuestionSubmitted({ timedOut }) {
      const message = timedOut ? "Time’s up. Answer recorded." : "Answer recorded.";
      render(`<div class="interstitial"><div class="status-message">${message}</div><button id="nextQuestion">Next</button></div>`);
      document.getElementById("nextQuestion").addEventListener("click", advanceAfterQuestion);
    }

    async function completeRound(roundKey) {
      return api("/api/complete_round", {
        subject_id: state.subjectId,
        round_id: roundKey,
        client_ts: new Date().toISOString()
      });
    }

    async function advanceAfterQuestion() {
      const roundKey = state.round === 1 ? "r1" : "r2";
      const order = state.orders[roundKey];

      state.questionIndex += 1;

      if (state.questionIndex < order.length) {
        renderQuestion();
      } else if (state.round === 1) {
        await completeRound("r1");
        if (PRETEST === 1) renderEnd();
        else renderReviewInstructions();
      } else {
        await completeRound("r2");
        renderPostRound2Confidence();
      }
    }

    function reviewOptionCard(item, label) {
      const game = item.game;
      const response = item.response;

      let resultText = "";
      if (response.outcome === "won") {
        resultText = `
          <p style="color: #16823a; font-weight: 800; font-size: 24px; margin: 14px 0 18px;">YOU WON!</p>
          <p style="color: #111; font-weight: 700;">You earned a bonus.</p>
          <p style="color: #111;">Review to see the game flow and how much closer your guess was than your opponent's guess to the final score margin.</p>
        `;
      } else if (response.outcome === "lost") {
        resultText = `
          <p style="color: #b3261e; font-weight: 800; font-size: 24px; margin: 14px 0 18px;">YOU LOST!</p>
          <p style="color: #111; font-weight: 700;">You did not earn a bonus.</p>
          <p style="color: #111;">Review to see the game flow and how much farther your guess was than your opponent's guess from the final score margin.</p>
        `;
      } else {
        resultText = `
          <p style="color: #b8860b; font-weight: 800; font-size: 24px; margin: 14px 0 18px;">YOU TIED!</p>
          <p style="color: #111; font-weight: 700;">You tied your opponent.</p>
          <p style="color: #111;">Review to see the game flow and how your guess compared with your opponent's guess.</p>
        `;
      }

      return `
        <div class="feedback-card">
          <div>
            <h2>${gameTitle(game)}</h2>
            ${resultText}
          </div>
          <button id="choose_${label}" class="review-choice-button">Prefer to review this game</button>
        </div>
      `;
    }

async function renderReviewChoice() {
  const data = await api("/api/review_options", {
    subject_id: state.subjectId,
    client_ts: new Date().toISOString()
  });

  state.reviewOptions = data.options;
  const [a, b] = state.reviewOptions;

  render(`
    <h1>Here are 2 of your results. Which would you prefer to review?</h1>
    <div class="feedback-grid">
      ${reviewOptionCard(a, "a")}
      <div class="no-pref-wrap"><button id="noPref" class="secondary-button review-choice-button">No preference</button></div>
      ${reviewOptionCard(b, "b")}
    </div>

    <div id="reviewReasonWrap" class="card review-reason-card hidden">
      <p id="reviewReasonChoiceText"></p>
      <label for="reviewChoiceReason">Please explain to us how you made this choice.</label>
      <textarea id="reviewChoiceReason"></textarea>
    </div>

    <p id="afterReviewText"><br>After you review, we will ask you simple questions to remind you what you saw.</p>
    <button id="submitReviewReason" class="hidden">Next</button>
  `);

  document.getElementById("choose_a").addEventListener("click", () => showReviewChoiceReason(a.game.id, "choose_a"));
  document.getElementById("choose_b").addEventListener("click", () => showReviewChoiceReason(b.game.id, "choose_b"));
  document.getElementById("noPref").addEventListener("click", () => showReviewChoiceReason(null, "noPref"));
}

function showReviewChoiceReason(gameId, buttonId) {
  // Lock the first preference selected. Participants cannot switch their
  // preference while writing the explanation.
  if (state.pendingReviewAssignmentChoiceGameId !== undefined &&
      state.pendingReviewAssignmentChoiceGameId !== null) {
    return;
  }

  const chosen = gameId
    ? state.reviewOptions.find(item => item.game.id === gameId)
    : null;

  // Use a sentinel for "no preference" so it is also treated as locked.
  state.pendingReviewAssignmentChoiceGameId = gameId === null ? "__NO_PREFERENCE__" : gameId;

  document.querySelectorAll(".review-choice-button").forEach(button => {
    button.classList.toggle("selected", button.id === buttonId);
    button.disabled = true;
  });

  document.getElementById("reviewReasonChoiceText").innerHTML = chosen
    ? `You indicated a preference to review <strong>${gameTitle(chosen.game)}</strong>.`
    : "You indicated that you have no preference between the two games.";

  const wrap = document.getElementById("reviewReasonWrap");
  wrap.classList.remove("hidden");

  const reasonInput = document.getElementById("reviewChoiceReason");
  const submitReasonButton = document.getElementById("submitReviewReason");
  submitReasonButton.classList.remove("hidden");
  submitReasonButton.onclick = () => {
    submitReviewChoice(gameId, reasonInput.value || "");
  };

  setTimeout(() => {
    reasonInput.focus();
    postHeight();
  }, 50);
}

async function submitReviewChoice(gameId, reason) {
  const reasonButton = document.getElementById("submitReviewReason");
  const noPrefButton = document.getElementById("noPref");

  if (reasonButton) reasonButton.disabled = true;
  if (noPrefButton && gameId === null) noPrefButton.disabled = true;

  try {
    const data = await api("/api/review_choice", {
      subject_id: state.subjectId,
      choice_game_id: gameId,
      reason: reason || "",
      client_ts: new Date().toISOString()
    });

    state.reviewChoiceGameId = gameId;
    state.assignedReviewGame = data.assigned;
    state.pendingReviewAssignmentChoiceGameId = gameId;

    renderReviewAssignment(gameId);
  } catch (e) {
    if (reasonButton) reasonButton.disabled = false;
    if (noPrefButton) noPrefButton.disabled = false;

    render(`
      <div class="interstitial">
        <div class="status-message">There was a connection problem. Please try again.</div>
        <p>${e.message}</p>
        <button id="backToReviewChoice">Back</button>
      </div>
    `);

    document.getElementById("backToReviewChoice").addEventListener("click", renderReviewChoice);
  }
}

    function renderReviewAssignment(choiceGameId) {
      const assigned = state.assignedReviewGame;
      const assignedGame = assigned.game;
      const chosen = choiceGameId
        ? state.reviewOptions.find(item => item.game.id === choiceGameId)
        : null;

      const preferenceText = chosen
        ? `You indicated a preference to review <strong>${gameTitle(chosen.game)}</strong>.`
        : "You indicated no preference about which game to review.";

      render(`
        <div class="interstitial">
          <h1>Review assignment</h1>
          <p>${preferenceText}</p>
          <br><br>
          <p>You have been assigned to review <strong>${gameTitle(assignedGame)}</strong>.</p>
          <button id="startAssignedReview">Continue</button>
        </div>
      `);

      document.getElementById("startAssignedReview").addEventListener("click", renderReview);
    }

    function renderPostRound2Confidence() {
      state.postR2ConfidenceStartedAtMs = performance.now();
      state.confidenceDetailsClickCount = 0;
      state.confidenceDetailsOpenMs = 0;
      state.confidenceDetailsOpenedAtMs = null;

      const excluded = new Set(
        state.reviewOptions.map(item => item && item.game ? String(item.game.id) : "").filter(Boolean)
      );
      const selectedGames = (state.orders.r2 || [])
        .filter(game => !excluded.has(String(game.id)))
        .slice(0, 3);

      const cards = selectedGames.map(game => {
        const response = state.responses.r2.find(r => String(r.game_id) === String(game.id));
        return `
          <div class="prediction-card">
            <h2>${gameTitle(game)}</h2>
            <div><div style="color: var(--muted);">Your guess</div><div class="prediction-value">${predictionSentence(game, response ? response.answer : null)}</div></div>
          </div>
        `;
      }).join("");

      render(`
        <h1>Here are 3 of your guesses.</h1>
        <div class="prediction-grid">${cards}</div>
        <p>Like you, your opponent listed on Prolific that they watch the NBA. Like you, they did not get to review any of these games. How likely do you think it is that your guess was closer to accurate than your opponent's guess for a majority <strong>(2 or 3)</strong> of these games?</p>
        <div class="confidence-wrap">
          <input id="confidenceSlider" class="confidence-slider untouched" type="range" min="0" max="100" step="5" value="50" aria-label="Likelihood percentage" />
          <div class="slider-labels" aria-hidden="true">${Array.from({length: 11}, (_, i) => `<span>${i * 10}%</span>`).join("")}</div>
          <div id="confidenceValue" class="slider-value">Click the slider to answer</div>
        </div>
        <p class="confidence-bonus-note">Answer honestly to maximize your chance at an additional $${CONFIDENCE_BONUS.toFixed(2)} bonus (<a href="#" id="confidenceDetailsLink">see details</a>).</p>
        <button id="confidenceNext" disabled>Next</button>
        <div id="confidenceError" class="check-message"></div>

        <div id="confidenceDetailsModal" class="modal-backdrop hidden" role="dialog" aria-modal="true" aria-labelledby="confidenceDetailsTitle">
          <div class="modal-card confidence-details-card">
            <button id="closeConfidenceDetails" type="button" class="modal-close-x" aria-label="Close details">×</button>
            <h2 id="confidenceDetailsTitle">How the additional bonus works</h2>
            <p>After you submit your answer, the computer will randomly select a percentage from 5% to 100%, in increments of 5 percentage points.</p>
            <p>If your answer is at least as high as the randomly selected percentage, you will receive the additional $${CONFIDENCE_BONUS.toFixed(2)} if your guesses were closer than your opponent’s guesses for at least 2 of the 3 games.</p>
            <p>If your answer is lower than the randomly selected percentage, your game results will not determine this bonus. Instead, you will have a chance equal to the randomly selected percentage of receiving the additional $${CONFIDENCE_BONUS.toFixed(2)}.</p>
            <p>Importantly, the additional bonus is designed so that giving your honest best estimate maximizes your chance of receiving the additional bonus.</p>
          </div>
        </div>
      `);

      const slider = document.getElementById("confidenceSlider");
      const valueLabel = document.getElementById("confidenceValue");
      const nextButton = document.getElementById("confidenceNext");
      const error = document.getElementById("confidenceError");
      const detailsLink = document.getElementById("confidenceDetailsLink");
      const modal = document.getElementById("confidenceDetailsModal");
      const closeModalButton = document.getElementById("closeConfidenceDetails");
      let answered = false;
      let priorFocus = null;

      function registerAnswer() {
        answered = true;
        slider.classList.remove("untouched");
        slider.style.setProperty("--slider-fill", `${slider.value}%`);
        valueLabel.textContent = `${slider.value}%`;
        nextButton.disabled = false;
      }

      async function checkpointDetailsActivity() {
        try {
          await api("/api/confidence_details_activity", {
            subject_id: state.subjectId,
            click_count: state.confidenceDetailsClickCount,
            total_open_ms: state.confidenceDetailsOpenMs,
            client_ts: new Date().toISOString()
          });
        } catch (e) {
          // The final confidence submission sends the same cumulative values as a backup.
        }
      }

      function openDetails(event) {
        event.preventDefault();
        if (!modal.classList.contains("hidden")) return;
        priorFocus = document.activeElement;
        state.confidenceDetailsClickCount += 1;
        state.confidenceDetailsOpenedAtMs = performance.now();
        modal.classList.remove("hidden");
        document.body.classList.add("modal-open");
        closeModalButton.focus();
        checkpointDetailsActivity();
      }

      function closeDetails() {
        if (modal.classList.contains("hidden")) return;
        if (state.confidenceDetailsOpenedAtMs !== null) {
          state.confidenceDetailsOpenMs += Math.max(
            0,
            Math.round(performance.now() - state.confidenceDetailsOpenedAtMs)
          );
          state.confidenceDetailsOpenedAtMs = null;
        }
        modal.classList.add("hidden");
        document.body.classList.remove("modal-open");
        checkpointDetailsActivity();
        if (priorFocus && typeof priorFocus.focus === "function") priorFocus.focus();
      }

      function modalKeyGuard(event) {
        if (modal.classList.contains("hidden")) return;
        if (event.key === "Tab") {
          event.preventDefault();
          closeModalButton.focus();
          return;
        }
        if ((event.key === "Enter" || event.key === " ") && document.activeElement === closeModalButton) {
          return;
        }
        event.preventDefault();
        event.stopPropagation();
      }

      slider.addEventListener("input", registerAnswer);
      slider.addEventListener("change", registerAnswer);
      slider.addEventListener("pointerdown", () => setTimeout(registerAnswer, 0));
      slider.addEventListener("keydown", registerAnswer);
      detailsLink.addEventListener("click", openDetails);
      closeModalButton.addEventListener("click", closeDetails);
      modal.addEventListener("pointerdown", event => {
        if (event.target === modal) {
          event.preventDefault();
          event.stopPropagation();
        }
      });
      document.addEventListener("keydown", modalKeyGuard, true);

      nextButton.addEventListener("click", async () => {
        if (!answered || !modal.classList.contains("hidden")) return;
        nextButton.disabled = true;
        error.textContent = "";
        const timeMs = Math.max(0, Math.round(performance.now() - state.postR2ConfidenceStartedAtMs));
        try {
          await api("/api/post_r2_confidence", {
            subject_id: state.subjectId,
            confidence_percent: Number(slider.value),
            time_ms: timeMs,
            game_ids: selectedGames.map(game => String(game.id)),
            details_click_count: state.confidenceDetailsClickCount,
            details_total_open_ms: state.confidenceDetailsOpenMs,
            client_ts: new Date().toISOString()
          });
          document.removeEventListener("keydown", modalKeyGuard, true);
          await renderEnd();
        } catch (e) {
          error.textContent = "There was a connection problem. Please try again.";
          error.className = "check-message bad";
          nextButton.disabled = false;
        }
      });
    }

    async function renderEnd() {
      await api("/api/complete", {
        subject_id: state.subjectId,
        client_ts: new Date().toISOString()
      });

      render(`
        <h1>Finished</h1>
      `, false);
    }

    function normalizeTimeline(timeline) {
      const pts = (timeline || [])
        .map(p => ({ minute: Number(p.minute), margin: Number(p.margin) }))
        .filter(p => Number.isFinite(p.minute) && Number.isFinite(p.margin))
        .sort((a, b) => a.minute - b.minute);

      if (!pts.length) return [{ minute: 0, margin: 0 }, { minute: 48, margin: 0 }];
      if (pts[0].minute > 0) pts.unshift({ minute: 0, margin: pts[0].margin });
      if (pts[pts.length - 1].minute < 48) pts.push({ minute: 48, margin: pts[pts.length - 1].margin });

      return pts;
    }

    function interpolateTimeline(timeline, minute) {
      const pts = normalizeTimeline(timeline);

      if (minute <= pts[0].minute) return pts[0].margin;
      if (minute >= pts[pts.length - 1].minute) return pts[pts.length - 1].margin;

      for (let i = 0; i < pts.length - 1; i++) {
        const a = pts[i], b = pts[i + 1];

        if (minute >= a.minute && minute <= b.minute) {
          const span = Math.max(0.0001, b.minute - a.minute);
          const t = (minute - a.minute) / span;
          return a.margin + (b.margin - a.margin) * t;
        }
      }

      return pts[pts.length - 1].margin;
    }

    function reviewLineColorAtMargin(currentMargin, yourGuess, opponentGuess) {
      const yourDistance = distanceFromCorrect(yourGuess, currentMargin);
      const opponentDistance = distanceFromCorrect(opponentGuess, currentMargin);

      if (yourDistance < opponentDistance) return "#16823a";
      if (yourDistance > opponentDistance) return "#b3261e";
      return "#b8860b";
    }

    function finalOutcomeForChart(yourGuess, opponentGuess, correct) {
      const yourDistance = distanceFromCorrect(yourGuess, correct);
      const opponentDistance = distanceFromCorrect(opponentGuess, correct);

      if (yourDistance < opponentDistance) {
        return { text: "YOU WON", color: "#16823a" };
      }

      if (yourDistance > opponentDistance) {
        return { text: "YOU LOST", color: "#b3261e" };
      }

      return { text: "YOU TIED", color: "#b8860b" };
    }

    function drawRightLabelDot({ ctx, dotX, dotY, text, dotColor, bold = false }) {
      ctx.fillStyle = dotColor;
      ctx.beginPath();
      ctx.arc(dotX, dotY, 6, 0, Math.PI * 2);
      ctx.fill();

      ctx.font = bold ? "bold 15px Arial" : "15px Arial";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "#111";
      ctx.fillText(text, dotX + 12, dotY);
    }

    function playReviewAnimation({ canvasId, game, yourGuess, opponentGuess, onDone }) {
      stopChartAnimation();

      const durationMs = REVIEW_MS_PER_GAME_MINUTE * 48;
      const start = performance.now();

      function frame(now) {
        const elapsed = now - start;
        const progress = Math.min(1, elapsed / durationMs);
        const progressMinutes = progress * 48;

        drawReviewChart({
          canvasId,
          game,
          yourGuess,
          opponentGuess,
          progressMinutes,
          showCorrect: progress >= 1
        });

        if (progress < 1) {
          state.chartAnimation.raf = requestAnimationFrame(frame);
        } else {
          state.chartAnimation.raf = null;
          if (typeof onDone === "function") onDone();
        }
      }

      state.chartAnimation.raf = requestAnimationFrame(frame);
    }

    async function boot() {
      renderInstructions();
    }

    boot();
  </script>
</body>
</html>
    '''

    @app.get("/")
    def index() -> str:
        return render_template_string(
            HTML,
            pretest=PRETEST,
            question_seconds=QUESTION_SECONDS,
            first_page_wait_seconds=FIRST_PAGE_WAIT_SECONDS,
            bonus_per_win=BONUS_PER_WIN,
            confidence_bonus=CONFIDENCE_BONUS,
            review_ms_per_game_minute=REVIEW_MS_PER_GAME_MINUTE,
            final_code=FINAL_CODE,
            practice_game=PRACTICE_GAME,
            quiz_games=QUIZ_GAMES,
        )

    @app.get("/health")
    def health() -> Any:
        return jsonify({
            "ok": True,
            "pretest": PRETEST,
            "games": len(QUIZ_GAMES),
            "fixed_pool_subjects": len(PRE_POOL_IDS),
            "balance_start_utc": BALANCE_START_ISO,
            "balance_abandoned_after_minutes": BALANCE_ABANDONED_AFTER_MINUTES,
        })

    @app.post("/api/start")
    def api_start() -> Any:
        payload = request.get_json(silent=True) or {}
        params = payload.get("url_params") or {}
        demo_outcome = str(payload.get("demo_outcome") or "")
        if demo_outcome not in ("win", "loss"):
            demo_outcome = None

        if not isinstance(params, dict):
            params = {}

        order = QUIZ_GAMES[:]
        random.shuffle(order)

        # PRETEST=1 only uses r1; r2_order is retained for schema compatibility.
        r2_order = order[:] if PRETEST == 0 else []

        prolific_pid = (
            params.get("PROLIFIC_PID")
            or params.get("prolific_pid")
            or params.get("prolificPid")
            or params.get("ProlificPID")
            or params.get("participant_id")
            or params.get("ParticipantID")
            or params.get("PID")
        )

        qualtrics_id = (
            params.get("Q_R")
            or params.get("ResponseId")
            or params.get("ResponseID")
            or params.get("responseid")
            or params.get("qualtrics_id")
            or params.get("QID")
        )

        conn = get_db()
        try:
            cur = conn.execute(
                """
                INSERT INTO subjects(
                  created_utc, pretest, url_params_json, prolific_pid, qualtrics_id,
                  r1_order_json, r2_order_json, demo_outcome, final_code
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    utc_now_iso(),
                    PRETEST,
                    json.dumps(params),
                    prolific_pid,
                    qualtrics_id,
                    json.dumps([g["id"] for g in order]),
                    json.dumps([g["id"] for g in r2_order]),
                    demo_outcome,
                    FINAL_CODE,
                ),
            )

            subject_id = int(cur.lastrowid)
            log_event(conn, subject_id, "started", {"pretest": PRETEST, "url_params": params, "demo_outcome": demo_outcome}, payload.get("client_ts"))
            conn.commit()

            return jsonify({"ok": True, "subject_id": subject_id, "r1_order": order, "r2_order": r2_order})
        finally:
            conn.close()

    @app.post("/api/comprehension_check")
    def api_comprehension_check() -> Any:
        payload = request.get_json(silent=True) or {}
        subject_id = int(payload.get("subject_id") or 0)
        attempt_number = int(payload.get("attempt_number") or 0)
        correct = 1 if int(payload.get("correct") or 0) == 1 else 0
        answers = payload.get("answers") or {}

        if not subject_id or attempt_number < 1:
            return jsonify({"ok": False, "error": "bad_request"}), 400

        conn = get_db()
        try:
            subj = conn.execute(
                "SELECT subject_id, comp_check_history FROM subjects WHERE subject_id=?",
                (subject_id,),
            ).fetchone()
            if not subj:
                return jsonify({"ok": False, "error": "subject_not_found"}), 404

            def comp_value(key: str) -> str:
                value = answers.get(key)
                return "" if value is None else str(value)

            attempt_history = (
                f"{attempt_number}="
                f"{{{comp_value('cc1')},{comp_value('cc2')},{comp_value('cc3')}}}"
            )
            prior_history = str(subj["comp_check_history"] or "")
            comp_check_history = f"{prior_history}_{attempt_history}" if prior_history else attempt_history

            if correct:
                conn.execute(
                    """
                    UPDATE subjects
                    SET comprehension_attempts=?,
                        comp_check_history=?,
                        comprehension_completed_utc=COALESCE(comprehension_completed_utc, ?)
                    WHERE subject_id=?
                    """,
                    (attempt_number, comp_check_history, utc_now_iso(), subject_id),
                )
            else:
                conn.execute(
                    "UPDATE subjects SET comprehension_attempts=?, comp_check_history=? WHERE subject_id=?",
                    (attempt_number, comp_check_history, subject_id),
                )

            log_event(
                conn,
                subject_id,
                "comprehension_check",
                {"attempt_number": attempt_number, "answers": answers, "correct": correct},
                payload.get("client_ts"),
            )
            conn.commit()

            return jsonify({"ok": True, "attempts": attempt_number, "correct": correct})
        finally:
            conn.close()

    @app.post("/api/answer")
    def api_answer() -> Any:
        payload = request.get_json(silent=True) or {}
        subject_id = int(payload.get("subject_id") or 0)
        round_id = str(payload.get("round_id") or "")
        game_id = str(payload.get("game_id") or "")
        qidx = int(payload.get("question_index") or 0)
        timed_out = 1 if int(payload.get("timed_out") or 0) == 1 else 0
        try:
            response_time_ms = max(0, int(payload.get("response_time_ms") or 0))
        except Exception:
            response_time_ms = None

        if not subject_id or round_id not in {"r1", "r2"} or game_id not in GAME_BY_ID:
            return jsonify({"ok": False, "error": "bad_request"}), 400

        if PRETEST == 1 and round_id != "r1":
            return jsonify({"ok": False, "error": "pretest_has_only_r1"}), 400

        try:
            answer = int(payload.get("answer"))
        except Exception:
            return jsonify({"ok": False, "error": "answer_must_be_integer"}), 400

        game = GAME_BY_ID[game_id]
        correct = int(game["correct"])

        conn = get_db()
        try:
            subj = conn.execute("SELECT subject_id FROM subjects WHERE subject_id=?", (subject_id,)).fetchone()
            if not subj:
                return jsonify({"ok": False, "error": "subject_not_found"}), 404

            opp_subject_id, opp_pool_id, opp_answer, opp_source = opponent_answer_for(
                conn, subject_id, round_id, game_id, correct
            )

            if opp_answer is None:
                outcome = None
                p_dist = distance(answer, correct)
                o_dist = None
            else:
                outcome = outcome_for(answer, int(opp_answer), correct)
                p_dist = distance(answer, correct)
                o_dist = distance(opp_answer, correct)

            conn.execute(
                """
                INSERT INTO responses(
                  subject_id, round_id, question_index, game_id, answer, timed_out, response_time_ms, client_ts, server_utc,
                  correct, opponent_subject_id, opponent_pool_id, opponent_answer, opponent_source, outcome,
                  participant_distance, opponent_distance
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(subject_id, round_id, game_id) DO UPDATE SET
                  question_index=excluded.question_index,
                  answer=excluded.answer,
                  timed_out=excluded.timed_out,
                  response_time_ms=excluded.response_time_ms,
                  client_ts=excluded.client_ts,
                  server_utc=excluded.server_utc,
                  correct=excluded.correct,
                  opponent_subject_id=excluded.opponent_subject_id,
                  opponent_pool_id=excluded.opponent_pool_id,
                  opponent_answer=excluded.opponent_answer,
                  opponent_source=excluded.opponent_source,
                  outcome=excluded.outcome,
                  participant_distance=excluded.participant_distance,
                  opponent_distance=excluded.opponent_distance
                """,
                (
                    subject_id,
                    round_id,
                    qidx,
                    game_id,
                    answer,
                    timed_out,
                    response_time_ms,
                    payload.get("client_ts"),
                    utc_now_iso(),
                    correct,
                    opp_subject_id,
                    opp_pool_id,
                    opp_answer,
                    opp_source,
                    outcome,
                    p_dist,
                    o_dist,
                ),
            )

            log_event(
                conn,
                subject_id,
                "answer",
                {
                    "round_id": round_id,
                    "game_id": game_id,
                    "answer": answer,
                    "timed_out": timed_out,
                    "response_time_ms": response_time_ms,
                },
                payload.get("client_ts"),
            )
            conn.commit()

            response = {
                "subject_id": subject_id,
                "round_id": round_id,
                "question_index": qidx,
                "game_id": game_id,
                "answer": answer,
                "timed_out": timed_out,
                "response_time_ms": response_time_ms,
                "correct": correct,
                "opponent_subject_id": opp_subject_id,
                "opponent_pool_id": opp_pool_id,
                "opponent_answer": opp_answer,
                "opponent_source": opp_source,
                "outcome": outcome,
                "participant_distance": p_dist,
                "opponent_distance": o_dist,
            }

            return jsonify({"ok": True, "response": response})
        finally:
            conn.close()

    @app.post("/api/complete_round")
    def api_complete_round() -> Any:
        payload = request.get_json(silent=True) or {}
        subject_id = int(payload.get("subject_id") or 0)
        round_id = str(payload.get("round_id") or "")

        if not subject_id or round_id not in {"r1", "r2"}:
            return jsonify({"ok": False, "error": "bad_request"}), 400

        if PRETEST == 1 and round_id != "r1":
            return jsonify({"ok": False, "error": "pretest_has_only_r1"}), 400

        conn = get_db()
        try:
            rows = all_responses_for_round(conn, subject_id, round_id)

            conn.execute(
                "INSERT INTO rounds(subject_id,round_id,completed_utc,responses_json) VALUES (?,?,?,?)",
                (subject_id, round_id, utc_now_iso(), json.dumps(rows)),
            )

            log_event(
                conn,
                subject_id,
                "round_completed",
                {"round_id": round_id, "n_responses": len(rows)},
                payload.get("client_ts"),
            )
            conn.commit()

            return jsonify({"ok": True, "round_id": round_id, "n_responses": len(rows)})
        finally:
            conn.close()

    @app.post("/api/review_options")
    def api_review_options() -> Any:
        payload = request.get_json(silent=True) or {}
        subject_id = int(payload.get("subject_id") or 0)

        if PRETEST == 1:
            return jsonify({"ok": False, "error": "no_review_in_pretest"}), 400

        conn = get_db()
        try:
            subj = conn.execute("SELECT * FROM subjects WHERE subject_id=?", (subject_id,)).fetchone()
            if not subj:
                return jsonify({"ok": False, "error": "subject_not_found"}), 404

            existing_a = subj["review_option_a_game_id"]
            existing_b = subj["review_option_b_game_id"]

            rows = all_responses_for_round(conn, subject_id, "r1")
            by_game = {r["game_id"]: r for r in rows}

            if not existing_a or not existing_b:
                wins = [r for r in rows if r.get("outcome") == "won"]
                losses = [r for r in rows if r.get("outcome") == "lost"]

                if wins and losses:
                    a_row = random.choice(wins)
                    b_row = random.choice(losses)
                    chosen = [a_row["game_id"], b_row["game_id"]]
                    random.shuffle(chosen)
                else:
                    ids = [g["id"] for g in QUIZ_GAMES if g["id"] in by_game]
                    chosen = random.sample(ids, min(2, len(ids)))

                if len(chosen) < 2:
                    return jsonify({"ok": False, "error": "not_enough_r1_responses"}), 400

                review_option_a_is_win = 1 if by_game[chosen[0]].get("outcome") == "won" else 0
                conn.execute(
                    """
                    UPDATE subjects
                    SET review_option_a_game_id=?,
                        review_option_a_is_win=?,
                        review_option_b_game_id=?
                    WHERE subject_id=?
                    """,
                    (chosen[0], review_option_a_is_win, chosen[1], subject_id),
                )
                log_event(
                    conn,
                    subject_id,
                    "review_options_generated",
                    {"game_ids": chosen, "review_option_a_is_win": review_option_a_is_win},
                    payload.get("client_ts"),
                )
                conn.commit()

                existing_a, existing_b = chosen[0], chosen[1]
            elif subj["review_option_a_is_win"] is None and existing_a in by_game:
                review_option_a_is_win = 1 if by_game[existing_a].get("outcome") == "won" else 0
                conn.execute(
                    "UPDATE subjects SET review_option_a_is_win=? WHERE subject_id=?",
                    (review_option_a_is_win, subject_id),
                )
                conn.commit()

            options = []
            for gid in [existing_a, existing_b]:
                row = by_game.get(gid)
                if row:
                    options.append({"game": GAME_BY_ID[gid], "response": row})

            touch_subject(conn, subject_id)
            conn.commit()
            return jsonify({"ok": True, "options": options})
        finally:
            conn.close()

    @app.post("/api/review_choice")
    def api_review_choice() -> Any:
        payload = request.get_json(silent=True) or {}
        subject_id = int(payload.get("subject_id") or 0)
        choice_game_id = payload.get("choice_game_id")
        reason = str(payload.get("reason") or "")

        if PRETEST == 1:
            return jsonify({"ok": False, "error": "no_review_in_pretest"}), 400

        conn = get_db()
        try:
            # Serialize assignment decisions so simultaneous requests cannot read/write
            # the same balance state at once.
            conn.execute("BEGIN IMMEDIATE")
            subj = conn.execute("SELECT * FROM subjects WHERE subject_id=?", (subject_id,)).fetchone()
            if not subj:
                conn.rollback()
                return jsonify({"ok": False, "error": "subject_not_found"}), 404

            # Idempotency: a retry returns the assignment already stored.
            if subj["assigned_review_game_id"]:
                assigned_game_id = str(subj["assigned_review_game_id"])
                row = conn.execute(
                    "SELECT * FROM responses WHERE subject_id=? AND round_id='r1' AND game_id=?",
                    (subject_id, assigned_game_id),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return jsonify({"ok": False, "error": "assigned_response_not_found"}), 400
                touch_subject(conn, subject_id)
                conn.commit()
                return jsonify({
                    "ok": True,
                    "assigned": {
                        "game": GAME_BY_ID[assigned_game_id],
                        "response": dict(row),
                        "method": subj["assigned_review_method"],
                        "balanced_condition": subj["balanced_review_condition"],
                    },
                })

            a = subj["review_option_a_game_id"]
            b = subj["review_option_b_game_id"]
            if not a or not b:
                conn.rollback()
                return jsonify({"ok": False, "error": "review_options_not_generated"}), 400

            valid = [str(a), str(b)]
            if choice_game_id not in valid:
                choice_game_id = None

            option_rows = conn.execute(
                """
                SELECT game_id, outcome
                FROM responses
                WHERE subject_id=? AND round_id='r1' AND game_id IN (?, ?)
                """,
                (subject_id, valid[0], valid[1]),
            ).fetchall()
            outcome_by_game = {str(row["game_id"]): row["outcome"] for row in option_rows}
            win_game_id = next((gid for gid in valid if outcome_by_game.get(gid) == "won"), None)
            loss_game_id = next((gid for gid in valid if outcome_by_game.get(gid) == "lost"), None)
            has_win_loss_pair = win_game_id is not None and loss_game_id is not None

            counts = {"win": 0, "loss": 0}
            balanced_condition: Optional[str] = None

            if choice_game_id is not None and random.random() < 0.10:
                assigned_game_id = str(choice_game_id)
                method = "preference_implemented"
            elif has_win_loss_pair:
                counts = balanced_assignment_counts(conn)
                balanced_condition = choose_balanced_condition(counts)
                assigned_game_id = win_game_id if balanced_condition == "win" else loss_game_id
                method = (
                    "balanced_no_preference"
                    if choice_game_id is None
                    else "balanced_random_assignment"
                )
            else:
                # Balancing is defined only when the offered pair contains one win and one loss.
                assigned_game_id = random.choice(valid)
                method = (
                    "no_preference_random_not_win_loss_pair"
                    if choice_game_id is None
                    else "random_assignment_not_win_loss_pair"
                )

            assignment_utc = utc_now_iso()
            conn.execute(
                """
                UPDATE subjects
                SET review_choice_game_id=?,
                    review_choice_reason=?,
                    assigned_review_game_id=?,
                    assigned_review_method=?,
                    balanced_review_condition=?,
                    balance_win_count_before=?,
                    balance_loss_count_before=?,
                    balance_cutoff_utc=?,
                    review_assignment_utc=?,
                    last_activity_utc=?
                WHERE subject_id=?
                """,
                (
                    choice_game_id,
                    reason,
                    assigned_game_id,
                    method,
                    balanced_condition,
                    counts["win"] if balanced_condition else None,
                    counts["loss"] if balanced_condition else None,
                    BALANCE_START_ISO,
                    assignment_utc,
                    assignment_utc,
                    subject_id,
                ),
            )

            log_event(
                conn,
                subject_id,
                "review_choice",
                {
                    "choice_game_id": choice_game_id,
                    "reason": reason,
                    "assigned_game_id": assigned_game_id,
                    "method": method,
                    "has_win_loss_pair": has_win_loss_pair,
                    "balanced_condition": balanced_condition,
                    "balance_counts_before": counts if balanced_condition else None,
                    "balance_start_utc": BALANCE_START_ISO,
                    "balance_abandoned_after_minutes": BALANCE_ABANDONED_AFTER_MINUTES,
                },
                payload.get("client_ts"),
            )

            row = conn.execute(
                "SELECT * FROM responses WHERE subject_id=? AND round_id='r1' AND game_id=?",
                (subject_id, assigned_game_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                return jsonify({"ok": False, "error": "assigned_response_not_found"}), 400

            conn.commit()
            return jsonify({
                "ok": True,
                "assigned": {
                    "game": GAME_BY_ID[assigned_game_id],
                    "response": dict(row),
                    "method": method,
                    "balanced_condition": balanced_condition,
                },
            })
        except sqlite3.OperationalError as exc:
            conn.rollback()
            return jsonify({"ok": False, "error": f"database_busy: {exc}"}), 503
        finally:
            conn.close()

    @app.post("/api/review_identification_check")
    def api_review_identification_check() -> Any:
        payload = request.get_json(silent=True) or {}
        subject_id = int(payload.get("subject_id") or 0)
        guessed_game_id = str(payload.get("guessed_game_id") or "")
        sequence = payload.get("sequence") or []
        correct = 1 if int(payload.get("correct") or 0) == 1 else 0

        if not subject_id or guessed_game_id not in GAME_BY_ID or not isinstance(sequence, list):
            return jsonify({"ok": False, "error": "bad_request"}), 400

        conn = get_db()
        try:
            subj = conn.execute("SELECT subject_id, assigned_review_game_id FROM subjects WHERE subject_id=?", (subject_id,)).fetchone()
            if not subj:
                return jsonify({"ok": False, "error": "subject_not_found"}), 404

            assigned = str(subj["assigned_review_game_id"] or "")
            server_correct = 1 if guessed_game_id == assigned else 0
            wrong_guesses = sum(1 for gid in sequence if str(gid) != assigned)

            conn.execute(
                """
                UPDATE subjects
                SET review_identification_wrong_guesses=?,
                    review_identification_sequence_json=?
                WHERE subject_id=?
                """,
                (wrong_guesses, json.dumps(sequence), subject_id),
            )
            log_event(
                conn,
                subject_id,
                "review_identification_check",
                {
                    "guessed_game_id": guessed_game_id,
                    "assigned_review_game_id": assigned,
                    "sequence": sequence,
                    "wrong_guesses": wrong_guesses,
                    "client_correct": correct,
                    "server_correct": server_correct,
                },
                payload.get("client_ts"),
            )
            conn.commit()

            return jsonify({"ok": True, "correct": server_correct, "wrong_guesses": wrong_guesses})
        finally:
            conn.close()

    @app.post("/api/review_margin_check")
    def api_review_margin_check() -> Any:
        payload = request.get_json(silent=True) or {}
        subject_id = int(payload.get("subject_id") or 0)
        sequence = payload.get("sequence") or []
        used_reopen = 1 if int(payload.get("used_reopen") or 0) == 1 else 0
        client_correct = 1 if int(payload.get("correct") or 0) == 1 else 0
        guessed_margin = payload.get("guessed_margin")

        if not subject_id or not isinstance(sequence, list):
            return jsonify({"ok": False, "error": "bad_request"}), 400

        conn = get_db()
        try:
            subj = conn.execute("SELECT subject_id, assigned_review_game_id FROM subjects WHERE subject_id=?", (subject_id,)).fetchone()
            if not subj:
                return jsonify({"ok": False, "error": "subject_not_found"}), 404

            assigned = str(subj["assigned_review_game_id"] or "")
            if assigned not in GAME_BY_ID:
                return jsonify({"ok": False, "error": "assigned_review_game_not_found"}), 400

            correct_margin = int(GAME_BY_ID[assigned]["correct"])
            numeric_guesses: list[int] = []
            for item in sequence:
                if isinstance(item, bool):
                    continue
                if isinstance(item, int):
                    numeric_guesses.append(int(item))
                elif isinstance(item, float) and item.is_integer():
                    numeric_guesses.append(int(item))
                elif isinstance(item, str) and re.fullmatch(r"[-+]?\d+", item.strip()):
                    numeric_guesses.append(int(item.strip()))

            server_correct = 1 if numeric_guesses and numeric_guesses[-1] == correct_margin else 0
            wrong_guesses = sum(1 for guess in numeric_guesses if guess != correct_margin)
            if server_correct:
                # Do not count the final correct guess as wrong.
                wrong_guesses = max(0, len(numeric_guesses) - 1)

            completed_utc = utc_now_iso() if server_correct else None

            if completed_utc:
                conn.execute(
                    """
                    UPDATE subjects
                    SET review_margin_wrong_guesses=?,
                        review_margin_sequence_json=?,
                        review_margin_used_reopen=MAX(COALESCE(review_margin_used_reopen, 0), ?),
                        review_checks_completed_utc=COALESCE(review_checks_completed_utc, ?)
                    WHERE subject_id=?
                    """,
                    (wrong_guesses, json.dumps(sequence), used_reopen, completed_utc, subject_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE subjects
                    SET review_margin_wrong_guesses=?,
                        review_margin_sequence_json=?,
                        review_margin_used_reopen=MAX(COALESCE(review_margin_used_reopen, 0), ?)
                    WHERE subject_id=?
                    """,
                    (wrong_guesses, json.dumps(sequence), used_reopen, subject_id),
                )

            log_event(
                conn,
                subject_id,
                "review_margin_check",
                {
                    "assigned_review_game_id": assigned,
                    "guessed_margin": guessed_margin,
                    "correct_margin": correct_margin,
                    "sequence": sequence,
                    "wrong_guesses": wrong_guesses,
                    "used_reopen": used_reopen,
                    "client_correct": client_correct,
                    "server_correct": server_correct,
                },
                payload.get("client_ts"),
            )
            conn.commit()

            return jsonify({"ok": True, "correct": server_correct, "wrong_guesses": wrong_guesses})
        finally:
            conn.close()

    @app.post("/api/page_timing")
    def api_page_timing() -> Any:
        payload = request.get_json(silent=True) or {}
        subject_id = int(payload.get("subject_id") or 0)
        page_name = str(payload.get("page_name") or "")
        try:
            time_ms = max(0, int(payload.get("time_ms") or 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad_time_ms"}), 400
        if not subject_id or page_name != "instructions_3":
            return jsonify({"ok": False, "error": "bad_request"}), 400

        conn = get_db()
        try:
            if not conn.execute("SELECT 1 FROM subjects WHERE subject_id=?", (subject_id,)).fetchone():
                return jsonify({"ok": False, "error": "subject_not_found"}), 404
            conn.execute("UPDATE subjects SET instructions_3_time_ms=? WHERE subject_id=?", (time_ms, subject_id))
            log_event(conn, subject_id, "page_timing", {"page_name": page_name, "time_ms": time_ms}, payload.get("client_ts"))
            conn.commit()
            return jsonify({"ok": True})
        finally:
            conn.close()

    @app.post("/api/confidence_details_activity")
    def api_confidence_details_activity() -> Any:
        payload = request.get_json(silent=True) or {}
        subject_id = int(payload.get("subject_id") or 0)
        try:
            click_count = max(0, int(payload.get("click_count") or 0))
            total_open_ms = max(0, int(payload.get("total_open_ms") or 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad_numeric_value"}), 400

        if not subject_id:
            return jsonify({"ok": False, "error": "bad_request"}), 400

        conn = get_db()
        try:
            subj = conn.execute(
                "SELECT post_r2_confidence_details_click_count, post_r2_confidence_details_open_ms "
                "FROM subjects WHERE subject_id=?",
                (subject_id,),
            ).fetchone()
            if not subj:
                return jsonify({"ok": False, "error": "subject_not_found"}), 404

            stored_clicks = int(subj["post_r2_confidence_details_click_count"] or 0)
            stored_ms = int(subj["post_r2_confidence_details_open_ms"] or 0)
            final_clicks = max(stored_clicks, click_count)
            final_ms = max(stored_ms, total_open_ms)

            conn.execute(
                """UPDATE subjects
                   SET post_r2_confidence_details_clicked=?,
                       post_r2_confidence_details_click_count=?,
                       post_r2_confidence_details_open_ms=?
                   WHERE subject_id=?""",
                (1 if final_clicks > 0 else 0, final_clicks, final_ms, subject_id),
            )
            log_event(
                conn,
                subject_id,
                "confidence_details_activity",
                {"click_count": final_clicks, "total_open_ms": final_ms},
                payload.get("client_ts"),
            )
            conn.commit()
            return jsonify({"ok": True, "click_count": final_clicks, "total_open_ms": final_ms})
        finally:
            conn.close()

    @app.post("/api/post_r2_confidence")
    def api_post_r2_confidence() -> Any:
        payload = request.get_json(silent=True) or {}
        subject_id = int(payload.get("subject_id") or 0)
        game_ids = payload.get("game_ids") or []
        try:
            confidence_percent = int(payload.get("confidence_percent"))
            time_ms = max(0, int(payload.get("time_ms") or 0))
            details_click_count = max(0, int(payload.get("details_click_count") or 0))
            details_total_open_ms = max(0, int(payload.get("details_total_open_ms") or 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad_numeric_value"}), 400

        if (not subject_id or confidence_percent < 0 or confidence_percent > 100
                or confidence_percent % 5 != 0 or not isinstance(game_ids, list)
                or len(game_ids) != 3 or any(str(gid) not in GAME_BY_ID for gid in game_ids)):
            return jsonify({"ok": False, "error": "bad_request"}), 400

        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            subj = conn.execute(
                """SELECT review_option_a_game_id, review_option_b_game_id, r2_order_json,
                          post_r2_confidence_details_click_count,
                          post_r2_confidence_details_open_ms,
                          post_r2_confidence_completed_utc,
                          post_r2_confidence_bonus_threshold_percent,
                          post_r2_confidence_bonus_branch,
                          post_r2_confidence_performance_wins,
                          post_r2_confidence_performance_success,
                          post_r2_confidence_lottery_draw,
                          post_r2_confidence_bonus_awarded,
                          post_r2_confidence_bonus_amount
                   FROM subjects WHERE subject_id=?""",
                (subject_id,),
            ).fetchone()
            if not subj:
                conn.rollback()
                return jsonify({"ok": False, "error": "subject_not_found"}), 404

            excluded = {str(gid) for gid in (subj["review_option_a_game_id"], subj["review_option_b_game_id"]) if gid}
            try:
                r2_order = [str(gid) for gid in json.loads(subj["r2_order_json"] or "[]")]
            except Exception:
                r2_order = []
            expected = [gid for gid in r2_order if gid not in excluded][:3]
            submitted = [str(gid) for gid in game_ids]
            if submitted != expected:
                conn.rollback()
                return jsonify({"ok": False, "error": "unexpected_game_ids"}), 400

            stored_clicks = int(subj["post_r2_confidence_details_click_count"] or 0)
            stored_open_ms = int(subj["post_r2_confidence_details_open_ms"] or 0)
            final_clicks = max(stored_clicks, details_click_count)
            final_open_ms = max(stored_open_ms, details_total_open_ms)

            # A duplicate submission returns the original persisted draw and outcome.
            if subj["post_r2_confidence_completed_utc"]:
                conn.execute(
                    """UPDATE subjects
                       SET post_r2_confidence_details_clicked=?,
                           post_r2_confidence_details_click_count=?,
                           post_r2_confidence_details_open_ms=?
                       WHERE subject_id=?""",
                    (1 if final_clicks > 0 else 0, final_clicks, final_open_ms, subject_id),
                )
                conn.commit()
                return jsonify({
                    "ok": True,
                    "already_completed": True,
                    "threshold_percent": subj["post_r2_confidence_bonus_threshold_percent"],
                    "bonus_branch": subj["post_r2_confidence_bonus_branch"],
                    "performance_wins": subj["post_r2_confidence_performance_wins"],
                    "performance_success": subj["post_r2_confidence_performance_success"],
                    "lottery_draw": subj["post_r2_confidence_lottery_draw"],
                    "bonus_awarded": subj["post_r2_confidence_bonus_awarded"],
                    "bonus_amount": subj["post_r2_confidence_bonus_amount"],
                })

            placeholders = ",".join("?" for _ in submitted)
            result_rows = conn.execute(
                f"""SELECT game_id, outcome
                    FROM responses
                    WHERE subject_id=? AND round_id='r2' AND game_id IN ({placeholders})""",
                (subject_id, *submitted),
            ).fetchall()
            outcome_by_game = {str(row["game_id"]): str(row["outcome"] or "") for row in result_rows}
            if len(outcome_by_game) != 3 or any(gid not in outcome_by_game for gid in submitted):
                conn.rollback()
                return jsonify({"ok": False, "error": "incomplete_r2_results"}), 409

            performance_wins = sum(1 for gid in submitted if outcome_by_game[gid] == "won")
            performance_success = 1 if performance_wins >= 2 else 0

            secure_rng = random.SystemRandom()
            threshold_percent = secure_rng.choice(list(range(5, 101, 5)))
            if confidence_percent >= threshold_percent:
                bonus_branch = "performance"
                lottery_draw = None
                bonus_awarded = performance_success
            else:
                bonus_branch = "lottery"
                lottery_draw = secure_rng.randint(1, 100)
                bonus_awarded = 1 if lottery_draw <= threshold_percent else 0

            bonus_amount = CONFIDENCE_BONUS if bonus_awarded else 0.0
            completed_utc = utc_now_iso()
            conn.execute(
                """UPDATE subjects
                   SET post_r2_confidence_percent=?, post_r2_confidence_time_ms=?,
                       post_r2_confidence_game_ids_json=?, post_r2_confidence_completed_utc=?,
                       post_r2_confidence_details_clicked=?,
                       post_r2_confidence_details_click_count=?,
                       post_r2_confidence_details_open_ms=?,
                       post_r2_confidence_bonus_threshold_percent=?,
                       post_r2_confidence_bonus_branch=?,
                       post_r2_confidence_performance_wins=?,
                       post_r2_confidence_performance_success=?,
                       post_r2_confidence_lottery_draw=?,
                       post_r2_confidence_bonus_awarded=?,
                       post_r2_confidence_bonus_amount=?,
                       post_r2_confidence_bonus_evaluated_utc=?
                   WHERE subject_id=?""",
                (
                    confidence_percent, time_ms, json.dumps(submitted), completed_utc,
                    1 if final_clicks > 0 else 0, final_clicks, final_open_ms,
                    threshold_percent, bonus_branch, performance_wins,
                    performance_success, lottery_draw, bonus_awarded,
                    bonus_amount, completed_utc, subject_id,
                ),
            )
            log_event(
                conn,
                subject_id,
                "post_r2_confidence",
                {
                    "confidence_percent": confidence_percent,
                    "time_ms": time_ms,
                    "game_ids": submitted,
                    "details_clicked": 1 if final_clicks > 0 else 0,
                    "details_click_count": final_clicks,
                    "details_total_open_ms": final_open_ms,
                    "threshold_percent": threshold_percent,
                    "bonus_branch": bonus_branch,
                    "performance_wins": performance_wins,
                    "performance_success": performance_success,
                    "lottery_draw": lottery_draw,
                    "bonus_awarded": bonus_awarded,
                    "bonus_amount": bonus_amount,
                },
                payload.get("client_ts"),
            )
            conn.commit()
            return jsonify({
                "ok": True,
                "threshold_percent": threshold_percent,
                "bonus_branch": bonus_branch,
                "performance_wins": performance_wins,
                "performance_success": performance_success,
                "lottery_draw": lottery_draw,
                "bonus_awarded": bonus_awarded,
                "bonus_amount": bonus_amount,
            })
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.post("/api/complete")
    def api_complete() -> Any:
        payload = request.get_json(silent=True) or {}
        subject_id = int(payload.get("subject_id") or 0)

        conn = get_db()
        try:
            rows = conn.execute("SELECT * FROM responses WHERE subject_id=?", (subject_id,)).fetchall()

            r1_wins = sum(1 for r in rows if r["round_id"] == "r1" and r["outcome"] == "won")
            r2_wins = sum(1 for r in rows if r["round_id"] == "r2" and r["outcome"] == "won")
            confidence_row = conn.execute(
                "SELECT post_r2_confidence_bonus_amount FROM subjects WHERE subject_id=?",
                (subject_id,),
            ).fetchone()
            confidence_bonus = (
                0.0 if PRETEST == 1 or not confidence_row
                else float(confidence_row["post_r2_confidence_bonus_amount"] or 0.0)
            )
            total_bonus = (
                0.0 if PRETEST == 1
                else (r1_wins + r2_wins) * BONUS_PER_WIN + confidence_bonus
            )

            conn.execute("UPDATE subjects SET completed_utc=? WHERE subject_id=?", (utc_now_iso(), subject_id))

            log_event(
                conn,
                subject_id,
                "completed",
                {
                    "r1_wins": r1_wins,
                    "r2_wins": r2_wins,
                    "confidence_bonus": confidence_bonus,
                    "total_bonus": total_bonus,
                },
                payload.get("client_ts"),
            )
            conn.commit()

            return jsonify({
                "ok": True,
                "r1_wins": r1_wins,
                "r2_wins": r2_wins,
                "confidence_bonus": confidence_bonus,
                "total_bonus": total_bonus,
                "final_code": FINAL_CODE,
            })
        finally:
            conn.close()

    return app


init_db()
app = create_app()

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)