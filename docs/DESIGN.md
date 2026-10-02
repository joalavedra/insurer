# insurer — Phase 0 design: "insurer in a box"

Goal: a sandbox insurer you can learn from by running it. A thin, owned insurance core + an AI claims adjuster + actuarial reserving + a Monte-Carlo book simulator that drives the *real* core end to end. Seed product: **Agent Spend Cover** (embedded cover for losses from purchases made by AI agents through Valet). Sandbox money only; no real capital, no licence.

Non-goals (Phase 0): real payments, auth, multi-tenant, Postgres, UI beyond the static report.

## Stack
- Python 3.10, `src/insurer` package, `pyproject.toml` (hatchling), console script `insurer`.
- Deps (pin exact): numpy, pandas, chainladder==0.10.1, pyyaml, fastapi, uvicorn, httpx. Dev: pytest, ruff, mypy.
- Storage: SQLite via stdlib `sqlite3`, schema in `src/insurer/schema.sql`. **All money is integer euro cents** (`int`), never floats in the core. Floats only in rating maths / simulation, rounded to cents (half-up) at the boundary.
- CLI via argparse (no typer).

## Modules
| Module | Concept it teaches |
|---|---|
| `products.py` | Product definition: coverages, limits, deductible, exclusions, warranties, wording clauses, rating factors, loads. Loaded from `products/agent_spend_cover.yaml`. Versioned (`version`, `effective_from`). |
| `rating.py` | Frequency × severity pricing → expected loss cost → technical premium via expense/profit loads; premium tax shown separately. Returns a step-by-step audit trail. |
| `policies.py` | Lifecycle: quote → bind → endorse (mid-term re-rate, pro-rata) → cancel (pro-rata refund) → renew. Effective-dated policy versions (never mutate a version; add a new one). |
| `ledger.py` | Double-entry journal + trial balance + earning (pro-rata by day). |
| `claims.py` | FNOL → coverage check → case reserve → adjuster decision → payment/denial/referral → close. |
| `adjuster.py` | `RulesAdjuster` (deterministic) and `GeminiAdjuster` (LLM, guarded by rules). |
| `reserving.py` | Loss triangles (accident month × development month) + chain-ladder IBNR via `chainladder`. |
| `bordereaux.py` | Premium and claims bordereaux CSV (what an MGA sends its carrier monthly). |
| `simulate.py` | Monte-Carlo book: generates agents, writes business through `policies`/`claims`, month-end closes, emits `results.json`. |
| `capital.py` | Vectorised many-year aggregate loss sim → 99.5% VaR (Solvency-II-style SCR proxy), with/without quota-share reinsurance. |
| `api.py` | FastAPI: the surface Valet will call in Phase 1. |
| `cli.py` | `insurer simulate|quote|serve|bordereaux|trial-balance`. |

## Product: `products/agent_spend_cover.yaml` (exact values)
```yaml
id: agent_spend_cover
version: 1
effective_from: 2027-01-01
currency: EUR
term_months: 12
coverage:
  per_claim_limit_cents: 100000        # EUR 1,000
  annual_aggregate_limit_cents: 300000 # EUR 3,000
  deductible_cents: 1000               # EUR 10
  covered_causes: [unauthorized_purchase, prompt_injection, duplicate_payment, merchant_non_delivery, wrong_item]
warranties:            # must hold to bind AND at time of loss, else decline / deny
  - id: W1
    field: kill_switch
    equals: true
    text: "The insured agent must have an operational kill-switch in Valet at all times."
exclusions:
  - id: E1
    text: "Purchases explicitly approved by a human through a Valet approval are excluded."
  - id: E2
    text: "Losses not evidenced by a purchase event in the Valet audit log are excluded."
  - id: E3
    text: "Losses arising more than 30 days before notification are excluded."
clauses:
  - id: C1
    text: "We pay the amount of the evidenced purchase, less the deductible, up to the per-claim limit and the remaining annual aggregate."
rating:
  base_annual_frequency: 0.40          # claims per agent-year for the base profile
  severity: {distribution: lognormal, median_cents: 6000, sigma: 1.0}
  frequency_factors:
    approval_threshold: {none: 1.6, eur_200: 1.0, eur_50: 0.6}
    merchant_allowlist: {"off": 1.3, "on": 0.8}
    rail: {card: 1.0, x402: 1.1}
    tenure: {lt_3m: 1.3, m3_12: 1.0, gt_12m: 0.85}
  loads: {acquisition: 0.15, admin: 0.10, lae: 0.05, profit: 0.05}
  minimum_premium_cents: 500
  premium_tax_rate: 0.08               # Spain IPS; collected for the state, not revenue
simulation:
  truth_frequency_factors:             # the "real world" the sim draws from; differs from priced on purpose
    approval_threshold: {none: 2.0, eur_200: 1.0, eur_50: 0.6}
    tenure: {lt_3m: 1.5, m3_12: 1.0, gt_12m: 0.85}
  fraud_rate: 0.04
  report_lag_mean_days: 12             # geometric/exponential; creates IBNR at period end
  lae_cost_cents: {rules: 50, llm: 20, human_referral: 2500}
```
Fields of an agent risk profile: `approval_threshold` (none|eur_200|eur_50), `merchant_allowlist` (bool → on/off), `rail` (card|x402), `tenure_months` (int → bucket lt_3m <3, m3_12 3–12, gt_12m >12), `monthly_spend_cap_cents` (int), `kill_switch` (bool).
Missing truth factors fall back to the priced factors.

