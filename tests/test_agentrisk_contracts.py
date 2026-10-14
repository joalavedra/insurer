from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from insurer.agentrisk.contracts import (
    normalized_evidence,
    parse_normalized_evidence,
    schema_path,
)
from insurer.agentrisk.exam import ExamCaseResult, manifest_dict
from insurer.agentrisk.manifest import load_manifest, parse_manifest
from insurer.agentrisk.scorecard import build_scorecard, scorecard_sha256
from insurer.agentrisk.traces import TraceData, load_trace_files
from insurer.cli import main
from insurer.policies import PolicyService
from insurer.products import load_agent_ops_product
from insurer.storage import connect
from tests.fixtures.contracts.regenerate import generate_goldens

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "contracts"
MANIFEST_PATH = ROOT / "examples" / "agents" / "manifests" / "kyc_bot.yaml"
SCHEMA_NAMES = (
    "agent-manifest.v1.schema.json",
    "agent-evidence.v1.schema.json",
    "agent-scorecard.v1.schema.json",
    "agent-quote.v1.schema.json",
)


def _schema(name: str) -> dict[str, Any]:
    return json.loads(schema_path(name).read_text(encoding="utf-8"))


def _quote_validator() -> Draft202012Validator:
    scorecard_schema = _schema("agent-scorecard.v1.schema.json")
    registry = Registry().with_resource(
        scorecard_schema["$id"], Resource.from_contents(scorecard_schema)
    )
    return Draft202012Validator(
        _schema("agent-quote.v1.schema.json"), registry=registry
    )


def _manifest_document() -> dict[str, Any]:
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_all_schemas_are_valid_draft_2020_12_schemas() -> None:
    for name in SCHEMA_NAMES:
        Draft202012Validator.check_schema(_schema(name))


@pytest.mark.parametrize(
    "path",
    [
        ROOT / "examples" / "agents" / "manifests" / "refund_bot.yaml",
        MANIFEST_PATH,
    ],
)
def test_example_manifests_and_exam_serializations_validate(path: Path) -> None:
    validator = Draft202012Validator(_schema("agent-manifest.v1.schema.json"))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest = parse_manifest(raw)

    assert validator.is_valid(raw)
    assert validator.is_valid(manifest_dict(manifest))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(unexpected=True),
        lambda value: value["tools"][0].update(unexpected=True),
        lambda value: value["tools"][0].update(effect="unsupported"),
        lambda value: value.update(autonomy="assisted"),
        lambda value: value["volume"].update(sessions_per_month=-1),
        lambda value: value["volume"].update(sessions_per_month=True),
        lambda value: value["volume"].update(sessions_per_month=1.5),
        lambda value: value["controls"][0].pop("threshold_eur"),
        lambda value: value["controls"][1].update(unexpected=True),
        lambda value: value["tools"][-1].update(max_amount_eur=0),
        lambda value: value.update(agent_id=""),
        lambda value: value.update(agent_id=" \t "),
    ],
    ids=[
        "unknown-top-level-field",
        "unknown-tool-field",
        "unsupported-effect",
        "bad-autonomy",
        "negative-sessions",
        "boolean-sessions",
        "fractional-sessions",
        "missing-approval-threshold",
        "extra-amount-limit-field",
        "zero-max-amount",
        "empty-agent-id",
        "whitespace-agent-id",
    ],
)
def test_manifest_schema_and_parser_reject_matching_cases(mutate: Any) -> None:
    document = copy.deepcopy(_manifest_document())
    mutate(document)
    validator = Draft202012Validator(_schema("agent-manifest.v1.schema.json"))

    assert not validator.is_valid(document)
    with pytest.raises(ValueError):
        parse_manifest(document)


