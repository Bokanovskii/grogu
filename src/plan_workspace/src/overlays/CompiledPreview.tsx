import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ProjectionResponse } from "../api/types";
import { Modal, ModalBody, ModalFooter, ModalHeader, ModalTitle } from "../shell/Modal";
import { useActions, useApp } from "../state/store";
import { Markdown } from "../lib/markdown";
import { shortDigest } from "../lib/format";

// Full-width compiled Markdown preview (⌘E). Header shows role, stage, include,
// budget and source digest. Two panes: Markdown source and rendered HTML (via
// the safe React renderer — no dangerouslySetInnerHTML). Elided items panel is
// always visible when non-empty.
export function CompiledPreview() {
  const state = useApp();
  const actions = useActions();
  const role = (state.role || "reviewer") as string;
  const [data, setData] = useState<ProjectionResponse | null>(null);
  const [synced, setSynced] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api
      .projection({ role, stage: state.stage, include: "normative", format: "md" })
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch(() => setData(null));
    return () => {
      cancelled = true;
    };
  }, [role, state.stage]);

  return (
    <Modal title="Compiled projection" onClose={actions.closeOverlay} width={1100} className="compiled-modal">
      <ModalHeader>
        <ModalTitle>
          Compiled projection · Role: {role} · Stage: {state.stage} · Include:{" "}
          {data?.include ?? "normative"} · Budget: {data?.budget ?? "—"} · Digest:{" "}
          {data ? shortDigest(data.digest) : "…"}
        </ModalTitle>
      </ModalHeader>
      <ModalBody>
        {state.equivalence ? (
          <div className="equivalence-banner equivalence-inline" role="alert">
            The compiled Markdown does not round-trip.
            <button
              type="button"
              className="btn btn-warn"
              onClick={() => {
                void navigator.clipboard?.writeText(
                  JSON.stringify({ ...state.equivalence, plan: state.plan }, null, 2),
                );
              }}
            >
              Copy diagnostic
            </button>
          </div>
        ) : null}
        <label className="sync-scroll-toggle">
          <input type="checkbox" checked={synced} onChange={(e) => setSynced(e.target.checked)} /> Synced scroll
        </label>
        <div className={`compiled-panes${synced ? " is-synced" : ""}`}>
          <pre className="compiled-source" aria-label="Markdown source">
            {data?.markdown ?? "Loading…"}
          </pre>
          <div className="compiled-rendered" aria-label="Rendered">
            {data ? <Markdown text={data.markdown} /> : null}
          </div>
        </div>
        {data && data.elided && data.elided.length ? (
          <div className="elided-panel">
            <strong>
              Elided: {data.elided.map((e) => `${e.count ?? 1} ${e.kind}`).join(" · ")}
            </strong>
            <details>
              <summary>Show elided ids</summary>
              <ul>
                {data.elided.map((e) => (
                  <li key={e.id}>{e.id}</li>
                ))}
              </ul>
            </details>
          </div>
        ) : null}
      </ModalBody>
      <ModalFooter>
        <button
          type="button"
          className="btn btn-text"
          onClick={() => data && void navigator.clipboard?.writeText(data.markdown)}
        >
          Copy Markdown
        </button>
        <button
          type="button"
          className="btn btn-text"
          onClick={() => {
            if (!data) return;
            const blob = new Blob([data.markdown], { type: "text/markdown" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `${state.plan}-${state.stage}.md`;
            a.click();
            URL.revokeObjectURL(url);
          }}
        >
          Save to file…
        </button>
        <button type="button" className="btn btn-primary" onClick={actions.closeOverlay}>
          Close
        </button>
      </ModalFooter>
    </Modal>
  );
}