## Rating (exact)
- `freq = base_annual_frequency × Π factor[level]` over all four factors.
- Severity X ~ lognormal(μ = ln(median), σ). Covered loss per claim = `min(max(X − d, 0), L)` where `d = deductible`, `L = min(per_claim_limit, monthly_spend_cap)`. Its expectation = `LEV(d + L) − LEV(d)` with the analytic lognormal limited expected value `LEV(u) = e^{μ+σ²/2} Φ((ln u − μ − σ²)/σ) + u (1 − Φ((ln u − μ)/σ))`. Use `math.erf` for Φ (no scipy).
- `expected_loss_cost = freq × expected_covered_severity` (annual).
- `technical_premium = max(ELC / (1 − Σ loads), minimum_premium)`; prorate for terms shorter than 12 months; round to cents.
- `premium_tax = round(technical_premium × premium_tax_rate)`; `total_payable = premium + tax`.
- Underwriting decline (quote returns `declined` + reason): warranty W1 false.
- Rating returns `RatingResult` with `steps: list[RatingStep(name, value, note)]` for the audit trail.

## Ledger (exact accounts & postings)
Accounts: `cash`, `premium_receivable`, `unearned_premium`, `earned_premium`, `premium_tax_payable`, `acquisition_expense`, `admin_expense`, `incurred_losses`, `case_reserve`, `ibnr_reserve`, `lae_expense`.
- bind: Dr premium_receivable (premium+tax) / Cr unearned_premium (premium), Cr premium_tax_payable (tax); collect immediately in sandbox: Dr cash / Cr premium_receivable. Acquisition: Dr acquisition_expense / Cr cash (premium × acquisition load). Admin likewise.
- earn (at each month-end close, through close date, day pro-rata over the term): Dr unearned_premium / Cr earned_premium.
- endorse: earn to the endorsement date, then re-rate remaining days; post the delta (+/−) to unearned_premium vs cash (and the tax delta).
- cancel: earn to cancel date; refund remaining unearned premium + proportional tax: Dr unearned_premium, Dr premium_tax_payable / Cr cash. Acquisition is not refunded.
- FNOL: Dr incurred_losses / Cr case_reserve (initial reserve = min(claimed − d, limits remaining), ≥0).
- decide approve: pay = decision amount: Dr case_reserve / Cr cash; release any residual reserve: Dr case_reserve / Cr incurred_losses (reverse). deny: release whole reserve. refer: keep reserve until resolved.
- LAE per adjudication: Dr lae_expense / Cr cash using `lae_cost_cents`.
- month-end IBNR: true-up `ibnr_reserve` to the chain-ladder estimate: Dr/Cr incurred_losses vs ibnr_reserve for the delta.
Invariant (tested everywhere): Σdebits == Σcredits per journal entry and overall.

## Claims & adjuster
Evidence = list of synthetic Valet audit events `{ts, type, amount_cents?, merchant?, purchase_id?, approver?, injection_flag?}` with type ∈ `grant_created | purchase | approval_requested | approval_granted | approval_denied | kill_switch_state | refund`.
`Decision = {decision: approve|deny|refer, amount_cents, reason, clause_ids: list[str], adjuster: "rules"|"gemini"}`.