def test_empty_recipient_allowlist_is_valid_and_means_no_allowed_domains() -> None:
    document = copy.deepcopy(_manifest_document())
    document["controls"][2]["allowed_domains"] = []
    validator = Draft202012Validator(_schema("agent-manifest.v1.schema.json"))

    assert validator.is_valid(document)
    assert parse_manifest(document).controls[2].allowed_domains == ()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["controls"][0].update(tool="missing_tool"),
        lambda value: value["tools"][3].update(effect="write"),
        lambda value: value["tools"][-1].pop("amount_arg"),
        lambda value: value["tools"].append(copy.deepcopy(value["tools"][0])),
        lambda value: value["controls"].append(copy.deepcopy(value["controls"][3])),
    ],
    ids=[
        "unknown-tool-reference",
        "approval-tool-has-wrong-effect",
        "max-amount-without-amount-arg",
        "duplicate-tool-name",
        "duplicate-control-id",
    ],
)
def test_parser_only_manifest_rules_remain_schema_valid(mutate: Any) -> None:
    document = copy.deepcopy(_manifest_document())
    mutate(document)
    validator = Draft202012Validator(_schema("agent-manifest.v1.schema.json"))

    assert validator.is_valid(document)
    with pytest.raises(ValueError):
        parse_manifest(document)


def _exam_results() -> list[ExamCaseResult]:
    return [
        ExamCaseResult(
            case_id=item["case_id"],
            peril=item["peril"],
            passed=item["passed"],
            metadata=item["metadata"],
        )
        for item in json.loads(
            (FIXTURE_DIR / "kyc-exam-results.json").read_text(encoding="utf-8")
        )
    ]


def _golden_quote_validator() -> Draft202012Validator:
    return _quote_validator()


def test_contract_goldens_validate_and_regenerate_deterministically() -> None:
    generated = generate_goldens()
    for name, value in generated.items():
        path = FIXTURE_DIR / name
        if name.endswith(".jsonl"):
            assert path.read_text(encoding="utf-8") == value
        else:
            assert json.loads(path.read_text(encoding="utf-8")) == value

    manifest_validator = Draft202012Validator(_schema("agent-manifest.v1.schema.json"))
    evidence_validator = Draft202012Validator(_schema("agent-evidence.v1.schema.json"))
    scorecard_validator = Draft202012Validator(
        _schema("agent-scorecard.v1.schema.json")
    )
    quote_validator = _golden_quote_validator()
    manifest_validator.validate(_manifest_document())
    evidence = json.loads((FIXTURE_DIR / "kyc-evidence.json").read_text())
    evidence_validator.validate(evidence)
    scorecard = json.loads((FIXTURE_DIR / "kyc-scorecard.json").read_text())
    scorecard_validator.validate(scorecard)
    quote_validator.validate(json.loads((FIXTURE_DIR / "kyc-quote.json").read_text()))
    exam_schema = _schema("agent-evidence.v1.schema.json")["$defs"]["examCaseResult"]
    for result in json.loads((FIXTURE_DIR / "kyc-exam-results.json").read_text()):
        Draft202012Validator(exam_schema).validate(result)


