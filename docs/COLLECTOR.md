# Live trace collector

The small Rust collector accepts OTLP/HTTP JSON traces and appends each request
to `data/traces/<agent_id>/<UTC-date>.jsonl`. It defaults to
`127.0.0.1:4318` and `./data/traces`:

```bash
cargo run --manifest-path collector/Cargo.toml
```

Set `COLLECTOR_ADDR` or `COLLECTOR_DATA_DIR` to change those defaults. The
collector has no authentication yet; keep it on localhost or another trusted
network. Authentication belongs to the hosted phase.

Point an OpenTelemetry HTTP exporter at it with the standard environment
variables:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_HEADERS=x-insurer-agent=kyc-bot-demo
```

`examples/agents/trace_exporter.py` currently writes OTLP/JSON files rather than
sending HTTP. Replay its existing compatible KYC fixture with curl:

```bash
while IFS= read -r document; do
  curl -fsS http://127.0.0.1:4318/v1/traces \
    -H 'content-type: application/json' \
    -H 'x-insurer-agent: kyc-bot-demo' \
    --data-binary "$document"
done < tests/fixtures/contracts/kyc-traces.jsonl
```

The collector stores those requests under `data/traces/kyc-bot-demo/`. Normalize
or score the stored OTLP JSON with the existing CLI:

```bash
insurer agent-evidence \
  --manifest examples/agents/manifests/kyc_bot.yaml \
  --traces data/traces/kyc-bot-demo/*.jsonl \
  --out kyc-evidence.json
insurer agent-score \
  --manifest examples/agents/manifests/kyc_bot.yaml \
  --traces data/traces/kyc-bot-demo/*.jsonl \
  --out kyc-scorecard.json
```

## Live monitoring

Set `INSURER_TRACE_DIR` on the API to the collector's data directory (defaults
to `data/traces` from the API's working directory). A bound Agent Ops policy
then polls those files for new incidents and warranty breaches; choose
**File N claims** to record incidents that have not already been filed.
