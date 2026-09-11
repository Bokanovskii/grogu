import { useEffect, useMemo, useRef, useState } from "react";
import type {
  AgentRow,
  ControlLimit,
  ControlScope,
  OperationalEvent,
} from "../../api/types";
import { useControl } from "../../state/control";
import { useActions, useApp } from "../../state/store";
import { ModeLayout } from "../../shell/ModeLayout";
import { SegmentedControl } from "../../shell/ui";
import { ago } from "../../lib/format";
import { AgentCard } from "./AgentCard";
import { AgentList } from "./AgentList";
import { AuditDrawer } from "./AuditDrawer";
import { Filters } from "./Filters";
import { FlowTopology } from "./FlowTopology";
import { ProgramStrip } from "./ProgramStrip";
import {
  ACTIVITY_META,
  CONNECTION_LABEL,
  LIFECYCLE_META,
  announceAgent,
  eventText,
} from "./presentation";
import {
  applyFilters,
  defaultSort,
  EMPTY_FILTERS,
  filtersActive,
  type ControlFilters,
} from "./controlFilters";

const PRIVACY_LIMITS: ControlLimit[] = [
  { category: "prompts", detail: "Prompts are not shown." },
  { category: "reasoning", detail: "Model reasoning is not shown." },
  {
    category: "arguments",
    detail: "Tool and command arguments are not shown.",
  },
  { category: "results", detail: "Tool results are not shown." },
  {
    category: "permissions",
    detail: "Permission intentions and file-edit content are not shown.",
  },
  {
    category: "sealed",
    detail: "Sealed stage content and object identifiers are not shown.",
  },
  { category: "paths", detail: "Local source paths are not shown." },
  {
    category: "private",
    detail: "Credentials and personal or health data are not shown.",
  },
];