def test_normalized_evidence_round_trip_preserves_scorecard() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    trace_data = load_trace_files([FIXTURE_DIR / "kyc-traces.jsonl"], manifest)
    exams = _exam_results()
    evidence = normalized_evidence(trace_data, exams)
    round_tripped, round_trip_exams, round_trip_evals = parse_normalized_evidence(
        evidence
    )

    assert (round_tripped, round_trip_exams, round_trip_evals) == (
        trace_data,
        exams,
        [],
    )
    scorecard = json.loads((FIXTURE_DIR / "kyc-scorecard.json").read_text())
    evidence_files = scorecard["evidence"]["inputs"]
    direct_scorecard = build_scorecard(
        manifest,
        trace_data,
        exams,
        evidence_files=evidence_files,
        exam_present=True,
    )
    round_trip_scorecard = build_scorecard(
        manifest,
        round_tripped,
        round_trip_exams,
        round_trip_evals,
        evidence_files=evidence_files,
        exam_present=True,
    )
    assert direct_scorecard == round_trip_scorecard == scorecard


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"version": "agent-evidence.v2"},
        {"version": "agent-evidence.v1", "tool_calls": []},
    ],
)
def test_parse_normalized_evidence_rejects_bad_version_or_shape(
    document: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        parse_normalized_evidence(document)


def test_scorecard_hash_matches_golden_and_literal_canonical_vector() -> None:
    scorecard = json.loads((FIXTURE_DIR / "kyc-scorecard.json").read_text())
    assert scorecard_sha256(scorecard) == scorecard["evidence"]["scorecard_sha256"]

    literal = {
        "agent_id": "é",
        "n": 1e-05,
        "f": 500.0,
        "i": 3,
        "evidence": {
            "scorecard_sha256": "x",
            "inputs": {"m": {"path": "p", "sha256": "s"}},
        },
    }
    canonical = (
        '{"agent_id":"é","evidence":{"inputs":{"m":{"sha256":"s"}}},'
        '"f":500.0,"i":3,"n":1e-05}'
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == (
        "6d16c62a5fe313912dc1782b555332c793c2085b79044b09e51ca9631096bb44"
    )
    assert scorecard_sha256(literal) == (
        "6d16c62a5fe313912dc1782b555332c793c2085b79044b09e51ca9631096bb44"
    )


def test_read_only_manifest_produces_valid_not_exposed_scorecard() -> None:
    manifest = parse_manifest(
        {
            "agent_id": "reader",
            "framework": "test",
            "owner": "owner",
            "model": "model",
            "autonomy": "autonomous",
            "data_classes": [],
            "volume": {"sessions_per_month": 0},
            "tools": [{"name": "lookup", "effect": "read"}],
            "controls": [],
        }
    )
    scorecard = build_scorecard(manifest, TraceData((), ()), exam_present=False)
    Draft202012Validator(_schema("agent-scorecard.v1.schema.json")).validate(scorecard)

    assert scorecard["agent_grade"] is None
    assert {peril["status"] for peril in scorecard["perils"]} == {"not_exposed"}
    assert set(scorecard["evidence"]["inputs"]) == {"battery", "framework"}


def test_tampered_scorecard_hash_declines_without_schedule() -> None:
    scorecard = json.loads((FIXTURE_DIR / "kyc-scorecard.json").read_text())
    scorecard["evidence"]["scorecard_sha256"] = "0" * 64
    connection = connect(":memory:")
    try:
        quote = PolicyService(connection, load_agent_ops_product()).quote(
            {
                "scorecard": scorecard,
                "sessions_per_month": scorecard["perils"][0]["exposure"][
                    "sessions_per_month"
                ],
            },
            "2026-10-01",
        )
    finally:
        connection.close()
    output = {"quote": quote}

    assert quote["status"] == "declined"
    assert "schedule" not in quote["rating"]
    _quote_validator().validate(output)


def test_agent_evidence_cli_writes_schema_valid_golden_equivalent(
    tmp_path: Path,
) -> None:
    exams = json.loads((FIXTURE_DIR / "kyc-exam-results.json").read_text())
    promptfoo_path = tmp_path / "promptfoo.json"
    promptfoo_path.write_text(
        json.dumps(
            {
                "results": {
                    "results": [
                        {
                            "success": item["passed"],
                            "metadata": {
                                **item["metadata"],
                                "case_id": item["case_id"],
                                "peril": item["peril"],
                            },
                        }
                        for item in exams
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "nested" / "evidence.json"
    main(
        [
            "agent-evidence",
            "--manifest",
            str(MANIFEST_PATH),
            "--traces",
            str(FIXTURE_DIR / "kyc-traces.jsonl"),
            "--exam",
            str(promptfoo_path),
            "--out",
            str(output_path),
        ]
    )
    result = json.loads(output_path.read_text(encoding="utf-8"))
    golden = json.loads((FIXTURE_DIR / "kyc-evidence.json").read_text())

    Draft202012Validator(_schema("agent-evidence.v1.schema.json")).validate(result)
    assert result == golden
