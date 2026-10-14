from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from insurer.agentrisk.controls import verify_controls
from insurer.agentrisk.manifest import AgentManifest, load_manifest, parse_manifest
from insurer.agentrisk.traces import (
    GuardrailCheck,
    ToolCall,
    TraceData,
    load_trace_files,
    parse_trace_documents,
)


def manifest_document() -> dict[str, Any]:
    return {
        "agent_id": "refund-bot",
        "framework": "langgraph",
        "owner": "support-ops",
        "model": "gemini-2.5-flash",
        "autonomy": "autonomous",
        "data_classes": ["customer_pii", "payment"],
        "volume": {"sessions_per_month": 5000},
        "tools": [
            {"name": "lookup_order", "effect": "read"},
            {
                "name": "fetch_attachment",
                "effect": "read",
                "untrusted_input": True,
            },
            {
                "name": "issue_refund",
                "effect": "money_out",
                "amount_arg": "amount_eur",
                "max_amount_eur": 500,
            },
            {"name": "request_approval", "effect": "approval"},
            {
                "name": "send_email",
                "effect": "external_comms",
                "recipient_arg": "to",
            },
            {"name": "update_case", "effect": "write"},
        ],
        "controls": [
            {
                "id": "approval",
                "type": "approval_threshold",
                "tool": "issue_refund",
                "threshold_eur": 100,
                "approval_tool": "request_approval",
            },
            {
                "id": "cap",
                "type": "amount_limit",
                "tool": "issue_refund",
                "max_eur": 500,
            },
            {
                "id": "allowlist",
                "type": "recipient_allowlist",
                "tool": "send_email",
                "allowed_domains": ["acme.test"],
            },
            {"id": "input", "type": "input_guardrail"},
            {"id": "output", "type": "output_guardrail"},
        ],
    }


def manifest() -> AgentManifest:
    return parse_manifest(manifest_document())


def _attribute(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": value}}


def _span(
    span_id: str,
    start_ns: int,
    attrs: dict[str, Any],
    *,
    trace_id: str = "ab" * 16,
    parent_span_id: str = "cd" * 8,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent_span_id,
        "startTimeUnixNano": str(start_ns),
        "attributes": [_attribute(key, value) for key, value in attrs.items()],
        "status": status or {"code": 1},
    }


def _document(*spans: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {"scopeSpans": [{"spans": list(spans)}]},
        ]
    }


def _call(
    span_id: str,
    trace_id: str,
    start_ns: int,
    tool: str,
    args: dict[str, Any] | None = None,
    result: str | None = None,
    error: bool = False,
) -> ToolCall:
    return ToolCall(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        start_ns=start_ns,
        agent="refund-bot",
        tool=tool,
        args=args or {},
        result=result,
        error=error,
        convention="gen_ai",
    )


def _trace_data(
    calls: list[ToolCall], checks: list[GuardrailCheck] | None = None
) -> TraceData:
    return TraceData(tuple(calls), tuple(checks or []))


def _control_status(data: TraceData, control_id: str) -> str:
    return {item.id: item.status for item in verify_controls(manifest(), data)}[
        control_id
    ]


def test_manifest_loads_and_validates_references(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    import yaml

    path.write_text(yaml.safe_dump(manifest_document()), encoding="utf-8")
    loaded = load_manifest(path)
    assert loaded.agent_id == "refund-bot"
    assert loaded.tools_by_name["issue_refund"].amount_arg == "amount_eur"
    assert loaded.controls[0].approval_tool == "request_approval"

    invalid = manifest_document()
    invalid["controls"][0]["tool"] = "missing"
    with pytest.raises(ValueError, match="references unknown tool"):
        parse_manifest(invalid)


@pytest.mark.parametrize(
    ("effect", "spec"),
    [
        ("read", {"name": "tool", "effect": "unknown"}),
        (
            "amount",
            {
                "name": "tool",
                "effect": "money_out",
                "amount_arg": "amount_eur",
            },
        ),
        (
            "recipient",
            {"name": "tool", "effect": "external_comms"},
        ),
    ],
)
def test_manifest_rejects_invalid_effects_and_missing_control_arguments(
    effect: str, spec: dict[str, Any]
) -> None:
    document = manifest_document()
    if effect == "read":
        document["tools"][0] = spec
    elif effect == "amount":
        document["tools"][2] = spec
    else:
        document["tools"][4] = spec
    if effect == "amount":
        document["controls"][1]["tool"] = "issue_refund"
        document["tools"][2].pop("amount_arg")
    elif effect == "recipient":
        document["controls"][2]["tool"] = "send_email"
    with pytest.raises(ValueError):
        parse_manifest(document)


def test_trace_parser_genai_precedence_and_string_int_value() -> None:
    span = _span(
        "01" * 8,
        20,
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "issue_refund",
            "gen_ai.tool.call.arguments": '{"amount_eur": 40}',
            "gcp.vertex.agent.tool_call_args": '{"amount_eur": 99}',
            "gen_ai.tool.call.result": "paid",
            "gen_ai.agent.name": "refund-bot-live",
            "custom.count": 13,
        },
    )
    output = parse_trace_documents([_document(span)], manifest())
    call = output.tool_calls[0]
    assert call.args == {"amount_eur": 40}
    assert call.result == "paid"
    assert call.agent == "refund-bot-live"
    assert call.convention == "gen_ai"
    assert output.warnings == 0


