// Tipi delle risposte HTTP dell'API IdeaI.
// Fonte autorevole: src/ideai/api/schemas.py (i nomi dei campi devono restare allineati).

export interface WatchInfo {
  enabled: boolean;
  mode: string;
  interval_s: number;
}

export interface IdeaListItem {
  id: number;
  title: string;
  canonical_summary: string;
  status: string;
  opportunity_score: number | null;
  score_version: string;
  verdict: string | null;
  confidence: number | null;
  category: string | null;
  tags: string[];
  source_kinds: string[];
  item_count: number;
  first_seen_at: string;
  last_activity_at: string;
  watch: WatchInfo;
}

export interface IdeaListResponse {
  total: number;
  items: IdeaListItem[];
}

export interface AnalysisRevision {
  revision: number;
  superseded_by: number | null;
  verdict: string | null;
  confidence: number | null;
  feasibility_score: number | null;
  economics_score: number | null;
  competition_score: number | null;
  opportunity_score: number | null;
  model_spec: string;
  provider: string;
  prompt_version: string;
  schema_version: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  latency_ms: number;
  created_at: string;
  payload: AnalysisPayload;
}

export interface IdeaDetailItem {
  id: number;
  external_id: string;
  kind: string;
  title: string | null;
  body: string;
  url: string | null;
  score: number | null;
  num_comments: number | null;
  role: string;
  similarity: number | null;
  state: string;
}

export interface IdeaUpdate {
  id: number;
  item_id: number | null;
  kind: string;
  summary: string;
  created_at: string;
}

export interface IdeaDetailResponse {
  idea: IdeaListItem;
  current_analysis: AnalysisRevision | null;
  revisions: AnalysisRevision[];
  items: IdeaDetailItem[];
  timeline: IdeaUpdate[];
}

export interface SourceTargetInfo {
  id: number;
  target_ref: string;
  target_kind: string;
  enabled: boolean;
  cursor: Record<string, unknown> | null;
  last_polled_at: string | null;
  next_poll_at: string | null;
  poll_interval_s: number;
  error_count: number;
  last_error: string | null;
}

export interface RateLimitInfo {
  bucket: string;
  window_start: string | null;
  used: number | null;
  limit: number | null;
}

export interface SourceCallsToday {
  calls: number;
  cost_usd: number;
}

export interface SourceInfo {
  id: number;
  kind: string;
  name: string;
  enabled: boolean;
  config: Record<string, unknown>;
  targets: SourceTargetInfo[];
  rate_limit: RateLimitInfo;
  calls_today: SourceCallsToday;
}

export interface JobInfo {
  id: number;
  topic: string;
  state: string;
  attempts: number;
  max_attempts: number;
  run_after: string;
  locked_by: string | null;
  last_error: string | null;
  created_at: string;
  finished_at: string | null;
  dedup_key: string | null;
}

export interface JobListResponse {
  total: number;
  oldest_pending: string | null;
  items: JobInfo[];
}

export interface IdeasStats {
  total: number;
  new: number;
  analyzed: number;
  watching: number;
  rejected: number;
  archived: number;
}

export interface LlmCostRow {
  day: string;
  role: string;
  calls: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
}

export interface SourceCostRow {
  day: string;
  source_id: number;
  calls: number;
  cost_usd: number;
}

export interface QueueHealthRow {
  topic: string;
  state: string;
  jobs: number;
  oldest_pending: string | null;
  last_done: string | null;
  dead: number;
}

export interface FailedLlmCall {
  id: number;
  role: string;
  model: string;
  status: string;
  error: string | null;
  created_at: string;
}

export interface StatsResponse {
  ideas: IdeasStats;
  analyses: number;
  watch_active: number;
  llm_cost_7d: LlmCostRow[];
  source_cost_7d: SourceCostRow[];
  queue: QueueHealthRow[];
  failed_llm_calls: FailedLlmCall[];
}

// --- Payload di analisi (§7.6 / AnalysisPayload di prompts/schemas.py) ---

export interface Monetization {
  model: string;
  price_hypothesis: string;
  unit_economics_note: string;
}

export interface Feasibility {
  score: number;
  rationale: string;
  hard_blockers: string[];
  tech_stack_hint: string[];
}

export interface Economics {
  score: number;
  tam_signal: string;
  rationale: string;
}

export interface Competition {
  score: number;
  named_players: string[];
  rationale: string;
}

export interface Risk {
  risk: string;
  severity: number;
  mitigation: string;
}

export interface Effort {
  weeks_to_mvp: number;
  team_size: number;
  confidence: number;
}

export interface EvidenceQuote {
  item_external_id: string;
  quote: string;
}

export interface AnalysisPayload {
  problem: string;
  target_customer: string;
  current_alternatives: string[];
  proposed_solution: string;
  mvp_scope: string[];
  differentiators: string[];
  monetization: Monetization;
  feasibility: Feasibility;
  economics: Economics;
  competition: Competition;
  risks: Risk[];
  effort: Effort;
  evidence_quotes: EvidenceQuote[];
  verdict: string;
  confidence: number;
  notes: string;
}
