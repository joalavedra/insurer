"""End-to-end seeded Monte-Carlo book simulation."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from insurer.adjuster import GeminiAdjuster, RulesAdjuster
from insurer.capital import _truth_frequency, simulate_capital
from insurer.claims import ClaimsService
from insurer.ledger import Ledger
from insurer.money import cents
from insurer.policies import PolicyService, add_months
from insurer.products import Product, load_product
from insurer.reserving import build_triangle
from insurer.storage import connect

APPROVAL_THRESHOLD_LEVELS = ("none", "eur_200", "eur_50")
APPROVAL_THRESHOLD_WEIGHTS = (0.30, 0.45, 0.25)
ALLOWLIST_ON_PROBABILITY = 0.60
RAIL_LEVELS = ("card", "x402")
RAIL_WEIGHTS = (0.70, 0.30)
TENURE_RANGE_MONTHS = (0, 24)
MONTHLY_SPEND_MEDIAN_CENTS = 30000
MONTHLY_SPEND_CLIP_CENTS = (2000, 500000)
KILL_SWITCH_TRUE_PROBABILITY = 0.92
CANCELLATION_HAZARD_PER_MONTH = 0.01
ENDORSEMENT_HAZARD_PER_MONTH = 0.02
CLAIM_CAUSES = (
    "unauthorized_purchase",
    "prompt_injection",
    "duplicate_payment",
    "merchant_non_delivery",
    "wrong_item",
)


def _profile(rng: np.random.Generator) -> dict[str, Any]:
    cap = int(
        np.clip(
            cents(rng.lognormal(np.log(MONTHLY_SPEND_MEDIAN_CENTS), 0.6)),
            *MONTHLY_SPEND_CLIP_CENTS,
        )
    )
    return {
        "approval_threshold": str(
            rng.choice(APPROVAL_THRESHOLD_LEVELS, p=APPROVAL_THRESHOLD_WEIGHTS)
        ),
        "merchant_allowlist": bool(rng.random() < ALLOWLIST_ON_PROBABILITY),
        "rail": str(rng.choice(RAIL_LEVELS, p=RAIL_WEIGHTS)),
        "tenure_months": int(rng.integers(*TENURE_RANGE_MONTHS)),
        "monthly_spend_cap_cents": cap,
        "kill_switch": bool(rng.random() < KILL_SWITCH_TRUE_PROBABILITY),
    }


def _generate_claims(
    rng: np.random.Generator,
    profile: dict[str, Any],
    policy_id: str,
    start: date,
    end: date,
    product: Product,
    sequence: int,
) -> list[dict[str, Any]]:
    days = (end - start).days
    if days <= 0:
        return []
    count = int(rng.poisson(_truth_frequency(profile, product) * days / 365))
    if count == 0:
        return []
    severity = product.rating["severity"]
    raw_losses = rng.lognormal(
        np.log(float(severity["median_cents"])), float(severity["sigma"]), size=count
    )
    records: list[dict[str, Any]] = []
    for index, raw_loss in enumerate(raw_losses):
        loss_day = start + timedelta(days=int(rng.integers(0, days)))
        purchase_amount = min(
            cents(float(raw_loss)), int(profile["monthly_spend_cap_cents"])
        )
        fraud = bool(rng.random() < float(product.simulation["fraud_rate"]))
        fraud_mode = (
            str(rng.choice(("inflated", "approval", "refund"))) if fraud else ""
        )
        claimed = purchase_amount
        if fraud_mode == "inflated":
            claimed += max(100, cents(purchase_amount * 0.5))
        purchase_id = f"{policy_id}-TX-{sequence + index + 1:06d}"
        timestamp = loss_day.isoformat() + "T12:00:00Z"
        evidence: list[dict[str, Any]] = [
            {"ts": timestamp, "type": "kill_switch_state", "state": True},
            {
                "ts": timestamp,
                "type": "purchase",
                "amount_cents": purchase_amount,
                "purchase_id": purchase_id,
                "merchant": "sandbox-merchant",
            },
        ]
        if fraud_mode == "approval":
            evidence.append(
                {
                    "ts": timestamp,
                    "type": "approval_granted",
                    "purchase_id": purchase_id,
                    "approver": "human",
                }
            )
        elif fraud_mode == "refund":
            evidence.append(
                {"ts": timestamp, "type": "refund", "purchase_id": purchase_id}
            )
        evidence.sort(key=lambda event: event["ts"])
        lag = max(
            0,
            int(
                round(
                    float(
                        rng.exponential(
                            float(product.simulation["report_lag_mean_days"])
                        )
                    )
                )
            ),
        )
        notified_day = loss_day + timedelta(days=lag)
        records.append(
            {
                "policy_id": policy_id,
                "cause": str(rng.choice(CLAIM_CAUSES)),
                "loss_date": loss_day.isoformat(),
                "notified_date": notified_day.isoformat(),
                "purchase_id": purchase_id,
                "claimed_cents": claimed,
                "evidence": evidence,
                "fraud_truth": fraud,
                "profile": profile.copy(),
                "_sequence": sequence + index,
            }
        )
    return records


def _trial_value(trial: list[dict[str, int | str]], account: str) -> int:
    row = next(item for item in trial if item["account"] == account)
    return int(row["balance_cents"])


def _claims_from_database(connection: Any) -> list[dict[str, Any]]:
    records = []
    for row in connection.execute("SELECT * FROM claims ORDER BY claim_id"):
        records.append(
            {
                "claim_id": row["claim_id"],
                "policy_id": row["policy_id"],
                "cause": row["cause"],
                "loss_date": row["loss_date"],
                "notified_date": row["notified_date"],
                "claimed_cents": int(row["claimed_cents"]),
                "paid_cents": int(row["paid_cents"]),
                "reserve_cents": int(row["reserve_cents"]),
                "initial_incurred_cents": int(row["initial_incurred_cents"]),
                "resolved_date": row["resolved_date"],
                "incurred_cents": int(row["paid_cents"]) + int(row["reserve_cents"]),
                "decision": json.loads(row["decision_json"]),
                "evidence": json.loads(row["evidence_json"]),
                "fraud_truth": bool(row["fraud_truth"]),
            }
        )
    return records


def _evidenced_rules_amount(
    claim: dict[str, Any],
    policy: dict[str, Any],
    rules: RulesAdjuster,
    reserved_cents: int = 0,
) -> int:
    purchase = next(
        (
            event
            for event in claim["evidence"]
            if event["type"] == "purchase"
            and event.get("purchase_id") == claim["purchase_id"]
        ),
        None,
    )
    if purchase is None:
        return 0
    policy_for_adjustment = policy.copy()
    policy_for_adjustment["case_reserve_cents"] = max(
        0, int(policy["case_reserve_cents"]) - reserved_cents
    )
    decision = rules.adjust(
        policy_for_adjustment,
        claim["cause"],
        claim["loss_date"],
        claim["notified_date"],
        claim["purchase_id"],
        int(purchase.get("amount_cents", 0)),
        claim["evidence"],
    )
    return decision.amount_cents if decision.decision == "approve" else 0


def _true_ibnr_cents(
    pending: list[dict[str, Any]],
    close_day: date,
    policies: PolicyService,
    rules: RulesAdjuster,
) -> int:
    delayed = sorted(
        (
            claim
            for claim in pending
            if date.fromisoformat(claim["loss_date"]) <= close_day
            and date.fromisoformat(claim["notified_date"]) > close_day
            and not claim["fraud_truth"]
        ),
        key=lambda claim: (claim["loss_date"], claim["_sequence"]),
    )
    paid_by_policy: dict[str, int] = {}
    total = 0
    for claim in delayed:
        policy_id = str(claim["policy_id"])
        policy = policies.get_policy(policy_id)
        policy["aggregate_paid_cents"] += paid_by_policy.get(policy_id, 0)
        amount = _evidenced_rules_amount(claim, policy, rules)
        paid_by_policy[policy_id] = paid_by_policy.get(policy_id, 0) + amount
        total += amount
    return total


def _book_metrics(connection: Any) -> dict[str, int | float]:
    trial = Ledger(connection).trial_balance()
    claims = _claims_from_database(connection)
    total_premium = int(
        connection.execute(
            """
            SELECT COALESCE(
                SUM(CAST(json_extract(payload_json, '$.premium_cents') AS INTEGER)),
                0
            )
            FROM bordereau_rows WHERE kind = 'premium'
            """
        ).fetchone()[0]
    )
    total_paid = sum(int(claim["paid_cents"]) for claim in claims)
    case_reserves = sum(int(claim["reserve_cents"]) for claim in claims)
    earned = -_trial_value(trial, "earned_premium")
    unearned = -_trial_value(trial, "unearned_premium")
    ibnr = max(0, -_trial_value(trial, "ibnr_reserve"))
    lae = _trial_value(trial, "lae_expense")
    dac = max(0, _trial_value(trial, "deferred_acquisition_costs"))
    acquisition = _trial_value(trial, "acquisition_expense")
    admin = _trial_value(trial, "admin_expense")
    tax = -_trial_value(trial, "premium_tax_payable")
    incurred = total_paid + case_reserves + ibnr
    loss_ratio = incurred / earned if earned else 0.0
    lae_ratio = lae / earned if earned else 0.0
    expense_ratio = (acquisition + admin) / earned if earned else 0.0
    return {
        "gwp_cents": total_premium,
        "earned_premium_cents": earned,
        "unearned_premium_cents": unearned,
        "premium_tax_cents": tax,
        "paid_losses_cents": total_paid,
        "case_reserves_cents": case_reserves,
        "ibnr_cents": ibnr,
        "incurred_losses_cents": incurred,
        "lae_cents": lae,
        "dac_cents": dac,
        "acquisition_cents": acquisition,
        "admin_cents": admin,
        "loss_ratio": round(loss_ratio, 6),
        "lae_ratio": round(lae_ratio, 6),
        "expense_ratio": round(expense_ratio, 6),
        "combined_ratio": round(loss_ratio + lae_ratio + expense_ratio, 6),
        "underwriting_result_cents": earned - incurred - lae - acquisition - admin,
    }


def simulate_book(
    agents: int = 1000,
    months: int = 12,
    seed: int = 42,
    start: str = "2027-01-01",
    llm_sample: int = 0,
    database: str = ":memory:",
    years: int = 1000,
    quota_share: float = 0.5,
    qs_commission: float = 0.30,
    product: Product | None = None,
) -> dict[str, Any]:
    if agents < 0 or months <= 0:
        raise ValueError("agents must be non-negative and months must be positive")
    if llm_sample < 0:
        raise ValueError("llm-sample must be non-negative")
    product = product or load_product()
    start_day = date.fromisoformat(start)
    if start_day.day != 1:
        raise ValueError("simulation start date must be the first day of a month")
    end_day = add_months(start_day, months)
    rng = np.random.default_rng(seed)
    connection = connect(database)
    policies = PolicyService(connection, product)
    gemini = (
        GeminiAdjuster(product=product)
        if llm_sample and os.getenv("GEMINI_API_KEY")
        else None
    )
    claims_service = ClaimsService(connection, product, gemini)
    months_generated = list(range(months))
    arrival_weights = np.asarray(
        [2 * (index + 1) for index in months_generated], dtype=float
    )
    arrival_weights /= arrival_weights.sum()
    arrivals = (
        rng.multinomial(agents, arrival_weights)
        if agents
        else np.zeros(months, dtype=int)
    )
    policy_ids: list[str] = []
    base_segment: dict[str, str] = {}
    pending: list[dict[str, Any]] = []
    sequence = 0
    quotes = 0
    declined = 0
    cancellations = 0
    endorsements = 0
    llm_routes = 0
    monthly_rows: list[dict[str, Any]] = []
    previous_earned = 0
    previous_incurred = 0
    previous_paid = 0
    current_year = start_day.year
    year_earned = 0
    year_incurred = 0

    for month_index in months_generated:
        month_start = add_months(start_day, month_index)
        if month_start.year != current_year:
            current_year = month_start.year
            year_earned = 0
            year_incurred = 0
        next_month = add_months(start_day, month_index + 1)
        month_end = next_month - timedelta(days=1)
        month_days = (next_month - month_start).days
        for _ in range(int(arrivals[month_index])):
            arrival = month_start + timedelta(days=int(rng.integers(0, month_days)))
            profile = _profile(rng)
            quotes += 1
            quote = policies.quote(profile, arrival)
            if quote["status"] == "declined":
                declined += 1
                continue
            policy = policies.bind(quote["quote_id"])
            policy_id = str(policy["policy_id"])
            policy_ids.append(policy_id)
            base_segment[policy_id] = str(profile["approval_threshold"])

        for policy_id in list(policy_ids):
            policy = policies.get_policy(policy_id)
            if policy["cancelled"]:
                continue
            policy_start = max(month_start, date.fromisoformat(policy["start_date"]))
            policy_end = min(next_month, date.fromisoformat(policy["end_date"]))
            span = (policy_end - policy_start).days
            if span <= 0:
                continue
            original_profile = policy["profile"].copy()
            events: list[tuple[date, str]] = []
            if rng.random() < CANCELLATION_HAZARD_PER_MONTH:
                events.append(
                    (
                        policy_start + timedelta(days=int(rng.integers(1, span)))
                        if span > 1
                        else policy_start,
                        "cancel",
                    )
                )
            if rng.random() < ENDORSEMENT_HAZARD_PER_MONTH:
                events.append(
                    (
                        policy_start + timedelta(days=int(rng.integers(1, span)))
                        if span > 1
                        else policy_start,
                        "endorse",
                    )
                )
            events.sort(key=lambda item: (item[0], item[1] != "endorse"))
            current_profile = original_profile
            segment_start = policy_start
            active = True
            for event_day, event_kind in events:
                if event_day > segment_start:
                    generated = _generate_claims(
                        rng,
                        current_profile,
                        policy_id,
                        segment_start,
                        event_day,
                        product,
                        sequence,
                    )
                    pending.extend(generated)
                    sequence += len(generated)
                if event_kind == "endorse":
                    if events and any(
                        kind == "cancel" and day == event_day for day, kind in events
                    ):
                        continue
                    changed: dict[str, Any]
                    if rng.random() < 0.5:
                        levels = [
                            level
                            for level in APPROVAL_THRESHOLD_LEVELS
                            if level != current_profile["approval_threshold"]
                        ]
                        changed = {"approval_threshold": str(rng.choice(levels))}
                    else:
                        changed = {
                            "monthly_spend_cap_cents": int(
                                np.clip(
                                    cents(
                                        rng.lognormal(
                                            np.log(MONTHLY_SPEND_MEDIAN_CENTS), 0.6
                                        )
                                    ),
                                    *MONTHLY_SPEND_CLIP_CENTS,
                                )
                            )
                        }
                    policies.endorse(policy_id, changed, event_day)
                    current_profile |= changed
                    endorsements += 1
                else:
                    policies.cancel(policy_id, event_day)
                    cancellations += 1
                    active = False
                    break
                segment_start = event_day
            if active and segment_start < policy_end:
                generated = _generate_claims(
                    rng,
                    current_profile,
                    policy_id,
                    segment_start,
                    policy_end,
                    product,
                    sequence,
                )
                pending.extend(generated)
                sequence += len(generated)

        due = sorted(
            (
                claim
                for claim in pending
                if date.fromisoformat(claim["notified_date"]) <= month_end
                and not claim.get("_reported", False)
            ),
            key=lambda claim: (
                claim["notified_date"],
                claim["loss_date"],
                claim["_sequence"],
            ),
        )
        for claim in due:
            use_gemini = bool(gemini is not None and llm_routes < llm_sample)
            result = claims_service.file_claim(
                claim["policy_id"],
                claim["cause"],
                claim["loss_date"],
                claim["notified_date"],
                claim["purchase_id"],
                claim["claimed_cents"],
                claim["evidence"],
                use_gemini=use_gemini,
                fraud_truth=claim["fraud_truth"],
            )
            if use_gemini:
                llm_routes += 1
            claim["_reported"] = True
            claim["_claim_id"] = result["claim_id"]
            claim["_initial_decision"] = result["decision"]

        for policy_id in policy_ids:
            policies.earn(policy_id, month_end)
        maturity = month_end - timedelta(days=30)
        pending_by_id = {
            str(claim["_claim_id"]): claim for claim in pending if "_claim_id" in claim
        }
        for row in connection.execute(
            """
            SELECT claim_id, notified_date, fraud_truth FROM claims
            WHERE notified_date <= ?
            """,
            (maturity.isoformat(),),
        ).fetchall():
            claim_row = connection.execute(
                "SELECT decision_json, reserve_cents FROM claims WHERE claim_id = ?",
                (row["claim_id"],),
            ).fetchone()
            decision_data = json.loads(claim_row["decision_json"])
            if decision_data["decision"] == "refer":
                if row["fraud_truth"]:
                    claims_service.resolve_referral(
                        str(row["claim_id"]),
                        False,
                        resolved_date=month_end.isoformat(),
                    )
                else:
                    referral_claim = pending_by_id.get(str(row["claim_id"]))
                    amount = 0
                    if referral_claim is not None:
                        policy = policies.get_policy(str(referral_claim["policy_id"]))
                        amount = _evidenced_rules_amount(
                            referral_claim,
                            policy,
                            claims_service.rules,
                            int(claim_row["reserve_cents"]),
                        )
                    claims_service.resolve_referral(
                        str(row["claim_id"]),
                        True,
                        amount_cents=amount,
                        resolved_date=month_end.isoformat(),
                    )

        claim_records = _claims_from_database(connection)
        triangle = build_triangle(claim_records, month_end)
        estimated_ibnr = max(0, sum(int(value) for value in triangle["ibnr_cents"]))
        current_ibnr = max(
            0, -_trial_value(Ledger(connection).trial_balance(), "ibnr_reserve")
        )
        delta = estimated_ibnr - current_ibnr
        if delta > 0:
            Ledger(connection).post(
                f"IBNR true-up {month_end.isoformat()}",
                [("incurred_losses", delta, 0), ("ibnr_reserve", 0, delta)],
            )
        elif delta < 0:
            Ledger(connection).post(
                f"IBNR release {month_end.isoformat()}",
                [("ibnr_reserve", -delta, 0), ("incurred_losses", 0, -delta)],
            )
        metrics = _book_metrics(connection)
        earned_delta = int(metrics["earned_premium_cents"]) - previous_earned
        incurred_delta = int(metrics["incurred_losses_cents"]) - previous_incurred
        paid_delta = int(metrics["paid_losses_cents"]) - previous_paid
        year_earned += earned_delta
        year_incurred += incurred_delta
        premium_month = int(
            connection.execute(
                """
                SELECT COALESCE(
                    SUM(CAST(json_extract(payload_json, '$.premium_cents') AS INTEGER)),
                    0
                )
                FROM bordereau_rows WHERE kind = 'premium' AND month = ?
                """,
                (month_start.strftime("%Y-%m"),),
            ).fetchone()[0]
        )
        reported_month = [
            claim
            for claim in claim_records
            if claim["notified_date"][:7] == month_start.strftime("%Y-%m")
        ]
        in_force = sum(
            not policy["cancelled"]
            and policy["start_date"] <= month_end.isoformat()
            and policy["end_date"] > month_end.isoformat()
            for policy in (policies.get_policy(policy_id) for policy_id in policy_ids)
        )
        monthly_rows.append(
            {
                "month": month_start.strftime("%Y-%m"),
                "gwp_cents": premium_month,
                "earned_premium_cents": earned_delta,
                "incurred_losses_cents": incurred_delta,
                "paid_losses_cents": paid_delta,
                "claims_reported": len(reported_month),
                "claims_approved": sum(
                    item["decision"]["decision"] == "approve" for item in reported_month
                ),
                "claims_denied": sum(
                    item["decision"]["decision"] == "deny" for item in reported_month
                ),
                "claims_referred": sum(
                    item["decision"]["decision"] == "refer" for item in reported_month
                ),
                "policies_in_force": in_force,
                "loss_ratio": incurred_delta / earned_delta if earned_delta else 0.0,
                "loss_ratio_ytd": (
                    round(year_incurred / year_earned, 6) if year_earned else 0.0
                ),
            }
        )
        previous_earned = int(metrics["earned_premium_cents"])
        previous_incurred = int(metrics["incurred_losses_cents"])
        previous_paid = int(metrics["paid_losses_cents"])

    close_day = end_day - timedelta(days=1)
    true_ibnr = _true_ibnr_cents(
        pending,
        close_day,
        policies,
        claims_service.rules,
    )
    connection.commit()
    claim_records = _claims_from_database(connection)
    initial_decisions = {
        str(claim["_claim_id"]): claim["_initial_decision"]
        for claim in pending
        if "_claim_id" in claim
    }
    for claim in claim_records:
        if claim["claim_id"] in initial_decisions:
            claim["initial_decision"] = initial_decisions[claim["claim_id"]]
    triangle = build_triangle(claim_records, close_day)
    estimated_ibnr = max(0, sum(int(value) for value in triangle["ibnr_cents"]))
    final_trial = Ledger(connection).trial_balance()
    metrics = _book_metrics(connection)
    policies_by_id = {
        policy_id: policies.get_policy(policy_id) for policy_id in policy_ids
    }
    in_force_profiles = [
        policy["profile"]
        for policy in policies_by_id.values()
        if not policy["cancelled"]
        and policy["start_date"] <= close_day.isoformat()
        and policy["end_date"] > close_day.isoformat()
    ]
    capital = simulate_capital(
        in_force_profiles,
        years=years,
        quota_share=quota_share,
        qs_commission=qs_commission,
        seed=seed + 10000,
        product=product,
    )
    total_quotes = quotes
    book: dict[str, Any] = {
        "quotes": total_quotes,
        "declined": declined,
        "policies_written": len(policy_ids),
        "policies_in_force_end": len(in_force_profiles),
        "cancellations": cancellations,
        "endorsements": endorsements,
        "gwp_cents": int(metrics["gwp_cents"]),
        "earned_premium_cents": int(metrics["earned_premium_cents"]),
        "unearned_premium_cents": int(metrics["unearned_premium_cents"]),
        "premium_tax_cents": int(metrics["premium_tax_cents"]),
        "paid_losses_cents": int(metrics["paid_losses_cents"]),
        "case_reserves_cents": int(metrics["case_reserves_cents"]),
        "ibnr_cents": estimated_ibnr,
        "true_ibnr_cents": true_ibnr,
        "incurred_losses_cents": (
            int(metrics["paid_losses_cents"])
            + int(metrics["case_reserves_cents"])
            + estimated_ibnr
        ),
        "lae_cents": int(metrics["lae_cents"]),
        "acquisition_cents": int(metrics["acquisition_cents"]),
        "admin_cents": int(metrics["admin_cents"]),
        "dac_cents": int(metrics["dac_cents"]),
        "loss_ratio": float(metrics["loss_ratio"]),
        "lae_ratio": float(metrics["lae_ratio"]),
        "expense_ratio": float(metrics["expense_ratio"]),
        "combined_ratio": float(metrics["combined_ratio"]),
        "underwriting_result_cents": int(metrics["underwriting_result_cents"]),
    }
    book["incurred_losses_cents"] = (
        book["paid_losses_cents"] + book["case_reserves_cents"] + book["ibnr_cents"]
    )
    book["loss_ratio"] = (
        round(book["incurred_losses_cents"] / book["earned_premium_cents"], 6)
        if book["earned_premium_cents"]
        else 0.0
    )
    book["combined_ratio"] = round(
        book["loss_ratio"] + book["lae_ratio"] + book["expense_ratio"], 6
    )
    book["underwriting_result_cents"] = (
        book["earned_premium_cents"]
        - book["incurred_losses_cents"]
        - book["lae_cents"]
        - book["acquisition_cents"]
        - book["admin_cents"]
    )
    segment_rows = []
    for level in APPROVAL_THRESHOLD_LEVELS:
        segment_policies = [
            policy_id for policy_id in policy_ids if base_segment[policy_id] == level
        ]
        earned = sum(
            int(policies_by_id[policy_id]["earned_premium_cents"])
            for policy_id in segment_policies
        )
        segment_claim_ids = (
            {
                str(row["claim_id"])
                for row in connection.execute(
                    "SELECT claim_id, policy_id FROM claims WHERE policy_id IN (%s)"
                    % (",".join("?" for _ in segment_policies) or "NULL"),
                    segment_policies,
                )
            }
            if segment_policies
            else set()
        )
        segment_claims = [
            claim for claim in claim_records if claim["claim_id"] in segment_claim_ids
        ]
        incurred = sum(int(claim["incurred_cents"]) for claim in segment_claims)
        rating_factor = float(
            product.rating["frequency_factors"]["approval_threshold"][level]
        )
        truth_factor = float(
            product.simulation["truth_frequency_factors"]["approval_threshold"][level]
        )
        segment_rows.append(
            {
                "factor": "approval_threshold",
                "level": level,
                "policies": len(segment_policies),
                "earned_premium_cents": earned,
                "incurred_losses_cents": incurred,
                "loss_ratio": round(incurred / earned, 6) if earned else 0.0,
                "priced_factor": rating_factor,
                "true_factor": truth_factor,
            }
        )
    reason_counts: dict[tuple[str, str], int] = {}
    for claim in claim_records:
        adjuster_decision = claim.get("initial_decision", claim["decision"])
        decision = str(adjuster_decision["decision"])
        clauses = adjuster_decision["clause_ids"]
        clause = (
            str(clauses[0]) if clauses else ("fraud" if decision == "refer" else "none")
        )
        reason_counts[(decision, clause)] = reason_counts.get((decision, clause), 0) + 1
    claims_by_reason = [
        {"decision": decision, "clause": clause, "count": count}
        for (decision, clause), count in sorted(reason_counts.items())
    ]
    sample_sources = sorted(
        claim_records,
        key=lambda claim: (
            claim["notified_date"],
            claim["loss_date"],
            claim["claim_id"],
        ),
    )
    selected: list[dict[str, Any]] = []
    for decision in ("approve", "deny", "refer"):
        sample = next(
            (
                claim
                for claim in sample_sources
                if claim.get("initial_decision", claim["decision"])["decision"]
                == decision
            ),
            None,
        )
        if sample is not None:
            selected.append(sample)
    for claim in sample_sources:
        if claim not in selected and len(selected) < 50:
            selected.append(claim)
    claims_sample = [
        {
            "decision": adjuster_decision["decision"],
            "claim_id": claim["claim_id"],
            "policy_id": claim["policy_id"],
            "cause": claim["cause"],
            "loss_date": claim["loss_date"],
            "notified": claim["notified_date"],
            "claimed_cents": int(claim["claimed_cents"]),
            "paid_cents": int(claim["paid_cents"]),
            "final_decision": claim["decision"]["decision"],
            "reason": adjuster_decision["reason"],
            "clause_ids": adjuster_decision["clause_ids"],
            "adjuster": adjuster_decision["adjuster"],
            "fraud_truth": bool(claim["fraud_truth"]),
        }
        for claim in selected
        for adjuster_decision in [claim.get("initial_decision", claim["decision"])]
    ]
    result = {
        "params": {
            "agents": agents,
            "months": months,
            "seed": seed,
            "start": start_day.isoformat(),
            "product": product.product_key,
        },
        "book": book,
        "monthly": monthly_rows,
        "segments": segment_rows,
        "triangle": {
            "origins": triangle["origins"],
            "dev_months": triangle["dev_months"],
            "values_cents": triangle["values_cents"],
            "ldf": triangle["ldf"],
            "cdf": triangle["cdf"],
            "ultimate_cents": triangle["ultimate_cents"],
            "ibnr_cents": triangle["ibnr_cents"],
            "note": triangle["note"],
        },
        "claims_by_reason": claims_by_reason,
        "claims_sample": claims_sample,
        "capital": capital,
        "trial_balance": final_trial,
    }
    if not Ledger(connection).entries_balanced():
        raise RuntimeError("ledger contains an unbalanced journal entry")
    return result


def write_results(result: dict[str, Any], out: str | Path) -> None:
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
