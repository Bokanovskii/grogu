import { useRef, useState } from "react";
import type { AgentRow } from "../../api/types";
import { badgeLabel, BADGE_META } from "./badges";
import { freshnessLabel } from "../../lib/format";

type SortKey = "state" | "role" | "workstream" | "plan" | "freshness" | "failures" | "blockers" | "unread";

const ROW_H = 40;
const OVERSCAN = 8;

// List view: 40px rows, proper <table> semantics, sortable columns, and
// windowed so a board with hundreds of agents renders only what is visible.
// Scroll position lives in the DOM node, which the Control room keeps mounted
// across refreshes, so it is preserved.
export function AgentList({
  agents,
  now,
  selected,
  onSelect,
  onWatch,
}: {
  agents: AgentRow[];
  now: number;
  selected: string | null;
  onSelect: (key: string) => void;
  onWatch: (key: string) => void;
}) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "state", dir: 1 });
  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(600);

  const sorted = [...agents].sort((a, b) => {
    const dir = sort.dir;
    switch (sort.key) {
      case "role":
        return dir * (a.role || "").localeCompare(b.role || "");
      case "workstream":
        return dir * (a.workstream || "").localeCompare(b.workstream || "");
      case "plan":
        return dir * (a.plan || "").localeCompare(b.plan || "");
      case "freshness":
        return dir * (a.last_observed_at - b.last_observed_at);
      case "failures":
        return dir * (a.failures.length - b.failures.length);
      case "blockers":
        return dir * (a.blockers.length - b.blockers.length);
      case "unread":
        return dir * (a.steering.unread - b.steering.unread);
      default:
        return dir * a.badge.localeCompare(b.badge);
    }
  });

  const total = sorted.length;
  const windowed = total > 60;
  const start = windowed ? Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN) : 0;
  const end = windowed ? Math.min(total, Math.ceil((scrollTop + viewport) / ROW_H) + OVERSCAN) : total;
  const visible = sorted.slice(start, end);
  const padTop = start * ROW_H;
  const padBottom = (total - end) * ROW_H;

  const header = (key: SortKey, label: string) => (
    <th scope="col">
      <button
        type="button"
        className="col-sort"
        onClick={() => setSort((s) => ({ key, dir: s.key === key && s.dir === 1 ? -1 : 1 }))}
        aria-sort={sort.key === key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}
      >
        {label}
        {sort.key === key ? <span aria-hidden="true">{sort.dir === 1 ? " ▲" : " ▼"}</span> : null}
      </button>
    </th>
  );

  return (
    <div
      className="agent-table-scroll"
      ref={scrollRef}
      onScroll={(e) => {
        setScrollTop(e.currentTarget.scrollTop);
        setViewport(e.currentTarget.clientHeight);
      }}
    >
      <table className="agent-table">
        <thead>
          <tr>
            {header("state", "State")}
            {header("role", "Role")}
            {header("workstream", "Workstream")}
            {header("plan", "Plan")}
            {header("freshness", "Freshness")}
            <th scope="col">Action</th>
            {header("failures", "Failures")}
            {header("blockers", "Blockers")}
            {header("unread", "Unread")}
          </tr>
        </thead>
        <tbody>
          {padTop > 0 ? (
            <tr aria-hidden="true" className="v-spacer">
              <td colSpan={9} style={{ height: padTop, padding: 0 }} />
            </tr>
          ) : null}
          {visible.map((a) => {
            const meta = BADGE_META[a.badge];
            return (
              <tr
                key={a.agent_key}
                className={`agent-tr badge-border-${meta.tone}${selected === a.agent_key ? " is-selected" : ""}`}
                tabIndex={0}
                onClick={() => onSelect(a.agent_key)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") onWatch(a.agent_key);
                }}
                aria-label={badgeLabel(a, now)}
              >
                <td>
                  <span className={`badge-text-${meta.tone}`}>
                    <span aria-hidden="true">{meta.glyph}</span> {meta.word}
                  </span>
                </td>
                <td>{a.role || "—"}</td>
                <td>{a.workstream || "—"}</td>
                <td title={a.plan_title ?? a.plan}>{a.plan || "—"}</td>
                <td>{freshnessLabel(a.last_observed_at, now)}</td>
                <td className="agent-td-action">
                  {a.current_action?.tool_name ?? (a.badge === "error" ? a.last_error : "Idle") ?? "Idle"}
                </td>
                <td>{a.failures.length}</td>
                <td>{a.blockers.length}</td>
                <td>{a.steering.unread}</td>
              </tr>
            );
          })}
          {padBottom > 0 ? (
            <tr aria-hidden="true" className="v-spacer">
              <td colSpan={9} style={{ height: padBottom, padding: 0 }} />
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}
