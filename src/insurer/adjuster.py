"""Deterministic claims rules and a rules-guarded Gemini adjuster."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from insurer.products import Product, load_product

GEMINI_PROMPT = "\n".join(
    [
        (
            'You are a claims adjuster for "Agent Spend Cover", an insurance policy '
            "that covers financial losses caused by purchases made by an AI agent."
        ),
        (
            "Decide the claim using ONLY the policy wording and the evidence below. "
            "Do not assume facts that are not in the evidence."
        ),
        "",
        "POLICY WORDING (clause id: text):",
        "{wording}",
        "",
        "POLICY:",
        "{policy_json}",
        "",
        "CLAIM:",
        "{claim_json}",
        "",
        "EVIDENCE (Valet audit log events, chronological):",
        "{evidence_json}",
        "",
        "Return JSON only, with exactly these keys:",
        (
            '{"decision": "approve" | "deny" | "refer", "amount_cents": '
            '<integer, 0 if not approve>, "reason": "<one or two sentences citing '
            'the evidence>", "clause_ids": ["<ids of the clauses, warranties or '
            'exclusions you relied on>"]}'
        ),
        (
            'Use "refer" when the evidence is inconsistent, suggests fraud, or is '
            "insufficient to decide."
        ),
    ]
)


@dataclass(frozen=True)
class Decision:
    decision: str
    amount_cents: int
    reason: str
    clause_ids: list[str]
    adjuster: str
    hard_rule_denial: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "amount_cents": self.amount_cents,
            "reason": self.reason,
            "clause_ids": self.clause_ids,
            "adjuster": self.adjuster,
        }


class RulesAdjuster:
    def __init__(self, product: Product | None = None) -> None:
        self.product = product or load_product()

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
        version = next(
            (
                item
                for item in reversed(policy["versions"])
                if item["effective_from"] <= loss_date
                and (item["effective_to"] is None or loss_date < item["effective_to"])
            ),
            None,
        )
        if version is None or version["status"] != "active":
            return Decision(
                "deny",
                0,
                "The policy was not in force on the loss date.",
                [],
                "rules",
                True,
            )
        coverage = self.product.coverage
        if cause not in coverage["covered_causes"]:
            return Decision(
                "deny", 0, f"Cause {cause} is not covered.", [], "rules", True
            )
        kill_switch_events = [
            event
            for event in evidence
            if event["type"] == "kill_switch_state" and event["ts"][:10] <= loss_date
        ]
        if kill_switch_events:
            state = max(kill_switch_events, key=lambda event: event["ts"])["state"]
            if state is False:
                return Decision(
                    "deny",
                    0,
                    "W1: the kill-switch was not operational at the loss date.",
                    ["W1"],
                    "rules",
                    True,
                )
        purchases = [
            event
            for event in evidence
            if event["type"] == "purchase" and event.get("purchase_id") == purchase_id
        ]
        if not purchases:
            return Decision(
                "deny",
                0,
                "E2: no matching purchase event appears in the audit log.",
                ["E2"],
                "rules",
                True,
            )
        purchase = max(purchases, key=lambda event: event["ts"])
        if any(
            event["type"] == "approval_granted"
            and event.get("purchase_id") == purchase_id
            for event in evidence
        ):
            return Decision(
                "deny",
                0,
                "E1: a human explicitly approved this purchase.",
                ["E1"],
                "rules",
                True,
            )
        if (notified_day - loss_day).days > 30:
            return Decision(
                "deny",
                0,
                "E3: notification was more than 30 days after the loss.",
                ["E3"],
                "rules",
                True,
            )
        purchase_amount = int(purchase.get("amount_cents", 0))
        if (
            claimed_cents > purchase_amount
            or any(
                event["type"] == "refund" and event.get("purchase_id") == purchase_id
                for event in evidence
            )
            or duplicate_purchase
        ):
            return Decision(
                "refer",
                0,
                "Evidence indicates possible fraud or a duplicate claim.",
                [],
                "rules",
            )
        aggregate_remaining = max(
            0,
            int(coverage["annual_aggregate_limit_cents"])
            - int(policy["aggregate_paid_cents"])
            - int(policy["case_reserve_cents"]),
        )
        amount = min(
            max(0, purchase_amount - int(coverage["deductible_cents"])),
            int(coverage["per_claim_limit_cents"]),
            int(version["profile"]["monthly_spend_cap_cents"]),
            aggregate_remaining,
        )
        return Decision(
            "approve",
            amount,
            "The evidenced purchase is covered under C1.",
            ["C1"],
            "rules",
        )


class GeminiAdjuster:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        product: Product | None = None,
    ) -> None:
        self.api_key = (
            api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")
        )
        self.model = model or os.getenv("INSURER_GEMINI_MODEL", "gemini-2.5-flash")
        self.timeout = timeout
        self.transport = transport
        self.product = product or load_product()
        self.rules = RulesAdjuster(self.product)

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
        rules = self.rules.adjust(
            policy,
            cause,
            loss_date,
            notified_date,
            purchase_id,
            claimed_cents,
            evidence,
            duplicate_purchase,
        )
        if not self.api_key:
            return rules
        wording = "\n".join(
            f"{item['id']}: {item['text']}"
            for group in ("clauses", "warranties", "exclusions")
            for item in self.product.raw.get(group, [])
        )
        claim = {
            "cause": cause,
            "loss_date": loss_date,
            "notified_date": notified_date,
            "purchase_id": purchase_id,
            "claimed_cents": claimed_cents,
        }
        prompt = GEMINI_PROMPT.replace("{wording}", wording)
        prompt = prompt.replace("{policy_json}", json.dumps(policy, sort_keys=True))
        prompt = prompt.replace("{claim_json}", json.dumps(claim, sort_keys=True))
        prompt = prompt.replace(
            "{evidence_json}",
            json.dumps(sorted(evidence, key=lambda item: item["ts"]), sort_keys=True),
        )
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
                response = client.post(url, params={"key": self.api_key}, json=body)
                response.raise_for_status()
            content = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            llm = json.loads(content)
            if set(llm) != {"decision", "amount_cents", "reason", "clause_ids"}:
                raise ValueError("Gemini response keys did not match the contract")
            if llm["decision"] not in {"approve", "deny", "refer"}:
                raise ValueError("Gemini returned an invalid decision")
            if rules.hard_rule_denial and llm["decision"] != rules.decision:
                return Decision(
                    "refer",
                    0,
                    "Gemini disagreed with a hard policy rule; refer for human review.",
                    rules.clause_ids,
                    "gemini",
                )
            if rules.decision == "refer":
                return Decision(
                    "refer", 0, str(llm["reason"]), list(llm["clause_ids"]), "gemini"
                )
            decision = str(llm["decision"])
            amount = 0
            if decision == "approve":
                amount = min(max(0, int(llm["amount_cents"])), rules.amount_cents)
            return Decision(
                decision, amount, str(llm["reason"]), list(llm["clause_ids"]), "gemini"
            )
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return rules
