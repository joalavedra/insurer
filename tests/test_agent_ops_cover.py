from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from insurer.agentrisk.controls import verify_controls
from insurer.agentrisk.exam import default_battery_path, load_battery
from insurer.agentrisk.incidents import Incident, find_incidents
from insurer.agentrisk.manifest import parse_manifest
from insurer.agentrisk.pricing import rate_scorecard
from insurer.agentrisk.scorecard import scorecard_sha256
from insurer.agentrisk.traces import ToolCall, TraceData
from insurer.claims import ClaimsService
from insurer.cli import _agent_scorecard_profile, main
from insurer.policies import PolicyService
from insurer.products import load_agent_ops_product, load_product
from insurer.rating import rate_for_product, rate_profile
from insurer.storage import connect


def _scorecard(
    grades: dict[str, str] | None = None,
    *,
    autonomy: str = "autonomous",
    exam_levels: dict[str, str] | None = None,
    battery_sha256: str | None = None,
    sessions_per_month: int = 1000,
) -> dict[str, Any]:
    grades = {"P1": "A"} if grades is None else grades
    exam_levels = {} if exam_levels is None else exam_levels
    battery = load_battery()
    result: dict[str, Any] = {
        "agent_id": "agent-ops-test",
        "framework": "test",
        "model": "test-model",
        "autonomy": autonomy,
        "agent_grade": None,
        "perils": [
            {
                "id": peril_id,
                "name": peril_name,
                "status": "exposed" if peril_id in grades else "not_exposed",
                "exam_level": exam_levels.get(peril_id, "pass"),
                "control_level": "verified",
                "grade": grades.get(peril_id),
                "controls": [
                    {
                        "id": f"{peril_id.lower()}-control",
                        "status": "verified",
                    }
                ]
                if peril_id in grades
                else [],
                "exposure": {"sessions_per_month": sessions_per_month},
            }
            for peril_id, peril_name in (
                ("P1", "unauthorized_action"),
                ("P2", "prompt_injection"),
                ("P3", "data_leakage"),
                ("P4", "misrepresentation"),
            )
        ],
        "self_reported": {"note": "no credit", "perils": {}},
        "evidence": {
            "inputs": {
                "battery": {
                    "path": "agent_exam_v2.yaml",
                    "sha256": battery_sha256 or battery.sha256,
                }
            }
        },
    }
    result["evidence"]["scorecard_sha256"] = scorecard_sha256(result)
    return result


def _profile(
    scorecard: dict[str, Any] | None = None, sessions_per_month: int = 1000
) -> dict[str, Any]:
    return {
        "scorecard": scorecard or _scorecard(),
        "sessions_per_month": sessions_per_month,
    }


def test_scorecard_hash_tamper_declines() -> None:
    scorecard = _scorecard()
    tampered = deepcopy(scorecard)
    tampered["agent_id"] = "tampered"

    result = rate_scorecard(_profile(tampered), load_agent_ops_product())

    assert result.status == "declined"
    assert result.reason == "scorecard hash mismatch"


def test_wrong_battery_declines() -> None:
    result = rate_scorecard(
        _profile(_scorecard(battery_sha256="0" * 64)),
        load_agent_ops_product(),
    )

    assert result.status == "declined"
    assert result.reason == "scorecard not produced with agent_exam_v2"


def test_untested_exposed_peril_declines() -> None:
    result = rate_scorecard(
        _profile(_scorecard(exam_levels={"P1": "untested"})),
        load_agent_ops_product(),
    )

    assert result.status == "declined"
    assert result.reason == "P1: insurer exam required"


def test_all_e_perils_decline() -> None:
    result = rate_scorecard(
        _profile(_scorecard({"P1": "E", "P2": "E"})),
        load_agent_ops_product(),
    )

    assert result.status == "declined"
    assert result.reason == "no insurable perils"


def test_no_exposed_perils_decline() -> None:
    result = rate_scorecard(
        _profile(_scorecard({})),
        load_agent_ops_product(),
    )

    assert result.status == "declined"
    assert result.reason == "no insurable perils"


def test_e_grade_is_excluded_and_reduces_premium() -> None:
    product = load_agent_ops_product()
    excluded = rate_scorecard(
        _profile(
            _scorecard({"P1": "E", "P2": "D"}, sessions_per_month=100_000),
            100_000,
        ),
        product,
    )
    covered = rate_scorecard(
        _profile(
            _scorecard({"P1": "D", "P2": "D"}, sessions_per_month=100_000),
            100_000,
        ),
        product,
    )

    assert excluded.schedule is not None
    assert excluded.schedule["excluded_perils"] == ["P1"]
    assert excluded.schedule["covered_perils"] == ["P2"]
    assert excluded.total_payable_cents < covered.total_payable_cents


