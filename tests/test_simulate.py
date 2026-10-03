from datetime import date

from insurer.adjuster import RulesAdjuster
from insurer.policies import PolicyService
from insurer.simulate import _true_ibnr_cents, simulate_book, write_results
from insurer.storage import connect


def test_seeded_results_are_byte_for_byte_deterministic(tmp_path):
    first = simulate_book(agents=60, months=6, seed=42, years=25)
    second = simulate_book(agents=60, months=6, seed=42, years=25)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    write_results(first, first_path)
    write_results(second, second_path)
    assert first_path.read_bytes() == second_path.read_bytes()


def test_results_schema_keys_match_design():
    result = simulate_book(agents=20, months=3, seed=8, years=10)
    assert set(result) == {
        "params",
        "book",
        "monthly",
        "segments",
        "triangle",
        "claims_by_reason",
        "claims_sample",
        "capital",
        "trial_balance",
    }
    assert set(result["params"]) == {"agents", "months", "seed", "start", "product"}
    assert set(result["book"]) == {
        "quotes",
        "declined",
        "policies_written",
        "policies_in_force_end",
        "cancellations",
        "endorsements",
        "gwp_cents",
        "earned_premium_cents",
        "unearned_premium_cents",
        "premium_tax_cents",
        "paid_losses_cents",
        "case_reserves_cents",
        "ibnr_cents",
        "true_ibnr_cents",
        "incurred_losses_cents",
        "lae_cents",
        "acquisition_cents",
        "admin_cents",
        "dac_cents",
        "loss_ratio",
        "lae_ratio",
        "expense_ratio",
        "combined_ratio",
        "underwriting_result_cents",
    }
    assert set(result["monthly"][0]) == {
        "month",
        "gwp_cents",
        "earned_premium_cents",
        "incurred_losses_cents",
        "paid_losses_cents",
        "claims_reported",
        "claims_approved",
        "claims_denied",
        "claims_referred",
        "policies_in_force",
        "loss_ratio",
    }
    assert set(result["segments"][0]) == {
        "factor",
        "level",
        "policies",
        "earned_premium_cents",
        "incurred_losses_cents",
        "loss_ratio",
        "priced_factor",
        "true_factor",
    }
    assert set(result["triangle"]) == {
        "origins",
        "dev_months",
        "values_cents",
        "ldf",
        "cdf",
        "ultimate_cents",
        "ibnr_cents",
        "note",
    }
    assert set(result["capital"]) == {
        "years",
        "expected_loss_cents",
        "p95_loss_cents",
        "p995_loss_cents",
        "scr_proxy_cents",
        "net_of_quota_share",
    }
    assert set(result["capital"]["net_of_quota_share"]) == {
        "cession",
        "commission",
        "expected_loss_cents",
        "p995_loss_cents",
        "scr_proxy_cents",
    }
    assert set(result["trial_balance"][0]) == {
        "account",
        "debit_cents",
        "credit_cents",
        "balance_cents",
    }
    for claim in result["claims_sample"]:
        assert set(claim) == {
            "claim_id",
            "policy_id",
            "cause",
            "loss_date",
            "notified",
            "claimed_cents",
            "decision",
            "paid_cents",
            "reason",
            "clause_ids",
            "adjuster",
            "fraud_truth",
        }


def test_default_seed_run_balances_and_has_required_slices():
    result = simulate_book(agents=1000, months=12, seed=42, years=1000)
    debit_total = sum(row["debit_cents"] for row in result["trial_balance"])
    credit_total = sum(row["credit_cents"] for row in result["trial_balance"])
    assert debit_total == credit_total
    assert 0.3 < result["book"]["loss_ratio"] < 1.2
    assert {"approve", "deny", "refer"} <= {
        row["decision"] for row in result["claims_sample"]
    }
    segments = {row["level"]: row for row in result["segments"]}
    assert segments["none"]["loss_ratio"] > segments["eur_50"]["loss_ratio"]


def test_true_ibnr_excludes_fraud_and_caps_payable_amount(profile):
    connection = connect()
    policies = PolicyService(connection)
    capped_profile = profile | {"monthly_spend_cap_cents": 5000}
    quote = policies.quote(capped_profile, "2027-01-01")
    policy = policies.bind(quote["quote_id"])

    def pending_claim(purchase_id, fraud_truth, sequence, notified_date):
        return {
            "policy_id": policy["policy_id"],
            "cause": "unauthorized_purchase",
            "loss_date": "2027-03-20",
            "notified_date": notified_date,
            "purchase_id": purchase_id,
            "evidence": [
                {
                    "ts": "2027-03-20T12:00:00Z",
                    "type": "kill_switch_state",
                    "state": True,
                },
                {
                    "ts": "2027-03-20T12:01:00Z",
                    "type": "purchase",
                    "purchase_id": purchase_id,
                    "amount_cents": 9000,
                },
            ],
            "fraud_truth": fraud_truth,
            "_sequence": sequence,
        }

    claims = [
        pending_claim("tx-payable", False, 0, "2027-04-05"),
        pending_claim("tx-fraud", True, 1, "2027-04-05"),
        pending_claim("tx-late", False, 2, "2027-05-01"),
    ]
    assert (
        _true_ibnr_cents(
            claims,
            date(2027, 3, 31),
            policies,
            RulesAdjuster(),
        )
        == 5000
    )
