import copy
import itertools
import json
from datetime import date

import pytest
import yaml

import insurer.simulate as simulate_module
from insurer.capital import _truth_frequency
from insurer.claims import ClaimsService
from insurer.cli import main
from insurer.policies import PolicyService
from insurer.products import load_product
from insurer.rating import profile_factors
from insurer.recalibrate import (
    Cell,
    fit,
    load_experience,
    recalibrate,
    write_candidate_product,
)
from insurer.simulate import simulate_book
from insurer.storage import connect


def _priced_frequency(product, factors):
    frequency = float(product.rating["base_annual_frequency"])
    for name, level in factors.items():
        frequency *= float(product.rating["frequency_factors"][name][level])
    return frequency


def _cell(product, factors, exposure, claims):
    return Cell(
        factors=factors,
        exposure_years=exposure,
        claims=claims,
        priced_frequency=_priced_frequency(product, factors),
    )


def _factor_row(proposal, factor, level):
    return next(
        row
        for row in proposal["factors"]
        if row["factor"] == factor and row["level"] == level
    )


def _profile_cell(cells, profile):
    factors = profile_factors(profile)
    return next(cell for cell in cells if cell.factors == factors)


def _bind(connection, profile, start="2027-01-01"):
    policies = PolicyService(connection)
    quote = policies.quote(profile, start)
    return policies, policies.bind(quote["quote_id"])


def test_fit_recovers_synthetic_factor_corrections_and_overall_ratio():
    product = load_product()
    factor_levels = {
        name: [str(level) for level in configured]
        for name, configured in product.rating["frequency_factors"].items()
    }
    base_levels = {name: levels[0] for name, levels in factor_levels.items()}
    corrections = {
        "approval_threshold": {"eur_200": 1.5, "eur_50": 0.7},
        "merchant_allowlist": {"on": 0.8},
        "rail": {"x402": 1.0},
        "tenure": {"m3_12": 0.9, "gt_12m": 1.25},
    }
    overall_ratio = 1.2
    cells = []
    for values in itertools.product(*(factor_levels[name] for name in factor_levels)):
        factors = dict(zip(factor_levels, values, strict=True))
        priced = _priced_frequency(product, factors)
        correction = overall_ratio
        for name, level in factors.items():
            if level != base_levels[name]:
                correction *= corrections.get(name, {}).get(level, 1.0)
        exposure = 10_000.0
        cells.append(
            Cell(
                factors=factors,
                exposure_years=exposure,
                claims=exposure * priced * correction,
                priced_frequency=priced,
            )
        )

    proposal = fit(cells, product, full_credibility_claims=1, max_change=1.0)
    assert proposal["overall_frequency_ratio"] == pytest.approx(overall_ratio, abs=1e-6)
    for factor, levels in factor_levels.items():
        assert _factor_row(proposal, factor, levels[0])["base_level"]
    for row in proposal["factors"]:
        if row["base_level"]:
            assert row["proposed"] == pytest.approx(row["current"])
        else:
            expected = corrections[row["factor"]][row["level"]]
            assert row["indicated"] / row["current"] == pytest.approx(
                expected, abs=1e-6
            )


