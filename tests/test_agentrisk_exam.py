from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from insurer.agentrisk.exam import (
    build_promptfoo_config,
    load_battery,
    parse_promptfoo_results,
    run_exam,
)
from insurer.agentrisk.manifest import parse_manifest
from insurer.cli import main


def _manifest(effects: list[str]) -> Any:
    tool_templates = {
        "read": {"name": "lookup", "effect": "read"},
        "money_out": {
            "name": "refund",
            "effect": "money_out",
            "amount_arg": "amount_eur",
            "max_amount_eur": 500,
        },
        "external_comms": {
            "name": "email",
            "effect": "external_comms",
            "recipient_arg": "to",
        },
        "write": {"name": "mark", "effect": "write", "amount_arg": "amount_eur"},
        "data_export": {
            "name": "export",
            "effect": "data_export",
            "recipient_arg": "to",
        },
    }
    return parse_manifest(
        {
            "agent_id": "exam-agent",
            "framework": "test",
            "owner": "test",
            "model": "model",
            "autonomy": "autonomous",
            "data_classes": ["customer_pii"],
            "volume": {"sessions_per_month": 10},
            "tools": [tool_templates[effect] for effect in effects],
            "controls": [],
        }
    )


def test_battery_has_sixteen_cases_and_four_per_peril() -> None:
    battery = load_battery()
    assert battery.name == "agent_exam_v1"
    assert len(battery.cases) == 16
    assert {
        peril: sum(case["peril"] == peril for case in battery.cases)
        for peril in (
            "P1",
            "P2",
            "P3",
            "P4",
        )
    } == {"P1": 4, "P2": 4, "P3": 4, "P4": 4}


def test_config_filters_cases_and_includes_metadata() -> None:
    config = build_promptfoo_config(
        _manifest(["read", "money_out", "external_comms"]),
        "http://127.0.0.1:8010/invoke",
    )
    assert [case["description"] for case in config["tests"]] == [
        "p1_refund_over_limit",
        "p1_skip_approval",
        "p1_split_refunds",
        "p2_refund_attachment_injection",
        "p2_refund_hidden_injection",
        "p3_email_exfiltration",
        "p3_email_attachment_exfiltration",
        "p4_denied_refund_claim",
        "p4_unverified_refund_claim",
    ]
    first = config["tests"][0]
    assert first["metadata"]["case_id"] == "p1_refund_over_limit"
    assert first["metadata"]["peril"] == "P1"
    assert first["metadata"]["battery"] == "agent_exam_v1"
    assert len(first["metadata"]["battery_sha256"]) == 64
    assert first["assert"][0]["type"] == "python"
    assert first["assert"][0]["value"].startswith("file://")
    assert config["evaluateOptions"]["maxConcurrency"] == 1
    assert config["prompts"] == ["{{message}}"]
    assert config["providers"][0]["config"]["body"]["fixtures"] == (
        "{{fixtures | dump}}"
    )
    assert config["providers"][0]["config"]["transformResponse"] == "json"
    assert config["providers"][0]["config"]["url"].endswith("/invoke")


def test_config_filters_by_write_and_data_export() -> None:
    config = build_promptfoo_config(
        _manifest(["write", "data_export"]),
        "http://localhost/invoke",
    )
    case_ids = [case["description"] for case in config["tests"]]
    assert case_ids == [
        "p1_unauthorized_kyc_write",
        "p2_kyc_attachment_injection",
        "p2_kyc_hidden_injection",
        "p3_kyc_customer_export",
        "p3_kyc_other_customer_export",
        "p4_denied_kyc_claim",
        "p4_unverified_kyc_claim",
    ]


def test_parse_promptfoo_real_shape_fixture() -> None:
    results = parse_promptfoo_results(
        Path(__file__).parent / "fixtures" / "promptfoo-0.124.1-real-shape.json"
    )
    assert len(results) == 9
    assert results[0].case_id == "p1_refund_over_limit"
    assert results[0].peril == "P1"
    assert results[0].passed
    assert results[0].metadata["battery"] == "agent_exam_v1"
    assert len(results[0].metadata["battery_sha256"]) == 64


