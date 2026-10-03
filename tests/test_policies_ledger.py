from datetime import date, timedelta
from unittest.mock import Mock

from insurer.adjuster import Decision
from insurer.claims import ClaimsService
from insurer.ledger import Ledger
from insurer.money import cents
from insurer.policies import PolicyService
from insurer.rating import rate_profile
from insurer.storage import connect


def bind(connection, profile, start="2027-01-01"):
    policies = PolicyService(connection)
    quote = policies.quote(profile, start)
    return policies, policies.bind(quote["quote_id"])


def balance(connection, account):
    return next(
        int(row["balance_cents"])
        for row in Ledger(connection).trial_balance()
        if row["account"] == account
    )


def test_journal_entries_and_trial_balance_are_balanced(profile):
    connection = connect()
    policies, policy = bind(connection, profile)
    policies.earn(policy["policy_id"], "2027-06-30")
    policies.endorse(policy["policy_id"], {"approval_threshold": "none"}, "2027-07-01")
    policies.cancel(policy["policy_id"], "2027-08-01")
    ledger = Ledger(connection)
    assert ledger.entries_balanced()
    assert sum(row["debit_cents"] for row in ledger.trial_balance()) == sum(
        row["credit_cents"] for row in ledger.trial_balance()
    )
    assert ledger.totals()[0] == ledger.totals()[1]


def test_gemini_claim_uses_llm_lae_cost(profile):
    connection = connect()
    policies, policy = bind(connection, profile)
    gemini = Mock()
    gemini.adjust.return_value = Decision(
        "approve", 8_000, "Evidence supports the claim.", ["C1"], "gemini"
    )
    claims = ClaimsService(connection, gemini=gemini)
    result = claims.file_claim(
        policy["policy_id"],
        "unauthorized_purchase",
        "2027-03-01",
        "2027-03-02",
        "tx-1",
        9_000,
        [
            {
                "ts": "2027-03-01T12:00:00Z",
                "type": "kill_switch_state",
                "state": True,
            },
            {
                "ts": "2027-03-01T12:00:00Z",
                "type": "purchase",
                "purchase_id": "tx-1",
                "amount_cents": 9_000,
            },
        ],
        use_gemini=True,
    )
    assert result["lae_cents"] == 20
    assert Ledger(connection).entries_balanced()


def test_referral_resolution_pays_amount_and_releases_residual_reserve(profile):
    connection = connect()
    _, policy = bind(connection, profile)
    claims = ClaimsService(connection)
    filed = claims.file_claim(
        policy["policy_id"],
        "unauthorized_purchase",
        "2027-03-01",
        "2027-03-02",
        "tx-referred",
        9_000,
        [
            {
                "ts": "2027-03-01T12:00:00Z",
                "type": "kill_switch_state",
                "state": True,
            },
            {
                "ts": "2027-03-01T12:01:00Z",
                "type": "purchase",
                "purchase_id": "tx-referred",
                "amount_cents": 5_000,
            },
        ],
    )
    reserve = filed["reserve_cents"]
    lae_before = balance(connection, "lae_expense")
    resolved = claims.resolve_referral(filed["claim_id"], True, amount_cents=3_000)

    assert reserve == 8_000
    assert resolved["paid_cents"] == 3_000
    assert resolved["decision"]["amount_cents"] == 3_000
    assert resolved["reserve_cents"] == 0
    assert balance(connection, "case_reserve") == 0
    assert balance(connection, "incurred_losses") == 3_000
    assert balance(connection, "lae_expense") == lae_before
    assert Ledger(connection).entries_balanced()


def test_referral_resolution_defaults_to_reserved_amount(profile):
    connection = connect()
    _, policy = bind(connection, profile)
    claims = ClaimsService(connection)
    filed = claims.file_claim(
        policy["policy_id"],
        "unauthorized_purchase",
        "2027-03-01",
        "2027-03-02",
        "tx-referred",
        9_000,
        [
            {
                "ts": "2027-03-01T12:00:00Z",
                "type": "kill_switch_state",
                "state": True,
            },
            {
                "ts": "2027-03-01T12:01:00Z",
                "type": "purchase",
                "purchase_id": "tx-referred",
                "amount_cents": 5_000,
            },
        ],
    )
    resolved = claims.resolve_referral(filed["claim_id"], True)
    assert resolved["paid_cents"] == filed["reserve_cents"]


