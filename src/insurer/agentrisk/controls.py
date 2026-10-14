from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from insurer.agentrisk.manifest import AgentManifest, ControlSpec, ToolSpec
from insurer.agentrisk.traces import GuardrailCheck, ToolCall, TraceData


@dataclass(frozen=True)
class ControlResult:
    id: str
    type: str
    status: str
    observations: int
    violations: int
    example_span_ids: tuple[str, ...]
    blocked_attempts: int = 0

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["example_span_ids"] = list(self.example_span_ids)
        return result


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _guardrail_checks_by_trace(
    checks: tuple[GuardrailCheck, ...],
) -> dict[str, list[GuardrailCheck]]:
    grouped: dict[str, list[GuardrailCheck]] = {}
    for check in checks:
        grouped.setdefault(check.trace_id, []).append(check)
    return grouped


def _is_blocked(call: ToolCall) -> bool:
    return call.error or "denied" in (call.result or "").casefold()


def _cumulative_amount(
    call: ToolCall,
    amount_arg: str | None,
    calls_by_trace: dict[str, list[ToolCall]],
) -> float | None:
    order_id = call.args.get("order_id")
    if order_id is None:
        return _number(call.args.get(amount_arg or ""))

    call_position = (call.start_ns, call.span_id)
    total = 0.0
    found = False
    for candidate in calls_by_trace.get(call.trace_id, []):
        if (
            candidate.tool != call.tool
            or candidate.args.get("order_id") != order_id
            or (candidate.start_ns, candidate.span_id) > call_position
            or _is_blocked(candidate)
        ):
            continue
        amount = _number(candidate.args.get(amount_arg or ""))
        if amount is None:
            return None
        total += amount
        found = True
    return total if found else None


def _approval_passed(
    call: ToolCall,
    control: ControlSpec,
    tool_map: dict[str, ToolSpec],
    calls_by_trace: dict[str, list[ToolCall]],
) -> bool:
    amount_arg = tool_map[control.tool or ""].amount_arg
    amount = _cumulative_amount(call, amount_arg, calls_by_trace)
    if amount is None or amount <= (control.threshold_eur or 0.0):
        return True
    approval_arg = None
    approval_spec = tool_map[control.approval_tool or ""]
    for candidate in calls_by_trace.get(call.trace_id, []):
        if candidate.tool != approval_spec.name or (
            candidate.start_ns,
            candidate.span_id,
        ) >= (call.start_ns, call.span_id):
            continue
        if candidate.error or "denied" in (candidate.result or "").casefold():
            continue
        target_order = call.args.get("order_id")
        approval_order = candidate.args.get("order_id")
        if target_order is not None and approval_order is not None:
            if target_order != approval_order:
                continue
        if "amount_eur" in candidate.args:
            approved_amount = _number(candidate.args["amount_eur"])
            if approved_amount is None or amount > approved_amount:
                continue
        approval_arg = candidate.span_id
        break
    return approval_arg is not None


def _violations_for_control(
    control: ControlSpec, manifest: AgentManifest, trace_data: TraceData
) -> tuple[int, int, int, list[str]]:
    calls_by_trace: dict[str, list[ToolCall]] = {}
    for call in trace_data.tool_calls:
        calls_by_trace.setdefault(call.trace_id, []).append(call)
    checks_by_trace = _guardrail_checks_by_trace(trace_data.guardrail_checks)
    tool_map = manifest.tools_by_name
    observations = 0
    blocked_attempts = 0
    examples: list[str] = []

    if control.type in {"approval_threshold", "amount_limit", "recipient_allowlist"}:
        matching = [call for call in trace_data.tool_calls if call.tool == control.tool]
        observations = len(matching)
        for call in matching:
            if _is_blocked(call):
                blocked_attempts += 1
                continue
            violated = False
            spec = tool_map[call.tool]
            if control.type == "approval_threshold":
                violated = not _approval_passed(call, control, tool_map, calls_by_trace)
            elif control.type == "amount_limit":
                amount = _cumulative_amount(call, spec.amount_arg, calls_by_trace)
                violated = amount is None or amount > (control.max_eur or 0.0)
            else:
                recipient = call.args.get(spec.recipient_arg or "")
                domain = (
                    recipient.rsplit("@", 1)[-1].lower()
                    if isinstance(recipient, str)
                    else ""
                )
                violated = domain not in control.allowed_domains
            if violated:
                examples.append(call.span_id)
    elif control.type == "input_guardrail":
        untrusted_tools = {tool.name for tool in manifest.tools if tool.untrusted_input}
        read_tools = {tool.name for tool in manifest.tools if tool.effect == "read"}
        for trace_id, trace_calls in calls_by_trace.items():
            ordered = sorted(
                trace_calls, key=lambda call: (call.start_ns, call.span_id)
            )
            untrusted = next(
                (call for call in ordered if call.tool in untrusted_tools), None
            )
            if untrusted is None:
                continue
            observations += 1
            subsequent = next(
                (
                    call
                    for call in ordered
                    if call.start_ns > untrusted.start_ns
                    and call.tool not in read_tools
                ),
                None,
            )
            if subsequent is None:
                continue
            guarded = any(
                check.kind == "input" and check.start_ns < subsequent.start_ns
                for check in checks_by_trace.get(trace_id, [])
            )
            if not guarded:
                examples.append(subsequent.span_id)
    elif control.type == "output_guardrail":
        for trace_id, trace_calls in calls_by_trace.items():
            if not trace_calls:
                continue
            observations += 1
            if not any(
                check.kind == "output" for check in checks_by_trace.get(trace_id, [])
            ):
                examples.append(trace_calls[0].span_id)

    violations = len(examples)
    return observations, violations, blocked_attempts, examples[:5]


def verify_controls(
    manifest: AgentManifest, trace_data: TraceData
) -> list[ControlResult]:
    results: list[ControlResult] = []
    for control in manifest.controls:
        observations, violations, blocked_attempts, examples = _violations_for_control(
            control, manifest, trace_data
        )
        status = (
            "violated"
            if violations
            else "verified"
            if observations >= 5
            else "unobserved"
        )
        results.append(
            ControlResult(
                id=control.id,
                type=control.type,
                status=status,
                observations=observations,
                violations=violations,
                example_span_ids=tuple(examples),
                blocked_attempts=blocked_attempts,
            )
        )
    return results
