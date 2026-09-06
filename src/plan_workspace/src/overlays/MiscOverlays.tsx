import { useState } from "react";
import { Modal, ModalBody, ModalFooter, ModalHeader, ModalTitle } from "../shell/Modal";
import { useActions, useApp } from "../state/store";
import { useControl } from "../state/control";
import { FeedbackComposer } from "../modes/controlroom/FeedbackComposer";
import { NewThreadComposer } from "../modes/shared/NewThreadComposer";

// Feedback composer as a modal (⌘⇧M or from a card action).
export function FeedbackComposerModal({ agentKey, nudge }: { agentKey?: string; nudge?: boolean }) {
  const state = useApp();
  const actions = useActions();
  const control = useControl();
  const agent = agentKey ? control.snapshot?.agents.find((a) => a.agent_key === agentKey) ?? null : null;

  return (
    <Modal title="Send feedback" onClose={actions.closeOverlay} width={560}>
      <ModalHeader>
        <ModalTitle>Send feedback</ModalTitle>
      </ModalHeader>
      <ModalBody>
        <FeedbackComposer
          agent={agent}
          plan={state.plan}
          nudge={nudge}
          onSent={actions.closeOverlay}
          onCancel={actions.closeOverlay}
        />
      </ModalBody>
    </Modal>
  );
}

// New comment composer as a modal (canvas / annotation anchoring).
export function NewCommentModal({ nodeId, quote }: { nodeId: string; quote?: string }) {
  const state = useApp();
  const actions = useActions();
  const node = state.nodes[nodeId];
  if (!node) {
    actions.closeOverlay();
    return null;
  }
  return (
    <Modal title="New comment" onClose={actions.closeOverlay} width={520}>
      <ModalHeader>
        <ModalTitle>Comment on {node.id}</ModalTitle>
      </ModalHeader>
      <ModalBody>
        <NewThreadComposer node={node} quote={quote ?? ""} onClose={actions.closeOverlay} />
      </ModalBody>
    </Modal>
  );
}

// Crash recovery: queued intents from a previous session restored from
// localStorage.
export function RestoreQueue() {
  const state = useApp();
  const actions = useActions();
  const [showing, setShowing] = useState(false);

  return (
    <Modal title="Restore unsaved edits?" onClose={actions.closeOverlay} width={520}>
      <ModalHeader>
        <ModalTitle>Restore unsaved edits?</ModalTitle>
      </ModalHeader>
      <ModalBody>
        <p>
          {state.queue.length} edit{state.queue.length === 1 ? "" : "s"} were queued locally when the
          last session ended. They still apply to {state.queue[0]?.base ?? state.base}.
        </p>
        {showing ? (
          <ul className="restore-list">
            {state.queue.map((q) => (
              <li key={q.id}>
                <code>{q.intent}</code> — {q.ops.length} op{q.ops.length === 1 ? "" : "s"}
              </li>
            ))}
          </ul>
        ) : null}
      </ModalBody>
      <ModalFooter>
        <button type="button" className="btn btn-text" onClick={() => setShowing((v) => !v)}>
          {showing ? "Hide" : "Show what changed"}
        </button>
        <button
          type="button"
          className="btn btn-text"
          onClick={() => {
            // Discard: clear the queue.
            for (const q of state.queue) actions.dismissToast(q.id);
            actions.closeOverlay();
            actions.toast("Discarded queued edits.", "info");
          }}
        >
          Discard
        </button>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            void actions.flushQueue();
            actions.closeOverlay();
          }}
        >
          Apply
        </button>
      </ModalFooter>
    </Modal>
  );
}
