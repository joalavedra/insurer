import { Link } from "@tanstack/react-router"
import type { ReactNode } from "react"

import { Separator } from "@/components/ui/separator"
import { useWorkflow } from "@/lib/session-store"

export function AppLayout({ children }: { children: ReactNode }) {
  const { state } = useWorkflow()
  const navClass =
    "rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition hover:bg-muted hover:text-foreground"
  const activeClass = "text-foreground"

  return (
    <div className="min-h-svh">
      <header className="border-b bg-card/80">
        <div className="mx-auto flex max-w-6xl flex-col gap-4 px-6 py-5 sm:flex-row sm:items-center sm:justify-between">
          <Link to="/" className="text-lg font-semibold tracking-tight">
            Agent Ops Cover
          </Link>
          <nav className="flex flex-wrap items-center gap-1" aria-label="Workflow">
            <Link to="/" className={navClass} activeProps={{ className: activeClass }}>
              Agents
            </Link>
            <span className="px-1 text-muted-foreground">→</span>
            {state.scorecard ? (
              <Link
                to="/scorecard"
                className={navClass}
                activeProps={{ className: activeClass }}
              >
                Scorecard
              </Link>
            ) : (
              <span className={`${navClass} cursor-not-allowed opacity-40`}>Scorecard</span>
            )}
            <span className="px-1 text-muted-foreground">→</span>
            {state.quote ? (
              <Link
                to="/quote"
                className={navClass}
                activeProps={{ className: activeClass }}
              >
                Quote
              </Link>
            ) : (
              <span className={`${navClass} cursor-not-allowed opacity-40`}>Quote</span>
            )}
            <span className="px-1 text-muted-foreground">→</span>
            {state.policy ? (
              <Link
                to="/policy/$policyId"
                params={{ policyId: state.policy.policy_id }}
                className={navClass}
                activeProps={{ className: activeClass }}
              >
                Policy
              </Link>
            ) : (
              <span className={`${navClass} cursor-not-allowed opacity-40`}>Policy</span>
            )}
          </nav>
        </div>
      </header>
      <Separator />
      <main className="mx-auto max-w-6xl space-y-8 px-6 py-10">{children}</main>
    </div>
  )
}
