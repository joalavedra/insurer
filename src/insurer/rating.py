"""Frequency-severity rating and its audit trail."""

from dataclasses import asdict, dataclass
from math import erf, exp, log, sqrt
from typing import Any

from insurer.money import cents
from insurer.products import Product, load_product


@dataclass(frozen=True)
class RatingStep:
    name: str
    value: float | int | str
    note: str


@dataclass(frozen=True)
class RatingResult:
    status: str
    reason: str | None
    frequency: float
    expected_covered_severity_cents: float
    expected_loss_cost_cents: float
    technical_premium_cents: int
    premium_tax_cents: int
    total_payable_cents: int
    steps: list[RatingStep]

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["steps"] = [asdict(step) for step in self.steps]
        return result


def normal_cdf(value: float) -> float:
    return 0.5 * (1 + erf(value / sqrt(2)))


def lognormal_lev(limit: float, median: float, sigma: float) -> float:
    if limit <= 0:
        return 0.0
    mu = log(median)
    first = exp(mu + sigma**2 / 2) * normal_cdf((log(limit) - mu - sigma**2) / sigma)
    second = limit * (1 - normal_cdf((log(limit) - mu) / sigma))
    return first + second


def tenure_bucket(months: int) -> str:
    if months < 3:
        return "lt_3m"
    if months <= 12:
        return "m3_12"
    return "gt_12m"


def profile_factors(profile: dict[str, Any]) -> dict[str, str]:
    return {
        "approval_threshold": str(profile["approval_threshold"]),
        "merchant_allowlist": "on" if profile["merchant_allowlist"] else "off",
        "rail": str(profile["rail"]),
        "tenure": tenure_bucket(int(profile["tenure_months"])),
    }


def rate_profile(
    profile: dict[str, Any],
    product: Product | None = None,
    *,
    term_days: int | None = None,
    annual_term_days: int = 365,
) -> RatingResult:
    product = product or load_product()
    rating = product.rating
    coverage = product.coverage
    steps: list[RatingStep] = []
    if not profile.get("kill_switch", False):
        steps.append(
            RatingStep(
                "underwriting", "declined", "W1 requires an operational kill-switch."
            )
        )
        return RatingResult(
            "declined", "W1: kill_switch must be true", 0, 0, 0, 0, 0, 0, steps
        )

    factors = profile_factors(profile)
    frequency = float(rating["base_annual_frequency"])
    steps.append(
        RatingStep("base_annual_frequency", frequency, "Claims per agent-year.")
    )
    for name, level in factors.items():
        factor = float(rating["frequency_factors"][name][level])
        steps.append(RatingStep(f"frequency_factor.{name}", factor, f"Level: {level}."))
        frequency *= factor
    steps.append(
        RatingStep(
            "annual_frequency",
            frequency,
            "Base frequency multiplied by all four factors.",
        )
    )

    severity = rating["severity"]
    median = float(severity["median_cents"])
    sigma = float(severity["sigma"])
    deductible = int(coverage["deductible_cents"])
    limit = int(coverage["per_claim_limit_cents"])
    cap = int(profile["monthly_spend_cap_cents"])
    expected_severity = max(
        0.0,
        lognormal_lev(min(deductible + limit, cap), median, sigma)
        - lognormal_lev(deductible, median, sigma),
    )
    steps.append(
        RatingStep(
            "expected_covered_severity_cents",
            expected_severity,
            (
                f"LEV(min(deductible + {limit}, {cap})) − LEV(deductible); "
                "purchase cap applies before the deductible."
            ),
        )
    )
    expected_loss_cost = frequency * expected_severity
    steps.append(
        RatingStep(
            "expected_loss_cost_cents",
            expected_loss_cost,
            "Annual frequency × expected severity.",
        )
    )

    loads = rating["loads"]
    total_load = sum(float(load) for load in loads.values())
    divisor = 1 - total_load
    annual_technical = expected_loss_cost / divisor
    steps.append(
        RatingStep("loads_total", total_load, f"Premium divisor: {divisor:.2f}.")
    )
    steps.append(
        RatingStep(
            "loads_divisor",
            divisor,
            "1 − sum of acquisition, admin, LAE and profit loads.",
        )
    )
    fraction = 1.0 if term_days is None else max(0, term_days) / annual_term_days
    premium = cents(
        max(
            annual_technical * fraction, int(rating["minimum_premium_cents"]) * fraction
        )
    )
    steps.append(
        RatingStep("term_fraction", fraction, "Annual rate prorated over the term.")
    )
    steps.append(
        RatingStep(
            "technical_premium_cents", premium, "Rounded half-up after minimum premium."
        )
    )
    tax = cents(premium * float(rating["premium_tax_rate"]))
    steps.append(
        RatingStep("premium_tax_cents", tax, "Premium tax is collected for the state.")
    )
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
    )
