import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from insurer.agentrisk.contracts import parse_normalized_evidence
from insurer.api import close_app_database, create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "examples/agents/manifests/kyc_bot.yaml"
EVIDENCE_PATH = REPO_ROOT / "tests/fixtures/contracts/kyc-evidence.json"
SCORECARD_PATH = REPO_ROOT / "tests/fixtures/contracts/kyc-scorecard.json"
TRACES_PATH = REPO_ROOT / "tests/fixtures/contracts/kyc-traces.jsonl"


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "agent-ops.sqlite"))
    with TestClient(app) as test_client:
        yield test_client
    close_app_database(app)


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


def _evidence() -> dict:
    return json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))


def _scorecard() -> dict:
    return json.loads(SCORECARD_PATH.read_text(encoding="utf-8"))


def _bound_policy(client: TestClient) -> dict:
    quote_response = client.post(
        "/agent-ops/quotes",
        json={"scorecard": _scorecard(), "start_date": "2026-10-01"},
    )
    assert quote_response.status_code == 200
    assert quote_response.json()["status"] == "quoted"
    return client.post(
        "/agent-ops/policies",
        json={"quote_id": quote_response.json()["quote_id"]},
    ).json()


def _live_status(
    client: TestClient, policy_id: str, manifest: dict, *, file_claims: bool = False
):
    return client.post(
        f"/agent-ops/policies/{policy_id}/live",
        json={"manifest": manifest, "file_claims": file_claims},
    )


def test_agent_ops_score_matches_golden_including_hash(client):
    response = client.post(
        "/agent-ops/score",
        json={"manifest": _manifest(), "evidence": _evidence()},
    )

    assert response.status_code == 200
    assert response.json() == _scorecard()


def test_agent_ops_score_registers_and_updates_agent(client):
    manifest = _manifest()
    request = {"manifest": manifest, "evidence": _evidence()}

    first_score = client.post("/agent-ops/score", json=request)

    assert first_score.status_code == 200
    first = client.get(f"/agent-ops/agents/{manifest['agent_id']}").json()
    assert first["manifest"] == manifest
    assert first["policy_id"] is None

    manifest["model"] = "updated-model"
    updated_score = client.post(
        "/agent-ops/score", json={"manifest": manifest, "evidence": _evidence()}
    )

    assert updated_score.status_code == 200
    updated = client.get(f"/agent-ops/agents/{manifest['agent_id']}").json()
    assert updated["manifest"] == manifest
    assert updated["updated_at"] > first["updated_at"]


def test_agent_ops_agents_list_policy_and_unknown_agent(client):
    manifest = _manifest()
    scored = client.post(
        "/agent-ops/score", json={"manifest": manifest, "evidence": _evidence()}
    )
    assert scored.status_code == 200
    other_manifest = _manifest()
    other_manifest["agent_id"] = "a-second-agent"
    other_scored = client.post(
        "/agent-ops/score",
        json={"manifest": other_manifest, "evidence": _evidence()},
    )
    assert other_scored.status_code == 200

    policy = _bound_policy(client)
    later_quote = client.post(
        "/agent-ops/quotes",
        json={"scorecard": _scorecard(), "start_date": "2026-10-01"},
    ).json()
    latest_policy = client.post(
        "/agent-ops/policies", json={"quote_id": later_quote["quote_id"]}
    ).json()

    response = client.get("/agent-ops/agents")
    assert response.status_code == 200
    agents = response.json()
    assert [agent["agent_id"] for agent in agents] == [
        "a-second-agent",
        manifest["agent_id"],
    ]
    assert agents[0]["policy_id"] is None
    assert agents[1]["policy_id"] == latest_policy["policy_id"]
    assert agents[1]["policy_id"] != policy["policy_id"]
    assert (
        client.get(f"/agent-ops/agents/{manifest['agent_id']}").json()["policy_id"]
        == latest_policy["policy_id"]
    )
    assert client.get("/agent-ops/agents/unknown").status_code == 404


