import { useEffect, useState } from "react";
import { api, ApiFailure } from "../api/client";
import type { ImpactItem, ProposalPreview as PreviewData, Selector } from "../api/types";
import { Modal, ModalBody, ModalFooter, ModalHeader, ModalTitle } from "../shell/Modal";
import { useActions, useApp } from "../state/store";

export function AskGrogu({ threadId }: { threadId: string }) {
  const state = useApp();
  const actions = useActions();
  const [instruction, setInstruction] = useState("");
  const [status, setStatus] = useState<"compose" | "sending">("compose");
  const thread = state.nodes[threadId];

  useEffect(() => {
    if (status !== "sending") return;
    const timeout = window.setTimeout(() => {
      setStatus("compose");
      actions.toast("Sending the revision request timed out. Try again.", "warn");
    }, 45000);
    return () => window.clearTimeout(timeout);
  }, [status, actions]);

  async function propose() {
    setStatus("sending");
    try {
      const res = await api.reviseThread(threadId, instruction.trim());
      await actions.refreshDoc();
      actions.toast(`Revision request ${res.request.seq} recorded; implementation waits for architect acknowledgement.`, "success", {
        label: "View ledger",
        event: "noop",
      });
      actions.closeOverlay();
    } catch (error) {
      setStatus("compose");
      actions.toast(
        error instanceof Error ? error.message : "Could not send the revision request.",
        "danger",
      );
    }
  }

  return (
    <Modal title="Ask Grogu to revise" onClose={actions.closeOverlay} width={720}>
      <ModalHeader>
        <ModalTitle>Ask Grogu to revise</ModalTitle>
      </ModalHeader>
      <ModalBody>
        <div className="ask-context">
          <div className="ask-context-line">Thread {threadId}</div>
          {thread ? <blockquote className="ask-quote">{thread.body || thread.title}</blockquote> : null}
          <div className="ask-context-line">
            {thread?.title} · {thread?.stage || "plan"}
          </div>
        </div>
        {status === "compose" ? (
          <textarea
            className="ask-textarea"
            autoFocus
            rows={4}
            value={instruction}
            placeholder="What would you like Grogu to change?"
            aria-label="Instruction"
            onChange={(e) => setInstruction(e.target.value)}
          />
        ) : (
          <div className="ask-proposing">
            <p>Sending revision request…</p>
            <div className="indeterminate-bar" />
          </div>
        )}
      </ModalBody>
      <ModalFooter>
        <span className="ask-base">Base: {state.base}</span>
        <button type="button" className="btn btn-text" onClick={actions.closeOverlay}>
          Cancel
        </button>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!instruction.trim() || status === "sending"}
          onClick={() => void propose()}
        >
          Send request
        </button>
      </ModalFooter>
    </Modal>
  );
}

function ImpactList({ items, consequential }: { items: ImpactItem[]; consequential?: boolean }) {
  return (
    <ul className="impact-list">
      {items.map((it) => (
        <li key={it.id} className={`impact-item${it.destructive ? " impact-destructive" : ""}`}>
          <span className="impact-id">{it.id}</span>
          {it.op ? <span className="impact-op">{it.op}</span> : null}
          <span className="impact-title">{it.title}</span>
          {consequential && it.reason ? <span className="impact-reason">{it.reason}</span> : null}
        </li>
      ))}
      {items.length === 0 ? <li className="inspector-muted">None.</li> : null}
    </ul>
  );
}

