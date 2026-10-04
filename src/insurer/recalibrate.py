"""Experience-based frequency factor recalibration."""

from __future__ import annotations

import copy
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from insurer.policies import version_at
from insurer.products import Product, load_product
from insurer.rating import profile_factors


@dataclass(frozen=True)
class Cell:
    factors: dict[str, str]
    exposure_years: float
    claims: int | float
    priced_frequency: float


def _as_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _factor_names(product: Product) -> list[str]:
    return list(product.rating["frequency_factors"])


def _cell_factors(profile: dict[str, Any], factor_names: list[str]) -> dict[str, str]:
    mapped = profile_factors(profile)
    return {name: str(mapped[name]) for name in factor_names}


def _priced_frequency(factors: dict[str, str], product: Product) -> float:
    rating = product.rating
    frequency = float(rating["base_annual_frequency"])
    for factor_name, level in factors.items():
        frequency *= float(rating["frequency_factors"][factor_name][level])
    return frequency


def _resolve_as_of(connection: sqlite3.Connection, as_of: date | None) -> date:
    if as_of is not None:
        return _as_date(as_of)
    latest = connection.execute("SELECT MAX(earned_through) FROM policies").fetchone()[
        0
    ]
    if latest is None:
        raise ValueError("as_of is required when the book has no earned policies")
    return _as_date(latest)


def load_experience(
    connection: sqlite3.Connection, product: Product, as_of: date | None = None
) -> list[Cell]:
    """Aggregate earned exposure and reported ground-up claims into rating cells."""
    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row
    cutoff = _resolve_as_of(connection, as_of)
    exposure_end = cutoff + timedelta(days=1)
    factor_names = _factor_names(product)
    aggregated: dict[tuple[str, ...], dict[str, Any]] = {}
    versions_by_policy: dict[str, list[dict[str, Any]]] = {}

    def cell_for(factors: dict[str, str]) -> dict[str, Any]:
        key = tuple(factors[name] for name in factor_names)
        if key not in aggregated:
            aggregated[key] = {
                "factors": factors,
                "exposure_years": 0.0,
                "claims": 0,
                "priced_frequency": _priced_frequency(factors, product),
            }
        return aggregated[key]

    policies = connection.execute(
        "SELECT policy_id, start_date, end_date FROM policies"
    ).fetchall()
    policy_periods: dict[str, tuple[date, date]] = {}
    for policy in policies:
        policy_id = str(policy["policy_id"])
        policy_start = _as_date(policy["start_date"])
        policy_end = _as_date(policy["end_date"])
        policy_periods[policy_id] = (policy_start, policy_end)
        versions = [
            dict(row)
            for row in connection.execute(
                """
                SELECT version, effective_from, effective_to, profile_json, status
                FROM policy_versions
                WHERE policy_id = ?
                """,
                (policy_id,),
            ).fetchall()
        ]
        versions.sort(
            key=lambda row: (
                _as_date(row["effective_from"]),
                int(row["version"]),
            )
        )
        versions_by_policy[policy_id] = versions
        for index, version in enumerate(versions):
            if version["status"] == "cancelled":
                continue
            start = _as_date(version["effective_from"])
            ends = [policy_end, exposure_end]
            if index + 1 < len(versions):
                ends.append(_as_date(versions[index + 1]["effective_from"]))
            end = min(ends)
            if end <= start:
                continue
            factors = _cell_factors(json.loads(version["profile_json"]), factor_names)
            cell = cell_for(factors)
            cell["exposure_years"] += (end - start).days / 365.0

    claims = connection.execute(
        """
        SELECT policy_id, loss_date, notified_date
        FROM claims
        """
    ).fetchall()
    for claim in claims:
        loss_day = _as_date(claim["loss_date"])
        notified_day = _as_date(claim["notified_date"])
        if loss_day > cutoff or notified_day > cutoff:
            continue
        policy_id = str(claim["policy_id"])
        policy_period = policy_periods.get(policy_id)
        if policy_period is None:
            continue
        policy_start, policy_end = policy_period
        if not policy_start <= loss_day < policy_end:
            continue
        versions = versions_by_policy.get(policy_id, [])
        matched_version = version_at({"versions": versions}, loss_day)
        if matched_version is None or matched_version["status"] == "cancelled":
            continue
        factors = _cell_factors(
            json.loads(matched_version["profile_json"]), factor_names
        )
        cell_for(factors)["claims"] += 1

    level_order = {
        name: {str(level): index for index, level in enumerate(values)}
        for name, values in product.rating["frequency_factors"].items()
    }
    cells = [
        Cell(
            factors=entry["factors"],
            exposure_years=float(entry["exposure_years"]),
            claims=int(entry["claims"]),
            priced_frequency=float(entry["priced_frequency"]),
        )
        for entry in aggregated.values()
    ]
    cells.sort(
        key=lambda cell: tuple(
            level_order[name][cell.factors[name]] for name in factor_names
        )
    )
    return cells


