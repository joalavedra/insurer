import numpy as np

from insurer.capital import cap_claims_by_year


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
