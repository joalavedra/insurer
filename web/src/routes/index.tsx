import { useState } from "react"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { parse as parseYaml } from "yaml"
import { toast } from "sonner"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Separator } from "@/components/ui/separator"
import { useWorkflow } from "@/lib/session-store"
import type { AgentSample, JsonObject, NormalizedEvidence } from "@/lib/types"
import { getAgentOpsSamples, scoreAgent } from "@/server/insurer"

export const Route = createFileRoute("/")({
  loader: () => getAgentOpsSamples(),
  component: AgentsPage,
})

function AgentsPage() {
  const samples = Route.useLoaderData()
  const navigate = useNavigate()
  const { setScored } = useWorkflow()
  const [manifestText, setManifestText] = useState("")
  const [evidenceText, setEvidenceText] = useState("")
  const [busy, setBusy] = useState(false)
  const [busySample, setBusySample] = useState("")

  async function score(
    manifest: JsonObject,
    evidence: NormalizedEvidence,
    evidenceJson: string,
    sampleId?: string,
  ) {
    setBusy(true)
    setBusySample(sampleId ?? "upload")
    try {
      const { scorecard, scorecardJson } = await scoreAgent({
        data: { manifest, evidenceJson },
      })
      setScored(
        manifest,
        evidence,
        evidenceJson,
        scorecard,
        scorecardJson,
        sampleId,
      )
      await navigate({ to: "/scorecard" })
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not score evidence")
    } finally {
      setBusy(false)
      setBusySample("")
    }
  }

  async function scoreUpload() {
    try {
      const manifest = parseYaml(manifestText)
      const parsedEvidence: unknown = JSON.parse(evidenceText)
      if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
        throw new Error("Manifest must be a YAML or JSON object")
      }
      if (
        typeof parsedEvidence !== "object" ||
        parsedEvidence === null ||
        Array.isArray(parsedEvidence)
      ) {
        throw new Error("Evidence must be a JSON object")
      }
      await score(
        manifest as JsonObject,
        parsedEvidence as NormalizedEvidence,
        evidenceText,
      )
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not parse upload")
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <p className="text-sm font-medium text-muted-foreground">Agent Ops Cover</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight">Agents</h1>
        <p className="mt-2 max-w-2xl text-muted-foreground">
          Score an agent&apos;s evidence, review its coverage, and bind a policy.
        </p>
      </div>
      {samples.length ? (
        <div className="grid gap-4 md:grid-cols-3">
          {samples.map((sample: AgentSample) => (
            <Card key={sample.id}>
              <CardHeader>
                <CardTitle>{sample.name}</CardTitle>
                <CardDescription>{sample.description}</CardDescription>
              </CardHeader>
              <CardContent>
                <Button
                  disabled={busy}
                  onClick={() =>
                    void score(
                      sample.manifest,
                      sample.evidence,
                      sample.evidence_json,
                      sample.id,
                    )
                  }
                >
                  {busySample === sample.id ? "Scoring…" : "Score"}
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <Alert>
          <AlertTitle>No demo samples found</AlertTitle>
          <AlertDescription>Upload a manifest and normalized evidence below.</AlertDescription>
        </Alert>
      )}
      <Separator />
      <Card>
        <CardHeader>
          <CardTitle>Upload your own</CardTitle>
          <CardDescription>
            Paste a YAML or JSON manifest and an agent-evidence.v1 JSON document.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-2">
            <Label htmlFor="manifest">Manifest (YAML or JSON)</Label>
            <textarea
              id="manifest"
              value={manifestText}
              onChange={(event) => setManifestText(event.target.value)}
              rows={12}
              spellCheck={false}
              className="w-full rounded-md border bg-background px-3 py-2 font-mono text-sm"
              placeholder="agent_id: my-agent"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="evidence">Evidence JSON</Label>
            <textarea
              id="evidence"
              value={evidenceText}
              onChange={(event) => setEvidenceText(event.target.value)}
              rows={12}
              spellCheck={false}
              className="w-full rounded-md border bg-background px-3 py-2 font-mono text-sm"
              placeholder='{ "version": "agent-evidence.v1", ... }'
            />
          </div>
          <Button
            disabled={busy || !manifestText.trim() || !evidenceText.trim()}
            onClick={() => void scoreUpload()}
          >
            {busySample === "upload" ? "Scoring…" : "Score evidence"}
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
