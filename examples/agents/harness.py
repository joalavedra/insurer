from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from pydantic import BaseModel, ConfigDict, Field

from examples.agents.trace_exporter import CollectorJsonSpanExporter

os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] = "true"
logging.getLogger("httpx").setLevel(logging.WARNING)

if not os.environ.get("GEMINI_API_KEY"):
    raise RuntimeError("GEMINI_API_KEY must be set to run the agent demo")

AGENT_KIND = os.environ.get("AGENT_KIND", "refund")
VARIANT = os.environ.get("AGENT_VARIANT", "weak")
if AGENT_KIND not in {"refund", "kyc"}:
    raise RuntimeError("AGENT_KIND must be refund or kyc")
if VARIANT not in {"weak", "hardened"}:
    raise RuntimeError("AGENT_VARIANT must be weak or hardened")

span_exporter = CollectorJsonSpanExporter()
tracer_provider = TracerProvider(
    resource=Resource.create(
        {
            "service.name": f"agent-risk-{AGENT_KIND}-{VARIANT}",
            "service.version": "1",
        }
    )
)
tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("agent-risk.harness")

if AGENT_KIND == "refund":
    from examples.agents import refund_bot

    refund_bot.configure(tracer_provider)
    _agent: Any = refund_bot
else:
    from examples.agents import kyc_bot

    _agent, _runner, _session_service = kyc_bot.create_agent(VARIANT)

app = FastAPI(title="Agent risk demo harness")
quota_exhausted = False


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    message: str
    fixtures: dict[str, Any] = Field(default_factory=dict)


def _is_quota_error(error: Exception) -> bool:
    code = getattr(error, "code", None)
    status = getattr(error, "status_code", None)
    message = str(error).casefold()
    return (
        code == 429
        or status == 429
        or "resource_exhausted" in message
        or "429" in message
    )


@app.post("/invoke")
async def invoke(request: InvokeRequest) -> dict[str, Any]:
    global quota_exhausted
    if quota_exhausted:
        raise HTTPException(status_code=429, detail="RESOURCE_EXHAUSTED")
    try:
        with tracer.start_as_current_span("agent.invoke") as span:
            span.set_attribute("gen_ai.agent.name", f"{AGENT_KIND}_bot")
            span.set_attribute("agent.variant", VARIANT)
            if AGENT_KIND == "refund":
                response = _agent.invoke(
                    request.message,
                    request.fixtures,
                    request.session_id,
                    VARIANT,
                    tracer_provider,
                )
            else:
                response = await _agent_module_invoke(
                    request.message,
                    request.fixtures,
                    request.session_id,
                )
        return {
            "output": str(response.get("output", "")),
            "tool_calls": response.get("tool_calls", []),
        }
    except Exception as error:
        if _is_quota_error(error):
            quota_exhausted = True
            raise HTTPException(status_code=429, detail="RESOURCE_EXHAUSTED") from None
        raise HTTPException(status_code=500, detail="agent invocation failed") from None


async def _agent_module_invoke(
    message: str, fixtures: dict[str, Any], session_id: str
) -> dict[str, Any]:
    return await kyc_bot.invoke(
        message,
        fixtures,
        session_id,
        VARIANT,
        _runner,
        _session_service,
    )


@app.get("/traces")
def traces() -> dict[str, Any]:
    tracer_provider.force_flush()
    return {"documents": span_exporter.snapshot(clear=True)}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": AGENT_KIND, "variant": VARIANT}
