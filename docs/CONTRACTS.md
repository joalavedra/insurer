# Agent Risk contracts

The four draft 2020-12 schemas are packaged from `src/insurer/agentrisk/schemas/` and available at runtime through `schema_path()`:

- `agent-manifest.v1.schema.json` validates customer-declared manifests consumed by `parse_manifest`.
- `agent-evidence.v1.schema.json` defines the normalized traces and exam/eval results emitted by `insurer agent-evidence`.
- `agent-scorecard.v1.schema.json` validates `build_scorecard` output.
- `agent-quote.v1.schema.json` validates `insurer agent-quote` output, including an optional bound policy.

Breaking changes require a new `vN` schema file. V1 may only gain additive optional fields; existing field meanings and hash semantics are fixed.

Normalized evidence records whether an exam file was supplied independently from its result list, so replay distinguishes an empty exam from no exam. Its `inputs` records the manifest, traces, optional exam file, and CI-eval file paths and SHA-256 digests. These inputs, together with the default battery and framework inputs, are the scorecard's evidence provenance.

## Parser-only rules

JSON Schema does not enforce manifest cross-references. `parse_manifest` additionally requires referenced tools to exist, approval tools to have effect `approval`, approval and amount-limit tools to declare `amount_arg`, recipient-allowlist tools to declare `recipient_arg`, `max_amount_eur` to have an `amount_arg`, and tool names/control IDs to be unique. An empty `allowed_domains` list is valid and allows no recipients.

## Scorecard hash

To compute `evidence.scorecard_sha256`, recursively remove every object key named `path`, remove `evidence.scorecard_sha256`, then serialize the remaining scorecard with Python's `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)` and hash the UTF-8 bytes with SHA-256.

Float formatting is part of the contract. Python's JSON encoder uses Python `repr` formatting, such as `500.0` and `1e-05`; this differs from `serde_json`/ryu formatting (`1e-5`). A Rust port must match the committed byte-level test vector, not merely hash equivalent numeric values.

## Golden fixtures

Regenerate the deterministic KYC fixtures from the repository root with:

```bash
python -m tests.fixtures.contracts.regenerate
```

The generator also exposes `generate_goldens()` to return the JSON objects and JSONL string without writing them. A Rust port passes the contract fixture when it emits JSON-equal evidence, scorecard, and quote outputs for these goldens (and matches the scorecard hash vector byte-for-byte).
