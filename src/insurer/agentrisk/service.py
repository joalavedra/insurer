from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from insurer.agentrisk.contracts import NormalizedEvidence
from insurer.agentrisk.controls import approval_for_call, approval_passed
from insurer.agentrisk.exam import default_battery_path
from insurer.agentrisk.incidents import Incident, find_incidents
from insurer.agentrisk.manifest import AgentManifest
from insurer.agentrisk.scorecard import build_scorecard, load_framework, sha256_file
from insurer.agentrisk.traces import ToolCall, TraceData
from insurer.claims import ClaimsService
from insurer.products import Product


def agent_scorecard_profile(scorecard: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(scorecard, dict):
        raise ValueError("scorecard JSON must contain an object")
    perils = scorecard.get("perils")
    if not isinstance(perils, list) or not perils:
        raise ValueError("scorecard must contain peril exposure values")
    sessions = [
        peril.get("exposure", {}).get("sessions_per_month")
        for peril in perils
        if isinstance(peril, dict)
    ]
    if not sessions or any(value is None for value in sessions):
        raise ValueError("scorecard is missing per-peril sessions_per_month")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in sessions):
        raise ValueError("scorecard sessions_per_month values must be integers")
    if len(set(sessions)) != 1:
        raise ValueError("scorecard perils have inconsistent sessions_per_month")
    return {"scorecard": scorecard, "sessions_per_month": sessions[0]}


def score_from_evidence(
    manifest: AgentManifest,
    evidence: NormalizedEvidence,
    battery_path: Path | None = None,
) -> dict[str, Any]:
    selected_battery = battery_path or default_battery_path()
    evidence_files = evidence.inputs | {
        "battery": {
            "path": str(battery_path)
            if battery_path is not None
            else selected_battery.name,
            "sha256": sha256_file(selected_battery),
        }
    }
    return build_scorecard(
        manifest=manifest,
        trace_data=evidence.trace_data,
        exam_results=evidence.exam_results,
        ci_eval_results=evidence.ci_eval_results,
        evidence_files=evidence_files,
        battery_path=selected_battery,
        exam_present=evidence.exam_present,
    )


def _validate_agent_claims_policy(
    policy: dict[str, Any], manifest: AgentManifest, product: Product
) -> None:
    if policy.get("product_key") != product.product_key:
        raise ValueError(
            f"policy product key {policy.get('product_key')!r} does not match "
            f"loaded product {product.product_key!r}"
        )
    profile = policy.get("profile")
    scorecard = profile.get("scorecard") if isinstance(profile, dict) else None
    policy_agent_id = scorecard.get("agent_id") if isinstance(scorecard, dict) else None
    if policy_agent_id != manifest.agent_id:
        raise ValueError(
            "manifest agent_id does not match policy-bound scorecard agent_id"
        )


def _agent_incident_evidence(
    incident: Incident,
    manifest: AgentManifest,
    trace_data: TraceData,
    notified_date: date,
) -> list[dict[str, Any]]:
    violating_call = next(
        call
        for call in trace_data.tool_calls
        if call.trace_id == incident.trace_id and call.span_id == incident.span_id
    )
    incident_record = incident.as_dict()
    incident_record["ts"] = datetime.fromtimestamp(
        violating_call.start_ns / 1_000_000_000, tz=timezone.utc
    ).isoformat()
    evidence: list[dict[str, Any]] = [
        incident_record,
        {
            "type": "manifest_controls",
            "ts": datetime.combine(
                notified_date, datetime.min.time(), tzinfo=timezone.utc
            ).isoformat(),
            "control_ids": [control.id for control in manifest.controls],
        },
    ]
    calls_by_trace: dict[str, list[ToolCall]] = {}
    for call in trace_data.tool_calls:
        calls_by_trace.setdefault(call.trace_id, []).append(call)
    tool_map = manifest.tools_by_name
    for control in manifest.controls:
        if (
            control.type != "approval_threshold"
            or control.tool != violating_call.tool
            or not approval_passed(violating_call, control, tool_map, calls_by_trace)
        ):
            continue
        approval_call = approval_for_call(
            violating_call, control, tool_map, calls_by_trace
        )
        if approval_call is None:
            continue
        evidence.append(
            {
                "type": "human_approval",
                "ts": datetime.fromtimestamp(
                    approval_call.start_ns / 1_000_000_000, tz=timezone.utc
                ).isoformat(),
                "span_id": violating_call.span_id,
            }
        )
        break
    return evidence


def run_agent_claims(
    claims: ClaimsService | None,
    policy_id: str,
    manifest: AgentManifest,
    trace_data: TraceData,
    notified_date: date,
    overrides: dict[str, int],
    product: Product,
    *,
    incident_finder: Callable[[AgentManifest, TraceData], list[Incident]] | None = None,
) -> list[dict[str, Any]]:
    incidents = (incident_finder or find_incidents)(manifest, trace_data)
    if claims is not None:
        policy = claims.policies.get_policy(policy_id)
        _validate_agent_claims_policy(policy, manifest, product)

    peril_names = {item["id"]: item["name"] for item in load_framework()}
    claims_output: list[dict[str, Any]] = []
    for incident in incidents:
        purchase_id = f"{incident.trace_id}:{incident.span_id}"
        if claims is not None and claims.claim_exists(policy_id, purchase_id):
            continue
        claimed_cents = overrides.get(incident.span_id)
        if claimed_cents is None:
            claimed_cents = incident.evidenced_amount_cents or 0
        evidence = _agent_incident_evidence(
            incident, manifest, trace_data, notified_date
        )
        if claims is None:
            claims_output.append(
                {
                    "trace_id": incident.trace_id,
                    "span_id": incident.span_id,
                    "cause": peril_names[incident.peril],
                    "loss_date": incident.loss_date.isoformat(),
                    "claimed_cents": claimed_cents,
                    "evidence": evidence,
                    "decision": "dry_run",
                }
            )
            continue
        claims_output.append(
            claims.file_claim(
                policy_id,
                peril_names[incident.peril],
                incident.loss_date.isoformat(),
                notified_date.isoformat(),
                purchase_id,
                claimed_cents,
                evidence,
            )
        )
    return claims_output
