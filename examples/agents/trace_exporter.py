from __future__ import annotations

import json
import threading
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


def _attribute_value(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {
            "arrayValue": {
                "values": [_attribute_value(item) for item in value],
            }
        }
    if isinstance(value, dict):
        return {
            "kvlistValue": {
                "values": [
                    {"key": str(key), "value": _attribute_value(item)}
                    for key, item in value.items()
                ]
            }
        }
    return {"stringValue": str(value)}


def _attributes(values: Any) -> list[dict[str, Any]]:
    return [
        {"key": str(key), "value": _attribute_value(value)}
        for key, value in (values or {}).items()
    ]


class CollectorJsonSpanExporter(SpanExporter):
    def __init__(self) -> None:
        self._documents: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        by_resource: dict[int, list[ReadableSpan]] = defaultdict(list)
        for span in spans:
            by_resource[id(span.resource)].append(span)
        documents: list[dict[str, Any]] = []
        for resource_spans in by_resource.values():
            resource = resource_spans[0].resource
            scopes: dict[tuple[str, str], list[ReadableSpan]] = defaultdict(list)
            for span in resource_spans:
                scope = span.instrumentation_scope
                scopes[(scope.name, scope.version or "")].append(span)
            scope_documents = []
            for (name, version), scope_spans in scopes.items():
                scope_documents.append(
                    {
                        "scope": {"name": name, "version": version},
                        "spans": [self._span(span) for span in scope_spans],
                    }
                )
            documents.append(
                {
                    "resourceSpans": [
                        {
                            "resource": {
                                "attributes": _attributes(resource.attributes),
                            },
                            "scopeSpans": scope_documents,
                        }
                    ]
                }
            )
        with self._lock:
            self._documents.extend(documents)
        return SpanExportResult.SUCCESS

    def _span(self, span: ReadableSpan) -> dict[str, Any]:
        context = span.context
        document = {
            "traceId": f"{context.trace_id:032x}",
            "spanId": f"{context.span_id:016x}",
            "name": span.name,
            "startTimeUnixNano": str(span.start_time or 0),
            "endTimeUnixNano": str(span.end_time or 0),
            "attributes": _attributes(span.attributes),
            "status": {
                "code": span.status.status_code.value,
                "message": span.status.description or "",
            },
        }
        if span.parent is not None:
            document["parentSpanId"] = f"{span.parent.span_id:016x}"
        return document

    def snapshot(self, *, clear: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            documents = list(self._documents)
            if clear:
                self._documents.clear()
        return documents

    def shutdown(self) -> None:
        return None


def write_collector_jsonl(
    path: str, documents: Sequence[dict[str, Any]], *, exclusive: bool = True
) -> None:
    mode = "x" if exclusive else "w"
    with open(path, mode, encoding="utf-8") as output:
        for document in documents:
            output.write(json.dumps(document, separators=(",", ":")) + "\n")
