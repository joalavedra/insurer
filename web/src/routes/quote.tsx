import { useState } from "react"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
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
import { bindAgentPolicy } from "@/server/insurer"

export const Route = createFileRoute("/quote")({ component: QuotePage })

function QuotePage() {
  const { state, setPolicy } = useWorkflow()
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const quote = state.quote

  if (!quote) {
    return (
      <Alert>
        <AlertTitle>No quote yet</AlertTitle>
        <AlertDescription>Get a quote from the scorecard first.</AlertDescription>
      </Alert>
    )
  }

  const readyQuote = quote
  const boundPolicy =
    state.policy?.quote_id === readyQuote.quote_id ? state.policy : null
  const schedule = readyQuote.rating.schedule
  async function bind() {
    setBusy(true)
    try {
      const policy = await bindAgentPolicy({
        data: { quote_id: readyQuote.quote_id },
      })
      setPolicy(policy)
      await navigate({
        to: "/policy/$policyId",
        params: { policyId: policy.policy_id },
      })
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not bind policy")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-sm font-medium text-muted-foreground">Quote · {quote.quote_id}</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight">
            {quote.status === "quoted" ? "Coverage quote" : "Quote declined"}
          </h1>
        </div>
        <Badge variant={quote.status === "quoted" ? "default" : "destructive"}>
          {quote.status}
        </Badge>
      </div>

      {quote.reason ? (
        <Alert variant={quote.status === "declined" ? "destructive" : "default"}>
          <AlertTitle>{quote.status === "declined" ? "Reason" : "Rating note"}</AlertTitle>
          <AlertDescription>{quote.reason}</AlertDescription>
        </Alert>
      ) : null}

      <div className="grid gap-4 md:grid-cols-3">
        <Card className="md:col-span-1">
          <CardHeader>
            <CardDescription>Total payable</CardDescription>
            <CardTitle className="text-3xl">
              {formatMoney(quote.rating.total_payable_cents)}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-muted-foreground">Premium</span>
              <span>{formatMoney(quote.rating.technical_premium_cents)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">Tax</span>
              <span>{formatMoney(quote.rating.premium_tax_cents)}</span>
            </div>
          </CardContent>
        </Card>
        <Card className="md:col-span-2">
          <CardHeader>
            <CardTitle>Policy period</CardTitle>
            <CardDescription>{quote.start_date} → {quote.end_date}</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-5 sm:grid-cols-2">
            <div className="space-y-2">
              <p className="text-sm font-medium">Covered perils</p>
              <div className="flex flex-wrap gap-2">
                {schedule?.covered_perils.map((peril) => (
                  <Badge key={peril}>{peril}</Badge>
                ))}
                {!schedule?.covered_perils.length ? (
                  <span className="text-sm text-muted-foreground">None</span>
                ) : null}
              </div>
            </div>
            <div className="space-y-2">
              <p className="text-sm font-medium">Excluded perils</p>
              <div className="flex flex-wrap gap-2">
                {schedule?.excluded_perils.map((peril) => (
                  <Badge key={peril} variant="destructive">{peril}</Badge>
                ))}
                {!schedule?.excluded_perils.length ? (
                  <span className="text-sm text-muted-foreground">None</span>
                ) : null}
              </div>
            </div>
            <div className="space-y-2 sm:col-span-2">
              <p className="text-sm font-medium">Warranted controls</p>
              <div className="flex flex-wrap gap-2">
                {schedule?.warranted_controls.map((control) => (
                  <Badge key={control} variant="secondary">{control}</Badge>
                ))}
                {!schedule?.warranted_controls.length ? (
                  <span className="text-sm text-muted-foreground">None</span>
                ) : null}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Rating steps</CardTitle>
          <CardDescription>Calculation details for this quote.</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Value</TableHead>
                <TableHead>Note</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {quote.rating.steps.map((step, index) => (
                <TableRow key={`${step.name}-${index}`}>
                  <TableCell className="font-mono text-xs">{step.name}</TableCell>
                  <TableCell>
                    {step.name.includes("_cents") && typeof step.value === "number"
                      ? formatMoney(step.value)
                      : step.value}
                  </TableCell>
                  <TableCell className="text-muted-foreground">{step.note}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {quote.status === "quoted" ? (
        <div className="flex justify-end">
          {boundPolicy ? (
            <Button
              onClick={() =>
                void navigate({
                  to: "/policy/$policyId",
                  params: { policyId: boundPolicy.policy_id },
                })
              }
            >
              View policy
            </Button>
          ) : (
            <Button disabled={busy} onClick={() => void bind()}>
              {busy ? "Binding…" : "Bind policy"}
            </Button>
          )}
        </div>
      ) : null}
    </div>
  )
}
