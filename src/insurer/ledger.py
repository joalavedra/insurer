"""Double-entry general ledger with euro-cent postings."""

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone

ACCOUNTS = (
    "cash",
    "premium_receivable",
    "unearned_premium",
    "earned_premium",
    "premium_tax_payable",
    "deferred_acquisition_costs",
    "acquisition_expense",
    "admin_expense",
    "incurred_losses",
    "case_reserve",
    "ibnr_reserve",
    "lae_expense",
)
Posting = tuple[str, int, int]


class Ledger:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def post(self, description: str, postings: Iterable[Posting]) -> int | None:
        lines = list(postings)
        if not lines:
            return None
        if any(account not in ACCOUNTS for account, _, _ in lines):
            raise ValueError("posting contains an unknown account")
        if any(
            debit < 0 or credit < 0 or (debit and credit) for _, debit, credit in lines
        ):
            raise ValueError("each posting must have one non-negative debit or credit")
        if sum(debit for _, debit, _ in lines) != sum(credit for _, _, credit in lines):
            raise ValueError(f"unbalanced journal entry: {description}")
        cursor = self.connection.execute(
            "INSERT INTO ledger_entries(description, created_at) VALUES (?, ?)",
            (description, datetime.now(timezone.utc).isoformat()),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("ledger entry insert did not return an id")
        entry_id = int(cursor.lastrowid)
        self.connection.executemany(
            """
            INSERT INTO ledger_postings(entry_id, account, debit_cents, credit_cents)
            VALUES (?, ?, ?, ?)
            """,
            [(entry_id, account, debit, credit) for account, debit, credit in lines],
        )
        self.connection.commit()
        return entry_id

    def trial_balance(self) -> list[dict[str, int | str]]:
        totals = {
            str(row["account"]): (int(row["debit_cents"]), int(row["credit_cents"]))
            for row in self.connection.execute(
                """
                SELECT account, SUM(debit_cents) AS debit_cents,
                       SUM(credit_cents) AS credit_cents
                FROM ledger_postings GROUP BY account
                """
            )
        }
        return [
            {
                "account": account,
                "debit_cents": totals.get(account, (0, 0))[0],
                "credit_cents": totals.get(account, (0, 0))[1],
                "balance_cents": totals.get(account, (0, 0))[0]
                - totals.get(account, (0, 0))[1],
            }
            for account in ACCOUNTS
        ]

    def totals(self) -> tuple[int, int]:
        row = self.connection.execute(
            """
            SELECT COALESCE(SUM(debit_cents), 0),
                   COALESCE(SUM(credit_cents), 0)
            FROM ledger_postings
            """
        ).fetchone()
        return int(row[0]), int(row[1])

    def entries_balanced(self) -> bool:
        row = self.connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT entry_id, SUM(debit_cents) AS debits,
                       SUM(credit_cents) AS credits
                FROM ledger_postings GROUP BY entry_id HAVING debits != credits
            )
            """
        ).fetchone()
        return int(row[0]) == 0
