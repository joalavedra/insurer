import math

import numpy as np

from insurer.rating import lognormal_lev, rate_profile


def test_lognormal_lev_matches_one_million_draw_monte_carlo(product, profile):
    rating = rate_profile(profile, product)
    draws = np.random.default_rng(501).lognormal(math.log(6000), 1.0, size=1_000_000)
    simulated = np.minimum(
        np.maximum(draws - product.coverage["deductible_cents"], 0),
        min(
            product.coverage["per_claim_limit_cents"],
            profile["monthly_spend_cap_cents"],
        ),
    ).mean()
    assert math.isclose(
        rating.expected_covered_severity_cents, float(simulated), rel_tol=0.01
    )


def test_rating_factor_product_and_audit_steps(product, profile):
    result = rate_profile(profile, product)
    expected_frequency = 0.4 * 1.0 * 0.8 * 1.0 * 1.0
    assert math.isclose(result.frequency, expected_frequency)
    assert result.steps[0].name == "base_annual_frequency"
    assert {step.name for step in result.steps} >= {
        "annual_frequency",
        "expected_covered_severity_cents",
        "expected_loss_cost_cents",
        "loads_divisor",
        "technical_premium_cents",
        "premium_tax_cents",
    }
    divisor = next(step.value for step in result.steps if step.name == "loads_divisor")
    assert divisor == 0.65
    assert result.technical_premium_cents >= 500
    assert (
        result.total_payable_cents
        == result.technical_premium_cents + result.premium_tax_cents
    )


def test_minimum_premium_and_w1_decline(product, profile):
    small_cap = profile | {"monthly_spend_cap_cents": 1000}
    result = rate_profile(small_cap, product)
    assert result.technical_premium_cents == 500
    declined = rate_profile(profile | {"kill_switch": False}, product)
    assert declined.status == "declined"
    assert declined.reason == "W1: kill_switch must be true"
    assert declined.steps


def test_limited_expected_value_is_zero_for_nonpositive_limit():
    assert lognormal_lev(0, 6000, 1) == 0
