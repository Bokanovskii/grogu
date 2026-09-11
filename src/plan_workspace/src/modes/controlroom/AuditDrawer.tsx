import { useMemo, useState } from "react";
import type { OperationalEvent } from "../../api/types";
import { useControl } from "../../state/control";
import { useApp } from "../../state/store";
import { clock } from "../../lib/format";
import { FeedbackComposer } from "./FeedbackComposer";
import { SegmentedControl } from "../../shell/ui";
import { eventText } from "./presentation";

const PROVENANCE =
  "Timeline shows observable events. Prompts, model reasoning, tool arguments and tool results are never recorded.";

type Range = "1h" | "24h" | "7d" | "all";
const RANGE_MS: Record<Range, number> = {
  "1h": 3_600_000,
  "24h": 86_400_000,
  "7d": 604_800_000,
  all: Number.POSITIVE_INFINITY,
};

export function AuditDrawer({
  now,
  onEscape,
}: {
  now: number;
  onEscape: () => void;
}) {
  const control = useControl();
  const state = useApp();
  const [range, setRange] = useState<Range>("24h");
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState<
    OperationalEvent["type"] | "all"
  >("all");

  const drill = control.drill;
  const agent = drill?.agent ?? null;
  const sourceEvents = drill?.recent_events ?? [];
  const status = drill?.events_status ?? agent?.events_status ?? "unavailable";
  const events = useMemo(() => {
    const cutoff = now - RANGE_MS[range];
    const query = search.trim().toLowerCase();
    return sourceEvents
      .filter((event) => !Number.isFinite(RANGE_MS[range]) || event.at >= cutoff)
      .filter((event) => typeFilter === "all" || event.type === typeFilter)
      .filter(
        (event) =>
          !query ||
          (event.name ?? "").toLowerCase().includes(query) ||
          (event.error_code ?? "").toLowerCase().includes(query) ||
          event.type.replace(/_/g, " ").includes(query),
      )
      .sort((a, b) => b.at - a.at || b.id.localeCompare(a.id));
  }, [now, range, search, sourceEvents, typeFilter]);

  return (
    <div
      className={`audit-drawer${control.selectedAgent ? " has-selection" : ""}`}
      id="control-agents"
      tabIndex={-1}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          onEscape();
        }
      }}
    >
      <div className="audit-header">
        <h3 className="audit-title">
          {agent ? `${agent.role || "agent"} · ${agent.agent}` : "Agent timeline"}
        </h3>
        <SegmentedControl
          ariaLabel="Time range"
          value={range}
          onChange={setRange}
          options={[
            { value: "1h", label: "1h" },
            { value: "24h", label: "24h" },
            { value: "7d", label: "7d" },
            { value: "all", label: "All" },
          ]}
        />
      </div>

      <div className="audit-filters">
        <select
          className="audit-type"
          value={typeFilter}
          onChange={(event) =>
            setTypeFilter(event.target.value as OperationalEvent["type"] | "all")
          }
          aria-label="Filter by event type"
        >
          <option value="all">All event types</option>
          <option value="command">Command</option>
          <option value="tool">Tool</option>
          <option value="run_started">Run started</option>
          <option value="run_finished">Run finished</option>
          <option value="permission_requested">Permission requested</option>
          <option value="permission_completed">Permission completed</option>
        </select>
        <input
          className="audit-search"
          type="search"
          placeholder="event name or error code"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          aria-label="Search timeline"
        />
      </div>

      {status === "stale" ? (
        <p className="audit-source-band">
          Timeline source is stale. These are the last retained observable events.
        </p>
      ) : null}

      <div className="audit-body" tabIndex={0} role="region" aria-label="Timeline events">
        {control.drillLoading ? (
          <TimelineState title="Loading timeline…">
            Reading this agent’s observable events.
          </TimelineState>
        ) : control.drillError ? (
          <TimelineState title="Timeline disconnected." alert>
            The event source could not be refreshed. Previously observed lifecycle
            remains unchanged.
            <button
              type="button"
              className="btn btn-text"
              onClick={() =>
                control.selectedAgent &&
                control.selectAgent(control.selectedAgent)
              }
            >
              Try again
            </button>
          </TimelineState>
        ) : !control.selectedAgent || !agent ? (
          <TimelineState title="Select an agent to see its timeline." />
        ) : status === "permission_limited" ? (
          <TimelineState title="Timeline permission limited.">
            The registered event source could not be read with current permissions.
          </TimelineState>
        ) : status === "disconnected" ? (
          <TimelineState title="Timeline disconnected.">
            The registered event source could not be read. Previously observed
            lifecycle remains unchanged.
          </TimelineState>
        ) : status === "unavailable" || agent.event_source_registered === false ? (
          <TimelineState title="No timeline for this agent: its session has no registered event source.">
            Event history is unavailable for this agent.
          </TimelineState>
        ) : sourceEvents.length === 0 ? (
          <TimelineState title={`No events in the last ${range === "all" ? "available range" : range}.`}>
            Widen the range to see older activity.
          </TimelineState>
        ) : events.length === 0 ? (
          <TimelineState title="No timeline event matches these filters.">
            Clear the event type or search filter.
          </TimelineState>
        ) : (
          <ol className="audit-list">
            {events.map((event) => (
              <li key={event.id} className={`audit-item audit-item-${event.type}`}>
                <time className="audit-time" dateTime={new Date(event.at).toISOString()}>
                  {clock(event.at)}
                </time>
                <span className="audit-summary">{eventText(event, now)}</span>
              </li>
            ))}
          </ol>
        )}
      </div>

      <p className="audit-provenance">{PROVENANCE}</p>

      <details className="audit-composer">
        <summary className="audit-composer-title">Send feedback</summary>
        <FeedbackComposer agent={agent} plan={agent?.plan ?? state.plan} />
      </details>
    </div>
  );
}

function TimelineState({
  title,
  children,
  alert = false,
}: {
  title: string;
  children?: React.ReactNode;
  alert?: boolean;
}) {
  return (
    <div className="audit-empty" role={alert ? "alert" : "status"}>
      <p className="audit-empty-title">{title}</p>
      {children ? <p>{children}</p> : null}
    </div>
  );
}