`RulesAdjuster` (deterministic; the source of truth on limits):
1. Policy in force at loss date, else deny ("not in force").
2. Cause not covered → deny.
3. W1: last `kill_switch_state` at or before the loss is false → deny W1.
4. E2: no `purchase` event matching `purchase_id` → deny E2.
5. E1: an `approval_granted` for that `purchase_id` → deny E1.
6. E3: notification − loss > 30 days → deny E3.
7. Fraud signals → refer: claimed amount > purchase amount, a `refund` event already exists for the purchase, or duplicate claim on the same purchase_id.
8. Else approve `min(purchase_amount − d, per_claim_limit, monthly_spend_cap, remaining_aggregate)`, clauses [C1].

`GeminiAdjuster`: REST `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=...` with `generationConfig.responseMimeType=application/json`, model from env `INSURER_GEMINI_MODEL` (default `gemini-2.5-flash`), key from env `GEMINI_API_KEY`. Guardrails: run `RulesAdjuster` first. If the LLM's decision differs from the rules on a hard rule (steps 1–6) → `refer`. The amount is always capped at the rules' amount. LLM errors or timeouts → fall back to the rules decision with `adjuster="rules"`. Prompt (use verbatim, fill `{...}`):

```
You are a claims adjuster for "Agent Spend Cover", an insurance policy that covers financial losses caused by purchases made by an AI agent.
Decide the claim using ONLY the policy wording and the evidence below. Do not assume facts that are not in the evidence.

POLICY WORDING (clause id: text):
{wording}

POLICY:
{policy_json}

CLAIM:
{claim_json}

EVIDENCE (Valet audit log events, chronological):
{evidence_json}

Return JSON only, with exactly these keys:
{"decision": "approve" | "deny" | "refer", "amount_cents": <integer, 0 if not approve>, "reason": "<one or two sentences citing the evidence>", "clause_ids": ["<ids of the clauses, warranties or exclusions you relied on>"]}
Use "refer" when the evidence is inconsistent, suggests fraud, or is insufficient to decide.
```
`{wording}` = every coverage clause, warranty and exclusion as `ID: text` lines.

## Reserving
`reserving.build_triangle(claims, as_of)`: origin = accident month of loss, development = months since origin; values = cumulative **reported incurred** (paid + case reserves) as of each development month-end. Fit `chainladder.Chainladder()` on `chainladder.Development()`. Return LDFs, CDFs, ultimate and IBNR by origin, and total IBNR (floored at 0). With fewer than 3 origins, return IBNR 0 and a `note`.

## Simulator (`insurer simulate`)
Args: `--agents 1000 --months 12 --seed 42 --start 2027-01-01 --llm-sample 0 --db :memory: --out results.json --years 1000 (capital) --quota-share 0.5 --qs-commission 0.30`.
- Agents arrive along a growth curve (≈ linear ramp so month m writes ~ 2m/(M(M+1)) of agents); profile drawn from fixed distributions (document them in code as constants): approval none 30% / eur_200 45% / eur_50 25%; allowlist on 60%; rail card 70% / x402 30%; tenure uniform 0–24 months; monthly cap lognormal median EUR 300 (cents, clipped 20 to 5000 EUR); kill_switch true 92%. Kill-switch=false quotes are declined (count them).
- For each bound policy, claims arrive as a Poisson process with **truth** frequency (annual rate prorated by day). Loss amount ~ the same lognormal severity, capped at the monthly spend cap. Generate a consistent evidence log. With probability `fraud_rate`, make the claim fraudulent (an inflated claimed amount, or an approval_granted present, or a refund present). Notification date = loss + exponential lag (`report_lag_mean_days`). Only claims notified ≤ the sim end are reported; the rest are the "true IBNR" (report it next to the chain-ladder estimate — key teaching point).
- Random lifecycle: 1%/month cancellation hazard; 2%/month endorsements that change `approval_threshold` or the monthly cap.
- Referred claims resolve after 30 days: approve the rules amount if not fraud, else deny.
- Month-end close each month: earn, IBNR true-up, snapshot KPIs.
- `--llm-sample N`: route the first N claims through `GeminiAdjuster` (only if `GEMINI_API_KEY` is set), otherwise rules.
- Deterministic for a given seed (numpy `default_rng(seed)`), excluding LLM calls.

