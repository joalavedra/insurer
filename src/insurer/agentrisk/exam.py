from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from insurer.agentrisk.manifest import AgentManifest


@dataclass(frozen=True)
class ExamCaseResult:
    case_id: str
    peril: str
    passed: bool
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ExamBattery:
    name: str
    cases: tuple[dict[str, Any], ...]
    sha256: str


def default_battery_path() -> Path:
    return Path(__file__).with_name("exams") / "agent_exam_v1.yaml"


def load_battery(path: str | Path | None = None) -> ExamBattery:
    battery_path = Path(path) if path is not None else default_battery_path()
    try:
        contents = battery_path.read_bytes()
        document = yaml.safe_load(contents)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not load exam battery {battery_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("exam battery must be a mapping")
    name = document.get("name")
    cases = document.get("cases")
    if not isinstance(name, str) or not name:
        raise ValueError("exam battery.name must be a non-empty string")
    if not isinstance(cases, list):
        raise ValueError("exam battery.cases must be a list")
    ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        label = f"exam battery.cases[{index}]"
        if not isinstance(case, dict):
            raise ValueError(f"{label} must be a mapping")
        for field in ("id", "peril", "applies_to_effects", "message", "assert"):
            if field not in case:
                raise ValueError(f"{label} is missing required field {field!r}")
        case_id = case["id"]
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"{label}.id must be a non-empty string")
        if case_id in ids:
            raise ValueError(f"duplicate exam case id: {case_id}")
        ids.add(case_id)
        if not isinstance(case["peril"], str) or not case["peril"]:
            raise ValueError(f"{label}.peril must be a non-empty string")
        if not isinstance(case["applies_to_effects"], list) or any(
            not isinstance(effect, str) for effect in case["applies_to_effects"]
        ):
            raise ValueError(f"{label}.applies_to_effects must be a list of strings")
        if not isinstance(case["message"], str):
            raise ValueError(f"{label}.message must be a string")
        if not isinstance(case["assert"], list) or any(
            not isinstance(assertion, dict) for assertion in case["assert"]
        ):
            raise ValueError(f"{label}.assert must be a list of mappings")
        fixtures = case.get("fixtures", {})
        if not isinstance(fixtures, dict):
            raise ValueError(f"{label}.fixtures must be a mapping")
        validated.append(case)
    return ExamBattery(
        name=name,
        cases=tuple(validated),
        sha256=hashlib.sha256(contents).hexdigest(),
    )


def manifest_dict(manifest: AgentManifest) -> dict[str, Any]:
    return {
        "agent_id": manifest.agent_id,
        "framework": manifest.framework,
        "owner": manifest.owner,
        "model": manifest.model,
        "autonomy": manifest.autonomy,
        "data_classes": list(manifest.data_classes),
        "volume": {"sessions_per_month": manifest.sessions_per_month},
        "tools": [
            {
                "name": tool.name,
                "effect": tool.effect,
                "untrusted_input": tool.untrusted_input,
                **(
                    {"amount_arg": tool.amount_arg}
                    if tool.amount_arg is not None
                    else {}
                ),
                **(
                    {"max_amount_eur": tool.max_amount_eur}
                    if tool.max_amount_eur is not None
                    else {}
                ),
                **(
                    {"recipient_arg": tool.recipient_arg}
                    if tool.recipient_arg is not None
                    else {}
                ),
            }
            for tool in manifest.tools
        ],
        "controls": [
            {
                "id": control.id,
                "type": control.type,
                **({"tool": control.tool} if control.tool is not None else {}),
                **(
                    {"approval_tool": control.approval_tool}
                    if control.approval_tool is not None
                    else {}
                ),
                **(
                    {"threshold_eur": control.threshold_eur}
                    if control.threshold_eur is not None
                    else {}
                ),
                **({"max_eur": control.max_eur} if control.max_eur is not None else {}),
                **(
                    {"allowed_domains": list(control.allowed_domains)}
                    if control.allowed_domains
                    else {}
                ),
            }
            for control in manifest.controls
        ],
    }


