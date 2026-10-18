import { useEffect, useState } from "react"
import { createFileRoute } from "@tanstack/react-router"
import { toast } from "sonner"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { formatMoney } from "@/lib/format"
import { useWorkflow } from "@/lib/session-store"
import type { AgentClaimResult, LiveStatus, Policy } from "@/lib/types"
import { getAgentPolicy, getLiveStatus, runAgentClaims } from "@/server/insurer"

export const Route = createFileRoute("/insurance/policy/$policyId")({
  loader: ({ params }) => getAgentPolicy({ data: params.policyId }),
  component: PolicyPage,
})

function PolicyPage() {
  const initialPolicy = Route.useLoaderData()
  const { state, setPolicy } = useWorkflow()
  const params = Route.useParams()
  const [policy, setPolicyView] = useState<Policy>(initialPolicy)
  const [notifiedDate, setNotifiedDate] = useState(
    state.sampleId ? "2026-10-20" : new Date().toISOString().slice(0, 10),
  )
  const [dryRun, setDryRun] = useState(false)
  const [busy, setBusy] = useState(false)
  const [results, setResults] = useState<AgentClaimResult[] | null>(null)
  const [liveStatus, setLiveStatus] = useState<LiveStatus | null>(null)
  const [liveError, setLiveError] = useState<string | null>(null)
  const [filingLiveClaims, setFilingLiveClaims] = useState(false)
  const manifest = state.manifest

  useEffect(() => {
    setPolicyView(initialPolicy)
    setPolicy(initialPolicy)
  }, [initialPolicy, setPolicy])

  useEffect(() => {
    if (state.sampleId) setNotifiedDate("2026-10-20")
  }, [state.sampleId])

  useEffect(() => {
    let active = true
    const poll = async () => {
      try {
        const status = await getLiveStatus({
          data: {
            policyId: params.policyId,
            ...(manifest ? { manifest } : {}),
            fileClaims: false,
          },
        })
        if (active) {
          setLiveStatus(status)
          setLiveError(null)
        }
      } catch (error) {
        if (active) {
          setLiveError(
            error instanceof Error ? error.message : "Could not read live status",
          )
        }
      }
    }
    void poll()
    const interval = window.setInterval(() => void poll(), 5000)
    return () => {
      active = false
      window.clearInterval(interval)
    }
  }, [manifest, params.policyId])

  async function detectClaims() {
    if (!state.manifest || !state.evidence || !state.evidenceJson) {
      toast.error("Score evidence before running claim detection")
      return
    }
    setBusy(true)
    try {
      const found = await runAgentClaims({
        data: {
          policyId: params.policyId,
          manifest: state.manifest,
          evidenceJson: state.evidenceJson,
          notified_date: notifiedDate,
          dry_run: dryRun,
        },
      })
      setResults(found)
      const updated = await getAgentPolicy({ data: params.policyId })
      setPolicyView(updated)
      setPolicy(updated)
      toast.success(dryRun ? "Dry run complete" : "Claims filed")
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not detect claims")
    } finally {
      setBusy(false)
    }
  }

  async function fileLiveClaims() {
    setFilingLiveClaims(true)
    try {
      const status = await getLiveStatus({
        data: {
          policyId: params.policyId,
          ...(manifest ? { manifest } : {}),
          fileClaims: true,
        },
      })
      setLiveStatus(status)
      const updated = await getAgentPolicy({ data: params.policyId })
      setPolicyView(updated)
      setPolicy(updated)
      toast.success(
        status.filed.length
          ? `Filed ${status.filed.length} claims`
          : "No new claims to file",
      )
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not file claims")
    } finally {
      setFilingLiveClaims(false)
    }
  }

  const unfiledIncidents = liveStatus?.incidents.filter(
    (incident) => !incident.filed,
  ) ?? []
  const unfiledClaims = new Set(
    unfiledIncidents.map((incident) => `${incident.trace_id}:${incident.span_id}`),
  ).size
  const agentId =
    typeof manifest?.agent_id === "string"
      ? manifest.agent_id
      : policy.profile.scorecard.agent_id
  const isLive =
    liveStatus?.last_seen !== null &&
    liveStatus?.last_seen !== undefined &&
    Date.now() - Date.parse(liveStatus.last_seen) < 5 * 60 * 1000

  return (
    <div className="space-y-8">
      <div>
        <p className="text-sm font-medium text-muted-foreground">Policy</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight">{policy.policy_id}</h1>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{policy.cancelled ? "Cancelled" : "Active"} · {policy.product_key}</CardTitle>
          <CardDescription>
            {policy.start_date} → {policy.end_date} · Quote {policy.quote_id}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-5 sm:grid-cols-3">
          <div>
            <p className="text-sm text-muted-foreground">Premium</p>
            <p className="text-xl font-semibold">{formatMoney(policy.premium_cents)}</p>
          </div>
          <div>
            <p className="text-sm text-muted-foreground">Tax</p>
            <p className="text-xl font-semibold">{formatMoney(policy.tax_cents)}</p>
          </div>
          <div className="sm:col-span-3">
            <p className="mb-2 text-sm font-medium">Warranted controls</p>
            <div className="flex flex-wrap gap-2">
              {policy.warranted_controls.length ? (
                policy.warranted_controls.map((control) => (
                  <span
                    key={control}
                    className="rounded-full border px-3 py-1 text-sm"
                  >
                    {control}
                  </span>
                ))
              ) : (
                <span className="text-sm text-muted-foreground">None</span>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Live monitoring</CardTitle>
          <CardDescription>
            Collector traces for {agentId} · refreshes every 5 seconds
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {liveError ? (
            <Alert variant="destructive">
              <AlertTitle>Could not load live status</AlertTitle>
              <AlertDescription>{liveError}</AlertDescription>
            </Alert>
          ) : null}
          <div className="flex flex-wrap items-center gap-5 text-sm">
            <div className="flex items-center gap-2">
              <span
                aria-label={isLive ? "live" : "not live"}
                className={`size-2 rounded-full ${isLive ? "bg-emerald-500" : "bg-muted-foreground"}`}
              />
              <span>{isLive ? "Live" : "Not live"}</span>
            </div>
            <span>
              Last seen:{" "}
              {liveStatus?.last_seen ? (
                <time title={liveStatus.last_seen}>
                  {relativeTime(liveStatus.last_seen)}
                </time>
              ) : (
                "Never"
              )}
            </span>
            <span>{liveStatus?.trace_files ?? 0} files</span>
            <span>{liveStatus?.tool_calls ?? 0} tool calls</span>
            <span>{liveStatus?.guardrail_checks ?? 0} guardrail checks</span>
          </div>
          {liveStatus?.trace_files === 0 ? (
            <p className="break-words text-xs text-muted-foreground">
              Replay demo traces:{" "}
              <code>
                while IFS= read -r document; do curl -fsS
                http://127.0.0.1:4318/v1/traces -H
                &apos;content-type: application/json&apos; -H
                &apos;x-insurer-agent: {agentId}&apos; --data-binary
                &quot;$document&quot;; done &lt;
                tests/fixtures/contracts/kyc-traces.jsonl
              </code>
            </p>
          ) : null}
          {liveStatus?.warranties.length ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Warranted control</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Incidents</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {liveStatus.warranties.map((warranty) => (
                  <TableRow key={warranty.control_id}>
                    <TableCell className="font-mono text-xs">
                      {warranty.control_id}
                    </TableCell>
                    <TableCell>
                      <Badge
                        className={
                          warranty.status === "ok"
                            ? "bg-emerald-600 text-white"
                            : warranty.status === "breached"
                              ? ""
                              : "bg-muted text-muted-foreground"
                        }
                        variant={
                          warranty.status === "breached" ? "destructive" : "secondary"
                        }
                      >
                        {warranty.status}
                      </Badge>
                    </TableCell>
                    <TableCell>{warranty.incidents}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : null}
          {unfiledIncidents.length ? (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-sm font-medium">
                  {unfiledIncidents.length} unfiled incident
                  {unfiledIncidents.length === 1 ? "" : "s"}
                </p>
                <Button
                  disabled={filingLiveClaims}
                  onClick={() => void fileLiveClaims()}
                >
                  {filingLiveClaims
                    ? "Filing…"
                    : `File ${unfiledClaims} claims`}
                </Button>
              </div>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Cause</TableHead>
                    <TableHead>Span</TableHead>
                    <TableHead>Loss date</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {unfiledIncidents.map((incident, index) => (
                    <TableRow
                      key={`${incident.trace_id}:${incident.span_id}:${index}`}
                    >
                      <TableCell>{incident.cause}</TableCell>
                      <TableCell className="font-mono text-xs">
                        {incident.span_id}
                      </TableCell>
                      <TableCell>{incident.loss_date}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : liveStatus?.incidents.length ? (
            <p className="text-sm text-muted-foreground">
              All detected incidents are already filed.
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Claims</CardTitle>
          <CardDescription>Claims recorded against this policy.</CardDescription>
        </CardHeader>
        <CardContent>
          {policy.claims.length ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>Cause</TableHead>
                  <TableHead>Loss</TableHead>
                  <TableHead>Notified</TableHead>
                  <TableHead>Claimed</TableHead>
                  <TableHead>Paid</TableHead>
                  <TableHead>Decision</TableHead>
                  <TableHead>Reason</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {policy.claims.map((claim) => (
                  <TableRow key={claim.id}>
                    <TableCell>{claim.id}</TableCell>
                    <TableCell>{claim.cause}</TableCell>
                    <TableCell>{claim.loss_date}</TableCell>
                    <TableCell>{claim.notified_date}</TableCell>
                    <TableCell>{formatMoney(claim.claimed_cents)}</TableCell>
                    <TableCell>{formatMoney(claim.paid_cents)}</TableCell>
                    <TableCell>{claim.decision}</TableCell>
                    <TableCell>{claim.reason}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="text-sm text-muted-foreground">No claims filed.</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Detect claims from traces</CardTitle>
          <CardDescription>
            Find incidents in the evidence used for this agent&apos;s scorecard.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          {!state.evidence || !state.manifest ? (
            <Alert>
              <AlertTitle>Evidence is unavailable</AlertTitle>
              <AlertDescription>Return to Agents and score evidence first.</AlertDescription>
            </Alert>
          ) : null}
          <div className="flex flex-wrap items-end gap-5">
            <div className="space-y-2">
              <Label htmlFor="notified-date">Notified date</Label>
              <Input
                id="notified-date"
                type="date"
                value={notifiedDate}
                onChange={(event) => setNotifiedDate(event.target.value)}
              />
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={dryRun}
                onChange={(event) => setDryRun(event.target.checked)}
              />
              Dry run
            </label>
            <Button
              disabled={busy || !state.evidence || !state.manifest || !notifiedDate}
              onClick={() => void detectClaims()}
            >
              {busy ? "Checking…" : "Run detection"}
            </Button>
          </div>
          {results ? (
            <div className="space-y-3">
              <h3 className="font-medium">Detection results</h3>
              {results.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Span</TableHead>
                      <TableHead>Tool / recipient</TableHead>
                      <TableHead>Cause</TableHead>
                      <TableHead>Loss date</TableHead>
                      <TableHead>Decision</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {results.map((result, index) => {
                      const spanId = "span_id" in result ? result.span_id : result.claim_id
                      const call = "span_id" in result
                        ? state.evidence?.tool_calls.find((item) => item.span_id === result.span_id)
                        : undefined
                      const decision =
                        typeof result.decision === "string"
                          ? result.decision
                          : result.decision.decision
                      const recipient =
                        call?.args.recipient === undefined
                          ? ""
                          : ` · ${String(call.args.recipient)}`
                      return (
                        <TableRow key={`${spanId}-${index}`}>
                          <TableCell className="font-mono text-xs">{spanId}</TableCell>
                          <TableCell>{call?.tool ?? "Filed claim"}{recipient}</TableCell>
                          <TableCell>{result.cause}</TableCell>
                          <TableCell>{result.loss_date}</TableCell>
                          <TableCell>{decision}</TableCell>
                        </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              ) : (
                <p className="text-sm text-muted-foreground">No incidents found.</p>
              )}
            </div>
          ) : null}
        </CardContent>
      </Card>
    </div>
  )
}

function relativeTime(timestamp: string): string {
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(timestamp)) / 1000))
  if (seconds < 60) return "just now"
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}
