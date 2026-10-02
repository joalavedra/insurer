"""First notice of loss, adjustment decisions and claim payments."""

import json
import sqlite3
from typing import Any

from insurer.adjuster import GeminiAdjuster, RulesAdjuster
from insurer.ledger import Ledger
from insurer.policies import PolicyService
from insurer.products import Product, load_product


class ClaimsService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        product: Product | None = None,
        gemini: GeminiAdjuster | None = None,
    ) -> None:
        self.connection = connection
        self.product = product or load_product()
        self.policies = PolicyService(connection, self.product)
        self.ledger = Ledger(connection)
        self.rules = RulesAdjuster(self.product)
        self.gemini = gemini

    def file_claim(
        self,
        policy_id: str,
        cause: str,
        loss_date: str,
        notified_date: str,
        purchase_id: str,
        claimed_cents: int,
        evidence: list[dict[str, Any]],
        *,
        use_gemini: bool = False,
        fraud_truth: bool = False,
    ) -> dict[str, Any]:
        policy = self.policies.get_policy(policy_id)
        duplicate = (
            self.connection.execute(
                "SELECT 1 FROM claims WHERE policy_id = ? AND purchase_id = ? LIMIT 1",
                (policy_id, purchase_id),
            ).fetchone()
            is not None
        )
        version = next(
            (
                item
                for item in reversed(policy["versions"])
                if item["effective_from"] <= loss_date
                and (item["effective_to"] is None or loss_date < item["effective_to"])
            ),
            None,
        )
        if version is None:
            raise ValueError("claim loss date is outside the policy period")
        coverage = self.product.coverage
        remaining_aggregate = max(
            0,
            int(coverage["annual_aggregate_limit_cents"])
            - int(policy["aggregate_paid_cents"])
            - int(policy["case_reserve_cents"]),
        )
        reserve = min(
            max(0, claimed_cents - int(coverage["deductible_cents"])),
            int(coverage["per_claim_limit_cents"]),
            int(version["profile"]["monthly_spend_cap_cents"]),
            remaining_aggregate,
        )
        claim_count = int(
            self.connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        )
        claim_id = f"C-{claim_count + 1:08d}"
        self.ledger.post(
            f"FNOL claim {claim_id}",
            [("incurred_losses", reserve, 0), ("case_reserve", 0, reserve)],
        )
        self.connection.execute(
            """
            UPDATE policies
            SET case_reserve_cents = case_reserve_cents + ?
            WHERE policy_id = ?
            """,
            (reserve, policy_id),
        )
        policy = self.policies.get_policy(policy_id)
        policy["case_reserve_cents"] -= reserve
        adjuster: RulesAdjuster | GeminiAdjuster = (
            self.gemini if use_gemini and self.gemini is not None else self.rules
        )
        decision = adjuster.adjust(
            policy,
            cause,
            loss_date,
            notified_date,
            purchase_id,
            claimed_cents,
            evidence,
            duplicate_purchase=duplicate,
        )
        paid = 0
        reserve_remaining = reserve
        if decision.decision == "approve":
            paid = decision.amount_cents
            if paid > reserve:
                increase = paid - reserve
                self.ledger.post(
                    f"Increase case reserve {claim_id}",
                    [("incurred_losses", increase, 0), ("case_reserve", 0, increase)],
                )
                reserve_remaining += increase
            release = max(0, reserve_remaining - paid)
            postings = [("case_reserve", paid, 0), ("cash", 0, paid)]
            if release:
                postings.extend(
                    [("case_reserve", release, 0), ("incurred_losses", 0, release)]
                )
            self.ledger.post(f"Pay claim {claim_id}", postings)
            reserve_remaining = 0
        elif decision.decision == "deny" and reserve:
            self.ledger.post(
                f"Release denied reserve {claim_id}",
                [("case_reserve", reserve, 0), ("incurred_losses", 0, reserve)],
            )
            reserve_remaining = 0
        self.connection.execute(
            """
            UPDATE policies
            SET case_reserve_cents = case_reserve_cents - ?,
                aggregate_paid_cents = aggregate_paid_cents + ?
            WHERE policy_id = ?
            """,
            (reserve - reserve_remaining, paid, policy_id),
        )
        costs = self.product.simulation["lae_cost_cents"]
        if decision.decision == "refer":
            lae_kind = "human_referral"
        elif decision.adjuster == "gemini":
            lae_kind = "llm"
        else:
            lae_kind = "rules"
        lae = int(costs[lae_kind])
        self.ledger.post(
            f"Adjudication LAE {claim_id}",
            [("lae_expense", lae, 0), ("cash", 0, lae)],
        )
        decision_data = decision.as_dict()
        self.connection.execute(
            """
            INSERT INTO claims(
                claim_id, policy_id, cause, loss_date, notified_date, purchase_id,
                claimed_cents, evidence_json, decision_json, paid_cents,
                reserve_cents, fraud_truth
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                claim_id,
                policy_id,
                cause,
                loss_date,
                notified_date,
                purchase_id,
                claimed_cents,
                json.dumps(evidence, sort_keys=True),
                json.dumps(decision_data, sort_keys=True),
                paid,
                reserve_remaining,
                int(fraud_truth),
            ),
        )
        self.connection.execute(
            """
            INSERT INTO bordereau_rows(kind, month, payload_json)
            VALUES ('claims', ?, ?)
            """,
            (
                notified_date[:7],
                json.dumps(
                    {
                        "claim_id": claim_id,
                        "policy_id": policy_id,
                        "cause": cause,
                        "claimed_cents": claimed_cents,
                        "paid_cents": paid,
                        "decision": decision.decision,
                    },
                    sort_keys=True,
                ),
            ),
        )
        self.connection.commit()
        return {
            "claim_id": claim_id,
            "policy_id": policy_id,
            "cause": cause,
            "loss_date": loss_date,
            "notified_date": notified_date,
            "purchase_id": purchase_id,
            "claimed_cents": claimed_cents,
            "decision": decision_data,
            "paid_cents": paid,
            "reserve_cents": reserve_remaining,
            "lae_cents": lae,
            "fraud_truth": fraud_truth,
        }

    def resolve_referral(self, claim_id: str, approve: bool) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if row is None:
            raise KeyError("claim not found")
        old_decision = json.loads(row["decision_json"])
        if old_decision["decision"] != "refer":
            raise ValueError("claim is not referred")
        reserve = int(row["reserve_cents"])
        paid = reserve if approve else 0
        postings = []
        if approve:
            postings.extend([("case_reserve", paid, 0), ("cash", 0, paid)])
        elif reserve:
            postings.extend(
                [("case_reserve", reserve, 0), ("incurred_losses", 0, reserve)]
            )
        self.ledger.post(f"Resolve referral {claim_id}", postings)
        decision = {
            "decision": "approve" if approve else "deny",
            "amount_cents": paid,
            "reason": "Human referral resolution.",
            "clause_ids": old_decision["clause_ids"],
            "adjuster": "rules",
        }
        self.connection.execute(
            """
            UPDATE claims
            SET decision_json = ?, paid_cents = ?, reserve_cents = 0
            WHERE claim_id = ?
            """,
            (json.dumps(decision, sort_keys=True), paid, claim_id),
        )
        self.connection.execute(
            """
            UPDATE policies SET case_reserve_cents = case_reserve_cents - ?,
                aggregate_paid_cents = aggregate_paid_cents + ? WHERE policy_id = ?
            """,
            (reserve, paid, row["policy_id"]),
        )
        self.connection.commit()
        result = dict(row)
        result.update(
            {
                "decision": decision,
                "paid_cents": paid,
                "reserve_cents": 0,
                "evidence": json.loads(row["evidence_json"]),
            }
        )
        return result
