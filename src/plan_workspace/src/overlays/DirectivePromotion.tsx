import { useState } from "react";
import { api } from "../api/client";
import type { PatchOp } from "../api/types";
import { Modal, ModalBody, ModalFooter, ModalHeader, ModalTitle } from "../shell/Modal";
import { useActions, useApp } from "../state/store";
import { Allocator } from "../lib/ids";

const AUDIENCES = ["architect", "designer", "engineer", "tester", "reviewer", "everyone"];

export function DirectivePromotion({ threadId }: { threadId: string }) {
  const state = useApp();
  const actions = useActions();
  const thread = state.nodes[threadId];
  const [step, setStep] = useState<1 | 2>(1);
  const [title, setTitle] = useState(thread?.title ?? "");
  const [body, setBody] = useState(thread?.body ?? "");
  const [audience, setAudience] = useState<string[]>(["engineer"]);
  const [binding, setBinding] = useState<"must" | "should">("must");

  const toggleAud = (a: string) =>
    setAudience((cur) => (cur.includes(a) ? cur.filter((x) => x !== a) : [...cur, a]));

  async function promote() {
    const alloc = new Allocator(state.counters);
    const dirId = alloc.node("directive");
    const edgeId = alloc.edge("derives_from");
    const ops: PatchOp[] = [
      ...alloc.counterOps(),
      {
        op: "add",
        path: `/nodes/${dirId}`,
        value: {
          id: dirId,
          kind: "directive",
          stage: thread?.stage ?? "",
          title,
          body,
          attrs: { binding, audience, status: "active", origin: `thread:${threadId}` },
          order: 100,
          created_rev: state.base,
          updated_rev: state.base,
        },
      },
      {
        op: "add",
        path: `/edges/${edgeId}`,
        value: { id: edgeId, kind: "derives_from", from: threadId, to: dirId, attrs: {}, created_rev: state.base },
      },
    ];
    const ok = await actions.writeGesture(ops, `promote ${threadId} to directive`, "workspace", "promote to directive");
    if (ok) {
      try {
        await api.resolveThread(threadId, `↗ ${dirId}`);
      } catch {
        /* thread resolution is best-effort */
      }
      await actions.refreshDoc();
      actions.toast(`Promoted to ${dirId}.`, "success", { label: "Open directive", event: "noop" });
      actions.live(`Promoted to ${dirId}.`);
      actions.closeOverlay();
    }
  }

  return (
    <Modal title="Promote to directive" onClose={actions.closeOverlay} width={640}>
      <ModalHeader>
        <ModalTitle>Promote to directive · Step {step} of 2</ModalTitle>
      </ModalHeader>
      <ModalBody>
        {step === 1 ? (
          <div className="promote-form">
            <label className="inspector-label">Directive title</label>
            <input
              className="inspector-input"
              value={title}
              maxLength={120}
              onChange={(e) => setTitle(e.target.value)}
              aria-label="Directive title"
            />
            <label className="inspector-label">Body</label>
            <textarea className="inspector-input" rows={4} value={body} onChange={(e) => setBody(e.target.value)} aria-label="Directive body" />
            <fieldset className="promote-audience">
              <legend>Audience</legend>
              {AUDIENCES.map((a) => (
                <label key={a} className="checkbox">
                  <input type="checkbox" checked={audience.includes(a)} onChange={() => toggleAud(a)} />
                  {a[0]!.toUpperCase() + a.slice(1)}
                </label>
              ))}
            </fieldset>
            <fieldset className="promote-binding">
              <legend>Binding</legend>
              <label className="radio">
                <input type="radio" name="binding" checked={binding === "must"} onChange={() => setBinding("must")} /> Must
              </label>
              <label className="radio">
                <input type="radio" name="binding" checked={binding === "should"} onChange={() => setBinding("should")} /> Should
              </label>
            </fieldset>
          </div>
        ) : (
          <div className="promote-preview">
            <h3>## Directives</h3>
            <p className="promote-preview-item">
              <strong>{title}</strong> <em>({binding})</em>
            </p>
            <p>{body}</p>
            <p className="promote-source">derives_from thread {threadId}</p>
          </div>
        )}
      </ModalBody>
      <ModalFooter>
        {step === 1 ? (
          <>
            <button type="button" className="btn btn-text" onClick={actions.closeOverlay}>
              Cancel
            </button>
            <button type="button" className="btn btn-primary" disabled={!title.trim() || audience.length === 0} onClick={() => setStep(2)}>
              Next
            </button>
          </>
        ) : (
          <>
            <button type="button" className="btn btn-text" onClick={() => setStep(1)}>
              Back
            </button>
            <button type="button" className="btn btn-primary" onClick={() => void promote()}>
              Promote
            </button>
          </>
        )}
      </ModalFooter>
    </Modal>
  );
}
