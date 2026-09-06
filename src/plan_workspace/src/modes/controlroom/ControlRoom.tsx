import { useEffect, useRef, useState } from "react";
import { useControl } from "../../state/control";
import { useActions, useApp } from "../../state/store";
import { ModeLayout } from "../../shell/ModeLayout";
import { SegmentedControl } from "../../shell/ui";
import { StatePanel, Kbd } from "../../shell/StatePanels";
import { ago } from "../../lib/format";
import { AgentCard } from "./AgentCard";
import { AgentList } from "./AgentList";
import { AuditDrawer } from "./AuditDrawer";
import { Filters } from "./Filters";
import { announceAgent, BADGE_META } from "./badges";
import {
  applyFilters,
  defaultSort,
  EMPTY_FILTERS,
  filtersActive,
  type ControlFilters,
} from "./controlFilters";

export function ControlRoom() {
  const control = useControl();
  const actions = useActions();
  const state = useApp();
  const [filters, setFilters] = useState<ControlFilters>(EMPTY_FILTERS);
  const [now, setNow] = useState(() => Date.now());
  const [focusIndex, setFocusIndex] = useState(0);
  const cardRefs = useRef<(HTMLDivElement | null)[]>([]);

  // Freshness ticks every 5s while visible; paused while hidden. The first
  // foregrounded tick carries the true elapsed time.
  useEffect(() => {
    let timer = 0;
    const tick = () => {
      setNow(Date.now());
      timer = window.setTimeout(tick, 5000);
    };
    if (document.visibilityState === "visible") tick();
    const onVis = () => {
      if (document.visibilityState === "visible") {
        setNow(Date.now());
        window.clearTimeout(timer);
        tick();
      } else {
        window.clearTimeout(timer);
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  const snapshot = control.snapshot;
  const allAgents = snapshot?.agents ?? [];
  const waitingKeys = new Set((snapshot?.waiting_on_you ?? []).map((w) => w.agent_key));
  const filtered = applyFilters(allAgents, filters, now);
  const agents = defaultSort(filtered, waitingKeys);
  const density = state.panels.density;

  const openComposer = (key: string, nudge?: boolean) => {
    control.selectAgent(key);
    actions.openOverlay(nudge ? { kind: "feedbackComposer", agentKey: key, nudge: true } : { kind: "feedbackComposer", agentKey: key });
  };

  const activeChips: { label: string; clear: () => void }[] = [
    ...filters.roles.map((r) => ({ label: r, clear: () => setFilters((f) => ({ ...f, roles: f.roles.filter((x) => x !== r) })) })),
    ...filters.plans.map((p) => ({ label: p, clear: () => setFilters((f) => ({ ...f, plans: f.plans.filter((x) => x !== p) })) })),
    ...filters.workstreams.map((w) => ({ label: w, clear: () => setFilters((f) => ({ ...f, workstreams: f.workstreams.filter((x) => x !== w) })) })),
    ...filters.states.map((s) => ({ label: BADGE_META[s].word, clear: () => setFilters((f) => ({ ...f, states: f.states.filter((x) => x !== s) })) })),
    ...filters.fresh.map((b) => ({ label: b, clear: () => setFilters((f) => ({ ...f, fresh: f.fresh.filter((x) => x !== b) })) })),
  ];

  function onGridKey(e: React.KeyboardEvent) {
    const cols = density === "grid" ? Math.max(1, Math.floor((e.currentTarget as HTMLElement).clientWidth / 336)) : 1;
    let next = focusIndex;
    if (e.key === "ArrowRight") next = Math.min(agents.length - 1, focusIndex + 1);
    else if (e.key === "ArrowLeft") next = Math.max(0, focusIndex - 1);
    else if (e.key === "ArrowDown") next = Math.min(agents.length - 1, focusIndex + cols);
    else if (e.key === "ArrowUp") next = Math.max(0, focusIndex - cols);
    else return;
    e.preventDefault();
    setFocusIndex(next);
    cardRefs.current[next]?.focus();
  }

  const freshAge = snapshot ? ago(snapshot.fresh_as_of, now) : "—";

  const left = (
    <div className="cr-left">
      <Filters agents={allAgents} filters={filters} onChange={setFilters} />
      {filtersActive(filters) ? (
        <button type="button" className="link-clear" onClick={() => setFilters(EMPTY_FILTERS)}>
          Clear all
        </button>
      ) : null}
    </div>
  );

  const right = <AuditDrawer now={now} />;

  return (
    <ModeLayout left={left} leftTitle="Filters" right={right} rightTitle="Audit timeline">
      <div className="control-room">
        <div className="cr-chipsrow">
          <div className="cr-active-filters">
            {activeChips.length === 0 ? (
              <span className="cr-lens-hint">All agents</span>
            ) : (
              activeChips.map((c, i) => (
                <span key={i} className="chip chip-neutral">
                  {c.label}
                  <button type="button" className="chip-remove" aria-label={`Remove ${c.label}`} onClick={c.clear}>
                    ×
                  </button>
                </span>
              ))
            )}
            <span className="cr-freshness" title="Age of the data on this board">
              data {freshAge}
            </span>
          </div>
          <SegmentedControl
            ariaLabel="Density"
            value={density}
            onChange={(v) => actions.patchPanels({ density: v })}
            options={[
              { value: "grid", label: "Grid" },
              { value: "list", label: "List" },
            ]}
          />
        </div>

        {control.loading && !snapshot ? (
          <div className="cr-loading" aria-busy="true">
            <div className="skeleton-line" style={{ width: "30%" }} />
          </div>
        ) : allAgents.length === 0 ? (
          <StatePanel title="No agents are running." tone="neutral">
            <p>
              Start one from your terminal, for example{" "}
              <Kbd>grogu engineer --plan {state.plan || "p-…"}</Kbd> or open a plan and use ‘Ask
              Grogu to revise’. Anything you send from here is durable and appears in the Delivery
              ledger.
            </p>
          </StatePanel>
        ) : agents.length === 0 ? (
          <StatePanel title="Nothing matches this filter." tone="neutral">
            <button type="button" className="link-clear" onClick={() => setFilters(EMPTY_FILTERS)}>
              Clear filters
            </button>
          </StatePanel>
        ) : density === "grid" ? (
          <div
            className="agent-grid"
            role="list"
            aria-label="Agents"
            onKeyDown={onGridKey}
          >
            {agents.map((a, i) => (
              <AgentCard
                key={a.agent_key}
                ref={(el) => {
                  cardRefs.current[i] = el;
                }}
                agent={a}
                now={now}
                selected={control.selectedAgent === a.agent_key}
                tabIndex={i === focusIndex ? 0 : -1}
                onFocus={() => {
                  setFocusIndex(i);
                  actions.live(announceAgent(a, now));
                }}
                onWatch={() => control.selectAgent(a.agent_key)}
                onFeedback={() => openComposer(a.agent_key)}
                onNudge={() => openComposer(a.agent_key, true)}
                onNavigate={() => {
                  if (a.plan === state.plan) actions.setMode("document");
                }}
              />
            ))}
          </div>
        ) : (
          <AgentList
            agents={agents}
            now={now}
            selected={control.selectedAgent}
            onSelect={(k) => control.selectAgent(k)}
            onWatch={(k) => control.selectAgent(k)}
          />
        )}
      </div>
    </ModeLayout>
  );
}
