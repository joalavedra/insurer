"""FastAPI entry point for quotes, policies, claims and ledger data."""

import os
import sqlite3
from datetime import date, datetime
from threading import Lock
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from insurer.bordereaux import export_bordereau
from insurer.claims import ClaimsService
from insurer.ledger import Ledger
from insurer.policies import PolicyService
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


def create_app(database: str | None = None) -> FastAPI:
    db_path = database or os.getenv("INSURER_DB") or "insurer.db"
    connection = connect(db_path)
    policies = PolicyService(connection)
    claims = ClaimsService(connection)
    lock = Lock()
    app = FastAPI(title="Insurer API")
    app.state.database = db_path
    app.state.connection = connection
    app.state.policies = policies
    app.state.claims = claims

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

    return app


def close_app_database(app: FastAPI) -> None:
    connection = app.state.connection
    if isinstance(connection, sqlite3.Connection):
        connection.close()
