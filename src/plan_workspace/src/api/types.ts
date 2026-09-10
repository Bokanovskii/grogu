// DTO types — the frozen HTTP/graph contract from the implementation plan.
// The application owns no model: these describe what the server sends and
// accepts. Nothing here may drift from the contract without an amendment.

export type Stage = "design" | "implementation" | "testing" | "evaluation";
export const STAGES: Stage[] = ["design", "implementation", "testing", "evaluation"];
export const STAGE_LABEL: Record<Stage, string> = {
  design: "UX",
  implementation: "Implementation",
  testing: "Testing",
  evaluation: "Evaluation",
};
export function stageLabel(stage: string): string {
  return STAGE_LABEL[stage as Stage] ?? stage;
}

export type Role =
  | "architect"
  | "designer"
  | "engineer"
  | "tester"
  | "reviewer"
  | "supervisor";

// Closed node-kind table. Unknown kinds fail closed everywhere.
export type NodeKind =
  | "goal"
  | "directive"
  | "constraint"
  | "invariant"
  | "decision"
  | "criterion"
  | "task"
  | "risk"
  | "question"
  | "note"
  | "evidence"
  | "reference"
  | "diagram"
  | "region"
  | "thread";

export const NODE_KINDS: NodeKind[] = [
  "goal",
  "directive",
  "constraint",
  "invariant",
  "decision",
  "criterion",
  "task",
  "risk",
  "question",
  "note",
  "evidence",
  "reference",
  "diagram",
  "region",
  "thread",
];

export const NORMATIVE_KINDS: ReadonlySet<NodeKind> = new Set<NodeKind>([
  "goal",
  "directive",
  "constraint",
  "invariant",
  "decision",
  "criterion",
  "task",
]);

// Closed edge-kind table.
export type EdgeKind =
  | "depends_on"
  | "blocks"
  | "refines"
  | "contains"
  | "validates"
  | "supersedes"
  | "derives_from"
  | "references"
  | "answers"
  | "anchors"
  | "diagram_edge";

export const EDGE_KINDS: EdgeKind[] = [
  "depends_on",
  "blocks",
  "refines",
  "contains",
  "validates",
  "supersedes",
  "derives_from",
  "references",
  "answers",
  "anchors",
  "diagram_edge",
];

export interface Geometry {
  x: number;
  y: number;
  w: number;
  h: number;
  z: number;
}

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [k: string]: JsonValue };

export interface PlanNode {
  id: string;
  kind: NodeKind;
  stage: Stage | "";
  title: string;
  body: string;
  /** Server-produced sanitised HTML for the body. Rendered as-is; never from
   * raw text via dangerouslySetInnerHTML. */
  body_html?: string;
  attrs: Record<string, JsonValue>;
  ext?: Record<string, JsonValue>;
  geometry?: Geometry;
  order: number;
  created_rev: string;
  updated_rev: string;
}

export interface PlanEdge {
  id: string;
  kind: EdgeKind;
  from: string;
  to: string;
  attrs: Record<string, JsonValue>;
  created_rev: string;
}

export interface Counters {
  [kindPrefix: string]: number;
}

export interface StagesInfo {
  readable: Stage[];
  sealed: Stage[];
  /** stage -> owner role, for "written" state. */
  written: Partial<Record<Stage, string>>;
}

export interface DocResponse {
  plan: string;
  title: string;
  revision: string;
  role: Role | "";
  stages: StagesInfo;
  nodes: PlanNode[];
  edges: PlanEdge[];
  counters: Counters;
  sealed_edge_count: number;
}

export interface HealthResponse {
  ok: boolean;
  plan: string;
  revision: string;
  role: Role | "";
  modes: string[];
}

// ---- Selectors (tagged union) ----

export interface TextQuote {
  exact: string;
  prefix: string;
  suffix: string;
}

export type Selector =
  | { type: "node"; id: string }
  | {
      type: "text";
      node: string;
      quote: TextQuote;
      position: { start: number; end: number };
      body_digest: string;
    }
  | { type: "object"; id: string; part: string }
  | { type: "edge"; id: string }
  | { type: "edge"; from: string; to: string; ordinal: number }
  | { type: "region"; id: string }
  | { type: "composite"; op: "all" | "any"; members: Selector[] };

export type AnchorState = "resolved" | "shifted" | "orphaned";

export interface Resolution {
  state: AnchorState;
  nodes: string[];
  edges: string[];
}

