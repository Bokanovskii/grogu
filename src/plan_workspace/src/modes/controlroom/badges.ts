import type { AgentBadge, AgentRow } from "../../api/types";
import { ago } from "../../lib/format";

// State badges are redundantly encoded by glyph, label and a 2px left-border
// colour token. Colour is never the only cue. The server computes the badge;
// the client renders the literal copy from the design.

export const BADGE_META: Record<
  AgentBadge,
  { glyph: string; tone: string; word: string }
> = {
  live: { glyph: "●", tone: "success", word: "Live" },
  idle: { glyph: "◐", tone: "neutral-500", word: "Idle" },
  stuck: { glyph: "!", tone: "warn", word: "Stuck" },
  finished: { glyph: "✓", tone: "success", word: "Finished" },
  disconnected: { glyph: "⌀", tone: "neutral-300", word: "Disconnected" },
  error: { glyph: "×", tone: "danger", word: "Error" },
  waiting: { glyph: "⚑", tone: "accent", word: "Waiting on you" },
};

export function badgeLabel(agent: AgentRow, now: number): string {
  const t = ago(agent.last_observed_at, now);
  switch (agent.badge) {
    case "live":
      return `Live · ${t}`;
    case "idle":
      return `Idle · ${t}`;
    case "stuck": {
      const action = agent.current_action?.tool_name ?? agent.grogu_commands.last_command ?? "last action";
      return `Stuck at ${action} · ${t} without new event`;
    }
    case "finished": {
      if (agent.workstream) return `Finished · ${agent.revision.current} · ${agent.workstream} merged`;
      return `Finished · ${agent.revision.current}`;
    }
    case "disconnected":
      return `Disconnected · last seen ${t}`;
    case "error":
      return `Error · ${agent.error_token ?? "error"}`;
    case "waiting":
      return `Waiting · ${agent.steering.unread} unread steering`;
  }
}

/** A11y live-region string for a focused card transition. */
export function announceAgent(agent: AgentRow, now: number): string {
  const who = `${agent.role || "agent"} on ${agent.plan || "no plan"}`;
  const label = badgeLabel(agent, now);
  return `${who}: ${label}`;
}