def test_trace_parser_decodes_otlp_typed_tool_arguments() -> None:
    span = _span(
        "09" * 8,
        25,
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "issue_refund",
        },
    )
    span["attributes"].append(
        {
            "key": "gen_ai.tool.call.arguments",
            "value": {
                "kvlistValue": {
                    "values": [
                        {"key": "amount_eur", "value": {"intValue": "42"}},
                        {"key": "approved", "value": {"boolValue": True}},
                        {"key": "factor", "value": {"doubleValue": 1.5}},
                        {
                            "key": "labels",
                            "value": {
                                "arrayValue": {
                                    "values": [
                                        {"stringValue": "risk"},
                                        {"stringValue": "test"},
                                    ]
                                }
                            },
                        },
                    ]
                }
            },
        }
    )

    output = parse_trace_documents([_document(span)], manifest())

    assert output.tool_calls[0].args == {
        "amount_eur": 42,
        "approved": True,
        "factor": 1.5,
        "labels": ["risk", "test"],
    }


def test_trace_parser_openinference_guardrails_and_fallback_agent() -> None:
    trace_id = "12" * 16
    tool_span = _span(
        "02" * 8,
        30,
        {
            "openinference.span.kind": "TOOL",
            "tool.name": "send_email",
            "input.value": '{"to": "ops@acme.test"}',
            "output.value": "sent",
        },
        trace_id=trace_id,
    )
    guardrail_span = _span(
        "03" * 8,
        40,
        {
            "openinference.span.kind": "GUARDRAIL",
            "insurer.guardrail.kind": "output",
            "insurer.guardrail.passed": True,
        },
        trace_id=trace_id,
    )
    output = parse_trace_documents([_document(tool_span, guardrail_span)], manifest())
    assert output.tool_calls[0].tool == "send_email"
    assert output.tool_calls[0].agent == "refund-bot"
    assert output.tool_calls[0].args == {"to": "ops@acme.test"}
    assert output.guardrail_checks == (GuardrailCheck(trace_id, "output", True, 40),)


def test_trace_parser_bad_args_and_non_tool_spans() -> None:
    malformed = _span(
        "04" * 8,
        20,
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "issue_refund",
            "gen_ai.tool.call.arguments": "not-json",
        },
    )
    non_dict = _span(
        "05" * 8,
        10,
        {
            "openinference.span.kind": "TOOL",
            "tool.name": "issue_refund",
            "input.value": '["not", "an", "object"]',
        },
    )
    llm_span = _span(
        "06" * 8,
        5,
        {"openinference.span.kind": "LLM", "input.value": "{}"},
    )
    output = parse_trace_documents(
        [_document(malformed, non_dict, llm_span)], manifest()
    )
    assert [call.start_ns for call in output.tool_calls] == [10, 20]
    assert all(call.args == {} for call in output.tool_calls)
    assert output.warnings == 2
    assert len(output.tool_calls) == 2


