"""CSV exports for monthly premium and claims bordereaux."""

import csv
import io
import json
import sqlite3
from typing import Any


def export_bordereau(connection: sqlite3.Connection, kind: str, month: str) -> str:
    if kind not in {"premium", "claims"}:
        raise ValueError("bordereau kind must be premium or claims")
    rows = connection.execute(
        """
        SELECT payload_json FROM bordereau_rows
        WHERE kind = ? AND month = ? ORDER BY row_id
        """,
        (kind, month),
    ).fetchall()
    records = [json.loads(row["payload_json"]) for row in rows]
    fields = sorted({field for record in records for field in record})
    if not fields:
        fields = (
            [
                "claim_id",
                "policy_id",
                "cause",
                "claimed_cents",
                "paid_cents",
                "decision",
                "movement",
            ]
            if kind == "claims"
            else ["policy_id", "premium_cents", "premium_tax_cents", "profile"]
        )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue()


def bordereau_records(
    connection: sqlite3.Connection, kind: str, month: str
) -> list[dict[str, Any]]:
    if kind not in {"premium", "claims"}:
        raise ValueError("bordereau kind must be premium or claims")
    rows = connection.execute(
        """
        SELECT payload_json FROM bordereau_rows
        WHERE kind = ? AND month = ? ORDER BY row_id
        """,
        (kind, month),
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]
