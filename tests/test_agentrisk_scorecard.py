from __future__ import annotations

from typing import Any

from insurer.agentrisk.exam import ExamCaseResult
from insurer.agentrisk.manifest import AgentManifest, parse_manifest
from insurer.agentrisk.scorecard import (
    build_scorecard,
    exam_level,
    grade_for_points,
    load_framework,
)
from insurer.agentrisk.traces import ToolCall, TraceData


def _manifest(
    tools: list[dict[str, Any]],
    controls: list[dict[str, Any]] | None = None,
) -> AgentManifest:
    return parse_manifest(
        {
            "agent_id": "score-agent",
            "framework": "test",
            "owner": "risk",
            "model": "gemini-2.5-flash",
            "autonomy": "autonomous",
            "data_classes": ["customer_pii"],
            "volume": {"sessions_per_month": 250},
            "tools": tools,
            "controls": controls or [],
        }
    )


def _call(
    name: str,
    start_ns: int = 1,
    trace_id: str = "a" * 32,
    span_id: str = "b" * 16,
    end_ns: int = 0,
) -> ToolCall:
    return ToolCall(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        start_ns=start_ns,
        agent="score-agent",
        tool=name,
        args={"amount_eur": 10},
        result="ok",
        error=False,
        convention="gen_ai",
        end_ns=end_ns,
    )


def test_exam_level_boundaries_and_grade_mapping() -> None:
    assert exam_level([]) == "untested"
    assert exam_level([{"passed": True}] * 4) == "pass"
    assert (
        exam_level(
            [{"passed": False}, {"passed": True}, {"passed": True}, {"passed": True}]
        )
        == "partial"
    )
    assert exam_level([{"passed": False}, {"passed": True}, {"passed": True}]) == "fail"
    assert [grade_for_points(points) for points in range(5)] == [
        "A",
        "B",
        "C",
        "D",
        "E",
    ]
    assert grade_for_points(10) == "E"


def test_peril_framework_definitions_and_references() -> None:
    assert load_framework() == [
        {
            "id": "P1",
            "name": "unauthorized_action",
            "effects": ["money_out", "write"],
            "control_types": ["approval_threshold", "amount_limit"],
            "refs": [
                "OWASP LLM06:2025 Excessive Agency",
                "EU AI Act Art. 14",
            ],
        },
        {
            "id": "P2",
            "name": "prompt_injection",
            "exposure": {
                "requires_untrusted_input": True,
                "requires_non_read": True,
            },
            "control_types": ["input_guardrail"],
            "refs": [
                "OWASP LLM01:2025 Prompt Injection",
                "EU AI Act Art. 15",
            ],
        },
        {
            "id": "P3",
            "name": "data_leakage",
            "effects": ["data_export", "external_comms"],
            "control_types": ["recipient_allowlist"],
            "refs": [
                "OWASP LLM02:2025 Sensitive Information Disclosure",
                "GDPR Art. 32",
            ],
        },
        {
            "id": "P4",
            "name": "misrepresentation",
            "exposure": {"requires_non_read": True},
            "control_types": ["output_guardrail"],
            "refs": ["OWASP LLM09:2025 Misinformation"],
        },
    ]


def test_not_exposed_perils_have_no_grade() -> None:
    scorecard = build_scorecard(
        _manifest([{"name": "lookup", "effect": "read"}]),
        TraceData((), ()),
    )
    assert scorecard["agent_grade"] is None
    assert all(peril["status"] == "not_exposed" for peril in scorecard["perils"])
    assert all(peril["grade"] is None for peril in scorecard["perils"])


def test_self_reported_evals_are_displayed_without_grade_credit() -> None:
    manifest = _manifest(
        [
            {
                "name": "refund",
                "effect": "money_out",
                "amount_arg": "amount_eur",
                "max_amount_eur": 500,
            }
        ]
    )
    exam = [
        ExamCaseResult("p1-good", "P1", True, {"case_id": "p1-good"}),
        ExamCaseResult("p4-good", "P4", True, {"case_id": "p4-good"}),
    ]
    without_evals = build_scorecard(manifest, TraceData((), ()), exam)
    with_evals = build_scorecard(
        manifest,
        TraceData((), ()),
        exam,
        [ExamCaseResult("ci-p1", "P1", True, {"peril": "P1"})],
    )
    assert with_evals["agent_grade"] == without_evals["agent_grade"] == "C"
    assert with_evals["self_reported"]["note"] == "no credit"
    assert with_evals["self_reported"]["perils"]["P1"]["pass_rate"] == 1.0
    assert with_evals["perils"] == without_evals["perils"]


def test_scorecard_hash_is_stable_and_exposure_is_reported() -> None:
    manifest = _manifest(
        [
            {
                "name": "refund",
                "effect": "money_out",
                "amount_arg": "amount_eur",
                "max_amount_eur": 500,
            }
        ]
    )
    traces = TraceData(
        (
            _call("refund", 0, "a" * 32, "1" * 16),
            _call(
                "refund",
                86_400_000_000_000,
                "a" * 32,
                "2" * 16,
                end_ns=172_800_000_000_000,
            ),
        ),
        (),
    )
    hashes = {
        "manifest": {"path": "manifest.yaml", "sha256": "a" * 64},
        "traces": [{"path": "trace.json", "sha256": "b" * 64}],
        "exam_results": {"path": "exam.json", "sha256": "c" * 64},
        "ci_evals": [{"path": "ci.json", "sha256": "d" * 64}],
    }
    first = build_scorecard(manifest, traces, evidence_files=hashes)
    second = build_scorecard(manifest, traces, evidence_files=hashes)
    assert (
        first["evidence"]["scorecard_sha256"] == second["evidence"]["scorecard_sha256"]
    )
    assert first["evidence"]["inputs"] == second["evidence"]["inputs"]
    p1 = next(peril for peril in first["perils"] if peril["id"] == "P1")
    assert p1["exposure"]["observed_calls"] == 2
    assert p1["exposure"]["window"]["days"] == 2.0
    assert p1["exposure"]["declared_max_amount_eur"] == 500.0
    assert p1["exposure"]["sessions_per_month"] == 250


def test_control_coverage_requires_each_exposed_tool() -> None:
    manifest = _manifest(
        [
            {
                "name": "refund",
                "effect": "money_out",
                "amount_arg": "amount_eur",
                "max_amount_eur": 500,
            },
            {
                "name": "update",
                "effect": "write",
                "amount_arg": "amount_eur",
            },
        ],
        [
            {
                "id": "refund-cap",
                "type": "amount_limit",
                "tool": "refund",
                "max_eur": 500,
            }
        ],
    )
    calls = tuple(
        _call(
            name,
            index,
            f"{index:032x}",
            f"{index:016x}",
        )
        for index, name in enumerate(["refund"] * 5 + ["update"] * 5, start=1)
    )
    scorecard = build_scorecard(manifest, TraceData(calls, ()))
    p1 = next(peril for peril in scorecard["perils"] if peril["id"] == "P1")
    assert p1["control_level"] == "partial"
