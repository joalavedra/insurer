import { useState } from "react"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { Copy } from "lucide-react"
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
import { Separator } from "@/components/ui/separator"
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs"
import { controlClass, gradeClass } from "@/lib/format"
import { useWorkflow } from "@/lib/session-store"
import { createAgentQuote } from "@/server/insurer"

export const Route = createFileRoute("/scorecard")({ component: ScorecardPage })

function ScorecardPage() {
  const { state, setQuote } = useWorkflow()
  const navigate = useNavigate()
  const [startDate, setStartDate] = useState("2026-10-01")
  const [busy, setBusy] = useState(false)
  const scorecard = state.scorecard

  if (!scorecard) {
    return (
      <Alert>
        <AlertTitle>No scorecard yet</AlertTitle>
        <AlertDescription>Choose an agent or upload evidence first.</AlertDescription>
      </Alert>
    )
  }

  const readyScorecard = scorecard
  async function getQuote() {
    setBusy(true)
    try {
      const quote = await createAgentQuote({
        data: { scorecard: readyScorecard, start_date: startDate },
      })
      setQuote(quote)
      await navigate({ to: "/quote" })
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not rate scorecard")
    } finally {
      setBusy(false)
    }
  }

  const hash = scorecard.evidence.scorecard_sha256
  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-sm font-medium text-muted-foreground">Scorecard</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight">{scorecard.agent_id}</h1>
          <p className="mt-2 text-muted-foreground">
            {scorecard.framework} · {scorecard.model} · {scorecard.autonomy}
          </p>
        </div>
        <Badge className={`px-5 py-3 text-2xl ${gradeClass(scorecard.agent_grade)}`}>
          {scorecard.agent_grade ?? "not_exposed"}
        </Badge>
      </div>

      <Tabs defaultValue="perils" className="space-y-5">
        <TabsList>
          <TabsTrigger value="perils">Perils</TabsTrigger>
          <TabsTrigger value="evaluations">Customer evals</TabsTrigger>
        </TabsList>
        <TabsContent value="perils">
          <div className="grid gap-4 md:grid-cols-2">
            {scorecard.perils.map((peril) => (
              <Card key={peril.id}>
                <CardHeader className="flex-row items-start justify-between">
                  <div>
                    <CardTitle>{peril.id} · {peril.name}</CardTitle>
                    <CardDescription className="mt-2">
                      Exam: {peril.exam_level} · Controls: {peril.control_level}
                    </CardDescription>
                  </div>
                  <Badge className={gradeClass(peril.grade)}>
                    {peril.grade ?? "not_exposed"}
                  </Badge>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="space-y-2">
                    <p className="text-sm font-medium">Controls</p>
                    {peril.controls?.length ? (
                      <div className="flex flex-wrap gap-2">
                        {peril.controls.map((control) => (
                          <Badge
                            key={control.id}
                            variant="outline"
                            className={controlClass(control.status)}
                          >
                            {control.id}: {control.status}
                          </Badge>
                        ))}
                      </div>
                    ) : (
                      <p className="text-sm text-muted-foreground">No controls exposed.</p>
                    )}
                  </div>
                  <Separator />
                  <div className="grid grid-cols-2 gap-3 text-sm">
                    <div>
                      <p className="text-muted-foreground">Incidents</p>
                      <p className="font-medium">{peril.incidents?.count ?? 0}</p>
                    </div>
                    <div>
                      <p className="text-muted-foreground">Observed calls</p>
                      <p className="font-medium">{peril.exposure?.observed_calls ?? 0}</p>
                    </div>
                  </div>
                  <div className="space-y-2">
                    <p className="text-sm font-medium">Exposed tools</p>
                    <div className="flex flex-wrap gap-2">
                      {peril.exposed_tools?.length ? (
                        peril.exposed_tools.map((tool) => (
                          <Badge key={tool} variant="secondary">{tool}</Badge>
                        ))
                      ) : (
                        <span className="text-sm text-muted-foreground">None</span>
                      )}
                    </div>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        </TabsContent>
        <TabsContent value="evaluations">
          <Card>
            <CardHeader>
              <CardTitle>Customer evals (no credit)</CardTitle>
              <CardDescription>
                Customer-reported evaluations are shown for context only.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <pre className="overflow-auto rounded-md bg-muted p-4 text-xs">
                {JSON.stringify(scorecard.self_reported, null, 2)}
              </pre>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      <Card>
        <CardHeader>
          <CardTitle>Scorecard integrity</CardTitle>
          <CardDescription>Integrity hash, not a signature</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-3">
          <code className="min-w-0 flex-1 break-all font-mono text-sm">{hash}</code>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              void navigator.clipboard.writeText(hash)
              toast.success("Hash copied")
            }}
          >
            <Copy className="mr-2 size-4" />
            Copy
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Get quote</CardTitle>
          <CardDescription>Choose the policy start date.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-4">
          <div className="space-y-2">
            <Label htmlFor="start-date">Start date</Label>
            <Input
              id="start-date"
              type="date"
              value={startDate}
              onChange={(event) => setStartDate(event.target.value)}
            />
          </div>
          <Button disabled={busy || !startDate} onClick={() => void getQuote()}>
            {busy ? "Rating…" : "Get quote"}
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
