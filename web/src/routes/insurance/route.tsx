import { createFileRoute, Link, Outlet } from "@tanstack/react-router"

import { useWorkflow } from "@/lib/session-store"

export const Route = createFileRoute("/insurance")({
  component: InsuranceLayout,
})

function InsuranceLayout() {
  const { state } = useWorkflow()
  const navClass =
    "rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition hover:bg-muted hover:text-foreground"

  return (
    <>
      <nav className="flex flex-wrap items-center gap-1" aria-label="Insurance workflow">
        <Link
          to="/insurance"
          className={navClass}
          activeProps={{ className: "text-foreground" }}
        >
          Agents
        </Link>
        <span className="px-1 text-muted-foreground">→</span>
        {state.scorecard ? (
          <Link
            to="/insurance/scorecard"
            className={navClass}
            activeProps={{ className: "text-foreground" }}
          >
            Scorecard
          </Link>
        ) : (
          <span className={`${navClass} cursor-not-allowed opacity-40`}>Scorecard</span>
        )}
        <span className="px-1 text-muted-foreground">→</span>
        {state.quote ? (
          <Link
            to="/insurance/quote"
            className={navClass}
            activeProps={{ className: "text-foreground" }}
          >
            Quote
          </Link>
        ) : (
          <span className={`${navClass} cursor-not-allowed opacity-40`}>Quote</span>
        )}
        <span className="px-1 text-muted-foreground">→</span>
        {state.policy ? (
          <Link
            to="/insurance/policy/$policyId"
            params={{ policyId: state.policy.policy_id }}
            className={navClass}
            activeProps={{ className: "text-foreground" }}
          >
            Policy
          </Link>
        ) : (
          <span className={`${navClass} cursor-not-allowed opacity-40`}>Policy</span>
        )}
      </nav>
      <Outlet />
    </>
  )
}