def _round(value: float) -> float:
    return round(float(value), 6)


def _exp(value: float) -> float:
    return math.exp(min(700.0, max(-700.0, value)))


def fit(
    cells: list[Cell],
    product: Product,
    full_credibility_claims: int = 1082,
    max_change: float = 0.25,
) -> dict[str, Any]:
    """Fit multiplicative factor corrections and credibility-weight proposals."""
    if full_credibility_claims <= 0:
        raise ValueError("full_credibility_claims must be positive")
    if max_change < 0:
        raise ValueError("max_change must be non-negative")

    rating_factors = product.rating["frequency_factors"]
    factor_names = _factor_names(product)
    levels = {
        name: [str(level) for level in factor_values]
        for name, factor_values in rating_factors.items()
    }
    exposure_by_level = {
        name: {level: 0.0 for level in levels[name]} for name in factor_names
    }
    claims_by_level: dict[str, dict[str, float]] = {
        name: {level: 0.0 for level in levels[name]} for name in factor_names
    }
    for cell in cells:
        for name in factor_names:
            level = cell.factors[name]
            if level not in exposure_by_level[name]:
                raise ValueError(f"unknown level {level!r} for factor {name!r}")
            exposure_by_level[name][level] += cell.exposure_years
            claims_by_level[name][level] += float(cell.claims)

    base_levels = {
        name: max(
            levels[name],
            key=lambda level: (
                exposure_by_level[name][level],
                -levels[name].index(level),
            ),
        )
        for name in factor_names
    }
    dummy_columns: list[tuple[str, str]] = []
    for name in factor_names:
        for level in levels[name]:
            if level != base_levels[name] and exposure_by_level[name][level] > 0:
                dummy_columns.append((name, level))

    observed = [cell for cell in cells if cell.exposure_years > 0]
    if any(cell.priced_frequency <= 0 for cell in observed):
        raise ValueError("priced frequencies must be positive")

    coefficients = np.zeros(1 + len(dummy_columns), dtype=float)
    covariance = np.zeros((len(coefficients), len(coefficients)), dtype=float)
    if observed and any(float(cell.claims) > 0 for cell in observed):
        design = np.ones((len(observed), len(coefficients)), dtype=float)
        for column, (factor_name, level) in enumerate(dummy_columns, start=1):
            design[:, column] = [
                float(cell.factors[factor_name] == level) for cell in observed
            ]
        counts = np.array([float(cell.claims) for cell in observed], dtype=float)
        offsets = np.log(
            np.array(
                [cell.exposure_years * cell.priced_frequency for cell in observed],
                dtype=float,
            )
        )
        penalty = np.diag([0.0] + [1e-6] * len(dummy_columns))

        def penalized_log_likelihood(parameters: np.ndarray) -> float:
            linear_predictor = offsets + design @ parameters
            means = np.exp(np.clip(linear_predictor, -700.0, 700.0))
            return float(
                counts @ linear_predictor
                - np.sum(means)
                - 0.5 * parameters @ penalty @ parameters
            )

        for _ in range(100):
            eta = offsets + design @ coefficients
            means = np.exp(np.clip(eta, -700.0, 700.0))
            information = design.T @ (means[:, None] * design) + penalty
            score = design.T @ (counts - means) - penalty @ coefficients
            delta = np.linalg.solve(information, score)
            current_likelihood = penalized_log_likelihood(coefficients)
            step = 1.0
            candidate = coefficients + delta
            while (
                penalized_log_likelihood(candidate) < current_likelihood
                and step > 1e-12
            ):
                step *= 0.5
                candidate = coefficients + step * delta
            if penalized_log_likelihood(candidate) < current_likelihood:
                break
            coefficients[:] = candidate
            if float(np.max(np.abs(step * delta))) < 1e-10:
                break
        eta = offsets + design @ coefficients
        means = np.exp(np.clip(eta, -700.0, 700.0))
        information = design.T @ (means[:, None] * design) + penalty
        covariance = np.linalg.inv(information)

    coefficient_indices = {
        descriptor: index for index, descriptor in enumerate(dummy_columns, start=1)
    }
    proposed_values: dict[str, dict[str, float]] = {}
    factor_rows: list[dict[str, Any]] = []
    for factor_name in factor_names:
        proposed_values[factor_name] = {}
        for level in levels[factor_name]:
            current = float(rating_factors[factor_name][level])
            is_base = level == base_levels[factor_name]
            index = coefficient_indices.get((factor_name, level))
            has_exposure = exposure_by_level[factor_name][level] > 0
            if index is not None and has_exposure:
                beta = float(coefficients[index])
                standard_error = math.sqrt(max(0.0, float(covariance[index, index])))
            else:
                beta = 0.0
                standard_error = 0.0
            credibility = (
                min(
                    1.0,
                    math.sqrt(
                        max(0.0, claims_by_level[factor_name][level])
                        / full_credibility_claims
                    ),
                )
                if has_exposure
                else 0.0
            )
            indicated = current * _exp(beta)
            ci95 = [
                current * _exp(beta - 1.96 * standard_error),
                current * _exp(beta + 1.96 * standard_error),
            ]
            credibility_weighted = current * _exp(credibility * beta)
            lower = current * (1.0 - max_change)
            upper = current * (1.0 + max_change)
            proposed = (
                current if is_base else min(max(credibility_weighted, lower), upper)
            )
            proposed = round(proposed, 2)
            proposed_values[factor_name][level] = proposed
            factor_rows.append(
                {
                    "factor": factor_name,
                    "level": level,
                    "base_level": is_base,
                    "exposure_years": _round(exposure_by_level[factor_name][level]),
                    "claims": int(round(claims_by_level[factor_name][level])),
                    "current": _round(current),
                    "indicated": _round(indicated),
                    "ci95": [_round(ci95[0]), _round(ci95[1])],
                    "credibility": _round(credibility),
                    "proposed": proposed,
                    "change": _round(proposed / current - 1.0),
                }
            )

    total_exposure = sum(cell.exposure_years for cell in cells)
    if total_exposure:
        book_rate_change = (
            sum(
                cell.exposure_years
                * (
                    math.prod(
                        proposed_values[name][cell.factors[name]]
                        for name in factor_names
                    )
                    / math.prod(
                        float(rating_factors[name][cell.factors[name]])
                        for name in factor_names
                    )
                    - 1.0
                )
                for cell in cells
            )
            / total_exposure
        )
    else:
        book_rate_change = 0.0

    return {
        "overall_frequency_ratio": _round(_exp(float(coefficients[0]))),
        "book_rate_change": _round(book_rate_change),
        "factors": factor_rows,
    }


