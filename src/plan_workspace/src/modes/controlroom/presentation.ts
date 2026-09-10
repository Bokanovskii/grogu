import {
  STAGES,
  type Activity,
  type AgentRow,
  type Connection,
  type CoverageState,
  type Lifecycle,
  type OperationalEvent,
  type PlanGateSummary,
  type PlanProgramSummary,
  type Stage,
} from "../../api/types";
import { ago, durationMs } from "../../lib/format";

export const LIFECYCLE_META: Record<
  Lifecycle,
  { glyph: string; tone: string; label: string }
> = {
  registered: { glyph: "◇", tone: "neutral", label: "Registered" },
  running: { glyph: "●", tone: "success", label: "Running" },
  finished: { glyph: "✓", tone: "success", label: "Finished" },
  failed: { glyph: "×", tone: "danger", label: "Failed" },
  cancelled: { glyph: "⊘", tone: "neutral", label: "Cancelled" },
  unknown: { glyph: "?", tone: "neutral", label: "Unknown" },
};

export const ACTIVITY_META: Record<
  Activity,
  { glyph: string; tone: string; label: string }
> = {
  active: { glyph: "▶", tone: "success", label: "Active" },
  quiet: { glyph: "–", tone: "neutral", label: "Quiet" },
  possibly_stuck: { glyph: "!", tone: "warn", label: "Possibly stuck" },
  blocked: { glyph: "⚑", tone: "accent", label: "Blocked" },
  unknown: { glyph: "?", tone: "neutral", label: "Unknown" },
};

export const CONNECTION_LABEL: Record<Connection, string> = {
  live: "Live",
  stale: "Stale",
  disconnected: "Disconnected",
  unknown: "Unknown",
};

export function observedAge(at: number | null | undefined, now: number): string {
  return at == null ? "unknown" : ago(at, now);
}

export function freshnessText(agent: AgentRow, now: number): string {
  const age = observedAge(agent.last_observed_at, now);
  if (agent.connection === "disconnected") {
    return age === "unknown" ? "Disconnected" : `Disconnected · last event ${age}`;
  }
  if (agent.connection === "unknown") return "Unknown";
  return age === "unknown"
    ? CONNECTION_LABEL[agent.connection]
    : `${CONNECTION_LABEL[agent.connection]} · ${age}`;
}

export function elapsedText(agent: AgentRow): string {
  if (agent.elapsed_ms == null || agent.elapsed_basis === "unknown") {
    return "Unknown · no start evidence";
  }
  const value = durationMs(agent.elapsed_ms);
  return agent.elapsed_basis === "lifecycle_start"
    ? `${value} · since run started`
    : `${value} · since first observed`;
}

export function revisionText(agent: AgentRow): string {
  const read = agent.revision.last_read;
  if (!read) return "Revision relation unknown";
  if (agent.revision.relation === "current") return `${read} · current`;
  if (agent.revision.relation === "behind") {
    const current = Number.parseInt(agent.revision.current.replace(/\D/g, ""), 10);
    const prior = Number.parseInt(read.replace(/\D/g, ""), 10);
    const count = Number.isFinite(current) && Number.isFinite(prior) ? current - prior : 0;
    return count > 0 ? `${read} · ${count} behind` : `${read} · behind`;
  }
  return `${read} · relation unknown`;
}

export function ownershipText(agent: AgentRow, compact = false): string {
  if (agent.stage_ownership !== "recorded" || !agent.owned_stages?.length) {
    return compact ? "—" : "Ownership unavailable";
  }
  return agent.owned_stages
    .map((stage) => (compact ? stage.slice(0, 1).toUpperCase() : stage))
    .join(", ");
}

export function coverageState(agent: AgentRow): CoverageState {
  const values = [
    agent.coverage.lifecycle,
    agent.coverage.tools,
    agent.coverage.outcomes,
  ];
  if (values.every((value) => value === "complete")) return "complete";
  if (values.every((value) => value === "unavailable")) return "unavailable";
  return "partial";
}

