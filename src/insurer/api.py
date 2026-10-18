"""FastAPI entry point for quotes, policies, claims and ledger data."""

import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from insurer.agentrisk.contracts import parse_normalized_evidence
from insurer.agentrisk.incidents import find_incidents
from insurer.agentrisk.manifest import parse_manifest
from insurer.agentrisk.service import (
    _validate_agent_claims_policy,
    agent_scorecard_profile,
    run_agent_claims,
    score_from_evidence,
)
from insurer.agentrisk.traces import load_trace_files
from insurer.bordereaux import export_bordereau
from insurer.claims import ClaimsService
from insurer.ledger import Ledger
from insurer.policies import PolicyService
from insurer.products import load_agent_ops_product
from insurer.storage import connect


class Profile(BaseModel):
    approval_threshold: Literal["none", "eur_200", "eur_50"]
    merchant_allowlist: bool
    rail: Literal["card", "x402"]
    tenure_months: int = Field(ge=0)
    monthly_spend_cap_cents: int = Field(gt=0)
    kill_switch: bool


class ProfileChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_threshold: Literal["none", "eur_200", "eur_50"] | None = None
    merchant_allowlist: bool | None = None
    rail: Literal["card", "x402"] | None = None
    tenure_months: int | None = Field(default=None, ge=0)
    monthly_spend_cap_cents: int | None = Field(default=None, gt=0)
    kill_switch: bool | None = None


class EvidenceEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    ts: str
    purchase_id: str | None = None
    amount_cents: int | None = Field(default=None, ge=0)
    state: bool | None = None

    @field_validator("ts")
    @classmethod
    def validate_iso_datetime(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("ts must be an ISO datetime") from error
        if value == parsed.date().isoformat():
            raise ValueError("ts must include a time")
        return value


class QuoteRequest(BaseModel):
    profile: Profile
    start_date: date


class BindRequest(BaseModel):
    quote_id: str


class EndorseRequest(BaseModel):
    profile_changes: ProfileChanges
    effective_date: date


class CancelRequest(BaseModel):
    effective_date: date


class ClaimRequest(BaseModel):
    policy_id: str
    cause: str
    loss_date: date
    notified_date: date
    purchase_id: str
    claimed_cents: int = Field(ge=0)
    evidence: list[EvidenceEvent] = Field(default_factory=list)


class AgentOpsScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, Any]
    evidence: dict[str, Any]


class AgentOpsQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scorecard: dict[str, Any]
    start_date: date


class AgentOpsBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_id: str


class AgentOpsClaimsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, Any]
    evidence: dict[str, Any]
    notified_date: date
    dry_run: bool = False


class AgentOpsLiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, Any] | None = None
    file_claims: bool = False


