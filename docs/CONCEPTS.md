# Insurance concepts

## GWP

Gross written premium (GWP) is the premium attached to policies written during a period, including endorsement changes and cancellations as adjustments. The sandbox records each signed premium movement in the premium bordereau; see `policies.PolicyService.bind`, `policies.PolicyService.endorse`, and `policies.PolicyService.cancel`.

## Earned and unearned premium

Premium is earned day by day as coverage is provided; the part for future coverage remains unearned and is a liability until time passes or it is refunded. The ledger transfers cents from unearned to earned premium, using actual term days; see `policies.PolicyService.earn`, `policies.PolicyService.renew`, and `ledger.Ledger.post`.

## Deferred acquisition costs

Deferred acquisition costs (DAC) are acquisition expenses attributable to future coverage, held as an asset until amortized over the policy's remaining coverage period. Positive endorsement deltas add DAC; cancellation writes off its remaining balance, while admin expense is accrued as premium is earned. See `policies.PolicyService.bind`, `policies.PolicyService.earn`, and `policies.PolicyService.cancel`.

## Loss ratio

The loss ratio compares incurred losses—including paid claims, outstanding case reserves, and IBNR—with earned premium. It describes claim cost relative to the premium earned; see `simulate._book_metrics` and `simulate.simulate_book`.

## LAE

Loss adjustment expense (LAE) is the cost of investigating and handling claims, distinct from the claim indemnity itself. Each adjudication posts its rules, Gemini, or human-referral handling cost; see `claims.ClaimsService.file_claim` and `adjuster.GeminiAdjuster.adjust`.

## Combined ratio

The combined ratio adds loss, LAE, and recognized acquisition/admin expense ratios. Acquisition costs are amortized with earned premium (with remaining DAC written off on cancellation), while admin costs accrue as premium is earned. Below 100% indicates underwriting profit before investment income in this simplified book; see `simulate._book_metrics`.

## Case reserve

A case reserve is the current claim-specific estimate of an individual reported claim's unpaid cost. FNOL creates the estimate, approvals pay it down, denials release it, and referrals retain it pending review; see `claims.ClaimsService.file_claim` and `claims.ClaimsService.resolve_referral`.

## IBNR

Incurred but not reported (IBNR) is an estimate of losses that have happened but are not fully present in the reported claim inventory. The simulation also exposes its known delayed-notification IBNR so the estimate can be compared with the generated truth; see `reserving.build_triangle` and the month-end close in `simulate.simulate_book`.

## Chain ladder

Chain ladder projects cumulative reported losses toward ultimate values using historical development patterns. Referral claims retain their initial incurred amount in earlier development periods and reflect the final paid amount from the resolution month onward. This implementation fits the `chainladder.Development` and `chainladder.Chainladder` methods and deliberately returns zero IBNR with a note when fewer than three accident-month origins are available; see `reserving.build_triangle`.

## LDF

A loss development factor (LDF) is the observed or selected multiplier between adjacent development ages in a claims triangle. Multiplying the factors from an origin's current age to ultimate gives its cumulative development factor; see the `ldf` and `cdf` values returned by `reserving.build_triangle`.

## Bordereaux

A bordereau is a periodic detail report of policies, premiums, or claims sent by an intermediary to its risk-bearing partner. This sandbox exports monthly premium and claims CSVs; see `bordereaux.export_bordereau` and the `/bordereaux/{kind}` API route.

## MGA, carrier, and fronting

A managing general agent (MGA) can distribute and administer policies under delegated authority; a carrier holds the insurance risk, while a fronting carrier may issue the paper and cede much of that risk elsewhere. The Phase 0 sandbox models these responsibilities conceptually, but it has no licensed carrier or real capital; see `simulate.simulate_book` and `capital.simulate_capital`.

## Warranty versus exclusion

A warranty is a condition the insured must satisfy, such as the operational W1 kill-switch; an exclusion removes a category of loss from cover, such as a human-approved purchase under E1. The adjuster checks W1 and the E1–E3 exclusions in a fixed order; see `adjuster.RulesAdjuster.adjust` and `products/agent_spend_cover.yaml`.

## Quota share

Quota-share reinsurance cedes a fixed percentage of covered losses to a reinsurer, usually in exchange for a commission arrangement. The capital proxy reports gross and ceded net losses for the specified cession; see `capital.simulate_capital`.

## SCR

The Solvency Capital Requirement (SCR) is a regulatory capital measure for adverse loss scenarios. Here, `SCR proxy` is only the simulated 99.5th-percentile annual aggregate loss less expected loss; it is an educational calculation, not a regulatory solvency assessment; see `capital.simulate_capital`.