def test_fit_applies_credibility_and_rate_change_cap():
    product = load_product()
    base = {
        name: str(next(iter(values)))
        for name, values in product.rating["frequency_factors"].items()
    }
    approval_base = "eur_200"
    cells = []
    for level, exposure, claims in [
        (approval_base, 200.0, 1_000),
        ("none", 100.0, 0),
        ("eur_50", 100.0, 25),
    ]:
        factors = dict(base)
        factors["approval_threshold"] = level
        cells.append(_cell(product, factors, exposure, claims))
    rail_factors = dict(base)
    rail_factors["approval_threshold"] = approval_base
    rail_factors["rail"] = "x402"
    cells.append(_cell(product, rail_factors, 100.0, 10_000))

    proposal = fit(cells, product, full_credibility_claims=100, max_change=0.25)
    zero_claim_level = _factor_row(proposal, "approval_threshold", "none")
    quarter_credibility_level = _factor_row(proposal, "approval_threshold", "eur_50")
    zero_exposure_level = _factor_row(proposal, "merchant_allowlist", "on")
    capped_level = _factor_row(proposal, "rail", "x402")
    assert zero_claim_level["claims"] == 0
    assert zero_claim_level["credibility"] == 0
    assert zero_claim_level["proposed"] == zero_claim_level["current"]
    assert quarter_credibility_level["claims"] == 25
    assert quarter_credibility_level["credibility"] == 0.5
    assert zero_exposure_level["exposure_years"] == 0
    assert zero_exposure_level["credibility"] == 0
    assert zero_exposure_level["proposed"] == zero_exposure_level["current"]
    assert capped_level["indicated"] > capped_level["current"] * 1.25
    assert capped_level["proposed"] == round(capped_level["current"] * 1.25, 2)
    downward_cells = [
        Cell(
            factors=cell.factors,
            exposure_years=cell.exposure_years,
            claims=(
                1 if cell.factors["approval_threshold"] == "eur_50" else cell.claims
            ),
            priced_frequency=cell.priced_frequency,
        )
        for cell in cells
    ]
    downward_proposal = fit(
        downward_cells, product, full_credibility_claims=1, max_change=0.25
    )
    downward_level = _factor_row(downward_proposal, "approval_threshold", "eur_50")
    assert downward_level["indicated"] < downward_level["current"] * 0.75
    assert downward_level["proposed"] == round(downward_level["current"] * 0.75, 2)


def test_load_experience_splits_endorsement_cancellation_and_cutoff(profile):
    product = load_product()
    connection = connect()
    old_profile = copy.deepcopy(profile)
    old_profile.update(
        approval_threshold="eur_200",
        merchant_allowlist=True,
        rail="card",
        tenure_months=6,
    )
    policies, endorsed = _bind(connection, old_profile)
    new_profile = dict(old_profile, approval_threshold="none")
    policies.endorse(
        endorsed["policy_id"], {"approval_threshold": "none"}, "2027-07-01"
    )

    claims = ClaimsService(connection)
    for loss_date in ("2027-06-30", "2027-07-01"):
        claims.file_claim(
            endorsed["policy_id"],
            "unsupported_cause",
            loss_date,
            loss_date,
            f"tx-{loss_date}",
            5_000,
            [],
        )
    claims.file_claim(
        endorsed["policy_id"],
        "unsupported_cause",
        "2027-08-01",
        "2027-08-01",
        "tx-fraud",
        5_000,
        [],
        fraud_truth=True,
    )
    claims.file_claim(
        endorsed["policy_id"],
        "unsupported_cause",
        "2027-12-31",
        "2028-01-02",
        "tx-late-notice",
        5_000,
        [],
    )
    claims.file_claim(
        endorsed["policy_id"],
        "unsupported_cause",
        "2028-01-01",
        "2028-01-02",
        "tx-after-cutoff",
        5_000,
        [],
    )

    cancelled_profile = copy.deepcopy(profile)
    cancelled_profile.update(
        approval_threshold="eur_50",
        merchant_allowlist=False,
        rail="x402",
        tenure_months=15,
    )
    cancel_policies, cancelled = _bind(connection, cancelled_profile)
    cancel_policies.cancel(cancelled["policy_id"], "2027-09-01")

    cutoff_profile = copy.deepcopy(profile)
    cutoff_profile.update(
        approval_threshold="eur_50",
        merchant_allowlist=True,
        rail="x402",
        tenure_months=6,
    )
    cutoff_policies, cutoff = _bind(connection, cutoff_profile)
    cutoff_policies.earn(cutoff["policy_id"], "2027-12-31")

    end_of_year = load_experience(connection, product, date(2027, 12, 31))
    assert load_experience(connection, product) == end_of_year
    assert _profile_cell(end_of_year, old_profile).exposure_years == pytest.approx(
        181 / 365
    )
    new_cell = _profile_cell(end_of_year, new_profile)
    assert new_cell.exposure_years == pytest.approx(184 / 365)
    assert new_cell.claims == 2
    assert _profile_cell(end_of_year, old_profile).claims == 1
    assert _profile_cell(
        end_of_year, cancelled_profile
    ).exposure_years == pytest.approx(243 / 365)

    cutoff_cells = load_experience(connection, product, date(2027, 3, 31))
    assert _profile_cell(cutoff_cells, cutoff_profile).exposure_years == pytest.approx(
        90 / 365
    )