def build_promptfoo_config(
    manifest: AgentManifest,
    endpoint: str,
    battery_path: str | Path | None = None,
) -> dict[str, Any]:
    battery = load_battery(battery_path)
    effects = {tool.effect for tool in manifest.tools}
    assertion_uri = Path(__file__).with_name("assertions.py").resolve().as_uri()
    tests: list[dict[str, Any]] = []
    for case in battery.cases:
        if not effects.intersection(case["applies_to_effects"]):
            continue
        tests.append(
            {
                "description": case["id"],
                "vars": {
                    "message": case["message"],
                    "session_id": f"exam-{case['id']}",
                    "fixtures": case.get("fixtures", {}),
                    "insurer_assertions": case["assert"],
                    "insurer_manifest": manifest_dict(manifest),
                },
                "metadata": {
                    "case_id": case["id"],
                    "peril": case["peril"],
                    "battery": battery.name,
                    "battery_sha256": battery.sha256,
                },
                "assert": [{"type": "python", "value": assertion_uri}],
            }
        )
    return {
        "description": f"Insurer-owned {battery.name}",
        "evaluateOptions": {"maxConcurrency": 1},
        "prompts": ["{{message}}"],
        "providers": [
            {
                "id": "http",
                "config": {
                    "url": endpoint,
                    "method": "POST",
                    "headers": {"Content-Type": "application/json"},
                    "body": {
                        "message": "{{message}}",
                        "session_id": "{{session_id}}",
                        "fixtures": "{{fixtures | dump}}",
                    },
                    "transformResponse": "json",
                },
            }
        ],
        "tests": tests,
    }


def write_promptfoo_config(config: dict[str, Any], path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return output


def parse_promptfoo_results(path: str | Path) -> list[ExamCaseResult]:
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read promptfoo results {source}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("promptfoo results must be a JSON object")
    results: Any = document.get("results")
    if isinstance(results, dict):
        results = results.get("results")
    if not isinstance(results, list):
        raise ValueError("promptfoo results JSON has no results array")
    parsed: list[ExamCaseResult] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise ValueError(f"promptfoo result {index} must be an object")
        grading = result.get("gradingResult")
        if isinstance(result.get("success"), bool):
            passed = result["success"]
        elif isinstance(grading, dict) and isinstance(grading.get("pass"), bool):
            passed = grading["pass"]
        elif isinstance(result.get("pass"), bool):
            passed = result["pass"]
        else:
            raise ValueError(f"promptfoo result {index} has no pass/fail status")
        metadata = result.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        test = result.get("test")
        if isinstance(test, dict) and isinstance(test.get("metadata"), dict):
            metadata = {**test["metadata"], **metadata}
        case_id = metadata.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            case_id = str(result.get("description") or f"case-{index + 1}")
        peril = metadata.get("peril")
        parsed.append(
            ExamCaseResult(
                case_id=case_id,
                peril=peril if isinstance(peril, str) and peril else "unmapped",
                passed=passed,
                metadata=metadata,
            )
        )
    return parsed


def run_exam(
    manifest: AgentManifest,
    endpoint: str,
    out: str | Path,
    battery_path: str | Path | None = None,
    config_out: str | Path | None = None,
) -> list[ExamCaseResult]:
    output = Path(out)
    output.parent.mkdir(parents=True, exist_ok=True)
    config_path = (
        Path(config_out)
        if config_out is not None
        else output.with_suffix(".promptfoo.yaml")
    )
    config = build_promptfoo_config(manifest, endpoint, battery_path)
    write_promptfoo_config(config, config_path)
    env = os.environ.copy()
    env["PROMPTFOO_DISABLE_TELEMETRY"] = "1"
    command = [
        "npx",
        "-y",
        "promptfoo@0.124.1",
        "eval",
        "-c",
        str(config_path.resolve()),
        "-o",
        str(output.resolve()),
        "--max-concurrency",
        "1",
        "--repeat",
        "1",
        "--no-cache",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    run_output = completed.stdout + completed.stderr
    if output.exists():
        run_output += output.read_text(encoding="utf-8")
    if "RESOURCE_EXHAUSTED" in run_output:
        raise RuntimeError(
            "promptfoo received 429 RESOURCE_EXHAUSTED; stopping without retry"
        )
    if completed.returncode != 0:
        try:
            document = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise RuntimeError(
                f"promptfoo evaluation failed:\n{completed.stdout}\n{completed.stderr}"
            ) from None
        evaluation = document.get("results") if isinstance(document, dict) else None
        stats = evaluation.get("stats") if isinstance(evaluation, dict) else None
        if not isinstance(stats, dict) or stats.get("errors") != 0:
            raise RuntimeError(
                f"promptfoo evaluation failed:\n{completed.stdout}\n{completed.stderr}"
            )
    return parse_promptfoo_results(output)
