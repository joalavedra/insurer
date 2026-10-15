import { createServerFn } from "@tanstack/react-start"

import type {
  AgentClaimResult,
  AgentSample,
  JsonObject,
  LiveStatus,
  Policy,
  Quote,
  Scorecard,
} from "@/lib/types"

const apiUrl = process.env.INSURER_API_URL ?? "http://127.0.0.1:8000"

async function requestResponse(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`${apiUrl}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  })
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as
      | { detail?: unknown }
      | null
    const detail =
      typeof body?.detail === "string"
        ? body.detail
        : JSON.stringify(body?.detail ?? `Request failed (${response.status})`)
    throw new Error(detail)
  }
  return response
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await requestResponse(path, init)
  return (await response.json()) as T
}

function assertJsonObject(text: string): void {
  const value: unknown = JSON.parse(text)
  if (
    typeof value !== "object" ||
    value === null ||
    Array.isArray(value)
  ) {
    throw new Error("evidenceJson must contain a JSON object")
  }
}

export const getAgentOpsSamples = createServerFn({ method: "GET" }).handler(
  async () => request<AgentSample[]>("/agent-ops/samples"),
)

export const scoreAgent = createServerFn({ method: "POST" })
  .validator(
    (data: { manifest: JsonObject; evidenceJson: string }) =>
      data,
  )
  .handler(async ({ data }) => {
    assertJsonObject(data.evidenceJson)
    const response = await requestResponse("/agent-ops/score", {
      method: "POST",
      body: `{"manifest":${JSON.stringify(data.manifest)},"evidence":${data.evidenceJson}}`,
    })
    const text = await response.text()
    return { scorecard: JSON.parse(text) as Scorecard, scorecardJson: text }
  })

export const createAgentQuote = createServerFn({ method: "POST" })
  .validator(
    (data: { scorecardJson: string; start_date: string }) => data,
  )
  .handler(async ({ data }) => {
    const scorecard: unknown = JSON.parse(data.scorecardJson)
    if (
      typeof scorecard !== "object" ||
      scorecard === null ||
      Array.isArray(scorecard)
    ) {
      throw new Error("scorecardJson must contain a JSON object")
    }
    return request<Quote>("/agent-ops/quotes", {
      method: "POST",
      body: `{"scorecard":${data.scorecardJson},"start_date":${JSON.stringify(data.start_date)}}`,
    })
  })

export const bindAgentPolicy = createServerFn({ method: "POST" })
  .validator((data: { quote_id: string }) => data)
  .handler(async ({ data }) =>
    request<Policy>("/agent-ops/policies", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  )

export const getAgentPolicy = createServerFn({ method: "GET" })
  .validator((policyId: string) => policyId)
  .handler(async ({ data }) =>
    request<Policy>(`/agent-ops/policies/${encodeURIComponent(data)}`),
  )

export const runAgentClaims = createServerFn({ method: "POST" })
  .validator(
    (data: {
      policyId: string
      manifest: JsonObject
      evidenceJson: string
      notified_date: string
      dry_run: boolean
    }) => data,
  )
  .handler(async ({ data }) => {
    assertJsonObject(data.evidenceJson)
    return request<AgentClaimResult[]>(
      `/agent-ops/policies/${encodeURIComponent(data.policyId)}/claims`,
      {
        method: "POST",
        body: `{"manifest":${JSON.stringify(data.manifest)},"evidence":${data.evidenceJson},"notified_date":${JSON.stringify(data.notified_date)},"dry_run":${JSON.stringify(data.dry_run)}}`,
      },
    )
  })

export const getLiveStatus = createServerFn({ method: "POST" })
  .validator(
    (data: {
      policyId: string
      manifest: JsonObject
      fileClaims: boolean
    }) => data,
  )
  .handler(async ({ data }) =>
    request<LiveStatus>(
      `/agent-ops/policies/${encodeURIComponent(data.policyId)}/live`,
      {
        method: "POST",
        body: JSON.stringify({
          manifest: data.manifest,
          file_claims: data.fileClaims,
        }),
      },
    ),
  )
