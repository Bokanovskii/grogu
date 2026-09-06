import { useMemo, useState } from "react";
import type { AuditEvent, AuditEventType } from "../../api/types";
import { useControl } from "../../state/control";
import { useActions, useApp } from "../../state/store";
import { clock, ago } from "../../lib/format";
import { FeedbackComposer } from "./FeedbackComposer";
import { SegmentedControl } from "../../shell/ui";

const AUDIT_ICON: Record<AuditEventType, string> = {
  revision: "⟳",
  patch: "◆",
  tool: "▷",
  gate_passed: "✓",
  gate_blocked: "⊘",
  steering_delivered: "✉",
  feedback_acknowledged: "☑",
  feedback_withdrawn: "⤺",
  error: "×",
  disconnected: "⌀",
  connected: "●",
  role_bound: "◎",
  workstream_started: "▸",
  workstream_merged: "⋈",
};

const PROVENANCE =
  "Timeline shows observable events. Grogu never records prompts, chain-of-thought, or raw tool arguments. What you see here is the same view the tester and reviewer see.";

type Range = "1h" | "24h" | "7d" | "all";
const RANGE_MS: Record<Range, number> = {
  "1h": 3600_000,
  "24h": 86_400_000,
  "7d": 604_800_000,
  all: Infinity,
};

export function AuditDrawer({ now }: { now: number }) {
  const control = useControl();
  const actions = useActions();
  const state = useApp();
  const [range, setRange] = useState<Range>("24h");
  const [autoScroll, setAutoScroll] = useState(true);
  const [hideConnect, setHideConnect] = useState(false);
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState<AuditEventType | "all">("all");

  const drill = control.drill;
  const agent = drill?.agent ?? null;
  const events = useMemo(() => {
    let evs: AuditEvent[] = drill?.activity ?? [];
    const cutoff = now - RANGE_MS[range];
    if (Number.isFinite(RANGE_MS[range])) evs = evs.filter((e) => e.at >= cutoff);
    if (hideConnect) evs = evs.filter((e) => e.type !== "connected" && e.type !== "disconnected");
    if (typeFilter !== "all") evs = evs.filter((e) => e.type === typeFilter);
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      evs = evs.filter(
        (e) =>
          e.summary.toLowerCase().includes(q) ||
          (e.objects?.ids ?? []).some((id) => id.toLowerCase().includes(q)),
      );
    }
    const sorted = [...evs].sort((a, b) => b.at - a.at);
    return sorted;
  }, [drill, range, hideConnect, typeFilter, search, now]);

  const newest = events[0];
  const quiet = newest && now - newest.at > 5 * 60 * 1000;

  function navigate(ev: AuditEvent) {
    if (!ev.objects) return;
    if (ev.objects.plan === state.plan) {
      const sel = ev.objects.ids
        .filter((id) => state.nodes[id])
        .map((id) => ({ type: "node" as const, id }));
      if (sel.length) {
        actions.setSelection(sel);
        if (state.mode === "control") actions.setMode("document");
        actions.live(`Navigated to ${sel.map((s) => s.id).join(", ")}`);
      }
    } else {
      actions.toast(`That object is in ${ev.objects.plan}. Open that plan to see it.`, "info");
    }
  }

  return (
    <div className="audit-drawer" id="control-agents">
      <div className="audit-header">
        <h3 className="audit-title">{agent ? `${agent.role} · ${agent.workstream || agent.agent}` : "Audit timeline"}</h3>
        <SegmentedControl
          ariaLabel="Time range"
          value={range}
          onChange={(v) => setRange(v)}
          options={[
            { value: "1h", label: "1h" },
            { value: "24h", label: "24h" },
            { value: "7d", label: "7d" },
            { value: "all", label: "All" },
          ]}
        />
        <label className="audit-toggle">
          <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} />
          Auto-scroll to newest
        </label>
      </div>

      <div className="audit-filters">
        <select
          className="audit-type"
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value as AuditEventType | "all")}
          aria-label="Filter by event type"
        >
          <option value="all">All event types</option>
          {Object.keys(AUDIT_ICON).map((t) => (
            <option key={t} value={t}>
              {t.replace(/_/g, " ")}
            </option>
          ))}
        </select>
        <label className="audit-toggle">
          <input type="checkbox" checked={hideConnect} onChange={(e) => setHideConnect(e.target.checked)} />
          Hide connect/disconnect
        </label>
        <input
          className="audit-search"
          type="search"
          placeholder="revision or node id"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search timeline"
        />
      </div>

      <div className="audit-body" tabIndex={0} role="region" aria-label="Timeline events">
        {!agent ? (
          <p className="audit-empty">
            Select an agent to watch its observable events.
          </p>
        ) : events.length === 0 ? (
          <div className="audit-empty">
            <p className="audit-empty-title">No events yet.</p>
            <p>
              Timeline shows observable events. Grogu never records prompts, chain-of-thought, or raw
              tool arguments.
            </p>
          </div>
        ) : (
          <ul className="audit-list">
            {quiet ? (
              <li className="audit-quiet">Quiet since {clock(newest!.at)}. No new events.</li>
            ) : null}
            {events.map((ev) => (
              <li key={ev.id} className={`audit-item audit-item-${ev.type}`}>
                <span className="audit-icon" aria-hidden="true">
                  {AUDIT_ICON[ev.type]}
                </span>
                <div className="audit-main">
                  <div className="audit-time">
                    {clock(ev.at)} · {ago(ev.at, now)}
                  </div>
                  <div className="audit-summary">{ev.summary}</div>
                  {ev.type === "tool" && ev.tool ? (
                    <div className="audit-tool-note">
                      Arguments not shown. Grogu never records prompts or reasoning.
                    </div>
                  ) : null}
                </div>
                {ev.objects && ev.objects.ids.length ? (
                  <button
                    type="button"
                    className="audit-jump"
                    onClick={() => navigate(ev)}
                    title="Jump to affected plan objects"
                  >
                    → {ev.objects.ids.join(", ")}
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>

      <p className="audit-provenance">{PROVENANCE}</p>

      {state.overlay?.kind !== "feedbackComposer" ? (
        <div className="audit-composer">
          <h3 className="audit-composer-title">Send feedback</h3>
          <FeedbackComposer agent={agent} plan={state.plan} />
        </div>
      ) : null}
    </div>
  );
}
