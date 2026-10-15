import json
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


def test_agent_ops_score_matches_golden_including_hash(client):
    response = client.post(
        "/agent-ops/score",
        json={"manifest": _manifest(), "evidence": _evidence()},
    )

    assert response.status_code == 200
    assert response.json() == _scorecard()


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

    response = client.post(
        f"/agent-ops/policies/{policy['policy_id']}/claims",
        json={
            "manifest": _manifest(),
            "evidence": _evidence(),
            "notified_date": "2026-10-20",
        },
    )

    assert response.status_code == 200
    assert len(response.json()) == 1
    claims = client.get(f"/agent-ops/policies/{policy['policy_id']}").json()["claims"]
    assert len(claims) == 1
    assert claims[0]["id"] == response.json()[0]["claim_id"]


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
    for sample in samples:
        score = client.post(
            "/agent-ops/score",
            json={"manifest": sample["manifest"], "evidence": sample["evidence"]},
        )
        assert score.status_code == 200, sample["id"]