def create_app(database: str | None = None) -> FastAPI:
    db_path = database or os.getenv("INSURER_DB") or "insurer.db"
    connection = connect(db_path)
    policies = PolicyService(connection)
    claims = ClaimsService(connection)
    agent_ops_product = load_agent_ops_product()
    agent_policies = PolicyService(connection, agent_ops_product)
    agent_claims = ClaimsService(connection, agent_ops_product)
    lock = Lock()
    app = FastAPI(title="Insurer API")
    app.state.database = db_path
    app.state.connection = connection
    app.state.policies = policies
    app.state.claims = claims
    app.state.agent_policies = agent_policies
    app.state.agent_claims = agent_claims

    def agent_ops_warranted_controls(policy: dict[str, Any]) -> list[str]:
        quote_row = connection.execute(
            "SELECT payload_json FROM quotes WHERE quote_id = ?",
            (policy["quote_id"],),
        ).fetchone()
        quote = json.loads(quote_row["payload_json"]) if quote_row else {}
        rating = quote.get("rating")
        schedule = rating.get("schedule") if isinstance(rating, dict) else None
        return (
            schedule.get("warranted_controls", []) if isinstance(schedule, dict) else []
        )

    def agent_ops_policy_id(agent_id: str) -> str | None:
        rows = connection.execute(
            """
            SELECT p.policy_id, p.profile_json, q.payload_json
            FROM policies AS p
            JOIN quotes AS q ON q.quote_id = p.quote_id
            WHERE p.cancelled = 0
            ORDER BY p.rowid DESC
            """
        ).fetchall()
        for row in rows:
            profile = json.loads(row["profile_json"])
            scorecard = profile.get("scorecard") if isinstance(profile, dict) else None
            quote = json.loads(row["payload_json"])
            if (
                quote.get("product_key") == agent_ops_product.product_key
                and isinstance(scorecard, dict)
                and scorecard.get("agent_id") == agent_id
            ):
                return row["policy_id"]
        return None

    @app.post("/quotes")
    def create_quote(request: QuoteRequest) -> dict[str, Any]:
        try:
            with lock:
                return policies.quote(
                    request.profile.model_dump(), request.start_date.isoformat()
                )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/policies")
    def bind_policy(request: BindRequest) -> dict[str, Any]:
        try:
            with lock:
                return policies.bind(request.quote_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/policies/{policy_id}/endorse")
    def endorse_policy(policy_id: str, request: EndorseRequest) -> dict[str, Any]:
        try:
            with lock:
                return policies.endorse(
                    policy_id,
                    request.profile_changes.model_dump(exclude_none=True),
                    request.effective_date.isoformat(),
                )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/policies/{policy_id}/cancel")
    def cancel_policy(policy_id: str, request: CancelRequest) -> dict[str, Any]:
        try:
            with lock:
                return policies.cancel(policy_id, request.effective_date.isoformat())
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/policies/{policy_id}")
    def get_policy(policy_id: str) -> dict[str, Any]:
        try:
            with lock:
                policy = policies.get_policy(policy_id)
                versions = policies.versions(policy_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return policy | {"versions": versions}

    @app.post("/claims")
    def file_claim(request: ClaimRequest) -> dict[str, Any]:
        try:
            with lock:
                return claims.file_claim(
                    request.policy_id,
                    request.cause,
                    request.loss_date.isoformat(),
                    request.notified_date.isoformat(),
                    request.purchase_id,
                    request.claimed_cents,
                    [event.model_dump(exclude_none=True) for event in request.evidence],
                )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/ledger/trial-balance")
    def trial_balance() -> list[dict[str, int | str]]:
        with lock:
            return Ledger(connection).trial_balance()

    @app.get("/bordereaux/{kind}")
    def get_bordereau(kind: str, month: str) -> PlainTextResponse:
        if kind not in {"premium", "claims"}:
            raise HTTPException(status_code=404, detail="bordereau kind not found")
        if len(month) != 7 or month[4] != "-":
            raise HTTPException(status_code=422, detail="month must be YYYY-MM")
        try:
            with lock:
                return PlainTextResponse(
                    export_bordereau(connection, kind, month), media_type="text/csv"
                )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/agent-ops/samples")
    def get_agent_ops_samples() -> list[dict[str, Any]]:
        sample_dirs = (
            Path(__file__).parent / "agent_ops_samples",
            Path(__file__).resolve().parents[2] / "examples" / "agents" / "samples",
        )
        sample_paths = next(
            (
                paths
                for directory in sample_dirs
                if directory.is_dir() and (paths := sorted(directory.glob("*.json")))
            ),
            [],
        )
        if not sample_paths:
            return []
        samples = []
        for path in sample_paths:
            sample = json.loads(path.read_text(encoding="utf-8"))
            sample["evidence_json"] = json.dumps(sample["evidence"], ensure_ascii=False)
            samples.append(sample)
        return samples

    @app.post("/agent-ops/score")
    def score_agent(request: AgentOpsScoreRequest) -> dict[str, Any]:
        try:
            manifest = parse_manifest(request.manifest)
            evidence = parse_normalized_evidence(request.evidence)
            scorecard = score_from_evidence(manifest, evidence)
            with lock:
                connection.execute(
                    """
                    INSERT INTO agents(agent_id, manifest_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                        manifest_json = excluded.manifest_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        manifest.agent_id,
                        json.dumps(request.manifest),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                connection.commit()
            return scorecard
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/agent-ops/agents")
    def list_agent_ops_agents() -> list[dict[str, Any]]:
        with lock:
            rows = connection.execute(
                "SELECT agent_id, updated_at FROM agents ORDER BY agent_id"
            ).fetchall()
            return [
                {
                    "agent_id": row["agent_id"],
                    "updated_at": row["updated_at"],
                    "policy_id": agent_ops_policy_id(row["agent_id"]),
                }
                for row in rows
            ]

    @app.get("/agent-ops/agents/{agent_id}")
    def get_agent_ops_agent(agent_id: str) -> dict[str, Any]:
        with lock:
            row = connection.execute(
                """
                SELECT agent_id, manifest_json, updated_at
                FROM agents WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="agent not found")
            return {
                "agent_id": row["agent_id"],
                "updated_at": row["updated_at"],
                "manifest": json.loads(row["manifest_json"]),
                "policy_id": agent_ops_policy_id(row["agent_id"]),
            }

    @app.post("/agent-ops/quotes")
    def create_agent_ops_quote(request: AgentOpsQuoteRequest) -> dict[str, Any]:
        try:
            profile = agent_scorecard_profile(request.scorecard)
            with lock:
                return agent_policies.quote(profile, request.start_date.isoformat())
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/agent-ops/policies")
    def bind_agent_ops_policy(request: AgentOpsBindRequest) -> dict[str, Any]:
        try:
            with lock:
                return agent_policies.bind(request.quote_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/agent-ops/policies/{policy_id}")
    def get_agent_ops_policy(policy_id: str) -> dict[str, Any]:
        try:
            with lock:
                policy = agent_policies.get_policy(policy_id)
                claims_for_policy = agent_claims.claims_for_policy(policy_id)
                return policy | {
                    "claims": claims_for_policy,
                    "warranted_controls": agent_ops_warranted_controls(policy),
                }
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/agent-ops/policies/{policy_id}/live")
    def get_agent_ops_live_status(
        policy_id: str, request: AgentOpsLiveRequest
    ) -> dict[str, Any]:
        try:
            if request.manifest is None:
                with lock:
                    policy = agent_policies.get_policy(policy_id)
                    profile = policy.get("profile")
                    scorecard = (
                        profile.get("scorecard") if isinstance(profile, dict) else None
                    )
                    agent_id = (
                        scorecard.get("agent_id")
                        if isinstance(scorecard, dict)
                        else None
                    )
                    registry_row = (
                        connection.execute(
                            "SELECT manifest_json FROM agents WHERE agent_id = ?",
                            (agent_id,),
                        ).fetchone()
                        if isinstance(agent_id, str)
                        else None
                    )
                    if registry_row is None:
                        raise HTTPException(status_code=404, detail="agent not found")
                    manifest_data = json.loads(registry_row["manifest_json"])
            else:
                manifest_data = request.manifest
            manifest = parse_manifest(manifest_data)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", manifest.agent_id):
                raise ValueError("manifest agent_id must match [A-Za-z0-9_-]{1,64}")
            trace_dir = Path(os.getenv("INSURER_TRACE_DIR", "data/traces"))
            trace_files = sorted((trace_dir / manifest.agent_id).glob("*.jsonl"))
            trace_data = load_trace_files(trace_files, manifest)
            with lock:
                policy = agent_policies.get_policy(policy_id)
                _validate_agent_claims_policy(policy, manifest, agent_ops_product)
                warranties = agent_ops_warranted_controls(policy)
                detected = find_incidents(manifest, trace_data)
                today = datetime.now(timezone.utc).date()
                incidents = run_agent_claims(
                    None,
                    policy_id,
                    manifest,
                    trace_data,
                    today,
                    {},
                    agent_ops_product,
                    incident_finder=lambda _manifest, _trace_data: detected,
                )
                filed = (
                    run_agent_claims(
                        agent_claims,
                        policy_id,
                        manifest,
                        trace_data,
                        today,
                        {},
                        agent_ops_product,
                        incident_finder=lambda _manifest, _trace_data: detected,
                    )
                    if request.file_claims
                    else []
                )
                incident_rows = [
                    incident
                    | {
                        "filed": agent_claims.claim_exists(
                            policy_id,
                            f"{incident['trace_id']}:{incident['span_id']}",
                        )
                    }
                    for incident in incidents
                ]
                current_control_ids = {control.id for control in manifest.controls}
                incidents_by_control: dict[str, int] = {}
                for incident in detected:
                    incidents_by_control[incident.control_id] = (
                        incidents_by_control.get(incident.control_id, 0) + 1
                    )
                warranty_rows = [
                    {
                        "control_id": control_id,
                        "status": (
                            "missing"
                            if control_id not in current_control_ids
                            else "breached"
                            if incidents_by_control.get(control_id, 0)
                            else "ok"
                        ),
                        "incidents": incidents_by_control.get(control_id, 0),
                    }
                    for control_id in warranties
                ]
            timestamps = [call.start_ns for call in trace_data.tool_calls] + [
                check.start_ns for check in trace_data.guardrail_checks
            ]
            last_seen: str | None = None
            if timestamps:
                last_seen_ns = max(timestamps)
                last_seen = (
                    datetime.fromtimestamp(
                        last_seen_ns // 1_000_000_000, tz=timezone.utc
                    )
                    .replace(microsecond=(last_seen_ns % 1_000_000_000) // 1_000)
                    .isoformat()
                )
            return {
                "agent_id": manifest.agent_id,
                "trace_files": len(trace_files),
                "tool_calls": len(trace_data.tool_calls),
                "guardrail_checks": len(trace_data.guardrail_checks),
                "last_seen": last_seen,
                "warranties": warranty_rows,
                "incidents": incident_rows,
                "filed": filed,
            }
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/agent-ops/policies/{policy_id}/claims")
    def file_agent_ops_claims(
        policy_id: str, request: AgentOpsClaimsRequest
    ) -> list[dict[str, Any]]:
        try:
            manifest = parse_manifest(request.manifest)
            evidence = parse_normalized_evidence(request.evidence)
            with lock:
                return run_agent_claims(
                    claims=None if request.dry_run else agent_claims,
                    policy_id=policy_id,
                    manifest=manifest,
                    trace_data=evidence.trace_data,
                    notified_date=request.notified_date,
                    overrides={},
                    product=agent_ops_product,
                )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    return app


def close_app_database(app: FastAPI) -> None:
    connection = app.state.connection
    if isinstance(connection, sqlite3.Connection):
        connection.close()