## `results.json` schema (exact; the HTML report consumes it)
```json
{
  "params": {"agents": 1000, "months": 12, "seed": 42, "start": "2027-01-01", "product": "agent_spend_cover@1"},
  "book": {"quotes": 0, "declined": 0, "policies_written": 0, "policies_in_force_end": 0, "cancellations": 0, "endorsements": 0,
           "gwp_cents": 0, "earned_premium_cents": 0, "unearned_premium_cents": 0, "premium_tax_cents": 0,
           "paid_losses_cents": 0, "case_reserves_cents": 0, "ibnr_cents": 0, "true_ibnr_cents": 0, "incurred_losses_cents": 0,
           "lae_cents": 0, "acquisition_cents": 0, "admin_cents": 0,
           "loss_ratio": 0.0, "lae_ratio": 0.0, "expense_ratio": 0.0, "combined_ratio": 0.0, "underwriting_result_cents": 0},
  "monthly": [{"month": "2027-01", "gwp_cents": 0, "earned_premium_cents": 0, "incurred_losses_cents": 0, "paid_losses_cents": 0,
               "claims_reported": 0, "claims_approved": 0, "claims_denied": 0, "claims_referred": 0, "policies_in_force": 0,
               "loss_ratio": 0.0}],
  "segments": [{"factor": "approval_threshold", "level": "none", "policies": 0, "earned_premium_cents": 0,
                "incurred_losses_cents": 0, "loss_ratio": 0.0, "priced_factor": 1.6, "true_factor": 2.0}],
  "triangle": {"origins": ["2027-01"], "dev_months": [0], "values_cents": [[0]], "ldf": [1.0], "cdf": [1.0],
               "ultimate_cents": [0], "ibnr_cents": [0], "note": null},
  "claims_by_reason": [{"decision": "deny", "clause": "E1", "count": 0}],
  "claims_sample": [{"claim_id": "", "policy_id": "", "cause": "", "loss_date": "", "notified": "", "claimed_cents": 0,
                     "decision": "", "paid_cents": 0, "reason": "", "clause_ids": [], "adjuster": "rules", "fraud_truth": false}],
  "capital": {"years": 1000, "expected_loss_cents": 0, "p95_loss_cents": 0, "p995_loss_cents": 0, "scr_proxy_cents": 0,
              "net_of_quota_share": {"cession": 0.5, "commission": 0.30, "expected_loss_cents": 0, "p995_loss_cents": 0, "scr_proxy_cents": 0}},
  "trial_balance": [{"account": "cash", "debit_cents": 0, "credit_cents": 0, "balance_cents": 0}]
}
```
Ratios: loss = incurred (paid + case + IBNR) / earned; lae = LAE / earned; expense = (acquisition + admin) / earned; combined = sum of the three. `claims_sample` holds up to 50 claims and must include some of each decision.

`capital.py`: for the book's in-force exposure (policies × their truth frequency × severity params, annualised), simulate `years` independent years vectorised (Poisson counts → lognormal severities with the same caps/deductible). Report expected, p95 and p99.5 aggregate losses. SCR proxy = p99.5 − expected. Quota share: net loss = (1 − cession) × gross.

## API (`insurer serve`, FastAPI, sqlite file via `INSURER_DB`, default `insurer.db`)
`POST /quotes` {profile, start_date} → quote with rating steps | declined. `POST /policies` {quote_id} → bound policy. `POST /policies/{id}/endorse` {profile_changes, effective_date}. `POST /policies/{id}/cancel` {effective_date}. `GET /policies/{id}` (all versions). `POST /claims` {policy_id, cause, loss_date, notified_date, purchase_id, claimed_cents, evidence[]} → claim + decision. `GET /ledger/trial-balance`. `GET /bordereaux/{premium|claims}?month=YYYY-MM` → CSV.

## Docs
`README.md`: what this is, quickstart (`pip install -e .[dev]`, `insurer simulate --out results.json`, `insurer serve`), an example output table. `docs/CONCEPTS.md`: a glossary (GWP, earned/unearned, loss ratio, LAE, combined ratio, case reserve, IBNR, chain ladder, LDF, bordereaux, MGA/carrier/fronting, warranty vs exclusion, quota share, SCR), each with one paragraph plus a pointer to the module and function that implements it.