def test_trace_parser_reads_json_lines(tmp_path: Path) -> None:
    first = _document(
        _span(
            "07" * 8,
            10,
            {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "lookup_order",
                "gcp.vertex.agent.tool_call_args": '{"order_id": "A"}',
                "gcp.vertex.agent.tool_response": '{"order_id": "A", "found": true}',
            },
        )
    )
    second = _document(
        _span(
            "08" * 8,
            15,
            {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "lookup_order",
                "gcp.vertex.agent.tool_call_args": '{"order_id": "B"}',
            },
            trace_id="34" * 16,
        )
    )
    path = tmp_path / "collector.jsonl"
    path.write_text(f"{json.dumps(first)}\n{json.dumps(second)}\n", encoding="utf-8")
    output = load_trace_files([path], manifest())
    assert len(output.tool_calls) == 2
    assert output.trace_ids == {"ab" * 16, "34" * 16}
    call_a = next(
        call for call in output.tool_calls if call.args.get("order_id") == "A"
    )
    assert call_a.result == '{"order_id": "A", "found": true}'


def test_controls_approval_order_denial_and_unobserved() -> None:
    valid_calls: list[ToolCall] = []
    for index in range(5):
        trace_id = f"{index:032x}"
        valid_calls.extend(
            [
                _call(
                    f"{index:016x}",
                    trace_id,
                    10,
                    "request_approval",
                    {"order_id": str(index)},
                    "approved",
                ),
                _call(
                    f"{index + 10:016x}",
                    trace_id,
                    20,
                    "issue_refund",
                    {"order_id": str(index), "amount_eur": 200},
                    "paid",
                ),
            ]
        )
    assert _control_status(_trace_data(valid_calls), "approval") == "verified"
    denied = [
        _call("aa", "a" * 32, 10, "request_approval", result="DENIED by manager"),
        _call("bb", "a" * 32, 20, "issue_refund", {"amount_eur": 200}),
    ]
    assert _control_status(_trace_data(denied), "approval") == "violated"
    late = [
        _call("cc", "b" * 32, 10, "issue_refund", {"amount_eur": 200}),
        _call("dd", "b" * 32, 20, "request_approval", result="approved"),
    ]
    assert _control_status(_trace_data(late), "approval") == "violated"
    assert _control_status(_trace_data([]), "approval") == "unobserved"

    wrong_order = [
        _call(
            "ee",
            "c" * 32,
            10,
            "request_approval",
            {"order_id": "O-1"},
            "approved",
        ),
        _call(
            "ff",
            "c" * 32,
            20,
            "issue_refund",
            {"order_id": "O-2", "amount_eur": 200},
            "paid",
        ),
    ]
    assert _control_status(_trace_data(wrong_order), "approval") == "violated"

    unbound_approval = [
        _call("aa", "d" * 32, 10, "request_approval", result="approved"),
        _call(
            "bb",
            "d" * 32,
            20,
            "issue_refund",
            {"order_id": "O-2", "amount_eur": 200},
            "paid",
        ),
    ]
    assert _control_status(_trace_data(unbound_approval), "approval") == "violated"


@pytest.mark.parametrize(
    "args",
    [{"order_id": "O-3"}, {"order_id": "O-3", "amount_eur": "not-a-number"}],
)
def test_approval_rejects_missing_or_nonnumeric_amount(
    args: dict[str, Any],
) -> None:
    call = _call("cc", "e" * 32, 10, "issue_refund", args, "paid")
    result = next(
        item
        for item in verify_controls(manifest(), _trace_data([call]))
        if item.id == "approval"
    )
    assert result.status == "violated"


def test_controls_amount_limit_and_recipient_allowlist() -> None:
    capped = [
        _call(
            f"{index:016x}",
            f"{index:032x}",
            10,
            "issue_refund",
            {"amount_eur": 100},
        )
        for index in range(5)
    ]
    assert _control_status(_trace_data(capped), "cap") == "verified"

    cumulative = [
        _call(
            f"{index:016x}",
            "a" * 32,
            index * 10,
            "issue_refund",
            {"order_id": "A", "amount_eur": 200},
        )
        for index in range(3)
    ]
    cap_result = next(
        item
        for item in verify_controls(manifest(), _trace_data(cumulative))
        if item.id == "cap"
    )
    assert cap_result.violations == 1
    separate_orders = [
        _call(
            f"{index + 10:016x}",
            "b" * 32,
            index * 10,
            "issue_refund",
            {"order_id": str(index), "amount_eur": 200},
        )
        for index in range(3)
    ]
    separate_result = next(
        item
        for item in verify_controls(manifest(), _trace_data(separate_orders))
        if item.id == "cap"
    )
    assert separate_result.violations == 0
    no_order_id = [
        _call(
            f"{index + 20:016x}",
            "c" * 32,
            index * 10,
            "issue_refund",
            {"amount_eur": 300},
        )
        for index in range(2)
    ]
    no_order_result = next(
        item
        for item in verify_controls(manifest(), _trace_data(no_order_id))
        if item.id == "cap"
    )
    assert no_order_result.violations == 0

    missing_amount = [
        _call("01", "a" * 32, 10, "issue_refund", {}),
    ]
    assert _control_status(_trace_data(missing_amount), "cap") == "violated"
    assert _control_status(_trace_data([]), "cap") == "unobserved"

    allowed = [
        _call(
            f"{index:016x}",
            f"{index:032x}",
            10,
            "send_email",
            {"to": "ops@ACME.TEST"},
        )
        for index in range(5)
    ]
    assert _control_status(_trace_data(allowed), "allowlist") == "verified"
    denied = [_call("02", "c" * 32, 10, "send_email", {"to": "ops@evil.test"})]
    assert _control_status(_trace_data(denied), "allowlist") == "violated"
    assert _control_status(_trace_data([]), "allowlist") == "unobserved"