def test_agent_ops_agents_exclude_future_policies(client):
    manifest = _manifest()
    scored = client.post(
        "/agent-ops/score", json={"manifest": manifest, "evidence": _evidence()}
    )
    assert scored.status_code == 200
    today = datetime.now(timezone.utc).date()

    def bind(start_date):
        quote = client.post(
            "/agent-ops/quotes",
            json={"scorecard": _scorecard(), "start_date": start_date.isoformat()},
        )
        assert quote.status_code == 200
        return client.post(
            "/agent-ops/policies",
            json={"quote_id": quote.json()["quote_id"]},
        ).json()

    active_policy = bind(today)
    future_policy = bind(today + timedelta(days=30))

    agents = client.get("/agent-ops/agents").json()
    agent = next(item for item in agents if item["agent_id"] == manifest["agent_id"])
    assert agent["policy_id"] == active_policy["policy_id"]
    assert agent["policy_id"] != future_policy["policy_id"]


def test_agent_ops_live_uses_registered_manifest(client, tmp_path, monkeypatch):
    manifest = _manifest()
    scored = client.post(
        "/agent-ops/score", json={"manifest": manifest, "evidence": _evidence()}
    )
    assert scored.status_code == 200
    policy = _bound_policy(client)
    monkeypatch.setenv("INSURER_TRACE_DIR", str(tmp_path / "empty"))

    response = client.post(f"/agent-ops/policies/{policy['policy_id']}/live", json={})

    assert response.status_code == 200
    assert response.json()["agent_id"] == manifest["agent_id"]
    assert response.json()["trace_files"] == 0


def test_agent_ops_live_without_registered_manifest_is_not_found(client):
    policy = _bound_policy(client)

    response = client.post(f"/agent-ops/policies/{policy['policy_id']}/live", json={})

    assert response.status_code == 404


def test_agent_ops_quote_covers_expected_perils(client):
    response = client.post(
        "/agent-ops/quotes",
        json={"scorecard": _scorecard(), "start_date": "2026-10-01"},
    )

    assert response.status_code == 200
    quote = response.json()
    assert quote["status"] == "quoted"
    assert quote["rating"]["schedule"]["covered_perils"] == ["P1", "P2", "P4"]


def test_agent_ops_bind_and_get_policy_starts_with_no_claims(client):
    bound = _bound_policy(client)

    response = client.get(f"/agent-ops/policies/{bound['policy_id']}")

    assert response.status_code == 200
    assert response.json()["versions"]
    assert response.json()["claims"] == []


def test_agent_ops_claim_dry_run_lists_evil_export(client):
    policy = _bound_policy(client)
    evidence = _evidence()
    parsed = parse_normalized_evidence(evidence)
    evil_call = next(
        call
        for call in parsed.trace_data.tool_calls
        if call.tool == "export_customer_data"
        and call.args.get("recipient", "").endswith("evil.test")
    )

    response = client.post(
        f"/agent-ops/policies/{policy['policy_id']}/claims",
        json={
            "manifest": _manifest(),
            "evidence": evidence,
            "notified_date": "2026-10-20",
            "dry_run": True,
        },
    )

    assert response.status_code == 200
    assert any(
        claim["span_id"] == evil_call.span_id and claim["decision"] == "dry_run"
        for claim in response.json()
    )


def test_agent_ops_claim_files_and_appears_on_policy(client):
    policy = _bound_policy(client)

    request = {
        "manifest": _manifest(),
        "evidence": _evidence(),
        "notified_date": "2026-10-20",
    }
    response = client.post(
        f"/agent-ops/policies/{policy['policy_id']}/claims",
        json=request,
    )

    assert response.status_code == 200
    assert len(response.json()) == 1
    duplicate = client.post(
        f"/agent-ops/policies/{policy['policy_id']}/claims", json=request
    )
    assert duplicate.status_code == 200
    assert duplicate.json() == []
    claims = client.get(f"/agent-ops/policies/{policy['policy_id']}").json()["claims"]
    assert len(claims) == 1
    assert claims[0]["id"] == response.json()[0]["claim_id"]


def test_agent_ops_live_empty_store_has_zero_counts_and_ok_warranties(
    client, tmp_path, monkeypatch
):
    policy = _bound_policy(client)
    monkeypatch.setenv("INSURER_TRACE_DIR", str(tmp_path / "empty"))

    response = _live_status(client, policy["policy_id"], _manifest())

    assert response.status_code == 200
    live = response.json()
    assert live["trace_files"] == 0
    assert live["tool_calls"] == 0
    assert live["guardrail_checks"] == 0
    assert live["last_seen"] is None
    assert live["incidents"] == []
    assert live["filed"] == []
    assert all(warranty["status"] == "ok" for warranty in live["warranties"])