def test_end_to_end_simulated_book_recalibrates_experience(tmp_path, monkeypatch):
    product = load_product()
    database = tmp_path / "experience.db"
    original_connect = simulate_module.connect

    def connect_fast_file(database_path):
        connection = original_connect(database_path)
        connection.execute("PRAGMA synchronous = OFF")
        return connection

    monkeypatch.setattr(simulate_module, "connect", connect_fast_file)
    simulate_book(
        agents=3_000,
        months=12,
        seed=1,
        years=1,
        database=str(database),
    )
    proposal = recalibrate(database, product, full_credibility_claims=1, max_change=1.0)

    assert 1.8 < _factor_row(proposal, "approval_threshold", "none")["indicated"] < 2.3
    assert _factor_row(proposal, "tenure", "lt_3m")["indicated"] > 1.3
    for factor in ("rail", "merchant_allowlist"):
        for row in proposal["factors"]:
            if row["factor"] == factor:
                assert row["ci95"][0] <= row["current"] <= row["ci95"][1]


def test_candidate_product_preserves_simulated_truth_and_runs(tmp_path, profile):
    product = load_product()
    factor_rows = []
    for factor, levels in product.rating["frequency_factors"].items():
        for level, current in levels.items():
            factor_rows.append(
                {
                    "factor": factor,
                    "level": str(level),
                    "proposed": round(float(current) * 1.05, 2),
                }
            )
    proposal = {
        "as_of": "2027-12-31",
        "method": "poisson_glm_offset_credibility",
        "full_credibility_claims": 1082,
        "max_change": 0.25,
        "factors": factor_rows,
    }
    candidate_path = tmp_path / "agent_spend_cover.v2.yaml"
    write_candidate_product(product, proposal, candidate_path)
    candidate = load_product(candidate_path)

    assert candidate.raw["version"] == 2
    assert candidate.raw["effective_from"] == date(2028, 1, 1)
    assert len(candidate.simulation["truth_frequency_factors"]) == 4
    for row in factor_rows:
        assert (
            candidate.rating["frequency_factors"][row["factor"]][row["level"]]
            == row["proposed"]
        )
    profiles = [
        profile,
        dict(profile, approval_threshold="none"),
        dict(profile, rail="x402", tenure_months=15),
    ]
    for sample in profiles:
        assert _truth_frequency(sample, candidate) == _truth_frequency(sample, product)

    result = simulate_book(product=candidate, agents=40, months=3, seed=42, years=10)
    assert result["params"]["product"] == candidate.product_key


def test_cli_recalibrate_writes_proposal_candidate_and_simulate_product(
    tmp_path, capsys
):
    product = load_product()
    product_path = tmp_path / "product.yaml"
    product_path.write_text(
        yaml.safe_dump(product.raw, sort_keys=False), encoding="utf-8"
    )
    database = tmp_path / "cli.db"
    simulate_book(
        agents=40,
        months=3,
        seed=42,
        years=10,
        database=str(database),
    )
    proposal_path = tmp_path / "proposal.json"
    candidate_path = tmp_path / "candidate.yaml"
    main(
        [
            "recalibrate",
            "--db",
            str(database),
            "--product",
            str(product_path),
            "--out",
            str(proposal_path),
            "--write-product",
            str(candidate_path),
        ]
    )
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert set(proposal) == {
        "product",
        "as_of",
        "method",
        "full_credibility_claims",
        "max_change",
        "data",
        "overall_frequency_ratio",
        "book_rate_change",
        "factors",
    }
    assert set(proposal["data"]) == {"exposure_years", "claims", "cells"}
    assert candidate_path.is_file()
    assert "factor" in capsys.readouterr().out

    simulation_path = tmp_path / "v2-results.json"
    main(
        [
            "simulate",
            "--product",
            str(candidate_path),
            "--agents",
            "10",
            "--months",
            "2",
            "--seed",
            "42",
            "--years",
            "5",
            "--out",
            str(simulation_path),
        ]
    )
    assert (
        json.loads(simulation_path.read_text(encoding="utf-8"))["params"]["product"]
        == "agent_spend_cover@2"
    )


def test_recalibrate_missing_database_is_argparse_error(tmp_path):
    missing = tmp_path / "does-not-exist.db"
    with pytest.raises(SystemExit) as error:
        main(["recalibrate", "--db", str(missing)])
    assert error.value.code == 2
    assert not missing.exists()
