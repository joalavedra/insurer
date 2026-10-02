"""Vectorised annual aggregate-loss simulation for a simple SCR proxy."""

from typing import Any

import numpy as np

from insurer.money import cents
from insurer.products import Product, load_product
from insurer.rating import profile_factors


def _truth_frequency(profile: dict[str, Any], product: Product) -> float:
    rating = product.rating
    simulation = product.simulation
    factors = profile_factors(profile)
    frequency = float(rating["base_annual_frequency"])
    for factor_name, level in factors.items():
        configured = simulation["truth_frequency_factors"].get(
            factor_name, rating["frequency_factors"][factor_name]
        )
        frequency *= float(configured[level])
    return frequency


def simulate_capital(
    profiles: list[dict[str, Any]],
    years: int = 1000,
    quota_share: float = 0.5,
    qs_commission: float = 0.30,
    seed: int = 42,
    product: Product | None = None,
) -> dict[str, Any]:
    product = product or load_product()
    if years <= 0:
        raise ValueError("years must be positive")
    if not 0 <= quota_share <= 1:
        raise ValueError("quota share must be between 0 and 1")
    rng = np.random.default_rng(seed)
    gross = np.zeros(years, dtype=np.int64)
    coverage = product.coverage
    severity = product.rating["severity"]
    mu = float(np.log(float(severity["median_cents"])))
    sigma = float(severity["sigma"])
    aggregate_limit = int(coverage["annual_aggregate_limit_cents"])
    deductible = int(coverage["deductible_cents"])
    per_claim_limit = int(coverage["per_claim_limit_cents"])
    for profile in profiles:
        claim_counts = rng.poisson(_truth_frequency(profile, product), size=years)
        number = int(claim_counts.sum())
        if number == 0:
            continue
        year_indices = np.repeat(np.arange(years), claim_counts)
        losses = np.minimum(
            np.maximum(rng.lognormal(mu, sigma, size=number) - deductible, 0),
            min(per_claim_limit, int(profile["monthly_spend_cap_cents"])),
        ).astype(np.int64)
        order = np.argsort(year_indices, kind="stable")
        year_indices = year_indices[order]
        losses = losses[order]
        cumulative = np.cumsum(losses)
        starts = np.r_[0, np.flatnonzero(np.diff(year_indices)) + 1]
        offsets = cumulative[starts] - losses[starts]
        offsets_by_claim = np.repeat(offsets, np.diff(np.r_[starts, number]))
        capped = np.minimum(
            losses,
            np.maximum(aggregate_limit - (cumulative - offsets_by_claim), 0),
        )
        np.add.at(gross, year_indices, capped)
    net = gross * (1 - quota_share)
    expected = cents(float(gross.mean()))
    p95 = cents(float(np.quantile(gross, 0.95)))
    p995 = cents(float(np.quantile(gross, 0.995)))
    net_expected = cents(float(net.mean()))
    net_p995 = cents(float(np.quantile(net, 0.995)))
    return {
        "years": years,
        "expected_loss_cents": expected,
        "p95_loss_cents": p95,
        "p995_loss_cents": p995,
        "scr_proxy_cents": p995 - expected,
        "net_of_quota_share": {
            "cession": quota_share,
            "commission": qs_commission,
            "expected_loss_cents": net_expected,
            "p995_loss_cents": net_p995,
            "scr_proxy_cents": net_p995 - net_expected,
        },
    }