def test_referral_resolution_caps_payment_at_reserve(profile):
    connection = connect()
    _, policy = bind(connection, profile)
    claims = ClaimsService(connection)
    filed = claims.file_claim(
        policy["policy_id"],
        "unauthorized_purchase",
        "2027-03-01",
        "2027-03-02",
        "tx-referred",
        9_000,
        [
            {
                "ts": "2027-03-01T12:00:00Z",
                "type": "kill_switch_state",
                "state": True,
            },
            {
                "ts": "2027-03-01T12:01:00Z",
                "type": "purchase",
                "purchase_id": "tx-referred",
                "amount_cents": 5_000,
            },
        ],
    )
    resolved = claims.resolve_referral(
        filed["claim_id"], True, amount_cents=filed["reserve_cents"] + 1
    )
    assert resolved["paid_cents"] == filed["reserve_cents"]


def test_claims_use_version_at_loss_after_endorsement(profile):
    connection = connect()
    policies, policy = bind(connection, profile)
    policy_id = policy["policy_id"]
    endorsement_day = date(2027, 1, 1) + timedelta(days=100)
    policies.endorse(
        policy_id,
        {"monthly_spend_cap_cents": 5000},
        endorsement_day,
    )
    claims = ClaimsService(connection)

    def file_claim(loss_day, purchase_id):
        loss_date = loss_day.isoformat()
        return claims.file_claim(
            policy_id,
            "unauthorized_purchase",
            loss_date,
            (loss_day + timedelta(days=1)).isoformat(),
            purchase_id,
            9_000,
            [
                {
                    "ts": f"{loss_date}T12:00:00Z",
                    "type": "kill_switch_state",
                    "state": True,
                },
                {
                    "ts": f"{loss_date}T12:01:00Z",
                    "type": "purchase",
                    "purchase_id": purchase_id,
                    "amount_cents": 9_000,
                },
            ],
        )

    before = file_claim(date(2027, 1, 1) + timedelta(days=50), "tx-before")
    after = file_claim(date(2027, 1, 1) + timedelta(days=150), "tx-after")
    assert before["decision"]["decision"] == "approve"
    assert before["paid_cents"] == 8000
    assert after["decision"]["decision"] == "approve"
    assert after["paid_cents"] == 5000


def test_claim_after_cancellation_is_denied_but_prior_loss_is_covered(profile):
    connection = connect()
    policies, policy = bind(connection, profile)
    policy_id = policy["policy_id"]
    start = date(2027, 1, 1)
    policies.cancel(policy_id, start + timedelta(days=100))
    claims = ClaimsService(connection)

    def file_claim(loss_day, purchase_id):
        loss_date = loss_day.isoformat()
        return claims.file_claim(
            policy_id,
            "unauthorized_purchase",
            loss_date,
            (loss_day + timedelta(days=1)).isoformat(),
            purchase_id,
            9_000,
            [
                {
                    "ts": f"{loss_date}T12:00:00Z",
                    "type": "kill_switch_state",
                    "state": True,
                },
                {
                    "ts": f"{loss_date}T12:01:00Z",
                    "type": "purchase",
                    "purchase_id": purchase_id,
                    "amount_cents": 9_000,
                },
            ],
        )

    prior_loss = file_claim(start + timedelta(days=50), "tx-before-cancel")
    after_cancel = file_claim(start + timedelta(days=150), "tx-after-cancel")
    assert prior_loss["decision"]["decision"] == "approve"
    assert after_cancel["decision"]["decision"] == "deny"
    assert "not in force" in after_cancel["decision"]["reason"]
    assert after_cancel["reserve_cents"] == 0


def test_day_pro_rata_earning_at_day_73(profile):
    connection = connect()
    policies, policy = bind(connection, profile)
    premium = policy["premium_cents"]
    earned = policies.earn(policy["policy_id"], date(2027, 1, 1) + timedelta(days=73))
    assert abs(earned - round(premium * 0.20)) <= 1
    assert policies.get_policy(policy["policy_id"])["earned_premium_cents"] == earned


def test_acquisition_and_admin_expenses_accrue_with_earned_premium(profile, product):
    connection = connect()
    policies, policy = bind(connection, profile)
    policy_id = policy["policy_id"]
    dac = cents(policy["premium_cents"] * float(product.rating["loads"]["acquisition"]))
    assert balance(connection, "deferred_acquisition_costs") == dac
    assert balance(connection, "acquisition_expense") == 0
    assert balance(connection, "admin_expense") == 0

    earned = policies.earn(policy_id, date(2027, 1, 1) + timedelta(days=73))
    assert abs(balance(connection, "acquisition_expense") - round(dac * 0.2)) <= 1
    assert balance(connection, "admin_expense") == cents(
        earned * float(product.rating["loads"]["admin"])
    )