export function coverageText(agent: AgentRow): string {
  const state = coverageState(agent);
  return state === "complete" ? "Full" : state === "partial" ? "Partial" : "Unavailable";
}

const EVENT_ICON: Record<OperationalEvent["type"], string> = {
  command: "⌘",
  tool: "▷",
  run_started: "▶",
  run_finished: "■",
  permission_requested: "⚑",
  permission_completed: "✓",
};

export function eventText(event: OperationalEvent, now?: number): string {
  const parts = [
    EVENT_ICON[event.type],
    event.name ?? event.type.replace(/_/g, " "),
    event.phase,
    event.success === true ? "ok" : event.success === false ? "failed" : null,
    event.duration_ms == null ? null : durationMs(event.duration_ms),
    event.error_code,
    now == null ? null : ago(event.at, now),
  ];
  return parts.filter(Boolean).join(" · ");
}

export function lastSafeAction(agent: AgentRow, now: number): string {
  if (agent.failures.length > 0) {
    const latest = [...agent.failures].sort((a, b) => b.at - a.at)[0];
    return `Failed · ${latest?.code ?? "unknown"}`;
  }
  if (agent.last_action) return eventText(agent.last_action, now);
  if (agent.current_action?.tool_name) {
    return [
      agent.current_action.tool_name,
      agent.current_action.phase || null,
      ago(agent.current_action.at, now),
    ]
      .filter(Boolean)
      .join(" · ");
  }
  if (agent.event_source_registered === false || agent.events_status === "unavailable") {
    return "Tool activity unavailable · no event source";
  }
  if (agent.events_status === "permission_limited") {
    return "Tool activity unavailable · permission limited";
  }
  if (agent.events_status === "disconnected") {
    return "Tool activity unavailable · source disconnected";
  }
  return agent.events_status === "empty"
    ? "No tool activity observed"
    : "Tool activity unavailable · coverage unknown";
}

const GATE_REASON: Record<PlanGateSummary["reasons"][number], string> = {
  design_review: "design review",
  user_approval: "user approval",
  open_defect: "open defect",
  open_amendment: "open amendment",
  requires_replan: "replan required",
  binding_feedback: "binding feedback",
  stage_incomplete: "stage incomplete",
  governance: "governance",
  unknown: "unknown reason",
};

export function gateText(plan: PlanProgramSummary): string {
  const gate = plan.primary_gate;
  if (!gate || gate.allowed == null) return "Gate: unknown";
  if (gate.allowed) return "Gate: open";
  return `Gate: blocked · ${GATE_REASON[gate.reasons[0] ?? "unknown"]}`;
}

export function stageToken(plan: PlanProgramSummary, stage: Stage): string {
  const value = plan.stages?.find((item) => item.stage === stage);
  if (!value) return "—";
  if (value.state === "complete" || value.state === "completed") return "complete";
  if (value.sealed) return "sealed";
  return value.written ? "written" : "unwritten";
}

export function stageSummary(plan: PlanProgramSummary): string {
  if (plan.stages == null) return "Stage metadata unavailable";
  return STAGES.map(
    (stage) => `${stage.slice(0, 1).toUpperCase()} ${stageToken(plan, stage)}`,
  ).join(" · ");
}

export function blockerCount(agent: AgentRow): number {
  return agent.blocker_details?.length ?? agent.blockers.length;
}

export function waitsOnUser(agent: AgentRow): boolean {
  return agent.blocker_details?.some((blocker) => blocker.waiting_on_user) ?? false;
}

export function announceAgent(agent: AgentRow, now: number): string {
  return `${agent.role || "agent"} ${agent.agent} on ${agent.plan}: ${
    LIFECYCLE_META[agent.lifecycle].label
  }, ${ACTIVITY_META[agent.activity].label}, ${freshnessText(agent, now)}`;
}
