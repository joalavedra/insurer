"""Accident-month reported-incurred triangles and chain-ladder estimates."""

from calendar import monthrange
from datetime import date
from typing import Any, cast

import chainladder as cl
import numpy as np
import pandas as pd


def _month_index(value: str) -> int:
    year, month = (int(part) for part in value[:7].split("-"))
    return year * 12 + month - 1


def _month_label(index: int) -> str:
    year, month = divmod(index, 12)
    return f"{year:04d}-{month + 1:02d}"


def _month_end(label: str) -> str:
    year, month = (int(part) for part in label.split("-"))
    return f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}"


def build_triangle(claims: list[dict[str, Any]], as_of: str | date) -> dict[str, Any]:
    as_of_day = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    reported = [
        claim
        for claim in claims
        if date.fromisoformat(str(claim["notified_date"])[:10]) <= as_of_day
    ]
    origin_indices = sorted(
        {_month_index(str(claim["loss_date"])) for claim in reported}
    )
    max_development = max(
        (
            _month_index(str(claim["notified_date"]))
            - _month_index(str(claim["loss_date"]))
            for claim in reported
        ),
        default=0,
    )
    month_index_as_of = _month_index(as_of_day.isoformat())
    max_development = max(
        max_development,
        max((month_index_as_of - origin for origin in origin_indices), default=0),
    )
    origins = [_month_label(origin) for origin in origin_indices]
    development = list(range(max_development + 1))
    cumulative: list[list[int]] = []
    observed_ages: list[int] = []
    for origin in origin_indices:
        origin_claims = [
            claim
            for claim in reported
            if _month_index(str(claim["loss_date"])) == origin
        ]
        observed_age = min(max_development, month_index_as_of - origin)
        observed_ages.append(observed_age)
        row: list[int] = []
        for dev in range(observed_age + 1):
            row.append(
                sum(
                    int(
                        claim.get(
                            "incurred_cents",
                            int(claim.get("paid_cents", 0))
                            + int(claim.get("reserve_cents", 0)),
                        )
                    )
                    for claim in origin_claims
                    if _month_index(str(claim["notified_date"])) - origin <= dev
                )
            )
        cumulative.append(row)
    if not origin_indices:
        return {
            "origins": [],
            "dev_months": [],
            "values_cents": [],
            "ldf": [1.0],
            "cdf": [1.0],
            "ultimate_cents": [],
            "ibnr_cents": [],
            "note": "No reported claim origins are available.",
        }
    if len(origins) < 3:
        ultimate = [row[-1] if row else 0 for row in cumulative]
        return {
            "origins": origins,
            "dev_months": development,
            "values_cents": cumulative,
            "ldf": [1.0],
            "cdf": [1.0],
            "ultimate_cents": ultimate,
            "ibnr_cents": [0 for _ in origins],
            "note": "Fewer than 3 accident-month origins; IBNR set to 0.",
        }

    records: list[dict[str, Any]] = []
    for row_index, origin_label in enumerate(origins):
        origin_date = f"{origin_label}-01"
        for dev, value in enumerate(
            cumulative[row_index][: observed_ages[row_index] + 1]
        ):
            development_month = _month_label(_month_index(origin_label) + dev)
            records.append(
                {
                    "origin": origin_date,
                    "development": _month_end(development_month),
                    "incurred": value,
                }
            )
    triangle_constructor = cast(Any, cl.Triangle)
    triangle = triangle_constructor(
        pd.DataFrame.from_records(records),
        origin="origin",
        development="development",
        columns=["incurred"],
        cumulative=True,
    )
    developed = cl.Development().fit_transform(triangle)
    model = cl.Chainladder().fit(developed)
    ldf = np.asarray(developed.ldf_.values, dtype=float).reshape(-1).tolist()
    cdf = np.asarray(model.cdf_.values, dtype=float).reshape(-1).tolist()
    ultimate_values = np.asarray(model.ultimate_.values, dtype=float).reshape(-1)
    latest = np.asarray(
        [
            cumulative[index][observed_age]
            for index, observed_age in enumerate(observed_ages)
        ],
        dtype=float,
    )
    ibnr = np.maximum(ultimate_values - latest, 0)
    return {
        "origins": origins,
        "dev_months": development,
        "values_cents": cumulative,
        "ldf": [float(value) for value in ldf],
        "cdf": [float(value) for value in cdf],
        "ultimate_cents": [int(round(float(value))) for value in ultimate_values],
        "ibnr_cents": [int(round(float(value))) for value in ibnr],
        "note": None,
    }