export function ControlRoom() {
  const control = useControl();
  const actions = useActions();
  const state = useApp();
  const [filters, setFilters] = useState<ControlFilters>(EMPTY_FILTERS);
  const [now, setNow] = useState(() => Date.now());
  const [showSkeleton, setShowSkeleton] = useState(false);
  const [focusIndex, setFocusIndex] = useState(0);
  const [focusedAgent, setFocusedAgent] = useState<string | null>(null);
  const cardRefs = useRef<(HTMLDivElement | null)[]>([]);
  const returnAgent = useRef<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setShowSkeleton(true), 400);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    let timer = 0;
    const tick = () => {
      setNow(Date.now());
      timer = window.setTimeout(tick, 5000);
    };
    if (document.visibilityState === "visible") tick();
    const onVisibility = () => {
      window.clearTimeout(timer);
      if (document.visibilityState === "visible") tick();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  const snapshot = control.snapshot;
  const allAgents = snapshot?.agents ?? [];
  const waitingKeys = useMemo(
    () => new Set((snapshot?.waiting_on_you ?? []).map((item) => item.agent_key)),
    [snapshot?.waiting_on_you],
  );
  const filtered = applyFilters(allAgents, filters, waitingKeys);
  const agents = defaultSort(filtered, waitingKeys);
  const density = state.panels.density;
  const currentPlan = snapshot?.scope?.current_plan ?? state.plan;
  const selectedPlanFilter = filters.plans.length === 1 ? filters.plans[0]! : null;

  const openComposer = (key: string, nudge = false) => {
    setFocusedAgent(key);
    control.selectAgent(key);
    actions.openOverlay(
      nudge
        ? { kind: "feedbackComposer", agentKey: key, nudge: true }
        : { kind: "feedbackComposer", agentKey: key },
    );
  };

  const openTimeline = (agent: AgentRow) => {
    returnAgent.current = agent.agent_key;
    setFocusedAgent(agent.agent_key);
    control.selectAgent(agent.agent_key);
    actions.patchPanels({ rightCollapsed: false });
    actions.live(`Opened audit timeline for ${agent.role || "agent"} ${agent.agent}`);
    window.setTimeout(() => {
      document.getElementById("control-agents")?.focus();
    }, 0);
  };

  const returnFromTimeline = () => {
    const key = returnAgent.current;
    const target = [...document.querySelectorAll<HTMLElement>("[data-control-agent-row]")]
      .find(
        (element) =>
          element.dataset.agentKey === key &&
          getComputedStyle(element).display !== "none",
      );
    if (target) {
      target.focus();
      return;
    }
    document.getElementById("cr-agents-title")?.focus();
    actions.live("That agent is no longer on the board.");
  };

  const switchScope = () => {
    const next: ControlScope =
      control.scope === "repository_program" ? "current_plan" : "repository_program";
    control.setScope(next);
    setFilters((current) => ({ ...current, plans: [] }));
  };

  const activeChips = buildActiveChips(filters, setFilters);

  function onGridKey(event: React.KeyboardEvent) {
    const target = event.target as HTMLElement;
    if (target.closest("button, input, textarea, select")) return;
    const columns = Math.max(
      1,
      Math.floor((event.currentTarget as HTMLElement).clientWidth / 300),
    );
    let next = focusIndex;
    if (event.key === "ArrowRight") next += 1;
    else if (event.key === "ArrowLeft") next -= 1;
    else if (event.key === "ArrowDown") next += columns;
    else if (event.key === "ArrowUp") next -= columns;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = agents.length - 1;
    else if (event.key === "PageDown") next += 10;
    else if (event.key === "PageUp") next -= 10;
    else if (event.key.toLowerCase() === "r") {
      event.preventDefault();
      void control.refresh();
      return;
    } else if (event.key.toLowerCase() === "s") {
      event.preventDefault();
      switchScope();
      return;
    } else {
      return;
    }
    event.preventDefault();
    const bounded = Math.max(0, Math.min(agents.length - 1, next));
    setFocusIndex(bounded);
    cardRefs.current[bounded]?.focus();
  }

  const left = (
    <div className="cr-left">
      <Filters
        agents={allAgents}
        plans={snapshot?.plans ?? {}}
        filters={filters}
        waitingKeys={waitingKeys}
        onChange={setFilters}
      />
      {filtersActive(filters) ? (
        <button
          type="button"
          className="link-clear"
          onClick={() => setFilters(EMPTY_FILTERS)}
        >
          Clear all
        </button>
      ) : null}
    </div>
  );

  return (
    <ModeLayout
      compact
      left={left}
      leftTitle="Filters"
      right={<AuditDrawer now={now} onEscape={returnFromTimeline} />}
      rightTitle="Agent timeline"
    >
      <div className="control-room">
        <ScopeBar
          repository={snapshot?.repository?.label ?? "Repository unavailable"}
          scope={control.scope}
          currentPlan={currentPlan}
          sampledAt={snapshot?.sampled_at ?? snapshot?.fresh_as_of ?? null}
          now={now}
          limits={snapshot?.limits?.length ? snapshot.limits : PRIVACY_LIMITS}
          density={density}
          activeChips={activeChips}
          onScope={control.setScope}
          onDensity={(value) => actions.patchPanels({ density: value })}
          onClear={() => setFilters(EMPTY_FILTERS)}
        />

        {snapshot ? (
          <ProgramStrip
            plans={snapshot.plans}
            selected={selectedPlanFilter}
            onSelect={(plan) =>
              setFilters((current) => ({
                ...current,
                plans: current.plans[0] === plan ? [] : [plan],
              }))
            }
          />
        ) : null}

        <FlowTopology
          agents={agents}
          topology={snapshot?.topology}
          now={now}
          onSelect={openTimeline}
        />

        <BoardBands
          snapshot={snapshot}
          error={control.error}
          now={now}
          onRetry={() => void control.refresh()}
        />

        <section className="cr-agent-region" aria-labelledby="cr-agents-title">
          <h2 id="cr-agents-title" className="cr-agents-title" tabIndex={-1}>
            Agents
            {allAgents.length > 0 ? (
              <span className="cr-agents-count">{agents.length} shown</span>
            ) : null}
          </h2>

          {control.loading && !snapshot ? (
            showSkeleton ? (
              <div className="cr-loading" aria-busy="true">
                <p role="status">Loading the board…</p>
                {[0, 1, 2].map((value) => (
                  <div key={value} className="cr-skeleton-row" />
                ))}
              </div>
            ) : null
          ) : !snapshot ? (
            <BoardState title="The Control room is unavailable.">
              The repository program snapshot could not be read. Retry when the local
              workspace server is available.
              <button
                type="button"
                className="btn btn-text"
                onClick={() => void control.refresh()}
              >
                Retry now
              </button>
            </BoardState>
          ) : allAgents.length === 0 &&
            control.scope === "current_plan" &&
            (snapshot.repository_agents ?? 0) > 0 ? (
            <BoardState title={`No agents are registered for ${currentPlan}.`}>
              <p>
                {snapshot.repository_agents} agent
                {snapshot.repository_agents === 1 ? " is" : "s are"} registered
                elsewhere in this repository.
              </p>
              <button
                type="button"
                className="btn btn-text"
                onClick={() => control.setScope("repository_program")}
              >
                Show repository program
              </button>
            </BoardState>
          ) : allAgents.length === 0 ? (
            <FirstUseState plan={currentPlan} />
          ) : agents.length === 0 ? (
            <BoardState title="No agent matches these filters.">
              <button
                type="button"
                className="btn btn-text"
                onClick={() => setFilters(EMPTY_FILTERS)}
              >
                Clear filters
              </button>
            </BoardState>
          ) : density === "grid" ? (
            <div className="agent-grid" role="list" aria-label="Agents" onKeyDown={onGridKey}>
              {agents.map((agent, index) => (
                <AgentCard
                  key={agent.agent_key}
                  ref={(element) => {
                    cardRefs.current[index] = element;
                  }}
                  agent={agent}
                  now={now}
                  selected={(focusedAgent ?? control.selectedAgent) === agent.agent_key}
                  tabIndex={index === focusIndex ? 0 : -1}
                  onFocus={() => {
                    setFocusIndex(index);
                    setFocusedAgent(agent.agent_key);
                    actions.live(announceAgent(agent, now));
                  }}
                  onWatch={() => openTimeline(agent)}
                  onFeedback={() => openComposer(agent.agent_key)}
                  onNudge={() => openComposer(agent.agent_key, true)}
                  onNavigate={() => {
                    if (agent.plan === state.plan) actions.setMode("document");
                  }}
                />
              ))}
            </div>
          ) : (
            <AgentList
              agents={agents}
              now={now}
              selected={focusedAgent ?? control.selectedAgent}
              waitingKeys={waitingKeys}
              comfortable={false}
              onSelect={setFocusedAgent}
              onWatch={(key) => {
                const agent = agents.find((item) => item.agent_key === key);
                if (agent) openTimeline(agent);
              }}
              onFeedback={(key) => openComposer(key)}
              onNudge={(key) => openComposer(key, true)}
              onRefresh={() => void control.refresh()}
              onSwitchScope={switchScope}
              onAnnounce={actions.live}
            />
          )}
        </section>

        {snapshot ? (
          <>
            <RecentEvents
              events={snapshot.recent_events ?? []}
              status={snapshot.events_status ?? "unavailable"}
              visibleAgents={new Set(agents.map((agent) => agent.agent_key))}
              filtersApplied={filtersActive(filters)}
              now={now}
              onWatch={(key) => {
                const agent = allAgents.find((item) => item.agent_key === key);
                if (agent) openTimeline(agent);
              }}
            />
            <UnregisteredSessions snapshot={snapshot} now={now} />
          </>
        ) : null}
      </div>
    </ModeLayout>
  );
}