def test_grade_is_monotonic_for_a_single_peril() -> None:
    product = load_agent_ops_product()
    premiums = [
        rate_scorecard(
            _profile(_scorecard({"P1": grade}, sessions_per_month=100_000), 100_000),
            product,
        ).technical_premium_cents
        for grade in ("A", "B", "C", "D")
    ]

    assert premiums[0] < premiums[1] < premiums[2] < premiums[3]


def test_human_in_loop_reduces_premium() -> None:
    product = load_agent_ops_product()
    autonomous = rate_scorecard(
        _profile(
            _scorecard({"P1": "C"}, autonomy="autonomous", sessions_per_month=100_000),
            100_000,
        ),
        product,
    )
    human_in_loop = rate_scorecard(
        _profile(
            _scorecard(
                {"P1": "C"},
                autonomy="human_in_loop",
                sessions_per_month=100_000,
            ),
            100_000,
        ),
        product,
    )

    assert human_in_loop.total_payable_cents < autonomous.total_payable_cents


def test_p1_frequency_hand_check_and_minimum_premium() -> None:
    result = rate_scorecard(
        _profile(_scorecard({"P1": "A"}), 1000),
        load_agent_ops_product(),
    )

    assert result.frequency == 0.012
    assert result.technical_premium_cents >= 25_000
    assert result.steps[0].name == "scorecard_integrity"
    assert result.steps[1].name == "battery"
    assert result.schedule is not None
    assert result.schedule["covered_perils"] == ["P1"]
    assert result.schedule["warranted_controls"] == ["p1-control"]


def test_spend_cover_dispatch_is_identical_to_rate_profile(profile) -> None:
    product = load_product()

    assert rate_for_product(profile, product) == rate_profile(profile, product)


def test_agent_ops_product_loads() -> None:
    product = load_agent_ops_product()
    assert product.product_id == "agent_ops_cover"
    assert str(product.raw["effective_from"]) == "2026-10-01"
    assert load_battery(default_battery_path()).name == "agent_exam_v2"


def test_scorecard_requires_sessions_per_month() -> None:
    with pytest.raises(ValueError, match="sessions_per_month"):
        rate_scorecard(
            {"scorecard": _scorecard()},
            load_agent_ops_product(),
        )


def _bound_policy(
    grades: dict[str, str] | None = None,
) -> tuple[Any, dict[str, Any], ClaimsService]:
    product = load_agent_ops_product()
    connection = connect()
    policies = PolicyService(connection, product)
    quote = policies.quote(_profile(_scorecard(grades)), "2027-01-01")
    policy = policies.bind(quote["quote_id"])
    return connection, policy, ClaimsService(connection, product)


def _claim_evidence(
    peril: str = "P1",
    amount: int | None = 200_000,
    control_ids: list[str] | None = None,
    span_id: str = "b" * 16,
) -> list[dict[str, Any]]:
    call_time = datetime(2027, 2, 10, 12, tzinfo=timezone.utc)
    control_id = f"{peril.lower()}-control"
    return [
        {
            "type": "incident",
            "peril": peril,
            "control_id": control_id,
            "trace_id": "a" * 32,
            "span_id": span_id,
            "tool": "issue_refund" if peril == "P1" else "export_customer_data",
            "ts": call_time.isoformat(),
            "evidenced_amount_cents": amount,
        },
        {
            "type": "manifest_controls",
            "ts": call_time.isoformat(),
            "control_ids": ([control_id] if control_ids is None else control_ids),
        },
    ]


def _file_test_claim(
    claims: ClaimsService,
    policy: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    cause: str = "unauthorized_action",
    loss_date: str = "2027-02-10",
    notified_date: str = "2027-02-10",
    purchase_id: str = f"{'a' * 32}:{'b' * 16}",
    claimed_cents: int = 200_000,
    **kwargs: Any,
) -> dict[str, Any]:
    return claims.file_claim(
        policy["policy_id"],
        cause,
        loss_date,
        notified_date,
        purchase_id,
        claimed_cents,
        evidence,
        **kwargs,
    )


