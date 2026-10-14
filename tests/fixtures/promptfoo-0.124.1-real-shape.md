# Promptfoo result fixture provenance

`promptfoo-0.124.1-real-shape.json` is an unmodified result file from the fixed refund-weak live exam. It was produced by `promptfoo@0.124.1` with telemetry disabled against the local agent-risk harness; the harness used synthetic cases and the Gemini API key was supplied through the process environment.

The command was:

```bash
.venv/bin/insurer agent-exam \
  --manifest examples/agents/manifests/refund_bot.yaml \
  --endpoint http://127.0.0.1:8000/invoke \
  --out /home/ubuntu/insurance/agent-risk-run/refund-weak-exam.json
```

The result for `p1_skip_approval` contains the full JSON harness response string in `response.output`; the matching `get_assert` call received that string and a dictionary context with `vars`, `test`, and `providerResponse`. The fixture preserves Promptfoo's nested `results.results` structure, per-result success and metadata. Promptfoo redacts some values in `testCase.vars`; top-level result metadata remains the parser's source of truth.
