import { createFileRoute, Link } from "@tanstack/react-router"

import { Badge } from "@/components/ui/badge"
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
import { getAgentOpsAgents } from "@/server/insurer"

export const Route = createFileRoute("/monitor")({
  loader: () => getAgentOpsAgents(),
  component: MonitorPage,
})

function MonitorPage() {
  const agents = Route.useLoaderData()

  return (
    <div className="space-y-8">
      <div>
        <p className="text-sm font-medium text-muted-foreground">Monitor</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight">Agents</h1>
        <p className="mt-2 text-muted-foreground">
          Agents scored through Agent Ops, insured or not.
        </p>
      </div>
      {agents.length ? (
        <Card>
          <CardHeader>
            <CardTitle>Registered agents</CardTitle>
            <CardDescription>Last scored and current coverage.</CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Agent</TableHead>
                  <TableHead>Last scored</TableHead>
                  <TableHead>Coverage</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {agents.map((agent) => (
                  <TableRow key={agent.agent_id}>
                    <TableCell className="font-medium">{agent.agent_id}</TableCell>
                    <TableCell>
                      <time title={agent.updated_at}>
                        {relativeTime(agent.updated_at)}
                      </time>
                    </TableCell>
                    <TableCell>
                      {agent.policy_id ? (
                        <Link
                          to="/insurance/policy/$policyId"
                          params={{ policyId: agent.policy_id }}
                        >
                          <Badge>Insured · {agent.policy_id}</Badge>
                        </Link>
                      ) : (
                        <Link to="/insurance">
                          <Badge variant="secondary">Not insured</Badge>
                        </Link>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>No agents yet</CardTitle>
            <CardDescription>
              Score an agent in Insurance to add it to Monitor.
            </CardDescription>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            Send OTLP/HTTP JSON traces to the collector at{" "}
            <code>http://127.0.0.1:4318</code>, then score the agent in{" "}
            <Link to="/insurance" className="font-medium text-foreground underline">
              Insurance
            </Link>
            .
          </CardContent>
        </Card>
      )}
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
