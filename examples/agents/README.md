# Agent-risk demo agents

These demos use synthetic data only. They are independent of the Agent Spend Cover product. Never send real customer data: the harness enables ADK message-content capture so tool arguments and results are present in trace evidence.

Install the pinned, optional frameworks from the repository root:

```bash
pip install -e '.[demo]'
```

Keep `GEMINI_API_KEY` in the process environment; do not echo, log, or place it in a URL. Run the commands from the repository root so the package can import the examples. Each process handles one variant at a time. Start the harness:

```bash
AGENT_KIND=refund AGENT_VARIANT=weak \
  python -m examples.agents.harness
```

The harness binds to `127.0.0.1` by default; set `HARNESS_HOST` to override it. `/invoke` and `/traces` are unauthenticated demo endpoints. Keep them on loopback and use synthetic data; do not expose them to untrusted networks.

In another terminal, replay 20 scripted sessions, run the insurer-owned exam, and score its traces:

```bash
python -m examples.agents.traffic \
  --agent-kind refund \
  --endpoint http://127.0.0.1:8000 \
  --out-traces /home/ubuntu/insurance/agent-risk-run/refund-weak-traces.jsonl
insurer agent-exam \
  --manifest examples/agents/manifests/refund_bot.yaml \
  --endpoint http://127.0.0.1:8000/invoke \
  --out /home/ubuntu/insurance/agent-risk-run/refund-weak-exam.json
insurer agent-score \
  --manifest examples/agents/manifests/refund_bot.yaml \
  --traces /home/ubuntu/insurance/agent-risk-run/refund-weak-traces.jsonl \
  --exam /home/ubuntu/insurance/agent-risk-run/refund-weak-exam.json \
  --out /home/ubuntu/insurance/agent-risk-run/refund-weak-score.json
```

Stop the server with Ctrl+C, then restart with `AGENT_VARIANT=hardened`. Repeat the replay and exam into new `refund-hardened-*` paths. For KYC use `AGENT_KIND=kyc` and `examples/agents/manifests/kyc_bot.yaml`, saving outputs with `kyc-weak-*` and `kyc-hardened-*` names. Run the four pipelines sequentially, not in parallel.

The weak and hardened variants share the exact same manifest. Weak tools omit caps, approval checks, allowlists, and guardrail spans; hardened tools enforce those controls and emit input/output guardrail evidence. The insurer exam is independent of customer-provided CI evals. Promptfoo concurrency is one and retries are disabled; stop on the first `429 RESOURCE_EXHAUSTED` and do not retry.
