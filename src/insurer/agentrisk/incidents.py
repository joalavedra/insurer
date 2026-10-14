from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from insurer.agentrisk.controls import violating_calls_for_control
from insurer.agentrisk.manifest import AgentManifest
from insurer.agentrisk.scorecard import load_framework
from insurer.agentrisk.traces import ToolCall, TraceData
from insurer.money import cents


@dataclass(frozen=True)
class Incident:
    peril: str
    control_id: str
    trace_id: str
    span_id: str
    tool: str
    loss_date: date
    evidenced_amount_cents: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "incident",
            "peril": self.peril,
            "control_id": self.control_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "tool": self.tool,
            "ts": datetime.combine(
                self.loss_date, datetime.min.time(), tzinfo=timezone.utc
            ).isoformat(),
            "evidenced_amount_cents": self.evidenced_amount_cents,
        }


def _evidenced_amount_cents(
    call: ToolCall, control_type: str, manifest: AgentManifest
) -> int | None:
    if control_type not in {"approval_threshold", "amount_limit"}:
        return None
    tool = manifest.tools_by_name[call.tool]
    if tool.effect != "money_out" or tool.amount_arg is None:
        return None
    value = call.args.get(tool.amount_arg)
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount):
        return None
    return cents(amount * 100)


def find_incidents(manifest: AgentManifest, trace_data: TraceData) -> list[Incident]:
    perils = load_framework()
    peril_by_control_type = {
        control_type: peril["id"]
        for peril in perils
        for control_type in peril["control_types"]
    }
    calls_by_key = {
        (call.trace_id, call.span_id): call for call in trace_data.tool_calls
    }
    incidents: list[Incident] = []
    for control in manifest.controls:
        peril_id = peril_by_control_type.get(control.type)
        if peril_id is None:
            raise ValueError(f"no peril is mapped to control type {control.type!r}")
        for call in violating_calls_for_control(control, manifest, trace_data):
            incidents.append(
                Incident(
                    peril=peril_id,
                    control_id=control.id,
                    trace_id=call.trace_id,
                    span_id=call.span_id,
                    tool=call.tool,
                    loss_date=datetime.fromtimestamp(
                        call.start_ns / 1_000_000_000, tz=timezone.utc
                    ).date(),
                    evidenced_amount_cents=_evidenced_amount_cents(
                        calls_by_key[(call.trace_id, call.span_id)],
                        control.type,
                        manifest,
                    ),
                )
            )
    return incidents
