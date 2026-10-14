from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from insurer.agentrisk.manifest import AgentManifest


@dataclass(frozen=True)
class ToolCall:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_ns: int
    agent: str
    tool: str
    args: dict[str, Any]
    result: str | None
    error: bool
    convention: str
    end_ns: int = 0


@dataclass(frozen=True)
class GuardrailCheck:
    trace_id: str
    kind: str
    passed: bool
    start_ns: int


@dataclass(frozen=True)
class TraceData:
    tool_calls: tuple[ToolCall, ...]
    guardrail_checks: tuple[GuardrailCheck, ...]
    warnings: int = 0

    @property
    def trace_ids(self) -> set[str]:
        return {call.trace_id for call in self.tool_calls} | {
            check.trace_id for check in self.guardrail_checks
        }


def _decode_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "boolValue", "intValue", "doubleValue"):
        if key in value:
            item = value[key]
            if key == "intValue":
                try:
                    return int(item)
                except (TypeError, ValueError):
                    return item
            if key == "doubleValue":
                try:
                    return float(item)
                except (TypeError, ValueError):
                    return item
            return item
    array = value.get("arrayValue")
    if isinstance(array, dict) and isinstance(array.get("values"), list):
        return [_decode_value(item) for item in array["values"]]
    nested = value.get("kvlistValue")
    if isinstance(nested, dict) and isinstance(nested.get("values"), list):
        return {
            item.get("key"): _decode_value(item.get("value"))
            for item in nested["values"]
            if isinstance(item, dict) and isinstance(item.get("key"), str)
        }
    return value


def _attributes(span: dict[str, Any]) -> dict[str, Any]:
    attrs = span.get("attributes", [])
    if isinstance(attrs, dict):
        return attrs
    if not isinstance(attrs, list):
        return {}
    result: dict[str, Any] = {}
    for item in attrs:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            result[item["key"]] = _decode_value(item.get("value"))
    return result


def _parse_args(value: Any) -> tuple[dict[str, Any], bool]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}, True
    if not isinstance(value, dict):
        return {}, True
    return value, False


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _time_ns(span: dict[str, Any]) -> int:
    value = span.get("startTimeUnixNano", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _end_time_ns(span: dict[str, Any]) -> int:
    value = span.get("endTimeUnixNano")
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _error(span: dict[str, Any], attrs: dict[str, Any]) -> bool:
    status = span.get("status", {})
    code = status.get("code") if isinstance(status, dict) else None
    return (
        code in (2, "2", "ERROR", "STATUS_CODE_ERROR")
        or attrs.get("error") is True
        or attrs.get("tool.error") is True
    )


def _spans(document: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(document, dict):
        raise ValueError("OTLP/JSON document must be an object")
    resources = document.get("resourceSpans", [])
    if not isinstance(resources, list):
        raise ValueError("OTLP/JSON resourceSpans must be a list")
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        scopes = resource.get(
            "scopeSpans", resource.get("instrumentationLibrarySpans", [])
        )
        if not isinstance(scopes, list):
            continue
        for scope in scopes:
            if not isinstance(scope, dict):
                continue
            spans = scope.get("spans", [])
            if isinstance(spans, list):
                for span in spans:
                    if isinstance(span, dict):
                        yield span


def parse_trace_documents(
    documents: Iterable[Any], manifest: AgentManifest
) -> TraceData:
    calls: list[ToolCall] = []
    checks: list[GuardrailCheck] = []
    warnings = 0
    for document in documents:
        for span in _spans(document):
            trace_id = str(span.get("traceId", ""))
            span_id = str(span.get("spanId", ""))
            parent = span.get("parentSpanId")
            parent_span_id = str(parent) if parent else None
            start_ns = _time_ns(span)
            attrs = _attributes(span)
            span_kind = str(attrs.get("openinference.span.kind", "")).upper()
            guardrail_kind = attrs.get(
                "insurer.guardrail.kind", attrs.get("guardrail.kind")
            )
            passed_value = attrs.get(
                "insurer.guardrail.passed", attrs.get("guardrail.passed")
            )
            if (
                (span_kind == "GUARDRAIL" or guardrail_kind is not None)
                and guardrail_kind in ("input", "output")
                and isinstance(passed_value, bool)
            ):
                checks.append(
                    GuardrailCheck(
                        trace_id=trace_id,
                        kind=guardrail_kind,
                        passed=passed_value,
                        start_ns=start_ns,
                    )
                )

            operation = attrs.get("gen_ai.operation.name")
            convention: str | None = None
            tool_name: Any = None
            args_value: Any = None
            result_value: Any = None
            agent_value: Any = None
            if operation == "execute_tool":
                convention = "gen_ai"
                tool_name = attrs.get("gen_ai.tool.name")
                args_value = attrs.get(
                    "gen_ai.tool.call.arguments",
                    attrs.get("gcp.vertex.agent.tool_call_args"),
                )
                result_value = attrs.get(
                    "gen_ai.tool.call.result",
                    attrs.get("gcp.vertex.agent.tool_response"),
                )
                agent_value = attrs.get("gen_ai.agent.name")
            elif span_kind == "TOOL":
                convention = "openinference"
                tool_name = attrs.get("tool.name")
                args_value = attrs.get("input.value")
                result_value = attrs.get("output.value")
                agent_value = attrs.get("gen_ai.agent.name")

            if convention is None or not isinstance(tool_name, str) or not tool_name:
                continue
            args, malformed = _parse_args(args_value)
            warnings += int(malformed)
            agent = (
                agent_value
                if isinstance(agent_value, str) and agent_value
                else manifest.agent_id
            )
            calls.append(
                ToolCall(
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    start_ns=start_ns,
                    agent=agent,
                    tool=tool_name,
                    args=args,
                    result=_as_text(result_value),
                    error=_error(span, attrs),
                    convention=convention,
                    end_ns=_end_time_ns(span),
                )
            )
    calls.sort(key=lambda call: (call.trace_id, call.start_ns, call.span_id))
    checks.sort(key=lambda check: (check.trace_id, check.start_ns))
    return TraceData(
        tool_calls=tuple(calls), guardrail_checks=tuple(checks), warnings=warnings
    )


def _documents(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        documents: list[Any] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                documents.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} is not valid JSON") from exc
        return documents
    return document if isinstance(document, list) else [document]


def load_trace_files(paths: Iterable[str | Path], manifest: AgentManifest) -> TraceData:
    documents: list[Any] = []
    for item in paths:
        path = Path(item)
        try:
            documents.extend(_documents(path))
        except OSError as exc:
            raise ValueError(f"could not read trace file {path}: {exc}") from exc
    return parse_trace_documents(documents, manifest)