// ---- Patch ops (RFC 6902 subset) ----

export type PatchOp =
  | { op: "add"; path: string; value: JsonValue }
  | { op: "remove"; path: string }
  | { op: "replace"; path: string; value: JsonValue }
  | { op: "test"; path: string; value: JsonValue }
  | { op: "move"; from: string; path: string }
  | { op: "copy"; from: string; path: string };

export interface PatchRequest {
  base: string;
  ops: PatchOp[];
  intent: string;
  origin?: string;
}

export interface PatchOk {
  revision: string;
  digest: string;
  changed: string[];
}

export interface PatchStale {
  error: "stale";
  revision: string;
  ops_since: PatchOp[];
}

export interface ApiError {
  error: string;
  message: string;
  op_index?: number;
  why?: string;
  field?: string;
}

// ---- Threads ----

export type ThreadKind =
  | "discussion"
  | "question"
  | "suggestion"
  | "blocking"
  | "directive";

export type ThreadStatus = "open" | "resolved" | "orphaned";

export interface Comment {
  id: string;
  author: string;
  role: Role | "grogu" | "";
  body: string;
  body_html?: string;
  at: string;
}

export interface Thread {
  id: string;
  selector: Selector;
  kind: ThreadKind;
  status: ThreadStatus;
  anchor_state: AnchorState;
  /** Revision where the anchor last matched exactly (for the Moved chip). */
  anchor_last_exact?: string;
  /** For a composite whose members partially resolve. */
  resolved_members?: number;
  total_members?: number;
  quote_context?: string;
  round?: number;
  comments: Comment[];
  promoted_directive?: string;
}

// ---- Proposals ----

export type ProposalStatus = "pending" | "accepted" | "rejected";

export interface Proposal {
  id: string;
  at: string;
  from_thread?: string;
  base: string;
  why: string;
  ops: PatchOp[];
  status: ProposalStatus;
  decided_at: string;
  decided_by: string;
  why_not: string;
}

export interface NodeDiffEntry {
  id: string;
  op: "add" | "set" | "remove" | "link" | "unlink";
  kind?: NodeKind | EdgeKind;
  title?: string;
  before?: Partial<PlanNode>;
  after?: Partial<PlanNode>;
}

export interface CompiledDiff {
  role: Role;
  before: string;
  after: string;
}

export interface ImpactItem {
  id: string;
  kind: NodeKind | EdgeKind;
  title: string;
  reason?: string;
  op?: string;
  destructive?: boolean;
}

export interface Impact {
  direct: ImpactItem[];
  transitive: ImpactItem[];
  cycles: string[][];
  compiled_delta: string[];
}

export interface ProposalPreview {
  node_diff: NodeDiffEntry[];
  compiled_diff: CompiledDiff[];
  impact: Impact;
  stale: boolean;
  head?: string;
}

// ---- Layout ----

export interface LayoutRequest {
  scope: string;
  stage?: Stage;
  algorithm: "dagre";
  direction?: "TB" | "LR";
}

export interface LayoutResponse {
  ops: PatchOp[];
}

// ---- Projection ----

export interface ProjectionResponse {
  markdown: string;
  digest: string;
  role: Role;
  stage: Stage | "";
  include: "normative" | "all";
  budget: number | null;
  elided: { id: string; kind: string; count?: number }[];
  nonequivalent?: { field: string } | null;
}

// ---- Revisions ----

export type RevisionOrigin =
  | "user"
  | "workspace"
  | "cli"
  | "migration"
  | "undo"
  | "layout"
  | "steering"
  | "amendment"
  | string;

export interface RevisionEnvelope {
  revision: string;
  seq: number;
  parent: string;
  at: string;
  actor: string;
  role: Role | "";
  agent: string;
  intent: string;
  origin: RevisionOrigin;
  ops: PatchOp[];
  before_digest: string;
  after_digest: string;
  summary?: string;
}

export interface GateRow {
  stage: Stage;
  state: "allowed" | "blocked" | "passed";
  blocked_by?: { feedback_id: string; summary: string; sent_at: string };
}

// ---- SSE events ----

export type SseEvent =
  | { type: "revision"; revision: string }
  | { type: "proposal"; id: string }
  | { type: "thread"; id: string }
  | { type: "control" }
  | { type: "shutdown" };

// ---- Control room ----

export type Basis = "observed" | "authored";

