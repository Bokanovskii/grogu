import { useState } from "react";
import { api } from "../../api/client";
import type { PlanNode, ThreadKind } from "../../api/types";
import { useActions, useApp } from "../../state/store";

const KIND_OPTIONS: { value: ThreadKind; label: string }[] = [
  { value: "discussion", label: "Discussion" },
  { value: "question", label: "Question" },
  { value: "suggestion", label: "Suggested change" },
  { value: "blocking", label: "Blocking" },
];

// Compose a new thread anchored to a node (object selector) or a text quote.
// The composer offers Discussion, Question, Suggested change, Blocking and —
// for the architect only — Directive.
export function NewThreadComposer({
  node,
  quote,
  onClose,
}: {
  node: PlanNode;
  quote: string;
  onClose: () => void;
}) {
  const state = useApp();
  const actions = useActions();
  const [kind, setKind] = useState<ThreadKind>("discussion");
  const [body, setBody] = useState("");
  const options =
    state.role === "architect"
      ? [...KIND_OPTIONS, { value: "directive" as ThreadKind, label: "Directive" }]
      : KIND_OPTIONS;

  async function post() {
    if (!body.trim()) return;
    try {
      await api.createThread({ type: "object", id: node.id, part: quote ? "body" : "title" }, body.trim(), kind);
      await actions.refreshDoc();
      actions.toast("Comment posted.", "success");
      onClose();
    } catch {
      actions.toast("Could not post comment.", "danger");
    }
  }

  return (
    <div className="new-thread-composer" role="dialog" aria-label={`Comment on ${node.id}`}>
      {quote ? <blockquote className="composer-quote">{quote}</blockquote> : null}
      <div className="composer-kinds" role="radiogroup" aria-label="Comment kind">
        {options.map((o) => (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={kind === o.value}
            className={`chip chip-btn${kind === o.value ? " is-active" : ""}`}
            onClick={() => setKind(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>
      <textarea
        className="composer-text"
        rows={3}
        value={body}
        autoFocus
        placeholder="What would you like to say?"
        aria-label="Comment"
        onChange={(e) => setBody(e.target.value)}
      />
      <div className="composer-actions">
        <button type="button" className="btn btn-text" onClick={onClose}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" disabled={!body.trim()} onClick={() => void post()}>
          Post
        </button>
      </div>
    </div>
  );
}
