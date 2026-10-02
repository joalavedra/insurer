"""SQLite database initialization."""

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(database: str | Path = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    return connection
