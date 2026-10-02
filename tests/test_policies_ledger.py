from datetime import date, timedelta
from unittest.mock import Mock

from insurer.adjuster import Decision
from insurer.claims import ClaimsService
from insurer.ledger import Ledger
from insurer.policies import PolicyService
from insurer.rating import rate_profile
from insurer.storage import connect


def bind(connection, profile, start="2027-01-01"):
    policies = PolicyService(connection)
    quote = policies.quote(profile, start)
    return policies, policies.bind(quote["quote_id"])


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


def test_day_pro_rata_earning_at_day_73(profile):
    connection = connect()
    policies, policy = bind(connection, profile)
    premium = policy["premium_cents"]
    earned = policies.earn(policy["policy_id"], date(2027, 1, 1) + timedelta(days=73))
    assert abs(earned - round(premium * 0.20)) <= 1
    assert policies.get_policy(policy["policy_id"])["earned_premium_cents"] == earned


def test_endorsements_rerate_only_remaining_days(profile):
    connection = connect()
    policies, policy = bind(connection, profile)
    policy_id = policy["policy_id"]
    day = date(2027, 1, 1) + timedelta(days=73)
    earned = policies.earn(policy_id, day)
    changed = profile | {"approval_threshold": "none"}
    result = policies.endorse(policy_id, {"approval_threshold": "none"}, day)
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
    down_result = down_policies.endorse(
        down_policy["policy_id"], {"approval_threshold": "eur_50"}, day
    )
    assert down_result["premium_cents"] < down_policy["premium_cents"]


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
    assert acquisition_after == acquisition_before
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