def test_p1_incident_claim_pays_net_of_deductible_and_balances_ledger() -> None:
    connection, policy, claims = _bound_policy()

    result = _file_test_claim(claims, policy, _claim_evidence())

    assert result["decision"]["decision"] == "approve"
    assert result["paid_cents"] == 175_000
    assert claims.ledger.entries_balanced()
    assert sum(row["debit_cents"] for row in claims.ledger.trial_balance()) == sum(
        row["credit_cents"] for row in claims.ledger.trial_balance()
    )
    connection.close()


def test_claim_above_evidenced_amount_is_referred() -> None:
    _, policy, claims = _bound_policy()

    result = _file_test_claim(
        claims,
        policy,
        _claim_evidence(),
        claimed_cents=200_001,
    )

    assert result["decision"]["decision"] == "refer"
    assert "exceeds" in result["decision"]["reason"]


def test_unobservable_p3_claim_is_referred() -> None:
    _, policy, claims = _bound_policy({"P3": "A"})

    result = _file_test_claim(
        claims,
        policy,
        _claim_evidence("P3", None),
        cause="data_leakage",
    )

    assert result["decision"]["decision"] == "refer"
    assert result["decision"]["reason"] == "loss amount needs human verification"


def test_excluded_peril_is_denied_with_e1() -> None:
    _, policy, claims = _bound_policy({"P1": "E", "P2": "A"})

    result = _file_test_claim(
        claims,
        policy,
        _claim_evidence("P1", control_ids=["p2-control"]),
    )

    assert result["decision"]["decision"] == "deny"
    assert result["decision"]["clause_ids"] == ["E1"]


def test_missing_warranted_control_is_denied_with_w1() -> None:
    _, policy, claims = _bound_policy()

    result = _file_test_claim(
        claims,
        policy,
        _claim_evidence(control_ids=[]),
    )

    assert result["decision"]["decision"] == "deny"
    assert result["decision"]["clause_ids"] == ["W1"]
    assert "p1-control" in result["decision"]["reason"]


def test_late_notice_is_denied_with_e3() -> None:
    _, policy, claims = _bound_policy()

    result = _file_test_claim(
        claims,
        policy,
        _claim_evidence(),
        notified_date="2027-03-13",
    )

    assert result["decision"]["decision"] == "deny"
    assert result["decision"]["clause_ids"] == ["E3"]


def test_out_of_force_claim_is_denied() -> None:
    _, policy, claims = _bound_policy()

    result = _file_test_claim(
        claims,
        policy,
        _claim_evidence(),
        loss_date="2028-01-01",
        notified_date="2028-01-01",
    )

    assert result["decision"]["decision"] == "deny"
    assert "not in force" in result["decision"]["reason"]


def test_duplicate_incident_is_referred() -> None:
    _, policy, claims = _bound_policy()
    evidence = _claim_evidence()
    _file_test_claim(claims, policy, evidence)

    result = _file_test_claim(claims, policy, evidence)

    assert result["decision"]["decision"] == "refer"
    assert result["decision"]["reason"] == "The incident has already been claimed."


def test_human_approved_action_is_denied_with_e4() -> None:
    _, policy, claims = _bound_policy()
    evidence = _claim_evidence()
    evidence.append({"type": "human_approval", "span_id": "b" * 16})

    result = _file_test_claim(claims, policy, evidence)

    assert result["decision"]["decision"] == "deny"
    assert result["decision"]["clause_ids"] == ["E4"]


def test_agent_ops_claims_reject_gemini_adjuster() -> None:
    _, policy, claims = _bound_policy()

    with pytest.raises(
        ValueError, match="gemini adjuster not available for agent_ops_cover"
    ):
        _file_test_claim(
            claims,
            policy,
            _claim_evidence(),
            use_gemini=True,
        )


def test_incidents_match_control_violation_counts() -> None:
    manifest = parse_manifest(
        {
            "agent_id": "weak-refund",
            "framework": "test",
            "owner": "risk",
            "model": "test",
            "autonomy": "autonomous",
            "data_classes": ["payment"],
            "volume": {"sessions_per_month": 1000},
            "tools": [
                {
                    "name": "issue_refund",
                    "effect": "money_out",
                    "amount_arg": "amount_eur",
                },
                {"name": "request_approval", "effect": "approval"},
            ],
            "controls": [
                {
                    "id": "refund-approval",
                    "type": "approval_threshold",
                    "tool": "issue_refund",
                    "threshold_eur": 100,
                    "approval_tool": "request_approval",
                },
                {
                    "id": "refund-cap",
                    "type": "amount_limit",
                    "tool": "issue_refund",
                    "max_eur": 500,
                },
            ],
        }
    )
    timestamp = int(datetime(2027, 2, 10, tzinfo=timezone.utc).timestamp() * 1e9)
    trace_data = TraceData(
        (
            ToolCall(
                trace_id="a" * 32,
                span_id="b" * 16,
                parent_span_id=None,
                start_ns=timestamp,
                agent="weak-refund",
                tool="issue_refund",
                args={"order_id": "R-10", "amount_eur": 450},
                result="refund issued",
                error=False,
                convention="gen_ai",
            ),
        ),
        (),
    )

    incidents = find_incidents(manifest, trace_data)
    violations = sum(
        result.violations for result in verify_controls(manifest, trace_data)
    )

    assert len(incidents) == violations == 1
    assert incidents[0].evidenced_amount_cents == 45_000
    assert incidents[0].loss_date == date(2027, 2, 10)


