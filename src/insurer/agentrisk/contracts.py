from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from insurer.agentrisk.exam import ExamCaseResult
from insurer.agentrisk.scorecard import sha256_file
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
_INPUT_FIELDS = {"manifest", "traces", "exam_results", "ci_evals"}


@dataclass(frozen=True)
class NormalizedEvidence:
    trace_data: TraceData
    exam_results: list[ExamCaseResult]
    ci_eval_results: list[ExamCaseResult]
    exam_present: bool
    inputs: dict[str, Any]


def schema_path(name: str) -> Path:
    return Path(__file__).with_name("schemas") / name


def evidence_inputs(
    manifest_path: str | Path,
    trace_paths: Iterable[str | Path],
    exam_path: str | Path | None,
    eval_paths: Iterable[str | Path],
) -> dict[str, Any]:
    def file_input(path: str | Path) -> dict[str, str]:
        return {"path": str(path), "sha256": sha256_file(Path(path))}

    return {
        "manifest": file_input(manifest_path),
        "traces": [file_input(path) for path in trace_paths],
        "exam_results": file_input(exam_path) if exam_path is not None else None,
        "ci_evals": [file_input(path) for path in eval_paths],
    }


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


def _validate_file_input(value: Any, name: str) -> dict[str, str]:
    item = _exact_fields(value, {"path", "sha256"}, name)
    if not isinstance(item["path"], str):
        raise ValueError(f"{name}.path must be a string")
    digest = item["sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError(f"{name}.sha256 must be a 64-character lowercase hex digest")
    return {"path": item["path"], "sha256": digest}


def _validate_inputs(value: Any) -> dict[str, Any]:
    item = _exact_fields(value, _INPUT_FIELDS, "inputs")
    if not isinstance(item["traces"], list):
        raise ValueError("inputs.traces must be a list")
    if item["exam_results"] is not None:
        exam_results = _validate_file_input(item["exam_results"], "inputs.exam_results")
    else:
        exam_results = None
    if not isinstance(item["ci_evals"], list):
        raise ValueError("inputs.ci_evals must be a list")
    return {
        "manifest": _validate_file_input(item["manifest"], "inputs.manifest"),
        "traces": [
            _validate_file_input(entry, f"inputs.traces[{index}]")
            for index, entry in enumerate(item["traces"])
        ],
        "exam_results": exam_results,
        "ci_evals": [
            _validate_file_input(entry, f"inputs.ci_evals[{index}]")
            for index, entry in enumerate(item["ci_evals"])
        ],
    }


def normalized_evidence(
    trace_data: TraceData,
    exam_results: Iterable[ExamCaseResult],
    ci_eval_results: Iterable[ExamCaseResult] = (),
    *,
    exam_present: bool,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(exam_present, bool):
        raise ValueError("exam_present must be a boolean")
    document: dict[str, Any] = {
        "version": EVIDENCE_VERSION,
        "tool_calls": [asdict(call) for call in trace_data.tool_calls],
        "guardrail_checks": [asdict(check) for check in trace_data.guardrail_checks],
        "warnings": trace_data.warnings,
        "exam_results": [asdict(result) for result in exam_results],
        "ci_eval_results": [asdict(result) for result in ci_eval_results],
        "exam_present": exam_present,
        "inputs": _validate_inputs(inputs),
    }
    parse_normalized_evidence(document)
    return document


def parse_normalized_evidence(
    document: Any,
) -> NormalizedEvidence:
    fields = {
        "version",
        "tool_calls",
        "guardrail_checks",
        "warnings",
        "exam_results",
        "ci_eval_results",
        "exam_present",
        "inputs",
    }
    raw = _exact_fields(document, fields, "evidence")
    if raw["version"] != EVIDENCE_VERSION:
        raise ValueError(f"evidence.version must be {EVIDENCE_VERSION!r}")
    for field in ("tool_calls", "guardrail_checks", "exam_results", "ci_eval_results"):
        if not isinstance(raw[field], list):
            raise ValueError(f"evidence.{field} must be a list")
    if not _is_int(raw["warnings"]):
        raise ValueError("evidence.warnings must be an integer")
    if not isinstance(raw["exam_present"], bool):
        raise ValueError("evidence.exam_present must be a boolean")
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
    return NormalizedEvidence(
        trace_data=trace_data,
        exam_results=exams,
        ci_eval_results=evals,
        exam_present=raw["exam_present"],
        inputs=_validate_inputs(raw["inputs"]),
    )
