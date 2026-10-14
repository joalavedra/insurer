from __future__ import annotations

from datetime import date
from typing import Any

from insurer.adjuster import Decision
from insurer.agentrisk.pricing import rate_scorecard
from insurer.agentrisk.scorecard import load_framework
from insurer.policies import version_at
from insurer.products import Product


class AgentOpsRulesAdjuster:
    def __init__(self, product: Product) -> None:
        self.product = product

    def adjust(
        self,
        policy: dict[str, Any],
        cause: str,
        loss_date: str,
        notified_date: str,
        purchase_id: str,
        claimed_cents: int,
        evidence: list[dict[str, Any]],
        duplicate_purchase: bool = False,
    ) -> Decision:
        loss_day = date.fromisoformat(loss_date)
        notified_day = date.fromisoformat(notified_date)
        version = version_at(policy, loss_day)
        in_policy_period = (
            date.fromisoformat(policy["start_date"])
            <= loss_day
            < date.fromisoformat(policy["end_date"])
        )
        if not in_policy_period or version is None or version["status"] != "active":
            return Decision(
                "deny",
                0,
                "The policy was not in force on the loss date.",
                [],
                "agent_ops",
                True,
            )

        framework = load_framework()
        peril = next((item for item in framework if item["name"] == cause), None)
        rating = rate_scorecard(version["profile"], self.product)
        schedule = rating.schedule or {}
        if cause not in self.product.coverage["covered_causes"] or (
            peril is not None and peril["id"] in schedule.get("excluded_perils", [])
        ):
            return Decision(
                "deny",
                0,
                f"E1: cause {cause} is not covered by the policy schedule.",
                ["E1"],
                "agent_ops",
                True,
            )

        incidents = [
            item
            for item in evidence
            if item.get("type") == "incident"
            and f"{item.get('trace_id')}:{item.get('span_id')}" == purchase_id
            and peril is not None
            and item.get("peril") == peril["id"]
        ]
        if not incidents:
            return Decision(
                "deny",
                0,
                "E2: no matching control incident appears in the agent traces.",
                ["E2"],
                "agent_ops",
                True,
            )
        incident = incidents[0]

        manifest_records = [
            item for item in evidence if item.get("type") == "manifest_controls"
        ]
        latest_manifest = max(
            manifest_records, key=lambda item: str(item.get("ts", "")), default={}
        )
        current_control_ids = set(latest_manifest.get("control_ids", []))
        missing_controls = sorted(
            set(schedule.get("warranted_controls", [])) - current_control_ids
        )
        if missing_controls:
            missing_text = ", ".join(missing_controls)
            return Decision(
                "deny",
                0,
                f"W1: missing warranted controls: {missing_text}.",
                ["W1"],
                "agent_ops",
                True,
            )

        if any(
            item.get("type") == "human_approval"
            and item.get("span_id") == incident.get("span_id")
            for item in evidence
        ):
            return Decision(
                "deny",
                0,
                "E4: a human approved the action through the declared approval tool.",
                ["E4"],
                "agent_ops",
                True,
            )

        notification_days = int(self.product.coverage["notification_days"])
        if (notified_day - loss_day).days > notification_days:
            return Decision(
                "deny",
                0,
                f"E3: notification was more than {notification_days} days after loss.",
                ["E3"],
                "agent_ops",
                True,
            )

        if duplicate_purchase:
            return Decision(
                "refer",
                0,
                "The incident has already been claimed.",
                ["C1"],
                "agent_ops",
            )
        evidenced_amount = incident.get("evidenced_amount_cents")
        if evidenced_amount is None:
            return Decision(
                "refer",
                0,
                "loss amount needs human verification",
                ["C1"],
                "agent_ops",
            )
        if evidenced_amount is not None and claimed_cents > int(evidenced_amount):
            return Decision(
                "refer",
                0,
                "Claimed amount exceeds the loss evidenced by the violating tool call.",
                ["C1"],
                "agent_ops",
            )

        coverage = self.product.coverage
        aggregate_remaining = max(
            0,
            int(coverage["annual_aggregate_limit_cents"])
            - int(policy["aggregate_paid_cents"])
            - int(policy["case_reserve_cents"]),
        )
        amount = min(
            max(0, claimed_cents - int(coverage["deductible_cents"])),
            int(coverage["per_claim_limit_cents"]),
            aggregate_remaining,
        )
        return Decision(
            "approve",
            amount,
            "The loss is evidenced by a covered control violation under C1.",
            ["C1"],
            "agent_ops",
        )
