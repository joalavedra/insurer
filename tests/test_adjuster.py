import json

import httpx

from insurer.adjuster import GeminiAdjuster, RulesAdjuster


def policy_for(profile, versions=None):
    return {
        "start_date": "2027-01-01",
        "end_date": "2028-01-01",
        "profile": profile,
        "aggregate_paid_cents": 0,
        "case_reserve_cents": 0,
        "versions": versions
        or [
            {
                "version": 1,
                "effective_from": "2027-01-01",
                "effective_to": "2028-01-01",
                "profile": profile,
                "status": "active",
            }
        ],
    }


def evidence(amount=9000, purchase_id="tx-1"):
    return [
        {
            "ts": "2027-03-01T12:00:00Z",
            "type": "kill_switch_state",
            "state": True,
        },
        {
            "ts": "2027-03-01T12:01:00Z",
            "type": "purchase",
            "purchase_id": purchase_id,
            "amount_cents": amount,
        },
    ]


def run(rules, profile, events=None, **kwargs):
    return rules.adjust(
        policy_for(profile),
        kwargs.pop("cause", "unauthorized_purchase"),
        kwargs.pop("loss_date", "2027-03-01"),
        kwargs.pop("notified_date", "2027-03-02"),
        kwargs.pop("purchase_id", "tx-1"),
        kwargs.pop("claimed_cents", 9000),
        evidence() if events is None else events,
        **kwargs,
    )


def test_rule_step_1_requires_policy_in_force(profile, product):
    rule = RulesAdjuster(product)
    policy = policy_for(
        profile,
        [
            {
                "effective_from": "2027-04-01",
                "effective_to": "2028-01-01",
                "profile": profile,
                "status": "active",
            }
        ],
    )
    decision = rule.adjust(
        policy,
        "unauthorized_purchase",
        "2027-03-01",
        "2027-03-02",
        "tx-1",
        9000,
        evidence(),
    )
    assert decision.decision == "deny"
    assert "not in force" in decision.reason


def test_rule_step_2_denies_uncovered_cause(profile, product):
    decision = run(RulesAdjuster(product), profile, cause="other")
    assert decision.decision == "deny"


def test_rule_step_3_denies_w1_when_last_kill_switch_state_is_false(profile, product):
    events = evidence()
    events.append(
        {"ts": "2027-03-01T12:02:00Z", "type": "kill_switch_state", "state": False}
    )
    decision = run(RulesAdjuster(product), profile, events)
    assert decision.decision == "deny"
    assert decision.clause_ids == ["W1"]


def test_rule_step_4_denies_missing_purchase_evidence(profile, product):
    decision = run(RulesAdjuster(product), profile, [])
    assert decision.decision == "deny"
    assert decision.clause_ids == ["E2"]


def test_rule_step_5_denies_human_approved_purchase(profile, product):
    events = evidence()
    events.append(
        {
            "ts": "2027-03-01T12:03:00Z",
            "type": "approval_granted",
            "purchase_id": "tx-1",
        }
    )
    decision = run(RulesAdjuster(product), profile, events)
    assert decision.decision == "deny"
    assert decision.clause_ids == ["E1"]


def test_rule_step_6_denies_late_notification(profile, product):
    decision = run(RulesAdjuster(product), profile, notified_date="2027-04-01")
    assert decision.decision == "deny"
    assert decision.clause_ids == ["E3"]


def test_rule_step_7_refers_fraud_signals(profile, product):
    decision = run(RulesAdjuster(product), profile, claimed_cents=10000)
    assert decision.decision == "refer"


def test_rule_step_8_approves_amount_capped_by_monthly_spend(profile, product):
    capped_profile = profile | {"monthly_spend_cap_cents": 5000}
    decision = run(RulesAdjuster(product), capped_profile)
    assert decision.decision == "approve"
    assert decision.amount_cents == 5000


def test_rule_step_8_aggregate_exhaustion_caps_at_zero(profile, product):
    policy = policy_for(profile)
    policy["aggregate_paid_cents"] = product.coverage["annual_aggregate_limit_cents"]
    decision = RulesAdjuster(product).adjust(
        policy,
        "unauthorized_purchase",
        "2027-03-01",
        "2027-03-02",
        "tx-1",
        9000,
        evidence(),
    )
    assert decision.decision == "approve"
    assert decision.amount_cents == 0


def gemini_response(decision, amount=0):
    payload = {
        "decision": decision,
        "amount_cents": amount,
        "reason": "The response cites the purchase evidence.",
        "clause_ids": ["C1"] if decision == "approve" else [],
    }
    return httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]},
    )


def gemini_call(profile, response, events=None, **kwargs):
    def handler(request):
        assert request.url.host == "generativelanguage.googleapis.com"
        payload = json.loads(request.content)
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        return response

    adjuster = GeminiAdjuster(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    return adjuster.adjust(
        policy_for(profile),
        kwargs.get("cause", "unauthorized_purchase"),
        kwargs.get("loss_date", "2027-03-01"),
        kwargs.get("notified_date", "2027-03-02"),
        "tx-1",
        kwargs.get("claimed_cents", 9000),
        evidence() if events is None else events,
    )


def test_gemini_disagreement_with_hard_rule_refers(profile):
    events = evidence()
    events.append(
        {"ts": "2027-03-01T12:02:00Z", "type": "kill_switch_state", "state": False}
    )
    decision = gemini_call(profile, gemini_response("approve", 8000), events)
    assert decision.decision == "refer"
    assert decision.adjuster == "gemini"


def test_gemini_amount_is_capped_by_rules(profile):
    decision = gemini_call(profile, gemini_response("approve", 100000))
    assert decision.decision == "approve"
    assert decision.amount_cents == 8000
    assert decision.adjuster == "gemini"


def test_gemini_http_error_falls_back_to_rules(profile):
    response = httpx.Response(
        503, request=httpx.Request("POST", "https://example.test")
    )
    decision = gemini_call(profile, response)
    assert decision.decision == "approve"
    assert decision.amount_cents == 8000
    assert decision.adjuster == "rules"