def test_blocked_tool_attempts_are_counted_without_violations() -> None:
    trace_id = "d" * 32
    calls = [
        _call(
            "01",
            trace_id,
            10,
            "issue_refund",
            {"order_id": "R-120", "amount_eur": 120},
            "denied: manager approval required",
        ),
        _call(
            "02",
            trace_id,
            20,
            "send_email",
            {"to": "ops@evil.test"},
            "mail transport error",
            error=True,
        ),
    ]
    results = {
        item.id: item for item in verify_controls(manifest(), _trace_data(calls))
    }
    for control_id in ("approval", "cap", "allowlist"):
        result = results[control_id]
        assert result.violations == 0
        assert result.blocked_attempts == 1
        assert result.as_dict()["blocked_attempts"] == 1


def test_blocked_amount_is_excluded_from_cumulative_controls() -> None:
    trace_id = "e" * 32
    calls = [
        _call(
            "01",
            trace_id,
            10,
            "issue_refund",
            {"order_id": "R-120", "amount_eur": 400},
            "denied: manager approval required",
        ),
        _call(
            "02",
            trace_id,
            20,
            "request_approval",
            {"order_id": "R-120", "amount_eur": 120},
            "approved",
        ),
        _call(
            "03",
            trace_id,
            30,
            "issue_refund",
            {"order_id": "R-120", "amount_eur": 120},
            "paid",
        ),
    ]
    results = {
        item.id: item for item in verify_controls(manifest(), _trace_data(calls))
    }
    for control_id in ("approval", "cap"):
        result = results[control_id]
        assert result.violations == 0
        assert result.blocked_attempts == 1


def test_controls_guardrails_require_observation_in_the_trace() -> None:
    calls: list[ToolCall] = []
    checks: list[GuardrailCheck] = []
    for index in range(5):
        trace_id = f"{index:032x}"
        calls.extend(
            [
                _call(f"{index + 1:016x}", trace_id, 10, "fetch_attachment"),
                _call(f"{index + 11:016x}", trace_id, 30, "update_case"),
            ]
        )
        checks.extend(
            [
                GuardrailCheck(trace_id, "input", False, 20),
                GuardrailCheck(trace_id, "output", False, 40),
            ]
        )
    observed = _trace_data(calls, checks)
    assert _control_status(observed, "input") == "violated"
    assert _control_status(observed, "output") == "verified"

    input_violation = _trace_data(
        [
            _call("01", "d" * 32, 10, "fetch_attachment"),
            _call("02", "d" * 32, 30, "update_case"),
        ]
    )
    assert _control_status(input_violation, "input") == "violated"
    output_violation = _trace_data([_call("03", "e" * 32, 10, "update_case")])
    assert _control_status(output_violation, "output") == "violated"
    assert _control_status(_trace_data([]), "input") == "unobserved"
    assert _control_status(_trace_data([]), "output") == "unobserved"


def test_input_guardrail_before_untrusted_read_covers_following_action() -> None:
    calls: list[ToolCall] = []
    checks: list[GuardrailCheck] = []
    for index in range(5):
        trace_id = f"{index + 20:032x}"
        calls.extend(
            [
                _call(f"{index + 21:016x}", trace_id, 10, "fetch_attachment"),
                _call(f"{index + 31:016x}", trace_id, 30, "update_case"),
            ]
        )
        checks.append(GuardrailCheck(trace_id, "input", True, 5))
    assert _control_status(_trace_data(calls, checks), "input") == "verified"
