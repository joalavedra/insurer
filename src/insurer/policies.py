"""Quote, bind, earn, endorse and cancel policy records."""

from __future__ import annotations

import json
import sqlite3
from calendar import monthrange
from datetime import date
from typing import Any

from insurer.ledger import Ledger, Posting
from insurer.money import cents
from insurer.products import Product, load_product
from insurer.rating import rate_profile


def parse_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return day.replace(
        year=year, month=month, day=min(day.day, monthrange(year, month)[1])
    )


class PolicyService:
    def __init__(
        self, connection: sqlite3.Connection, product: Product | None = None
    ) -> None:
        self.connection = connection
        self.product = product or load_product()
        self.ledger = Ledger(connection)

    def _next_id(self, table: str, prefix: str, column: str) -> str:
        count = (
            int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            + 1
        )
        return f"{prefix}-{count:08d}"

    def quote(self, profile: dict[str, Any], start_date: str | date) -> dict[str, Any]:
        start = parse_date(start_date)
        rating = rate_profile(profile, self.product)
        quote_id = self._next_id("quotes", "Q", "quote_id")
        payload = {
            "quote_id": quote_id,
            "profile": profile,
            "start_date": start.isoformat(),
            "end_date": add_months(
                start, int(self.product.raw["term_months"])
            ).isoformat(),
            "rating": rating.as_dict(),
        }
        self.connection.execute(
            """
            INSERT INTO quotes(quote_id, payload_json, status, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                quote_id,
                json.dumps(payload, sort_keys=True),
                rating.status,
                start.isoformat(),
            ),
        )
        self.connection.commit()
        return payload | {"status": rating.status, "reason": rating.reason}

    def bind(self, quote_id: str) -> dict[str, Any]:
        quote_row = self.connection.execute(
            "SELECT payload_json, status FROM quotes WHERE quote_id = ?", (quote_id,)
        ).fetchone()
        if quote_row is None:
            raise KeyError("quote not found")
        if quote_row["status"] != "quoted":
            raise ValueError("declined quotes cannot be bound")
        quote = json.loads(quote_row["payload_json"])
        policy_id = self._next_id("policies", "P", "policy_id")
        start = parse_date(quote["start_date"])
        end = parse_date(quote["end_date"])
        premium = int(quote["rating"]["technical_premium_cents"])
        tax = int(quote["rating"]["premium_tax_cents"])
        profile = quote["profile"]
        self.connection.execute(
            """
            INSERT INTO policies(
                policy_id, quote_id, start_date, end_date, profile_json, premium_cents,
                tax_cents, earned_through
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_id,
                quote_id,
                start.isoformat(),
                end.isoformat(),
                json.dumps(profile, sort_keys=True),
                premium,
                tax,
                start.isoformat(),
            ),
        )
        self.connection.execute(
            """
            INSERT INTO policy_versions(
                policy_id, version, effective_from, effective_to, profile_json,
                premium_cents, tax_cents, earned_before_cents,
                earned_tax_before_cents, status
            ) VALUES (?, 1, ?, ?, ?, ?, ?, 0, 0, 'active')
            """,
            (
                policy_id,
                start.isoformat(),
                end.isoformat(),
                json.dumps(profile, sort_keys=True),
                premium,
                tax,
            ),
        )
        self.connection.execute(
            "UPDATE quotes SET status = 'bound' WHERE quote_id = ?", (quote_id,)
        )
        loads = self.product.rating["loads"]
        acquisition = cents(premium * float(loads["acquisition"]))
        admin = cents(premium * float(loads["admin"]))
        self.ledger.post(
            f"Bind policy {policy_id}",
            [
                ("premium_receivable", premium + tax, 0),
                ("unearned_premium", 0, premium),
                ("premium_tax_payable", 0, tax),
            ],
        )
        self.ledger.post(
            f"Collect policy {policy_id}",
            [("cash", premium + tax, 0), ("premium_receivable", 0, premium + tax)],
        )
        self.ledger.post(
            f"Acquisition expense {policy_id}",
            [("acquisition_expense", acquisition, 0), ("cash", 0, acquisition)],
        )
        self.ledger.post(
            f"Admin expense {policy_id}",
            [("admin_expense", admin, 0), ("cash", 0, admin)],
        )
        self.connection.execute(
            """
            INSERT INTO bordereau_rows(kind, month, payload_json)
            VALUES ('premium', ?, ?)
            """,
            (
                start.strftime("%Y-%m"),
                json.dumps(
                    {
                        "policy_id": policy_id,
                        "premium_cents": premium,
                        "premium_tax_cents": tax,
                        "profile": profile,
                    },
                    sort_keys=True,
                ),
            ),
        )
        self.connection.commit()
        return self.get_policy(policy_id)

    def earn(self, policy_id: str, through: str | date) -> int:
        policy = self._policy_row(policy_id)
        if policy["cancelled"]:
            return 0
        through_date = min(parse_date(through), parse_date(policy["end_date"]))
        previous = parse_date(policy["earned_through"])
        if through_date <= previous:
            return 0
        version = self.connection.execute(
            """
            SELECT * FROM policy_versions
            WHERE policy_id = ? AND effective_from <= ? ORDER BY version DESC LIMIT 1
            """,
            (policy_id, through_date.isoformat()),
        ).fetchone()
        if version is None:
            return 0
        term_days = (
            parse_date(policy["end_date"]) - parse_date(version["effective_from"])
        ).days
        elapsed_days = (through_date - parse_date(version["effective_from"])).days
        earned_target = int(version["earned_before_cents"]) + cents(
            int(version["premium_cents"]) * min(elapsed_days, term_days) / term_days
        )
        tax_target = int(version["earned_tax_before_cents"]) + cents(
            int(version["tax_cents"]) * min(elapsed_days, term_days) / term_days
        )
        premium_delta = max(
            0,
            min(earned_target, int(policy["premium_cents"]))
            - int(policy["earned_cents"]),
        )
        tax_delta = max(
            0,
            min(tax_target, int(policy["tax_cents"])) - int(policy["earned_tax_cents"]),
        )
        self.ledger.post(
            f"Earn premium {policy_id} through {through_date.isoformat()}",
            [
                ("unearned_premium", premium_delta, 0),
                ("earned_premium", 0, premium_delta),
            ],
        )
        self.connection.execute(
            """
            UPDATE policies
            SET earned_cents = ?, earned_tax_cents = ?, earned_through = ?
            WHERE policy_id = ?
            """,
            (
                int(policy["earned_cents"]) + premium_delta,
                int(policy["earned_tax_cents"]) + tax_delta,
                through_date.isoformat(),
                policy_id,
            ),
        )
        self.connection.commit()
        return premium_delta

    def endorse(
        self,
        policy_id: str,
        profile_changes: dict[str, Any],
        effective_date: str | date,
    ) -> dict[str, Any]:
        day = parse_date(effective_date)
        policy = self._policy_row(policy_id)
        if policy["cancelled"] or not parse_date(
            policy["start_date"]
        ) <= day < parse_date(policy["end_date"]):
            raise ValueError("endorsement date must be during an active policy")
        self.earn(policy_id, day)
        policy = self._policy_row(policy_id)
        current_profile = json.loads(policy["profile_json"])
        new_profile = current_profile | profile_changes
        remaining_days = (parse_date(policy["end_date"]) - day).days
        rating = rate_profile(
            new_profile,
            self.product,
            term_days=remaining_days,
            annual_term_days=(
                parse_date(policy["end_date"]) - parse_date(policy["start_date"])
            ).days,
        )
        if rating.status != "quoted":
            raise ValueError(rating.reason or "endorsement declined")
        new_total = int(policy["earned_cents"]) + rating.technical_premium_cents
        new_tax_total = int(policy["earned_tax_cents"]) + rating.premium_tax_cents
        premium_delta = new_total - int(policy["premium_cents"])
        tax_delta = new_tax_total - int(policy["tax_cents"])
        postings: list[Posting] = []
        if premium_delta > 0:
            postings.extend(
                [("cash", premium_delta, 0), ("unearned_premium", 0, premium_delta)]
            )
        elif premium_delta < 0:
            postings.extend(
                [("unearned_premium", -premium_delta, 0), ("cash", 0, -premium_delta)]
            )
        if tax_delta > 0:
            postings.extend(
                [("cash", tax_delta, 0), ("premium_tax_payable", 0, tax_delta)]
            )
        elif tax_delta < 0:
            postings.extend(
                [("premium_tax_payable", -tax_delta, 0), ("cash", 0, -tax_delta)]
            )
        self.ledger.post(f"Endorse policy {policy_id} {day.isoformat()}", postings)
        version = int(
            self.connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1 FROM policy_versions
                WHERE policy_id = ?
                """,
                (policy_id,),
            ).fetchone()[0]
        )
        self.connection.execute(
            """
            INSERT INTO policy_versions(
                policy_id, version, effective_from, effective_to, profile_json,
                premium_cents, tax_cents, earned_before_cents,
                earned_tax_before_cents, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                policy_id,
                version,
                day.isoformat(),
                policy["end_date"],
                json.dumps(new_profile, sort_keys=True),
                rating.technical_premium_cents,
                rating.premium_tax_cents,
                int(policy["earned_cents"]),
                int(policy["earned_tax_cents"]),
            ),
        )
        self.connection.execute(
            """
            UPDATE policies SET profile_json = ?, premium_cents = ?, tax_cents = ?
            WHERE policy_id = ?
            """,
            (
                json.dumps(new_profile, sort_keys=True),
                new_total,
                new_tax_total,
                policy_id,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO bordereau_rows(kind, month, payload_json)
            VALUES ('premium', ?, ?)
            """,
            (
                day.strftime("%Y-%m"),
                json.dumps(
                    {
                        "policy_id": policy_id,
                        "premium_cents": premium_delta,
                        "premium_tax_cents": tax_delta,
                        "profile": new_profile,
                    },
                    sort_keys=True,
                ),
            ),
        )
        self.connection.commit()
        return self.get_policy(policy_id)

    def cancel(self, policy_id: str, effective_date: str | date) -> dict[str, Any]:
        day = parse_date(effective_date)
        policy = self._policy_row(policy_id)
        if policy["cancelled"] or not parse_date(
            policy["start_date"]
        ) <= day < parse_date(policy["end_date"]):
            raise ValueError("cancellation date must be during an active policy")
        self.earn(policy_id, day)
        policy = self._policy_row(policy_id)
        refund_premium = int(policy["premium_cents"]) - int(policy["earned_cents"])
        refund_tax = int(policy["tax_cents"]) - int(policy["earned_tax_cents"])
        self.ledger.post(
            f"Cancel policy {policy_id} {day.isoformat()}",
            [
                ("unearned_premium", refund_premium, 0),
                ("premium_tax_payable", refund_tax, 0),
                ("cash", 0, refund_premium + refund_tax),
            ],
        )
        version = int(
            self.connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1 FROM policy_versions
                WHERE policy_id = ?
                """,
                (policy_id,),
            ).fetchone()[0]
        )
        self.connection.execute(
            """
            INSERT INTO policy_versions(
                policy_id, version, effective_from, effective_to, profile_json,
                premium_cents, tax_cents, earned_before_cents,
                earned_tax_before_cents, status
            ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, 'cancelled')
            """,
            (
                policy_id,
                version,
                day.isoformat(),
                day.isoformat(),
                policy["profile_json"],
                int(policy["earned_cents"]),
                int(policy["earned_tax_cents"]),
            ),
        )
        self.connection.execute(
            "UPDATE policies SET cancelled = 1 WHERE policy_id = ?", (policy_id,)
        )
        self.connection.execute(
            """
            INSERT INTO bordereau_rows(kind, month, payload_json)
            VALUES ('premium', ?, ?)
            """,
            (
                day.strftime("%Y-%m"),
                json.dumps(
                    {
                        "policy_id": policy_id,
                        "premium_cents": -refund_premium,
                        "premium_tax_cents": -refund_tax,
                        "profile": json.loads(policy["profile_json"]),
                    },
                    sort_keys=True,
                ),
            ),
        )
        self.connection.commit()
        return self.get_policy(policy_id)

    def renew(
        self, policy_id: str, effective_date: str | date | None = None
    ) -> dict[str, Any]:
        policy = self._policy_row(policy_id)
        if policy["cancelled"]:
            raise ValueError("cancelled policies cannot be renewed")
        renewal_date = parse_date(effective_date or policy["end_date"])
        if renewal_date != parse_date(policy["end_date"]):
            raise ValueError("renewal date must match the policy expiry")
        self.earn(policy_id, renewal_date)
        profile = json.loads(policy["profile_json"])
        quote = self.quote(profile, renewal_date)
        if quote["status"] != "quoted":
            raise ValueError(quote["reason"] or "renewal declined")
        return self.bind(quote["quote_id"])

    def versions(self, policy_id: str) -> list[dict[str, Any]]:
        self._policy_row(policy_id)
        rows = self.connection.execute(
            "SELECT * FROM policy_versions WHERE policy_id = ? ORDER BY version",
            (policy_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            end = row["effective_to"]
            if index + 1 < len(rows):
                end = rows[index + 1]["effective_from"]
            result.append(
                {
                    "version": int(row["version"]),
                    "effective_from": row["effective_from"],
                    "effective_to": end,
                    "profile": json.loads(row["profile_json"]),
                    "premium_cents": int(row["premium_cents"]),
                    "tax_cents": int(row["tax_cents"]),
                    "status": row["status"],
                }
            )
        return result

    def get_policy(self, policy_id: str) -> dict[str, Any]:
        row = self._policy_row(policy_id)
        return {
            "policy_id": policy_id,
            "quote_id": row["quote_id"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "profile": json.loads(row["profile_json"]),
            "premium_cents": int(row["premium_cents"]),
            "tax_cents": int(row["tax_cents"]),
            "earned_premium_cents": int(row["earned_cents"]),
            "earned_tax_cents": int(row["earned_tax_cents"]),
            "aggregate_paid_cents": int(row["aggregate_paid_cents"]),
            "case_reserve_cents": int(row["case_reserve_cents"]),
            "cancelled": bool(row["cancelled"]),
            "versions": self.versions(policy_id),
        }

    def _policy_row(self, policy_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM policies WHERE policy_id = ?", (policy_id,)
        ).fetchone()
        if row is None:
            raise KeyError("policy not found")
        return row