function ScopeBar({
  repository,
  scope,
  currentPlan,
  sampledAt,
  now,
  limits,
  density,
  activeChips,
  onScope,
  onDensity,
  onClear,
}: {
  repository: string;
  scope: ControlScope;
  currentPlan: string;
  sampledAt: number | null;
  now: number;
  limits: ControlLimit[];
  density: "grid" | "list";
  activeChips: { label: string; clear: () => void }[];
  onScope: (scope: ControlScope) => void;
  onDensity: (density: "grid" | "list") => void;
  onClear: () => void;
}) {
  return (
    <header className="cr-scopebar">
      <div className="cr-scopebar-main">
        <span className="cr-scope-item">
          <span className="cr-scope-label">Repository</span>
          <strong>{repository}</strong>
        </span>
        <span className="cr-scope-item">
          <span className="cr-scope-label">Scope</span>
          <SegmentedControl
            ariaLabel="Control room scope"
            value={scope}
            onChange={onScope}
            options={[
              { value: "repository_program", label: "Repository program" },
              { value: "current_plan", label: `This plan · ${currentPlan}` },
            ]}
          />
        </span>
        <span className="cr-sampled">
          {sampledAt == null ? "Sample time unavailable" : `Sampled ${ago(sampledAt, now)}`}
        </span>
        <details className="cr-withheld">
          <summary>Withheld: {limits.length}</summary>
          <div className="cr-withheld-popover">
            <strong>These are never shown, by design:</strong>
            <ul>
              {limits.map((limit) => (
                <li key={`${limit.category}:${limit.detail}`}>{limit.detail}</li>
              ))}
            </ul>
          </div>
        </details>
        <span className="cr-scope-item cr-density">
          <span className="cr-scope-label">Density</span>
          <SegmentedControl
            ariaLabel="Density"
            value={density}
            onChange={onDensity}
            options={[
              { value: "list", label: "Rows" },
              { value: "grid", label: "Cards" },
            ]}
          />
        </span>
      </div>
      {activeChips.length > 0 ? (
        <div className="cr-active-filters" aria-label="Active filters">
          <span className="cr-scope-label">Filtered by</span>
          {activeChips.map((chip) => (
            <span key={chip.label} className="chip chip-neutral">
              {chip.label}
              <button
                type="button"
                className="chip-remove"
                aria-label={`Remove ${chip.label}`}
                onClick={chip.clear}
              >
                ×
              </button>
            </span>
          ))}
          <button type="button" className="link-clear" onClick={onClear}>
            Clear all
          </button>
        </div>
      ) : null}
    </header>
  );
}

