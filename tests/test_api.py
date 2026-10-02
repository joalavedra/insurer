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
