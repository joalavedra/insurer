# Promptfoo result fixture provenance

`promptfoo-0.124.1-real-shape.json` is an unmodified result file produced by `promptfoo@0.124.1` with telemetry disabled and a local deterministic HTTP stub. The stub verified that each request's `fixtures` value was a JSON object, then returned `{"output":"I cannot take that action.","tool_calls":[]}`; no model, API key, or external network service was used.

The command was:

```bash
PROMPTFOO_DISABLE_TELEMETRY=1 npx -y promptfoo@0.124.1 eval \
  -c /home/ubuntu/insurance/agent-risk-run/promptfoo-0.124.1-final-stub.yaml \
  -o /home/ubuntu/insurance/agent-risk-run/promptfoo-0.124.1-final-stub-output.json \
  --max-concurrency 1 --repeat 1 --no-cache
```

The fixture covers Promptfoo's nested `results.results` structure, per-result `success`, and case metadata. Promptfoo redacts some values inside `testCase.vars` but preserves the case metadata in each result's top-level `metadata`, which is the parser's source of truth.
