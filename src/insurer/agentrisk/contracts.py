from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from insurer.agentrisk.exam import ExamCaseResult
from insurer.agentrisk.traces import GuardrailCheck, ToolCall, TraceData

EVIDENCE_VERSION = "agent-evidence.v1"
_TOOL_CALL_FIELDS = {
    "trace_id",
    "span_id",
    "parent_span_id",
    "start_ns",
    "agent",
    "tool",
    "args",
    "result",
    "error",
    "convention",
    "end_ns",
}
_GUARDRAIL_FIELDS = {"trace_id", "kind", "passed", "start_ns"}
_EXAM_RESULT_FIELDS = {"case_id", "peril", "passed", "metadata"}


def schema_path(name: str) -> Path:
    return Path(__file__).resolve().parents[3] / "schemas" / name


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} keys must be strings")
    return value


def _exact_fields(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    mapping = _mapping(value, name)
    missing = sorted(fields - mapping.keys())
    unknown = sorted(mapping.keys() - fields)
    if missing:
        raise ValueError(f"{name} is missing required fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{name} has unknown fields: {', '.join(unknown)}")
    return mapping


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_tool_call(value: Any, index: int) -> ToolCall:
    name = f"tool_calls[{index}]"
    item = _exact_fields(value, _TOOL_CALL_FIELDS, name)
    for field in ("trace_id", "span_id", "agent", "tool"):
        if not isinstance(item[field], str):
            raise ValueError(f"{name}.{field} must be a string")
    parent = item["parent_span_id"]
    if parent is not None and not isinstance(parent, str):
        raise ValueError(f"{name}.parent_span_id must be a string or null")
    if not _is_int(item["start_ns"]) or not _is_int(item["end_ns"]):
        raise ValueError(f"{name}.start_ns and end_ns must be integers")
    args = _mapping(item["args"], f"{name}.args")
    result = item["result"]
    if result is not None and not isinstance(result, str):
        raise ValueError(f"{name}.result must be a string or null")
    if not isinstance(item["error"], bool):
        raise ValueError(f"{name}.error must be a boolean")
    if item["convention"] not in {"gen_ai", "openinference"}:
        raise ValueError(f"{name}.convention is unsupported")
    return ToolCall(
        trace_id=item["trace_id"],
        span_id=item["span_id"],
        parent_span_id=parent,
        start_ns=item["start_ns"],
        agent=item["agent"],
        tool=item["tool"],
        args=args,
        result=result,
        error=item["error"],
        convention=item["convention"],
        end_ns=item["end_ns"],
    )


def _validate_guardrail(value: Any, index: int) -> GuardrailCheck:
    name = f"guardrail_checks[{index}]"
    item = _exact_fields(value, _GUARDRAIL_FIELDS, name)
    if not isinstance(item["trace_id"], str):
        raise ValueError(f"{name}.trace_id must be a string")
    if item["kind"] not in {"input", "output"}:
        raise ValueError(f"{name}.kind is unsupported")
    if not isinstance(item["passed"], bool):
        raise ValueError(f"{name}.passed must be a boolean")
    if not _is_int(item["start_ns"]):
        raise ValueError(f"{name}.start_ns must be an integer")
    return GuardrailCheck(
        trace_id=item["trace_id"],
        kind=item["kind"],
        passed=item["passed"],
        start_ns=item["start_ns"],
    )


def _validate_exam_result(
    value: Any, index: int, *, require_battery: bool
) -> ExamCaseResult:
    name = f"exam_results[{index}]" if require_battery else f"ci_eval_results[{index}]"
    item = _exact_fields(value, _EXAM_RESULT_FIELDS, name)
    if not isinstance(item["case_id"], str) or not isinstance(item["peril"], str):
        raise ValueError(f"{name}.case_id and peril must be strings")
    if not isinstance(item["passed"], bool):
        raise ValueError(f"{name}.passed must be a boolean")
    metadata = _mapping(item["metadata"], f"{name}.metadata")
    if require_battery and any(
        not isinstance(metadata.get(field), str)
        for field in ("battery", "battery_sha256")
    ):
        raise ValueError(
            f"{name}.metadata must contain string battery and battery_sha256"
        )
    return ExamCaseResult(
        case_id=item["case_id"],
        peril=item["peril"],
        passed=item["passed"],
        metadata=metadata,
    )


def normalized_evidence(
    trace_data: TraceData,
    exam_results: Iterable[ExamCaseResult],
    ci_eval_results: Iterable[ExamCaseResult] = (),
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "version": EVIDENCE_VERSION,
        "tool_calls": [asdict(call) for call in trace_data.tool_calls],
        "guardrail_checks": [asdict(check) for check in trace_data.guardrail_checks],
        "warnings": trace_data.warnings,
        "exam_results": [asdict(result) for result in exam_results],
        "ci_eval_results": [asdict(result) for result in ci_eval_results],
    }
    parse_normalized_evidence(document)
    return document


def parse_normalized_evidence(
    document: Any,
) -> tuple[TraceData, list[ExamCaseResult], list[ExamCaseResult]]:
    fields = {
        "version",
        "tool_calls",
        "guardrail_checks",
        "warnings",
        "exam_results",
        "ci_eval_results",
    }
    raw = _exact_fields(document, fields, "evidence")
    if raw["version"] != EVIDENCE_VERSION:
        raise ValueError(f"evidence.version must be {EVIDENCE_VERSION!r}")
    for field in ("tool_calls", "guardrail_checks", "exam_results", "ci_eval_results"):
        if not isinstance(raw[field], list):
            raise ValueError(f"evidence.{field} must be a list")
    if not _is_int(raw["warnings"]):
        raise ValueError("evidence.warnings must be an integer")
    trace_data = TraceData(
        tool_calls=tuple(
            _validate_tool_call(item, index)
            for index, item in enumerate(raw["tool_calls"])
        ),
        guardrail_checks=tuple(
            _validate_guardrail(item, index)
            for index, item in enumerate(raw["guardrail_checks"])
        ),
        warnings=raw["warnings"],
    )
    exams = [
        _validate_exam_result(item, index, require_battery=True)
        for index, item in enumerate(raw["exam_results"])
    ]
    evals = [
        _validate_exam_result(item, index, require_battery=False)
        for index, item in enumerate(raw["ci_eval_results"])
    ]
    return trace_data, exams, evals
