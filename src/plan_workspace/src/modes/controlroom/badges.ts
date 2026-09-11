import type { AgentBadge, AgentRow } from "../../api/types";
import {
  ACTIVITY_META,
  LIFECYCLE_META,
  announceAgent,
  freshnessText,
} from "./presentation";

// Compatibility metadata for older card consumers. The Control room renders
// lifecycle, activity, and connection independently and never displays this
// composite badge as lifecycle.
export const BADGE_META: Record<
  AgentBadge,
  { glyph: string; tone: string; word: string }
> = {
  live: { glyph: "●", tone: "success", word: "Observed" },
  idle: { glyph: "?", tone: "neutral", word: "Unknown" },
  stuck: { glyph: "!", tone: "warn", word: "Possibly stuck" },
  finished: { glyph: "✓", tone: "success", word: "Finished" },
  disconnected: { glyph: "⌀", tone: "neutral", word: "Disconnected" },
  error: { glyph: "×", tone: "danger", word: "Failed" },
  waiting: { glyph: "⚑", tone: "accent", word: "Waiting on you" },
};

export function badgeLabel(agent: AgentRow, now: number): string {
  return `${LIFECYCLE_META[agent.lifecycle].label} · ${
    ACTIVITY_META[agent.activity].label
  } · ${freshnessText(agent, now)}`;
}

export { announceAgent };
