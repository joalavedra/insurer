"""FastAPI entry point for quotes, policies, claims and ledger data."""

import os
import sqlite3
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from insurer.bordereaux import export_bordereau
from insurer.claims import ClaimsService
from insurer.ledger import Ledger
from insurer.policies import PolicyService
from insurer.storage import connect


class QuoteRequest(BaseModel):
    profile: dict[str, Any]
    start_date: str


class BindRequest(BaseModel):
    quote_id: str


class EndorseRequest(BaseModel):
    profile_changes: dict[str, Any]
    effective_date: str


class CancelRequest(BaseModel):
    effective_date: str


class ClaimRequest(BaseModel):
    policy_id: str
    cause: str
    loss_date: str
    notified_date: str
    purchase_id: str
    claimed_cents: int
    evidence: list[dict[str, Any]] = Field(default_factory=list)


def create_app(database: str | None = None) -> FastAPI:
    db_path = database or os.getenv("INSURER_DB") or "insurer.db"
    connection = connect(db_path)
    policies = PolicyService(connection)
    claims = ClaimsService(connection)
    app = FastAPI(title="Insurer API")
    app.state.database = db_path
    app.state.connection = connection
    app.state.policies = policies
    app.state.claims = claims

    @app.post("/quotes")
    def create_quote(request: QuoteRequest) -> dict[str, Any]:
        return policies.quote(request.profile, request.start_date)

    @app.post("/policies")
    def bind_policy(request: BindRequest) -> dict[str, Any]:
        try:
            return policies.bind(request.quote_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/policies/{policy_id}/endorse")
    def endorse_policy(policy_id: str, request: EndorseRequest) -> dict[str, Any]:
        try:
            return policies.endorse(
                policy_id, request.profile_changes, request.effective_date
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/policies/{policy_id}/cancel")
    def cancel_policy(policy_id: str, request: CancelRequest) -> dict[str, Any]:
        try:
            return policies.cancel(policy_id, request.effective_date)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/policies/{policy_id}")
    def get_policy(policy_id: str) -> dict[str, Any]:
        try:
            policy = policies.get_policy(policy_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return policy | {"versions": policies.versions(policy_id)}

    @app.post("/claims")
    def file_claim(request: ClaimRequest) -> dict[str, Any]:
        try:
            return claims.file_claim(
                request.policy_id,
                request.cause,
                request.loss_date,
                request.notified_date,
                request.purchase_id,
                request.claimed_cents,
                request.evidence,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/ledger/trial-balance")
    def trial_balance() -> list[dict[str, int | str]]:
        return Ledger(connection).trial_balance()

    @app.get("/bordereaux/{kind}")
    def get_bordereau(kind: str, month: str) -> PlainTextResponse:
        if kind not in {"premium", "claims"}:
            raise HTTPException(status_code=404, detail="bordereau kind not found")
        if len(month) != 7 or month[4] != "-":
            raise HTTPException(status_code=422, detail="month must be YYYY-MM")
        try:
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
