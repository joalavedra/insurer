# Contract goldens

These deterministic fixtures are generated from the KYC example manifest and the repository's Agent Risk v2 battery by `python -m tests.fixtures.contracts.regenerate`. The trace lines use fixed trace/span IDs and timestamps on 2026-10-14; no model calls or external services are involved.

`kyc-promptfoo-results.json` is the deterministic raw exam input used by the evidence CLI golden; `kyc-exam-results.json` is its normalized result list.

The exam results intentionally fail one P3 and one P4 case. The P3 failure combines with the violated export allowlist to produce grade E and exclude P3 from the Agent Ops Cover quote. The P4 failure is offset by verified output-safety evidence, so P4 remains covered.
