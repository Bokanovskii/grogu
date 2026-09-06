import { useEffect, useMemo, useState } from "react";
import { api } from "../../api/client";
import type { RevisionEnvelope, Stage } from "../../api/types";
import { STAGES } from "../../api/types";
import { ModeLayout } from "../../shell/ModeLayout";
import { useActions, useApp } from "../../state/store";
import { useControl } from "../../state/control";
import { ago, dayKey, shortDigest } from "../../lib/format";
import { diffLines, diffWords } from "../../lib/diff";
import { SegmentedControl } from "../../shell/ui";

function OriginChip({ origin }: { origin: string }) {
  const short = origin.startsWith("proposal:") ? origin : origin;
  return <span className={`origin-chip origin-${origin.split(":")[0]}`}>{short}</span>;
}

function Timeline({
  revisions,
  selected,
  onSelect,
}: {
  revisions: RevisionEnvelope[];
  selected: string | null;
  onSelect: (r: string) => void;
}) {
  const groups = useMemo(() => {
    const byDay = new Map<string, RevisionEnvelope[]>();
    for (const r of revisions) {
      const key = dayKey(new Date(r.at).getTime());
      const list = byDay.get(key) ?? [];
      list.push(r);
      byDay.set(key, list);
    }
    return [...byDay.entries()];
  }, [revisions]);

  if (revisions.length === 0) {
    return (
      <div className="timeline-empty">
        <p className="state-panel-title">Nothing to compare.</p>
        <p>Make an edit and a revision will appear here.</p>
      </div>
    );
  }

  return (
    <nav className="rev-timeline" aria-label="Revision timeline">
      {groups.map(([day, revs]) => (
        <div key={day} className="rev-day">
          <div className="rev-day-label">{day}</div>
          <ul className="rev-list">
            {revs.map((r) => (
              <li key={r.revision}>
                <button
                  type="button"
                  className={`rev-row${selected === r.revision ? " is-selected" : ""}`}
                  onClick={() => onSelect(r.revision)}
                >
                  <span className="rev-id">{r.revision}</span>
                  <span className="rev-age">{ago(new Date(r.at).getTime(), Date.now())}</span>
                  <span className="rev-actor">{r.actor}</span>
                  <span className="rev-intent">{r.intent || r.summary}</span>
                  <OriginChip origin={r.origin} />
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}

function CompiledDiff({ before, after }: { before: string; after: string }) {
  const lines = useMemo(() => diffLines(before, after), [before, after]);
  return (
    <div className="compiled-diff" role="group" aria-label="Compiled diff">
      <div className="diff-pane diff-left" tabIndex={0} role="region" aria-label="Base revision">
        {lines
          .filter((l) => l.type !== "add")
          .map((l, i) => (
            <div key={i} className={`diff-line diff-${l.type}`}>
              {l.type === "del" ? <DiffWords line={l.text} other="" side="del" /> : l.text || "\u00a0"}
            </div>
          ))}
      </div>
      <div className="diff-pane diff-right" tabIndex={0} role="region" aria-label="This revision">
        {lines
          .filter((l) => l.type !== "del")
          .map((l, i) => (
            <div key={i} className={`diff-line diff-${l.type}`}>
              {l.type === "add" ? <DiffWords line={l.text} other="" side="add" /> : l.text || "\u00a0"}
            </div>
          ))}
      </div>
    </div>
  );
}

function DiffWords({ line, side }: { line: string; other: string; side: "add" | "del" }) {
  // Word-level marks within a fully-added or fully-removed line: mark the whole
  // line's non-space tokens. (Same-line word diffs would require line pairing;
  // the LCS above pairs whole lines.)
  const parts = diffWords(side === "add" ? "" : line, side === "add" ? line : "");
  const ops = side === "add" ? parts.right : parts.left;
  return (
    <>
      {ops.map((o, i) => (
        <span key={i} className={o.type === "same" ? "" : `w-${o.type}`}>
          {o.text}
        </span>
      ))}
    </>
  );
}

function GateRows() {
  const actions = useActions();
  const control = useControl();

  return (
    <div className="gate-rows">
      <h3 className="filter-heading">Gates</h3>
      <ul className="gate-list">
        {STAGES.map((stage) => {
          const blocking = control.feedback.find(
            (f) =>
              f.binding &&
              (f.state === "sent" || f.state === "routed" || f.state === "delivered") &&
              f.gate?.stage === stage,
          );
          return (
            <li key={stage} className={`gate-row${blocking ? " gate-blocked" : ""}`}>
              <span className="gate-stage">{stage}</span>
              {blocking ? (
                <>
                  <span className="gate-blocked-copy">
                    Blocked by {blocking.id} (binding) · sent {ago(blocking.at, Date.now())} · not yet
                    acknowledged.
                  </span>
                  <button type="button" className="rel-link" onClick={() => actions.setMode("control")}>
                    Open in Control room
                  </button>
                  <button type="button" className="rel-link" onClick={() => void control.withdrawFeedback(blocking.id)}>
                    Withdraw
                  </button>
                </>
              ) : (
                <span className="gate-open">open</span>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function RevisionMode() {
  const state = useApp();
  const actions = useActions();
  const [revisions, setRevisions] = useState<RevisionEnvelope[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [diff, setDiff] = useState<{ before: string; after: string } | null>(null);
  const [rightTab, setRightTab] = useState<"source" | "digest" | "provenance">("source");

  useEffect(() => {
    let cancelled = false;
    api
      .revisions()
      .then((r) => {
        if (cancelled) return;
        setRevisions(r.revisions);
        if (r.revisions.length && !selected) setSelected(r.revisions[0]!.revision);
      })
      .catch(() => setRevisions([]));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.revision]);

  const selectedRev = revisions.find((r) => r.revision === selected);

  useEffect(() => {
    if (!selectedRev) return;
    let cancelled = false;
    api
      .diff(selectedRev.parent || selectedRev.revision, selectedRev.revision)
      .then((d) => {
        if (!cancelled) setDiff({ before: d.before, after: d.after });
      })
      .catch(() => setDiff(null));
    return () => {
      cancelled = true;
    };
  }, [selectedRev]);

  const restore = async () => {
    if (!selectedRev) return;
    try {
      const res = await api.restoreAsProposal(selectedRev.revision, state.base);
      actions.openOverlay({ kind: "proposalPreview", proposalId: res.proposal.id });
    } catch {
      actions.toast("Could not build a restore proposal.", "danger");
    }
  };

  const right = (
    <div className="rev-right">
      <SegmentedControl
        ariaLabel="Revision details"
        value={rightTab}
        onChange={setRightTab}
        options={[
          { value: "source", label: "Compiled source" },
          { value: "digest", label: "Source digest" },
          { value: "provenance", label: "Provenance" },
        ]}
      />
      <div className="rev-right-body">
        {rightTab === "source" ? (
          <pre className="rev-source" tabIndex={0} role="region" aria-label="Compiled source">{diff?.after ?? ""}</pre>
        ) : rightTab === "digest" ? (
          <p className="rev-digest">{selectedRev ? shortDigest(selectedRev.after_digest) : "—"}</p>
        ) : selectedRev ? (
          <dl className="inspector-dl">
            <dt>actor</dt>
            <dd>{selectedRev.actor}</dd>
            <dt>role</dt>
            <dd>{selectedRev.role || "—"}</dd>
            <dt>agent</dt>
            <dd>{selectedRev.agent || "—"}</dd>
            <dt>origin</dt>
            <dd>{selectedRev.origin}</dd>
            <dt>intent</dt>
            <dd>{selectedRev.intent}</dd>
          </dl>
        ) : (
          <p className="inspector-muted">Select a revision.</p>
        )}
        <GateRows />
      </div>
    </div>
  );

  return (
    <ModeLayout
      left={<Timeline revisions={revisions} selected={selected} onSelect={setSelected} />}
      leftTitle="Timeline"
      right={right}
      rightTitle="Details"
    >
      <div className="revision-mode">
        {selectedRev ? (
          <>
            <div className="diff-header">
              <div className="diff-header-meta">
                <span className="diff-rev">{selectedRev.revision}</span>
                <span className="diff-author">{selectedRev.actor}</span>
                <span className="diff-reason">{selectedRev.intent}</span>
              </div>
              <button type="button" className="btn btn-primary" onClick={() => void restore()}>
                Restore as proposal
              </button>
            </div>
            {diff ? (
              <CompiledDiff before={diff.before} after={diff.after} />
            ) : (
              <p className="inspector-muted">Loading diff…</p>
            )}
          </>
        ) : (
          <div className="timeline-empty">
            <p className="state-panel-title">Nothing to compare.</p>
            <p>Make an edit and a revision will appear here.</p>
          </div>
        )}
      </div>
    </ModeLayout>
  );
}

export type { Stage };