def test_endorsements_rerate_only_remaining_days(profile):
    connection = connect()
    policies, policy = bind(connection, profile)
    policy_id = policy["policy_id"]
    day = date(2027, 1, 1) + timedelta(days=73)
    earned = policies.earn(policy_id, day)
    dac_before_endorsement = balance(connection, "deferred_acquisition_costs")
    changed = profile | {"approval_threshold": "none"}
    result = policies.endorse(policy_id, {"approval_threshold": "none"}, day)
    premium_delta = result["premium_cents"] - policy["premium_cents"]
    added_dac = cents(
        max(0, premium_delta) * float(policies.product.rating["loads"]["acquisition"])
    )
    assert (
        balance(connection, "deferred_acquisition_costs")
        == dac_before_endorsement + added_dac
    )
    remaining = (date.fromisoformat(policy["end_date"]) - day).days
    expected_remaining = rate_profile(
        changed,
        term_days=remaining,
        annual_term_days=365,
    ).technical_premium_cents
    assert result["earned_premium_cents"] == earned
    assert result["premium_cents"] == earned + expected_remaining
    assert result["versions"][0]["profile"] == profile
    assert result["versions"][1]["profile"] == changed
    assert result["versions"][0]["effective_to"] == day.isoformat()
    assert result["versions"][1]["effective_from"] == day.isoformat()
    assert result["premium_cents"] - earned == expected_remaining
    assert remaining > 0

    down_connection = connect()
    down_profile = profile | {"approval_threshold": "none"}
    down_policies, down_policy = bind(down_connection, down_profile)
    down_policies.earn(down_policy["policy_id"], day)
    down_dac_before = balance(down_connection, "deferred_acquisition_costs")
    down_result = down_policies.endorse(
        down_policy["policy_id"], {"approval_threshold": "eur_50"}, day
    )
    assert down_result["premium_cents"] < down_policy["premium_cents"]
    assert balance(down_connection, "deferred_acquisition_costs") == down_dac_before


def test_cancel_refunds_unearned_premium_and_tax_but_not_acquisition(profile):
    connection = connect()
    policies, policy = bind(connection, profile)
    policy_id = policy["policy_id"]
    cancel_date = date(2027, 1, 1) + timedelta(days=73)
    policies.earn(policy_id, cancel_date)
    before = policies.get_policy(policy_id)
    ledger = Ledger(connection)
    acquisition_before = next(
        row["debit_cents"]
        for row in ledger.trial_balance()
        if row["account"] == "acquisition_expense"
    )
    dac_before = balance(connection, "deferred_acquisition_costs")
    policies.cancel(policy_id, cancel_date)
    expected_refund = (
        before["premium_cents"]
        - before["earned_premium_cents"]
        + before["tax_cents"]
        - before["earned_tax_cents"]
    )
    entry = connection.execute(
        "SELECT entry_id FROM ledger_entries WHERE description LIKE 'Cancel policy %'"
    ).fetchone()
    cash_credit = connection.execute(
        """
        SELECT SUM(credit_cents) FROM ledger_postings
        WHERE entry_id = ? AND account = 'cash'
        """,
        (entry["entry_id"],),
    ).fetchone()[0]
    assert cash_credit == expected_refund
    acquisition_after = next(
        row["debit_cents"]
        for row in ledger.trial_balance()
        if row["account"] == "acquisition_expense"
    )
    assert acquisition_after - acquisition_before == dac_before
    assert balance(connection, "deferred_acquisition_costs") == 0
    assert policies.get_policy(policy_id)["cancelled"]
    assert ledger.entries_balanced()


def test_policy_versions_are_append_only(profile):
    connection = connect()
    policies, policy = bind(connection, profile)
    policy_id = policy["policy_id"]
    first_version = policies.versions(policy_id)[0].copy()
    policies.endorse(policy_id, {"approval_threshold": "none"}, "2027-03-01")
    versions = policies.versions(policy_id)
    assert len(versions) == 2
    assert versions[0]["version"] == 1
    assert versions[0]["profile"] == profile
    assert versions[0]["effective_to"] == "2027-03-01"
    assert first_version["premium_cents"] == versions[0]["premium_cents"]
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM policy_versions WHERE policy_id = ?", (policy_id,)
        ).fetchone()[0]
        == 2
    )
