from __future__ import annotations

from pathlib import Path
from typing import Any

from insurer.agentrisk.exam import load_battery
from insurer.agentrisk.scorecard import scorecard_sha256
from insurer.money import cents
from insurer.products import Product
from insurer.rating import RatingResult, RatingStep, lognormal_lev


def _declined(reason: str, steps: list[RatingStep]) -> RatingResult:
    return RatingResult("declined", reason, 0, 0, 0, 0, 0, 0, steps)


def _battery_path(name: str) -> Path:
    path = Path(name)
    if path.suffix:
        return path
    return Path(__file__).with_name("exams") / f"{name}.yaml"


def rate_scorecard(
    profile: dict[str, Any],
    product: Product,
    *,
    term_days: int | None = None,
    annual_term_days: int = 365,
) -> RatingResult:
    scorecard = profile.get("scorecard")
    if not isinstance(scorecard, dict):
        raise ValueError("profile.scorecard must be a scorecard mapping")
    sessions = profile.get("sessions_per_month")
    if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions < 0:
        raise ValueError("profile.sessions_per_month must be a non-negative integer")

    expected_hash = (
        scorecard.get("evidence", {}).get("scorecard_sha256")
        if isinstance(scorecard.get("evidence"), dict)
        else None
    )
    actual_hash = scorecard_sha256(scorecard)
    steps = [
        RatingStep(
            "scorecard_integrity",
            "verified" if expected_hash == actual_hash else "declined",
            "The scorecard hash matches its contents."
            if expected_hash == actual_hash
            else "The scorecard hash does not match its contents.",
        )
    ]
    if expected_hash != actual_hash:
        return _declined("scorecard hash mismatch", steps)

    battery_name = str(product.raw["underwriting"]["battery"])
    battery = load_battery(_battery_path(battery_name))
    inputs = scorecard.get("evidence", {}).get("inputs", {})
    battery_input = inputs.get("battery", {}) if isinstance(inputs, dict) else {}
    battery_hash = (
        battery_input.get("sha256") if isinstance(battery_input, dict) else None
    )
    battery_matches = battery_hash == battery.sha256
    steps.append(
        RatingStep(
            "battery",
            "verified" if battery_matches else "declined",
            f"Scorecard battery matches {battery_name}."
            if battery_matches
            else f"Scorecard battery does not match {battery_name}.",
        )
    )
    if not battery_matches:
        return _declined(f"scorecard not produced with {battery_name}", steps)

    rating = product.rating
    coverage = product.coverage
    severity_config = rating["severity"]
    grade_factors = rating["grade_factors"]
    autonomy = scorecard.get("autonomy")
    autonomy_factor = rating["autonomy_factors"].get(autonomy)
    if autonomy_factor is None:
        raise ValueError(f"unsupported scorecard autonomy: {autonomy!r}")

    covered_perils: list[str] = []
    excluded_perils: list[str] = []
    peril_frequency: list[float] = []
    expected_loss_cost = 0.0
    exposed_count = 0
    for peril in scorecard.get("perils", []):
        peril_id = str(peril.get("id", ""))
        if peril.get("status") != "exposed":
            steps.append(RatingStep(f"peril.{peril_id}", 0, "Peril is not exposed."))
            continue
        exposed_count += 1
        exam_level = peril.get("exam_level")
        if exam_level == "untested" and product.raw["underwriting"].get(
            "decline_if_untested"
        ):
            reason = f"{peril_id}: insurer exam required"
            steps.append(RatingStep(f"peril.{peril_id}", "declined", reason))
            return _declined(reason, steps)
        grade = peril.get("grade")
        if not peril.get("controls"):
            excluded_perils.append(peril_id)
            steps.append(
                RatingStep(
                    f"peril.{peril_id}",
                    0,
                    "no declared controls; incidents not claimable",
                )
            )
            continue
        if grade == product.raw["underwriting"]["exclude_peril_at_grade"]:
            excluded_perils.append(peril_id)
            steps.append(
                RatingStep(
                    f"peril.{peril_id}",
                    0,
                    f"Grade {grade}; peril excluded from coverage.",
                )
            )
            continue
        if grade not in grade_factors:
            raise ValueError(f"unsupported grade for {peril_id}: {grade!r}")
        try:
            base_frequency = float(rating["base_frequency_per_10k_sessions"][peril_id])
            severity = severity_config[peril_id]
        except KeyError as exc:
            raise ValueError(f"missing rating data for peril {peril_id}") from exc
        frequency = (
            sessions
            * 12
            / 10_000
            * base_frequency
            * float(grade_factors[grade])
            * float(autonomy_factor)
        )
        deductible = int(coverage["deductible_cents"])
        limit = int(coverage["per_claim_limit_cents"])
        expected_severity = lognormal_lev(
            deductible + limit,
            float(severity["median_cents"]),
            float(severity["sigma"]),
        ) - lognormal_lev(
            deductible,
            float(severity["median_cents"]),
            float(severity["sigma"]),
        )
        peril_loss = frequency * expected_severity
        peril_frequency.append(frequency)
        expected_loss_cost += peril_loss
        covered_perils.append(peril_id)
        steps.extend(
            [
                RatingStep(
                    f"peril.{peril_id}.frequency",
                    frequency,
                    "Frequency from sessions, base rate, grade, and autonomy.",
                ),
                RatingStep(
                    f"peril.{peril_id}.expected_covered_severity_cents",
                    expected_severity,
                    "LEV(deductible + per-claim limit) − LEV(deductible).",
                ),
                RatingStep(
                    f"peril.{peril_id}.expected_loss_cost_cents",
                    peril_loss,
                    "Annual peril frequency × expected covered severity.",
                ),
            ]
        )

    if exposed_count == 0 or not covered_perils:
        steps.append(RatingStep("coverage", "declined", "No insurable perils."))
        return _declined("no insurable perils", steps)

    frequency = sum(peril_frequency)
    expected_severity = expected_loss_cost / frequency if frequency else 0.0
    steps.append(
        RatingStep(
            "frequency",
            frequency,
            "Sum of annual frequencies for covered perils.",
        )
    )
    steps.append(
        RatingStep(
            "expected_covered_severity_cents",
            expected_severity,
            "Expected loss cost divided by annual frequency.",
        )
    )
    steps.append(
        RatingStep(
            "expected_loss_cost_cents",
            expected_loss_cost,
            "Sum of expected loss costs for covered perils.",
        )
    )

    loads = rating["loads"]
    total_load = sum(float(load) for load in loads.values())
    divisor = 1 - total_load
    annual_technical = expected_loss_cost / divisor
    steps.extend(
        [
            RatingStep("loads_total", total_load, f"Premium divisor: {divisor:.2f}."),
            RatingStep(
                "loads_divisor",
                divisor,
                "1 − sum of acquisition, admin, LAE and profit loads.",
            ),
        ]
    )
    fraction = 1.0 if term_days is None else max(0, term_days) / annual_term_days
    premium = cents(
        max(
            annual_technical * fraction,
            int(rating["minimum_premium_cents"]) * fraction,
        )
    )
    steps.extend(
        [
            RatingStep(
                "term_fraction", fraction, "Annual rate prorated over the term."
            ),
            RatingStep(
                "technical_premium_cents",
                premium,
                "Rounded half-up after minimum premium.",
            ),
        ]
    )
    tax = cents(premium * float(rating["premium_tax_rate"]))
    steps.append(
        RatingStep("premium_tax_cents", tax, "Premium tax is collected for the state.")
    )

    warranted_controls = sorted(
        {
            control["id"]
            for peril in scorecard["perils"]
            if peril.get("id") in covered_perils
            for control in peril.get("controls", [])
            if control.get("status") in {"verified", "partial"}
        }
    )
    schedule = {
        "covered_perils": covered_perils,
        "excluded_perils": excluded_perils,
        "warranted_controls": warranted_controls,
        "agent_id": scorecard["agent_id"],
        "scorecard_sha256": expected_hash,
    }
    return RatingResult(
        "quoted",
        None,
        frequency,
        expected_severity,
        expected_loss_cost,
        premium,
        tax,
        premium + tax,
        steps,
        schedule,
    )