def test_agent_quote_cli_binds_policy(tmp_path: Path, capsys: Any) -> None:
    scorecard_path = tmp_path / "scorecard.json"
    scorecard_path.write_text(json.dumps(_scorecard()), encoding="utf-8")
    database_path = tmp_path / "agent-ops.sqlite"

    main(
        [
            "agent-quote",
            "--db",
            str(database_path),
            "--scorecard",
            str(scorecard_path),
            "--start-date",
            "2027-01-01",
            "--bind",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert result["quote"]["status"] == "quoted"
    assert result["quote"]["rating"]["schedule"]["covered_perils"] == ["P1"]
    assert result["policy"]["policy_id"] == "P-00000001"


def test_agent_quote_profile_requires_equal_peril_exposures(tmp_path: Path) -> None:
    path = tmp_path / "scorecard.json"
    scorecard = _scorecard()
    scorecard["perils"][1]["exposure"].pop("sessions_per_month")
    path.write_text(json.dumps(scorecard), encoding="utf-8")

    with pytest.raises(ValueError, match="missing per-peril sessions_per_month"):
        _agent_scorecard_profile(path)

    scorecard["perils"][1]["exposure"]["sessions_per_month"] = 2000
    path.write_text(json.dumps(scorecard), encoding="utf-8")
    with pytest.raises(ValueError, match="inconsistent sessions_per_month"):
        _agent_scorecard_profile(path)


def test_agent_claims_dry_run_lists_without_database_writes(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    import insurer.cli as cli

    manifest = parse_manifest(
        {
            "agent_id": "weak-refund",
            "framework": "test",
            "owner": "risk",
            "model": "test",
            "autonomy": "autonomous",
            "data_classes": ["payment"],
            "volume": {"sessions_per_month": 1000},
            "tools": [
                {
                    "name": "issue_refund",
                    "effect": "money_out",
                    "amount_arg": "amount_eur",
                },
                {"name": "request_approval", "effect": "approval"},
            ],
            "controls": [
                {
                    "id": "p1-control",
                    "type": "approval_threshold",
                    "tool": "issue_refund",
                    "threshold_eur": 100,
                    "approval_tool": "request_approval",
                }
            ],
        }
    )
    timestamp = int(datetime(2027, 2, 10, tzinfo=timezone.utc).timestamp() * 1e9)
    trace_data = TraceData(
        (
            ToolCall(
                trace_id="a" * 32,
                span_id="b" * 16,
                parent_span_id=None,
                start_ns=timestamp,
                agent="weak-refund",
                tool="issue_refund",
                args={"order_id": "R-10", "amount_eur": 450},
                result="refund issued",
                error=False,
                convention="gen_ai",
            ),
        ),
        (),
    )
    incident = Incident(
        "P1",
        "p1-control",
        "a" * 32,
        "b" * 16,
        "issue_refund",
        date(2027, 2, 10),
        45_000,
    )
    monkeypatch.setattr(cli, "load_manifest", lambda _: manifest)
    monkeypatch.setattr(cli, "load_trace_files", lambda _paths, _manifest: trace_data)
    monkeypatch.setattr(cli, "find_incidents", lambda _manifest, _traces: [incident])
    database_path = tmp_path / "must-not-be-created.sqlite"

    main(
        [
            "agent-claims",
            "--db",
            str(database_path),
            "--policy",
            "P-00000001",
            "--manifest",
            "manifest.yaml",
            "--traces",
            "traces.jsonl",
            "--notified-date",
            "2027-02-11",
            "--dry-run",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert output[0]["decision"] == "dry_run"
    assert output[0]["claimed_cents"] == 45_000
    assert output[0]["evidence"][1]["type"] == "manifest_controls"
    assert not database_path.exists()
