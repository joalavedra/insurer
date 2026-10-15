import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react"

import type {
  JsonObject,
  NormalizedEvidence,
  Policy,
  Quote,
  Scorecard,
} from "@/lib/types"

export type WorkflowState = {
  manifest: JsonObject | null
  evidence: NormalizedEvidence | null
  scorecard: Scorecard | null
  scorecardJson: string | null
  quote?: Quote
  policy?: Policy
  sampleId?: string
}

type WorkflowContextValue = {
  state: WorkflowState
  ready: boolean
  setScored: (
    manifest: JsonObject,
    evidence: NormalizedEvidence,
    scorecard: Scorecard,
    scorecardJson: string,
    sampleId?: string,
  ) => void
  setQuote: (quote: Quote) => void
  setPolicy: (policy: Policy) => void
}

const emptyState: WorkflowState = {
  manifest: null,
  evidence: null,
  scorecard: null,
  scorecardJson: null,
}

const WorkflowContext = createContext<WorkflowContextValue | null>(null)

export function WorkflowProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<WorkflowState>(emptyState)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    try {
      const saved = sessionStorage.getItem("agent-ops-workflow")
      if (saved) {
        const restored = JSON.parse(saved) as WorkflowState
        setState({
          ...emptyState,
          ...restored,
          scorecardJson:
            typeof restored.scorecardJson === "string"
              ? restored.scorecardJson
              : null,
        })
      }
    } catch {
      sessionStorage.removeItem("agent-ops-workflow")
    }
    setReady(true)
  }, [])

  useEffect(() => {
    if (ready) sessionStorage.setItem("agent-ops-workflow", JSON.stringify(state))
  }, [ready, state])

  const setScored = useCallback(
    (
      manifest: JsonObject,
      evidence: NormalizedEvidence,
      scorecard: Scorecard,
      scorecardJson: string,
      sampleId?: string,
    ) => setState({ manifest, evidence, scorecard, scorecardJson, sampleId }),
    [],
  )
  const setQuote = useCallback(
    (quote: Quote) => setState((old) => ({ ...old, quote, policy: undefined })),
    [],
  )
  const setPolicy = useCallback(
    (policy: Policy) => setState((old) => ({ ...old, policy })),
    [],
  )
  const value = useMemo(
    () => ({ state, ready, setScored, setQuote, setPolicy }),
    [state, ready, setScored, setQuote, setPolicy],
  )
  return <WorkflowContext.Provider value={value}>{children}</WorkflowContext.Provider>
}

export function useWorkflow() {
  const context = useContext(WorkflowContext)
  if (!context) throw new Error("useWorkflow must be used inside WorkflowProvider")
  return context
}