def recalibrate(
    db_path: str | Path,
    product: Product | None = None,
    as_of: date | None = None,
    full_credibility_claims: int = 1082,
    max_change: float = 0.25,
) -> dict[str, Any]:
    """Read a book database and return a credibility-weighted proposal."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"database file not found: {path}")
    selected_product = product or load_product()
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        cutoff = _resolve_as_of(connection, as_of)
        cells = load_experience(connection, selected_product, cutoff)
    finally:
        connection.close()

    fitted = fit(cells, selected_product, full_credibility_claims, max_change)
    return {
        "product": selected_product.product_key,
        "as_of": cutoff.isoformat(),
        "method": "poisson_glm_offset_credibility",
        "full_credibility_claims": full_credibility_claims,
        "max_change": _round(max_change),
        "data": {
            "exposure_years": _round(sum(cell.exposure_years for cell in cells)),
            "claims": sum(int(cell.claims) for cell in cells),
            "cells": len(cells),
        },
        "overall_frequency_ratio": fitted["overall_frequency_ratio"],
        "book_rate_change": fitted["book_rate_change"],
        "factors": fitted["factors"],
    }


def write_candidate_product(
    product: Product,
    proposal: dict[str, Any],
    path: str | Path,
    effective_from: str | date | None = None,
) -> None:
    """Write an unsigned next-version product candidate from a proposal."""
    raw = copy.deepcopy(product.raw)
    source_factors = product.rating["frequency_factors"]
    candidate_factors = raw["rating"]["frequency_factors"]
    proposed: dict[str, dict[str, float]] = {
        row["factor"]: {} for row in proposal["factors"]
    }
    for row in proposal["factors"]:
        proposed[row["factor"]][row["level"]] = float(row["proposed"])
    for factor_name, factor_levels in candidate_factors.items():
        for level in factor_levels:
            factor_levels[level] = proposed[factor_name][str(level)]

    truth_factors = raw.setdefault("simulation", {}).setdefault(
        "truth_frequency_factors", {}
    )
    for factor_name, factor_levels in source_factors.items():
        truth_factors.setdefault(factor_name, copy.deepcopy(factor_levels))

    raw["version"] = int(raw["version"]) + 1
    raw["effective_from"] = _as_date(
        effective_from
        if effective_from is not None
        else date.fromisoformat(proposal["as_of"]) + timedelta(days=1)
    )
    raw["recalibration"] = {
        "from": product.product_key,
        "data_as_of": proposal["as_of"],
        "method": proposal["method"],
        "full_credibility_claims": proposal["full_credibility_claims"],
        "max_change": proposal["max_change"],
    }
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as destination:
        yaml.safe_dump(raw, destination, sort_keys=False)
