import numpy as np

from insurer.capital import _covered_losses, cap_claims_by_year


def test_capital_caps_purchase_before_applying_deductible():
    losses = _covered_losses(
        np.array([7000, 4000]),
        deductible=1000,
        per_claim_limit=100_000,
        monthly_cap=5000,
    )
    assert losses.tolist() == [4000, 3000]


def test_aggregate_cap_applies_to_claim_that_crosses_limit():
    paid = cap_claims_by_year(
        np.array([2800, 500]),
        np.array([0, 0]),
        aggregate_limit=3000,
    )
    assert paid.tolist() == [2800, 200]


def test_aggregate_cap_resets_for_each_year():
    paid = cap_claims_by_year(
        np.array([2800, 500, 100]),
        np.array([0, 0, 1]),
        aggregate_limit=3000,
    )
    assert paid.tolist() == [2800, 200, 100]