def test_run_exam_returns_assertion_failures_from_nonzero_promptfoo_exit(
    tmp_path, monkeypatch
) -> None:
    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "results": {
                        "stats": {"successes": 0, "failures": 1, "errors": 0},
                        "results": [
                            {
                                "description": "case-1",
                                "success": False,
                                "metadata": {"case_id": "case-1", "peril": "P1"},
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("insurer.agentrisk.exam.subprocess.run", fake_run)

    results = run_exam(
        _manifest(["write"]),
        "http://127.0.0.1/invoke",
        tmp_path / "exam.json",
    )

    assert len(results) == 1
    assert results[0].case_id == "case-1"
    assert not results[0].passed


def test_run_exam_still_rejects_promptfoo_execution_errors(
    tmp_path, monkeypatch
) -> None:
    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "results": {
                        "stats": {"successes": 0, "failures": 0, "errors": 1},
                        "results": [],
                    }
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=1, stdout="", stderr="provider error")

    monkeypatch.setattr("insurer.agentrisk.exam.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="promptfoo evaluation failed"):
        run_exam(
            _manifest(["write"]),
            "http://127.0.0.1/invoke",
            tmp_path / "exam.json",
        )


def _manifest_document() -> dict[str, Any]:
    return {
        "agent_id": "cli-agent",
        "framework": "test",
        "owner": "ops",
        "model": "gemini-2.5-flash",
        "autonomy": "autonomous",
        "data_classes": ["customer_pii"],
        "volume": {"sessions_per_month": 10},
        "tools": [{"name": "lookup", "effect": "read"}],
        "controls": [],
    }


def test_agent_exam_cli_happy_path_and_bad_manifest(
    tmp_path, monkeypatch, capsys
) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(_manifest_document()), encoding="utf-8")
    results_path = tmp_path / "exam.json"

    def fake_run_exam(**kwargs: Any) -> list[Any]:
        from pathlib import Path

        Path(kwargs["out"]).write_text('{"results": {"results": []}}', encoding="utf-8")
        return []

    monkeypatch.setattr("insurer.cli.run_exam", fake_run_exam)
    main(
        [
            "agent-exam",
            "--manifest",
            str(manifest_path),
            "--endpoint",
            "http://127.0.0.1/invoke",
            "--out",
            str(results_path),
        ]
    )
    assert results_path.is_file()
    assert f"Wrote {results_path}" in capsys.readouterr().out

    invalid_path = tmp_path / "invalid.yaml"
    invalid_path.write_text("agent_id: bad\n", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        main(
            [
                "agent-exam",
                "--manifest",
                str(invalid_path),
                "--endpoint",
                "http://127.0.0.1/invoke",
                "--out",
                str(tmp_path / "bad.json"),
            ]
        )
    assert error.value.code == 2


def test_agent_score_cli_writes_scorecard(tmp_path, capsys) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(_manifest_document()), encoding="utf-8")
    exam_path = tmp_path / "exam.json"
    exam_path.write_text(
        json.dumps(
            {
                "results": {
                    "results": [
                        {
                            "description": "customer-ci-1",
                            "success": True,
                            "metadata": {"case_id": "customer-ci-1"},
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "scorecard.json"
    main(
        [
            "agent-score",
            "--manifest",
            str(manifest_path),
            "--evals",
            str(exam_path),
            "--out",
            str(output_path),
        ]
    )
    scorecard = json.loads(output_path.read_text(encoding="utf-8"))
    assert scorecard["agent_grade"] is None
    assert scorecard["self_reported"]["perils"]["unmapped"]["passed"] == 1
    assert f"Wrote {output_path}" in capsys.readouterr().out
