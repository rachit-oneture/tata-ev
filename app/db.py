"""Durable analysis snapshots and client feedback, linked by an opaque analysis ID."""
import json
import os
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

from .config import settings


@contextmanager
def connection():
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url:
        import psycopg
        with psycopg.connect(database_url) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS analysis_records (
                id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                original_log TEXT NOT NULL, response_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS analysis_feedback (
                analysis_id TEXT PRIMARY KEY REFERENCES analysis_records(id),
                rating TEXT CHECK(rating IN ('up','down')), feedback TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            yield db
        return
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(settings.db_path, timeout=15)
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS analysis_records (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                original_log TEXT NOT NULL,
                response_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS analysis_feedback (
                analysis_id TEXT PRIMARY KEY REFERENCES analysis_records(id),
                rating TEXT CHECK(rating IN ('up','down')),
                feedback TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
        """)
        with db:
            yield db
    finally:
        db.close()


def save_analysis(original_log: str, report: dict) -> dict:
    snapshot = {**report, "analysis_id": str(uuid4())}
    serialized = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
    with connection() as db:
        placeholder = "%s" if os.environ.get("DATABASE_URL") else "?"
        db.execute(f"INSERT INTO analysis_records (id, original_log, response_json) VALUES ({placeholder}, {placeholder}, {placeholder})",
                   (snapshot["analysis_id"], original_log, serialized))
    return snapshot


def save_feedback(analysis_id: str, rating: str | None, feedback: str | None) -> dict | None:
    with connection() as db:
        postgres = bool(os.environ.get("DATABASE_URL"))
        mark = "%s" if postgres else "?"
        feedback_check = "%s::text" if postgres else "?"
        now = "CURRENT_TIMESTAMP" if postgres else "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        if not db.execute(f"SELECT 1 FROM analysis_records WHERE id = {mark}", (analysis_id,)).fetchone():
            return None
        db.execute(f"""
            INSERT INTO analysis_feedback (analysis_id, rating, feedback) VALUES ({mark}, {mark}, COALESCE({mark}, ''))
            ON CONFLICT(analysis_id) DO UPDATE SET
                rating = COALESCE(excluded.rating, analysis_feedback.rating),
                feedback = CASE WHEN {feedback_check} IS NULL THEN analysis_feedback.feedback ELSE excluded.feedback END,
                updated_at = {now}
        """, (analysis_id, rating, feedback, feedback))
        row = db.execute(f"SELECT rating, feedback FROM analysis_feedback WHERE analysis_id = {mark}", (analysis_id,)).fetchone()
    return {"analysis_id": analysis_id, "rating": row[0], "feedback": row[1], "saved": True}
