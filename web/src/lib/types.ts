export type Grade = "A" | "B" | "C" | "D" | "E" | null
export type JsonValue = string | number | boolean | null | JsonValue[] | JsonObject
export type JsonObject = { [key: string]: JsonValue }
export type ControlStatus =
  | "verified"
  | "violated"
  | "unobserved"

export type FileInput = { path: string; sha256: string }
export type EvidenceInputs = {
  manifest: FileInput
  traces: FileInput[]
  exam_results: FileInput | null
  ci_evals: FileInput[]
}

export type ToolCall = {
  trace_id: string
  span_id: string
  parent_span_id: string | null
  start_ns: number
  agent: string
  tool: string
  args: JsonObject
  result: string | null
  error: boolean
  convention: "gen_ai" | "openinference"
  end_ns: number
}

export type GuardrailCheck = {
  trace_id: string
  kind: "input" | "output"
  passed: boolean
  start_ns: number
}

export type ExamCaseResult = {
  case_id: string
  peril: string
  passed: boolean
  metadata: JsonObject
}

export type NormalizedEvidence = {
  version: "agent-evidence.v1"
  tool_calls: ToolCall[]
  guardrail_checks: GuardrailCheck[]
  warnings: number
  exam_results: ExamCaseResult[]
  ci_eval_results: ExamCaseResult[]
  exam_present: boolean
  inputs: EvidenceInputs
}

export type ControlResult = {
  id: string
  type: string
  status: ControlStatus
  observations: number
  violations: number
  blocked_attempts: number
  example_span_ids: string[]
}

export type Peril = {
  id: string
  name: string
  status: "exposed" | "not_exposed"
  points: number | null
  grade: Grade
  exam_level: "pass" | "partial" | "fail" | "untested" | "not_exposed"
  control_level: "verified" | "partial" | "none" | "violated" | "not_exposed"
  controls?: ControlResult[]
  exam?: {
    cases: number
    passed: number
    failed: number
    case_ids: string[]
    failed_cases: string[]
  }
  refs?: string[]
  exposed_tools?: string[]
  incidents?: { count: number; examples: JsonObject[] }
  exposure?: {
    observed_calls: number
    sessions_per_month: number
    declared_max_amount_eur: number | null
    window: { start_ns: number | null; end_ns: number | null; days: number }
  }
}

export type Scorecard = {
  agent_id: string
  framework: string
  model: string
  autonomy: "autonomous" | "human_in_loop"
  agent_grade: Grade
  perils: Peril[]
  self_reported: {
    note: "no credit"
    perils: Record<
      string,
      {
        cases: number
        passed: number
        pass_rate: number | null
        case_ids: string[]
      }
    >
  }
  evidence: {
    inputs: EvidenceInputs & {
      battery: FileInput
      framework: FileInput
    }
    scorecard_sha256: string
  }
}

export type RatingStep = {
  name: string
  value: number | string
  note: string
}

export type Schedule = {
  covered_perils: string[]
  excluded_perils: string[]
  warranted_controls: string[]
  agent_id: string
  scorecard_sha256: string
}

export type Quote = {
  quote_id: string
  profile: { scorecard: Scorecard; sessions_per_month: number }
  start_date: string
  end_date: string
  rating: {
    status: "quoted" | "declined"
    reason: string | null
    frequency: number
    expected_covered_severity_cents: number
    expected_loss_cost_cents: number
    technical_premium_cents: number
    premium_tax_cents: number
    total_payable_cents: number
    schedule?: Schedule
    steps: RatingStep[]
  }
  status: "quoted" | "declined"
  reason: string | null
}

export type PolicyVersion = {
  version: number
  effective_from: string
  effective_to: string | null
  profile: JsonObject
  premium_cents: number
  tax_cents: number
  earned_before_cents: number
  earned_tax_before_cents: number
  dac_cents: number
  dac_earned_before_cents: number
  status: string
}

export type Claim = {
  id: string
  cause: string
  loss_date: string
  notified_date: string
  claimed_cents: number
  paid_cents: number
  decision: string | null
  reason: string | null
}

export type Policy = {
  policy_id: string
  quote_id: string
  product_key: string | null
  start_date: string
  end_date: string
  profile: { scorecard: Scorecard; sessions_per_month: number }
  premium_cents: number
  tax_cents: number
  earned_premium_cents: number
  earned_tax_cents: number
  aggregate_paid_cents: number
  case_reserve_cents: number
  cancelled: boolean
  versions: PolicyVersion[]
  claims: Claim[]
  warranted_controls: string[]
}

export type AgentClaimResult =
  | {
      trace_id: string
      span_id: string
      cause: string
      loss_date: string
      claimed_cents: number
      evidence: JsonObject[]
      decision: "dry_run"
    }
  | {
      claim_id: string
      policy_id: string
      cause: string
      loss_date: string
      notified_date: string
      purchase_id: string
      claimed_cents: number
      decision: { decision: string; reason: string }
      paid_cents: number
    }

export type AgentSample = {
  id: string
  name: string
  description: string
  manifest: JsonObject
  evidence: NormalizedEvidence
  evidence_json: string
}