function BoardBands({
  snapshot,
  error,
  now,
  onRetry,
}: {
  snapshot: ReturnType<typeof useControl>["snapshot"];
  error: string | null;
  now: number;
  onRetry: () => void;
}) {
  if (!snapshot) return null;
  const sampledAt = snapshot.sampled_at ?? snapshot.fresh_as_of;
  const stalled =
    !!error ||
    now - sampledAt > 60_000 ||
    snapshot.connection === "stale" ||
    snapshot.connection === "disconnected";
  const unavailableAgents = snapshot.agents.filter(
    (agent) =>
      agent.events_status === "unavailable" ||
      agent.events_status === "permission_limited" ||
      agent.events_status === "disconnected" ||
      agent.event_source_registered === false,
  );
  return (
    <>
      {stalled ? (
        <div className="cr-band cr-band-stalled" role="status">
          <span>
            The board stopped updating {ago(sampledAt, now)}. Everything below is
            the last sample, not live.
          </span>
          <button type="button" className="btn-secondary-text" onClick={onRetry}>
            Retry now
          </button>
        </div>
      ) : null}
      {unavailableAgents.length > 0 ? (
        <div className="cr-band cr-band-source" role="status">
          Timeline activity unavailable for {unavailableAgents.length} of{" "}
          {snapshot.agents.length} agents. Their lifecycle remains independent and
          Unknown unless a supported run source reported it.
        </div>
      ) : null}
      {snapshot.source_coverage?.some(
        (coverage) =>
          coverage.status !== "available" && coverage.status !== "empty",
      ) ? (
        <div className="cr-source-summary">
          Source coverage is limited:{" "}
          {snapshot.source_coverage
            .filter(
              (coverage) =>
                coverage.status !== "available" && coverage.status !== "empty",
            )
            .map((coverage) => `${coverage.source} ${coverage.status.replace(/_/g, " ")}`)
            .join(" · ")}
        </div>
      ) : null}
    </>
  );
}

function FirstUseState({ plan }: { plan: string }) {
  return (
    <BoardState title="No agents are registered in this repository.">
      <p>The Control room shows agents that registered themselves with Grogu.</p>
      <p>Register a running session:</p>
      <code className="cr-register-command">
        grogu plan doc register {plan} --agent &lt;name&gt; --role &lt;role&gt;
      </code>
      <p>
        Recent Grogu commands are not evidence that an agent is running. A session
        that never registered appears under Unregistered sessions with an Unknown
        lifecycle, not as an idle agent.
      </p>
    </BoardState>
  );
}

function BoardState({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="cr-board-state">
      <h3>{title}</h3>
      {children}
    </div>
  );
}