def test_agent_ops_live_reads_traces_and_files_incidents_once(
    client, tmp_path, monkeypatch
):
    policy = _bound_policy(client)
    manifest = _manifest()
    trace_dir = tmp_path / "traces" / manifest["agent_id"]
    trace_dir.mkdir(parents=True)
    (trace_dir / "2026-10-14.jsonl").write_bytes(TRACES_PATH.read_bytes())
    monkeypatch.setenv("INSURER_TRACE_DIR", str(tmp_path / "traces"))

    detected = _live_status(client, policy["policy_id"], manifest).json()
    assert detected["trace_files"] == 1
    assert detected["tool_calls"] > 0
    assert detected["guardrail_checks"] > 0
    assert detected["last_seen"].startswith("2026-10-14T")
    assert detected["incidents"]
    assert all(not incident["filed"] for incident in detected["incidents"])

    filed = _live_status(client, policy["policy_id"], manifest, file_claims=True).json()
    assert len(filed["filed"]) == len(detected["incidents"])
    assert all(incident["filed"] for incident in filed["incidents"])

    repeated = _live_status(
        client, policy["policy_id"], manifest, file_claims=True
    ).json()
    assert repeated["filed"] == []
    assert all(incident["filed"] for incident in repeated["incidents"])
    policy_claims = client.get(f"/agent-ops/policies/{policy['policy_id']}").json()[
        "claims"
    ]
    assert len(policy_claims) == len(detected["incidents"])


def test_agent_ops_live_marks_removed_warranty_missing(client, tmp_path, monkeypatch):
    policy = _bound_policy(client)
    manifest = _manifest()
    warranties = client.get(f"/agent-ops/policies/{policy['policy_id']}").json()[
        "warranted_controls"
    ]
    manifest["controls"] = [
        control for control in manifest["controls"] if control["id"] != warranties[0]
    ]
    monkeypatch.setenv("INSURER_TRACE_DIR", str(tmp_path / "empty"))

    response = _live_status(client, policy["policy_id"], manifest)

    assert response.status_code == 200
    missing = next(
        warranty
        for warranty in response.json()["warranties"]
        if warranty["control_id"] == warranties[0]
    )
    assert missing["status"] == "missing"


def test_agent_ops_live_rejects_mismatched_or_invalid_agent_id(
    client, tmp_path, monkeypatch
):
    policy = _bound_policy(client)
    monkeypatch.setenv("INSURER_TRACE_DIR", str(tmp_path / "traces"))
    for agent_id in ("another-agent", "invalid/agent"):
        manifest = _manifest()
        manifest["agent_id"] = agent_id

        response = _live_status(client, policy["policy_id"], manifest)

        assert response.status_code == 400


def test_agent_ops_tampered_hash_declines_quote_without_schedule(client):
    scorecard = _scorecard()
    scorecard["evidence"]["scorecard_sha256"] = "0" * 64

    response = client.post(
        "/agent-ops/quotes",
        json={"scorecard": scorecard, "start_date": "2026-10-01"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "declined"
    assert "schedule" not in response.json()["rating"]


def test_agent_ops_malformed_evidence_is_bad_request(client):
    response = client.post(
        "/agent-ops/score",
        json={"manifest": _manifest(), "evidence": {"version": "agent-evidence.v1"}},
    )

    assert response.status_code == 400


def test_agent_ops_unknown_policy_is_not_found(client):
    response = client.get("/agent-ops/policies/unknown")

    assert response.status_code == 404


def test_agent_ops_samples_each_score(client):
    response = client.get("/agent-ops/samples")

    assert response.status_code == 200
    samples = response.json()
    assert len(samples) >= 3
    large_start_ns: list[int] = []
    for sample in samples:
        sample_path = (
            REPO_ROOT / "examples" / "agents" / "samples" / f"{sample['id']}.json"
        )
        file_evidence = json.loads(sample_path.read_text(encoding="utf-8"))["evidence"]
        assert json.loads(sample["evidence_json"]) == file_evidence
        assert sample["evidence_json"] == json.dumps(file_evidence, ensure_ascii=False)

        evidence = json.loads(sample["evidence_json"])
        large_start_ns.extend(
            value["start_ns"]
            for collection in ("tool_calls", "guardrail_checks")
            for value in evidence[collection]
        )
        score = client.post(
            "/agent-ops/score",
            json={"manifest": sample["manifest"], "evidence": sample["evidence"]},
        )
        assert score.status_code == 200, sample["id"]
    assert any(value > 2**53 and value % 256 != 0 for value in large_start_ns)
