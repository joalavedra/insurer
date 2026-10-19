import { Link } from "@tanstack/react-router"
import type { ReactNode } from "react"

import { Separator } from "@/components/ui/separator"

export function AppLayout({ children }: { children: ReactNode }) {
  const navClass =
    "rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition hover:bg-muted hover:text-foreground"

  return (
    <div className="min-h-svh">
      <header className="border-b bg-card/80">
        <div className="mx-auto flex max-w-6xl flex-col gap-4 px-6 py-5 sm:flex-row sm:items-center sm:justify-between">
          <Link to="/monitor" className="text-lg font-semibold tracking-tight">
            Agent Ops Cover
          </Link>
          <nav className="flex flex-wrap items-center gap-1" aria-label="Main navigation">
            <Link
              to="/monitor"
              className={navClass}
              activeProps={{ className: "bg-muted text-foreground" }}
            >
              Monitor
            </Link>
            <Link
              to="/insurance"
              className={navClass}
              activeProps={{ className: "bg-muted text-foreground" }}
            >
              Insurance
            </Link>
          </nav>
        </div>
      </header>
      <Separator />
      <main className="mx-auto max-w-6xl space-y-8 px-6 py-10">{children}</main>
    </div>
  )
}
