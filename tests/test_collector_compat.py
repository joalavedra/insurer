import json
from pathlib import Path

from insurer.agentrisk.manifest import load_manifest
from insurer.agentrisk.traces import load_trace_files

ROOT = Path(__file__).resolve().parents[1]
TRACE_FIXTURE = ROOT / "tests" / "fixtures" / "contracts" / "kyc-traces.jsonl"
MANIFEST = ROOT / "examples" / "agents" / "manifests" / "kyc_bot.yaml"


def test_collector_jsonl_is_compatible_with_trace_loader(tmp_path: Path) -> None:
    collector_file = tmp_path / "kyc-bot-demo" / "2026-10-01.jsonl"
    collector_file.parent.mkdir()
    with TRACE_FIXTURE.open(encoding="utf-8") as source:
        with collector_file.open("w", encoding="utf-8") as output:
            for line in source:
                output.write(json.dumps(json.loads(line), separators=(",", ":")) + "\n")

    manifest = load_manifest(MANIFEST)
    assert load_trace_files([collector_file], manifest) == load_trace_files(
        [TRACE_FIXTURE], manifest
    )