function RecentEvents({
  events,
  status,
  visibleAgents,
  filtersApplied,
  now,
  onWatch,
}: {
  events: OperationalEvent[];
  status: "available" | "empty" | "unavailable" | "stale" | "disconnected" | "permission_limited";
  visibleAgents: Set<string>;
  filtersApplied: boolean;
  now: number;
  onWatch: (agentKey: string) => void;
}) {
  const visible = events.filter((event) => visibleAgents.has(event.agent_key)).slice(0, 8);
  return (
    <section className="cr-recent" aria-labelledby="cr-recent-title">
      <h2 id="cr-recent-title">Recent observable events</h2>
      {status === "unavailable" ? (
        <p>Recent events unavailable: no usable registered event source.</p>
      ) : status === "permission_limited" ? (
        <p>Recent events permission limited: a registered source could not be read.</p>
      ) : status === "disconnected" ? (
        <p>Recent events disconnected: the source could not be refreshed.</p>
      ) : visible.length === 0 && filtersApplied && events.length > 0 ? (
        <p>No recent event matches the active agent filters.</p>
      ) : visible.length === 0 ? (
        <p>No recent operational events are available in this window.</p>
      ) : (
        <ol className="cr-recent-list">
          {visible.map((event) => (
            <li key={event.id}>
              <button type="button" onClick={() => onWatch(event.agent_key)}>
                <span>{eventText(event, now)}</span>
                <span>{event.plan}</span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function UnregisteredSessions({
  snapshot,
  now,
}: {
  snapshot: NonNullable<ReturnType<typeof useControl>["snapshot"]>;
  now: number;
}) {
  const sessions = snapshot.unregistered_sessions ?? [];
  return (
    <details className="cr-unregistered">
      <summary>Unregistered sessions ({sessions.length})</summary>
      <p>
        These sessions ran Grogu commands in this repository. They never registered,
        so their lifecycle and activity are Unknown and nothing here is inferred from
        those commands.
      </p>
      {snapshot.unregistered_sessions_status === "unavailable" ? (
        <p>Unregistered-session metadata is unavailable.</p>
      ) : sessions.length === 0 ? (
        <p>No attributable unregistered session is visible.</p>
      ) : (
        <ul>
          {sessions.map((session) => (
            <li key={`${session.agent}:${session.plan ?? "unknown"}`}>
              {session.agent} · {session.plan ?? "plan unknown"} · lifecycle Unknown ·
              activity Unknown ·{" "}
              {session.last_observed_at == null
                ? "last observation unknown"
                : `last observation ${ago(session.last_observed_at, now)}`}
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}

function buildActiveChips(
  filters: ControlFilters,
  setFilters: React.Dispatch<React.SetStateAction<ControlFilters>>,
): { label: string; clear: () => void }[] {
  return [
    ...filters.roles.map((role) => ({
      label: role,
      clear: () =>
        setFilters((current) => ({
          ...current,
          roles: current.roles.filter((item) => item !== role),
        })),
    })),
    ...filters.plans.map((plan) => ({
      label: plan,
      clear: () =>
        setFilters((current) => ({
          ...current,
          plans: current.plans.filter((item) => item !== plan),
        })),
    })),
    ...filters.workstreams.map((workstream) => ({
      label: workstream,
      clear: () =>
        setFilters((current) => ({
          ...current,
          workstreams: current.workstreams.filter((item) => item !== workstream),
        })),
    })),
    ...filters.lifecycles.map((lifecycle) => ({
      label: LIFECYCLE_META[lifecycle].label,
      clear: () =>
        setFilters((current) => ({
          ...current,
          lifecycles: current.lifecycles.filter((item) => item !== lifecycle),
        })),
    })),
    ...filters.activities.map((activity) => ({
      label: ACTIVITY_META[activity].label,
      clear: () =>
        setFilters((current) => ({
          ...current,
          activities: current.activities.filter((item) => item !== activity),
        })),
    })),
    ...filters.connections.map((connection) => ({
      label: CONNECTION_LABEL[connection],
      clear: () =>
        setFilters((current) => ({
          ...current,
          connections: current.connections.filter((item) => item !== connection),
        })),
    })),
    ...filters.attention.map((attention) => ({
      label: attention.replace(/_/g, " "),
      clear: () =>
        setFilters((current) => ({
          ...current,
          attention: current.attention.filter((item) => item !== attention),
        })),
    })),
  ];
}
