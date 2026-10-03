"""SQLite database initialization."""

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(database: str | Path = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    claim_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(claims)")
    }
    if "initial_incurred_cents" not in claim_columns:
        connection.execute(
            """
            ALTER TABLE claims
            ADD COLUMN initial_incurred_cents INTEGER NOT NULL DEFAULT 0
            """
        )
    if "resolved_date" not in claim_columns:
        connection.execute("ALTER TABLE claims ADD COLUMN resolved_date TEXT")
    connection.commit()
    return connection
