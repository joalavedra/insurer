from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from insurer.api import create_app


def test_api_quote_bind_claim_and_trial_balance_happy_path(profile):
    client = TestClient(create_app(":memory:"))
    quote_response = client.post(
        "/quotes", json={"profile": profile, "start_date": "2027-01-01"}
    )
    assert quote_response.status_code == 200
    quote = quote_response.json()
    assert quote["status"] == "quoted"
    assert quote["rating"]["steps"]

    policy_response = client.post("/policies", json={"quote_id": quote["quote_id"]})
    assert policy_response.status_code == 200
    policy_id = policy_response.json()["policy_id"]

    claim_response = client.post(
        "/claims",
        json={
            "policy_id": policy_id,
            "cause": "unauthorized_purchase",
            "loss_date": "2027-03-01",
            "notified_date": "2027-03-02",
            "purchase_id": "api-tx-1",
            "claimed_cents": 9000,
            "evidence": [
                {
                    "ts": "2027-03-01T11:00:00Z",
                    "type": "kill_switch_state",
                    "state": True,
                },
                {
                    "ts": "2027-03-01T12:00:00Z",
                    "type": "purchase",
                    "purchase_id": "api-tx-1",
                    "amount_cents": 9000,
                },
            ],
        },
    )
    assert claim_response.status_code == 200
    assert claim_response.json()["decision"]["decision"] == "approve"
    trial_balance = client.get("/ledger/trial-balance").json()
    assert sum(row["debit_cents"] for row in trial_balance) == sum(
        row["credit_cents"] for row in trial_balance
    )


def test_api_returns_declined_quote(profile):
    client = TestClient(create_app(":memory:"))
    response = client.post(
        "/quotes",
        json={"profile": profile | {"kill_switch": False}, "start_date": "2027-01-01"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "declined"
    assert "W1" in response.json()["reason"]


def test_backdated_endorse_and_cancel_return_bad_request(profile):
    app = create_app(":memory:")
    with TestClient(app) as client:
        quote = client.post(
            "/quotes", json={"profile": profile, "start_date": "2027-01-01"}
        ).json()
        policy_id = client.post(
            "/policies", json={"quote_id": quote["quote_id"]}
        ).json()["policy_id"]
        app.state.policies.earn(policy_id, "2027-04-11")

        endorse_response = client.post(
            f"/policies/{policy_id}/endorse",
            json={
                "profile_changes": {"approval_threshold": "none"},
                "effective_date": "2027-02-20",
            },
        )
        cancel_response = client.post(
            f"/policies/{policy_id}/cancel",
            json={"effective_date": "2027-02-20"},
        )

    assert endorse_response.status_code == 400
    assert cancel_response.status_code == 400
    assert endorse_response.json()["detail"] == "cannot be backdated before 2027-04-11"
    assert cancel_response.json()["detail"] == "cannot be backdated before 2027-04-11"


def test_api_rejects_invalid_quote_profile_and_date(profile):
    client = TestClient(create_app(":memory:"))
    incomplete_profile = client.post(
        "/quotes",
        json={"profile": {"kill_switch": True}, "start_date": "2027-01-01"},
    )
    invalid_date = client.post(
        "/quotes", json={"profile": profile, "start_date": "not-a-date"}
    )

    assert incomplete_profile.status_code == 422
    assert invalid_date.status_code == 422


def test_api_rejects_invalid_claim_evidence_and_profile_changes(profile):
    client = TestClient(create_app(":memory:"))
    claim_payload = {
        "policy_id": "P-00000001",
        "cause": "unauthorized_purchase",
        "loss_date": "2027-01-02",
        "notified_date": "2027-01-03",
        "purchase_id": "tx-api",
        "claimed_cents": 1000,
        "evidence": [{"ts": "2027-01-02T12:00:00Z"}],
    }
    missing_type = client.post("/claims", json=claim_payload)
    invalid_timestamp = client.post(
        "/claims",
        json={
            **claim_payload,
            "evidence": [{"type": "purchase", "ts": "not-a-date"}],
        },
    )
    negative_claim_amount = client.post(
        "/claims", json={**claim_payload, "claimed_cents": -1}
    )
    unknown_profile_change = client.post(
        "/policies/P-00000001/endorse",
        json={
            "profile_changes": {"unknown_setting": True},
            "effective_date": "2027-02-01",
        },
    )

    assert missing_type.status_code == 422
    assert invalid_timestamp.status_code == 422
    assert negative_claim_amount.status_code == 422
    assert unknown_profile_change.status_code == 422


def test_concurrent_quote_requests_allocate_unique_ids(profile):
    with TestClient(create_app(":memory:")) as client:
        with ThreadPoolExecutor(max_workers=20) as executor:
            responses = list(
                executor.map(
                    lambda _: client.post(
                        "/quotes",
                        json={"profile": profile, "start_date": "2027-01-01"},
                    ),
                    range(20),
                )
            )

    assert [response.status_code for response in responses] == [200] * 20
    quote_ids = [response.json()["quote_id"] for response in responses]
    assert len(set(quote_ids)) == 20