export type Lifecycle =
  | "registered"
  | "running"
  | "finished"
  | "failed"
  | "cancelled"
  | "unknown";

export type Activity =
  | "active"
  | "quiet"
  | "possibly_stuck"
  | "blocked"
  | "unknown";

export type Connection = "live" | "stale" | "disconnected" | "unknown";

// UI-facing state badge, derived from lifecycle + activity + connection + steering.
export type AgentBadge =
  | "live"
  | "idle"
  | "stuck"
  | "finished"
  | "disconnected"
  | "error"
  | "waiting";

export type Coverage = "complete" | "partial" | "unavailable";

export interface AgentAction {
  tool_name: string;
  phase: string;
  at: number;
  /** authored summary text, when provenance permits; else null. Labelled. */
  summary?: string | null;
}

export interface ToolStats {
  started: number;
  completed: number;
  failed: number;
  inflight: number;
  complete: boolean;
}

export interface AgentRow {
  agent_key: string;
  agent: string;
  run_id: string;
  role: Role | "";
  roles: Role[];
  workstream: string;
  plan: string;
  plan_title?: string;
  revision: { last_read: string; current: string; relation: "behind" | "current" | "unknown" };
  lifecycle: Lifecycle;
  activity: Activity;
  connection: Connection;
  badge: AgentBadge;
  started_at: number | null;
  last_observed_at: number;
  elapsed_ms: number;
  elapsed_basis: "lifecycle_start" | "first_observed" | "unknown";
  current_action: AgentAction | null;
  last_tool?: { tool_name: string; duration_ms: number; outcome: "ok" | "err" | "timeout" } | null;
  tools: ToolStats;
  grogu_commands: { calls: number; failures: number; last_command: string };
  failures: { code: string; at: number }[];
  blockers: string[];
  steering: { unread: number; delivered: number; acknowledged: number };
  coverage: { lifecycle: Coverage; tools: Coverage; outcomes: Coverage };
  basis: Basis;
  stuck_threshold_s?: number;
  last_error?: string;
  error_token?: string;
}

export interface ControlLimit {
  category: string;
  detail: string;
}

export interface WaitingItem {
  agent_key: string;
  agent: string;
  reason: string;
}

export interface ControlResponse {
  fresh_as_of: number;
  limits: ControlLimit[];
  waiting_on_you: WaitingItem[];
  agents: AgentRow[];
  plans: Record<string, { title: string; agents: number }>;
}

export type AuditEventType =
  | "revision"
  | "patch"
  | "tool"
  | "gate_passed"
  | "gate_blocked"
  | "steering_delivered"
  | "feedback_acknowledged"
  | "feedback_withdrawn"
  | "error"
  | "disconnected"
  | "connected"
  | "role_bound"
  | "workstream_started"
  | "workstream_merged";

export interface AuditEvent {
  id: string;
  type: AuditEventType;
  at: number;
  summary: string;
  /** plan objects this event names, if any (revision id / node id / edge id). */
  objects?: { plan: string; ids: string[] };
  /** tool events only: name/duration/outcome — never arguments. */
  tool?: { tool_name: string; duration_ms: number; outcome: "ok" | "err" | "timeout" };
  /** if the tool wrote a patch, the patch/revision id link. */
  patch?: string;
  basis: Basis;
}

export interface AgentDrillResponse {
  agent: AgentRow;
  activity: AuditEvent[];
  evidence: {
    id: string;
    label: string;
    plan: string;
    node?: string;
    revision?: string;
    available: boolean;
  }[];
  limits: ControlLimit[];
  blockers: string[];
}

// ---- Feedback ----

export type FeedbackScope =
  | { kind: "agent"; agent_key: string; label: string }
  | { kind: "role"; role: Role; label: string }
  | { kind: "plan"; plan: string; label: string }
  | { kind: "role_plan"; role: Role; plan: string; label: string };

export type DeliveryState =
  | "sent"
  | "routed"
  | "delivered"
  | "acknowledged"
  | "undeliverable"
  | "withdrawn";

export interface FeedbackRecord {
  id: string;
  scope: FeedbackScope;
  binding: boolean;
  text: string;
  state: DeliveryState;
  reason?: string;
  ack_event?: string;
  gate?: { stage: Stage; state: "blocked" | "open" };
  history: { state: DeliveryState; at: number }[];
  at: number;
}

export interface FeedbackResponse {
  seq: string;
  delivered_to: string;
  gates_closed: string[];
  record: FeedbackRecord;
}