export function ProposalPreview({ proposalId }: { proposalId: string }) {
  const state = useApp();
  const actions = useActions();
  const [data, setData] = useState<PreviewData | null>(null);
  const [roleTab, setRoleTab] = useState(0);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError("");
    api
      .proposalPreview(proposalId)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        // Consequential destructive rows expand by default.
        const auto = new Set(d.impact.transitive.filter((t) => t.destructive).map((t) => t.id));
        setExpanded(auto);
      })
      .catch((failure) => {
        if (cancelled) return;
        setError(
          failure instanceof ApiFailure
            ? failure.message
            : "The proposal preview did not finish.",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [proposalId, attempt]);

  const destructiveUnread =
    data?.impact.transitive.filter((t) => t.destructive).some((t) => !expanded.has(t.id)) ?? false;

  async function accept() {
    try {
      const res = await api.acceptProposal(proposalId);
      actions.toast(`Applied as ${res.revision}.`, "success", { label: "Open revision", event: "noop" });
      actions.live(`Proposal ${proposalId} applied as ${res.revision}`);
      await actions.refreshDoc();
      await actions.reloadProposals();
      actions.closeOverlay();
    } catch {
      actions.toast("Could not accept the proposal.", "danger");
    }
  }
  async function reject() {
    try {
      await api.rejectProposal(proposalId, reason.trim());
      await actions.reloadProposals();
      actions.closeOverlay();
    } catch {
      actions.toast("Could not reject the proposal.", "danger");
    }
  }

  return (
    <Modal title="Proposal preview" onClose={actions.closeOverlay} width={1000} className="proposal-modal">
      <ModalHeader>
        <ModalTitle>Proposal {proposalId}</ModalTitle>
        {data?.stale ? (
          <div className="proposal-stale" role="alert">
            Stale — the plan moved to {data.head ?? state.revision} since this proposal was made.
            <button type="button" className="btn btn-text" onClick={() => void actions.refreshDoc()}>
              Rebase
            </button>
            <button type="button" className="btn btn-text" onClick={() => void reject()}>
              Abandon
            </button>
          </div>
        ) : null}
      </ModalHeader>
      <ModalBody>
        {error ? (
          <div className="compiled-error" role="alert">
            <strong>Could not load this proposal preview.</strong>
            <p>{error}</p>
            <button
              type="button"
              className="btn btn-text"
              onClick={() => setAttempt((value) => value + 1)}
            >
              Retry
            </button>
          </div>
        ) : !data ? (
          <p className="inspector-muted">Loading preview…</p>
        ) : (
          <div className="proposal-cols">
            <section className="proposal-col proposal-nodediff">
              <h3 className="proposal-col-title">Node diff</h3>
              <ul className="node-diff-list">
                {data.node_diff.map((d) => (
                  <li key={d.id} className="node-diff-item">
                    <span className={`nd-op nd-${d.op}`}>{d.op}</span>
                    <span className="nd-id">{d.id}</span>
                    {d.before?.title && d.before.title !== d.after?.title ? (
                      <span className="nd-old">{d.before.title}</span>
                    ) : null}
                    {d.after?.title ? <span className="nd-new">{d.after.title}</span> : null}
                  </li>
                ))}
              </ul>
            </section>

            <section className="proposal-col proposal-compiled">
              <div className="proposal-role-tabs" role="tablist" aria-label="Roles">
                {data.compiled_diff.map((c, i) => (
                  <button key={c.role} type="button" role="tab" aria-selected={roleTab === i} className={`palette-tab${roleTab === i ? " is-active" : ""}`} onClick={() => setRoleTab(i)}>
                    {c.role}
                  </button>
                ))}
              </div>
              <pre className="compiled-preview-diff">
                {data.compiled_diff[roleTab]?.after ?? ""}
              </pre>
            </section>

            <section className="proposal-col proposal-impact">
              <h3 className="proposal-col-title">Impact</h3>
              <h4 className="impact-sub">Primary changes</h4>
              <ImpactList items={data.impact.direct} />
              <h4 className="impact-sub">Consequential changes</h4>
              <ul className="impact-list">
                {data.impact.transitive.map((it) => (
                  <li key={it.id} className={`impact-item${it.destructive ? " impact-destructive" : ""}`}>
                    <button
                      type="button"
                      className="impact-expand"
                      aria-expanded={expanded.has(it.id)}
                      onClick={() =>
                        setExpanded((s) => {
                          const next = new Set(s);
                          if (next.has(it.id)) next.delete(it.id);
                          else next.add(it.id);
                          return next;
                        })
                      }
                    >
                      {expanded.has(it.id) ? "▾" : "▸"} {it.id}
                    </button>
                    <span className="impact-title">{it.title}</span>
                    {expanded.has(it.id) && it.reason ? <div className="impact-reason">{it.reason}</div> : null}
                  </li>
                ))}
                {data.impact.cycles.length ? (
                  <li className="impact-item impact-destructive">Cycle introduced: {data.impact.cycles.map((c) => c.join(" → ")).join("; ")}</li>
                ) : null}
              </ul>
            </section>
          </div>
        )}
      </ModalBody>
      <ModalFooter>
        {destructiveUnread ? (
          <span className="proposal-warn">Review each highlighted consequence before accepting.</span>
        ) : null}
        <button type="button" className="btn btn-text" onClick={actions.closeOverlay}>
          Save as draft
        </button>
        <span className="proposal-divider" aria-hidden="true" />
        {rejecting ? (
          <div className="reject-row">
            <input
              className="reject-reason"
              autoFocus
              placeholder="Reason"
              value={reason}
              aria-label="Reject reason"
              onChange={(e) => setReason(e.target.value)}
            />
            <button type="button" className="btn btn-danger" disabled={!reason.trim()} onClick={() => void reject()}>
              Reject
            </button>
          </div>
        ) : (
          <button type="button" className="btn btn-text" onClick={() => setRejecting(true)}>
            Reject with reason
          </button>
        )}
        <button type="button" className="btn btn-primary" disabled={destructiveUnread} onClick={() => void accept()}>
          Accept proposal
        </button>
      </ModalFooter>
    </Modal>
  );
}

export function ImpactPreview() {
  const state = useApp();
  const actions = useActions();
  const [data, setData] = useState<{ direct: ImpactItem[]; transitive: ImpactItem[] } | null>(null);
  const sel = state.selection[0];

  useEffect(() => {
    if (!sel) return;
    const selector: Selector = { type: "node", id: sel.id };
    api
      .impact(selector, 99)
      .then((d) => setData({ direct: d.direct, transitive: d.transitive }))
      .catch(() => setData(null));
  }, [sel?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") actions.closeOverlay();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [actions]);

  return (
    <aside className="impact-popover" role="dialog" aria-modal="false" aria-label="Impact">
      <ModalHeader>
        <ModalTitle>Impact of {sel?.id ?? "selection"}</ModalTitle>
        <button type="button" className="btn btn-text" onClick={actions.closeOverlay} aria-label="Close impact preview">
          Close
        </button>
      </ModalHeader>
      <ModalBody>
        {!data ? (
          <p className="inspector-muted">Select a node to see its impact.</p>
        ) : (
          <>
            <h4 className="impact-sub">Primary changes</h4>
            <ImpactList items={data.direct} />
            <h4 className="impact-sub">Consequential changes</h4>
            <ImpactList items={data.transitive} consequential />
          </>
        )}
      </ModalBody>
    </aside>
  );
}
