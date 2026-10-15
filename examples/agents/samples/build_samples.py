from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_DIR = Path("/home/ubuntu/insurance/agent-risk-run")
SAMPLES_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from insurer.agentrisk.contracts import parse_normalized_evidence  # noqa: E402
from insurer.agentrisk.manifest import parse_manifest  # noqa: E402
from insurer.agentrisk.service import score_from_evidence  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _relative_input(path: str, run_dir: Path) -> str:
    source = Path(path)
    if not source.is_absolute():
        source = run_dir / source
    return os.path.relpath(source, run_dir)


def _agent_evidence(
    run_dir: Path,
    manifest_path: Path,
    trace_paths: list[str],
    exam_path: str | None,
    eval_paths: list[str],
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "insurer.cli",
        "agent-evidence",
        "--manifest",
        os.path.relpath(manifest_path, run_dir),
    ]
    for path in trace_paths:
        command.extend(["--traces", path])
    if exam_path is not None:
        command.extend(["--exam", exam_path])
    for path in eval_paths:
        command.extend(["--evals", path])

    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), environment.get("PYTHONPATH", "")]
    )
    with tempfile.TemporaryDirectory(prefix=".agent-ops-sample-", dir=run_dir) as temp:
        output_path = Path(temp) / "evidence.json"
        command.extend(["--out", str(output_path)])
        subprocess.run(
            command,
            check=True,
            cwd=run_dir,
            env=environment,
            text=True,
        )
        return _read_json(output_path)


def _verify_grades(
    sample: dict[str, Any], scorecard_path: Path, variant: str
) -> dict[str, str | None]:
    manifest = parse_manifest(sample["manifest"])
    evidence = parse_normalized_evidence(sample["evidence"])
    actual_scorecard = score_from_evidence(manifest, evidence)
    stored_scorecard = _read_json(scorecard_path)
    actual = {peril["id"]: peril["grade"] for peril in actual_scorecard["perils"]}
    expected = {peril["id"]: peril["grade"] for peril in stored_scorecard["perils"]}
    if actual != expected or actual_scorecard["agent_grade"] != stored_scorecard.get(
        "agent_grade"
    ):
        raise ValueError(
            f"{variant} sample grades do not match {scorecard_path}: "
            f"actual={actual_scorecard['agent_grade']} {actual}, "
            f"expected={stored_scorecard.get('agent_grade')} {expected}"
        )
    if variant == "kyc-hardened" and set(actual.values()) != {"A"}:
        raise ValueError(f"{variant} must score all A, got {actual}")
    if variant == "kyc-weak" and "E" not in actual.values():
        raise ValueError(f"{variant} must include an E peril, got {actual}")
    return actual


def build_samples(
    run_dir: Path = RUN_DIR, samples_dir: Path = SAMPLES_DIR
) -> dict[str, dict[str, Any]]:
    run_dir = Path(run_dir)
    samples_dir = Path(samples_dir)
    manifest_path = REPO_ROOT / "examples/agents/manifests/kyc_bot.yaml"
    golden = {
        "id": "kyc-golden",
        "name": "KYC bot · golden",
        "description": "Hardened KYC bot with one data-export leak (P3 excluded).",
        "manifest": yaml.safe_load(manifest_path.read_text(encoding="utf-8")),
        "evidence": _read_json(
            REPO_ROOT / "tests/fixtures/contracts/kyc-evidence.json"
        ),
    }
    samples = {"kyc-golden": golden}

    if not run_dir.is_dir():
        print(f"Skipping v4/v4b samples; run directory not found: {run_dir}")
    else:
        for variant in ("kyc-hardened", "kyc-weak"):
            scorecard_path = run_dir / "v4b" / f"{variant}-scorecard.json"
            stored_scorecard = _read_json(scorecard_path)
            inputs = stored_scorecard["evidence"]["inputs"]
            manifest_input = inputs["manifest"]["path"]
            source_manifest = (
                Path(manifest_input)
                if Path(manifest_input).is_absolute()
                else REPO_ROOT / manifest_input
            )
            print(f"{variant} manifest: {manifest_input}")
            trace_paths = [
                _relative_input(item["path"], run_dir) for item in inputs["traces"]
            ]
            exam_input = inputs["exam_results"]
            exam_path = (
                _relative_input(exam_input["path"], run_dir)
                if exam_input is not None
                else None
            )
            eval_paths = [
                _relative_input(item["path"], run_dir) for item in inputs["ci_evals"]
            ]
            sample = {
                "id": variant,
                "name": "KYC bot · hardened"
                if variant.endswith("hardened")
                else "KYC bot · weak",
                "description": (
                    "Hardened KYC bot scored from v4 traces and v4b exam results."
                    if variant.endswith("hardened")
                    else "Weak KYC bot scored from v4 traces and v4b exam results."
                ),
                "manifest": yaml.safe_load(source_manifest.read_text(encoding="utf-8")),
                "evidence": _agent_evidence(
                    run_dir,
                    source_manifest,
                    trace_paths,
                    exam_path,
                    eval_paths,
                ),
            }
            grades = _verify_grades(sample, scorecard_path, variant)
            print(f"{variant} grades: {grades}")
            samples[variant] = sample

    samples_dir.mkdir(parents=True, exist_ok=True)
    for sample_id, sample in samples.items():
        output_path = samples_dir / f"{sample_id}.json"
        output_path.write_text(
            json.dumps(sample, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"Wrote {output_path}")
    return samples


if __name__ == "__main__":
    build_samples()
