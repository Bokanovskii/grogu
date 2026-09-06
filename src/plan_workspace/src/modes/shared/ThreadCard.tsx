import { useState } from "react";
import { api } from "../../api/client";
import { useActions, useApp } from "../../state/store";
import { ago } from "../../lib/format";
import { Markdown } from "../../lib/markdown";
import { THREAD_KIND_META, type ThreadView } from "./threads";
import { Menu } from "../../shell/Menu";

// One comment thread card. Kind is signalled by a chip and a 2px left-border
// glyph column, never colour alone. Moved and Orphaned states show their chips
// and the composer is disabled while orphaned.
export function ThreadCard({
  thread,
  active,
  onSelect,
}: {
  thread: ThreadView;
  active: boolean;
  onSelect: () => void;
}) {
  const state = useApp();
  const actions = useActions();
  const [reply, setReply] = useState("");
  const meta = THREAD_KIND_META[thread.kind];
  const isArchitect = state.role === "architect";
  const orphaned = thread.anchorState === "orphaned" || thread.status === "orphaned";
  const moved = thread.anchorState === "shifted";

  async function post() {
    if (!reply.trim()) return;
    try {
      await api.replyThread(thread.id, reply.trim());
      setReply("");
      await actions.refreshDoc();
      actions.toast("Reply posted.", "success");
    } catch {
      actions.toast("Could not post reply.", "danger");
    }
  }

  async function resolve() {
    try {
      await api.resolveThread(thread.id, "resolved from rail");
      await actions.refreshDoc();
      actions.live("Thread resolved.");
    } catch {
      actions.toast("Could not resolve.", "danger");
    }
  }

  async function reopen() {
    try {
      await api.reopenThread(thread.id);
      await actions.refreshDoc();
    } catch {
      actions.toast("Could not reopen.", "danger");
    }
  }

  return (
    <article
      className={`thread-card thread-${meta.tone}${active ? " is-active" : ""}${orphaned ? " is-orphaned" : ""}`}
      aria-label={`${meta.label} thread ${thread.id}`}
      onClick={onSelect}
    >
      <span className="thread-glyph-col" aria-hidden="true">
        {meta.glyph}
      </span>
      <div className="thread-inner">
        <header className="thread-head">
          <span className="thread-author">
            {thread.comments[0]?.role === "grogu" ? (
              <span className="system-tag">Grogu</span>
            ) : (
              thread.comments[0]?.author ?? "you"
            )}
          </span>
          <span className={`thread-kind-chip chip chip-${meta.tone}`}>{meta.label}</span>
          <span className="thread-time">
            {thread.comments[0] ? ago(new Date(thread.comments[0].at).getTime(), Date.now()) : ""}
          </span>
          <span className={`thread-status thread-status-${thread.status}`}>{thread.status}</span>
        </header>

        {moved ? (
          <div className="thread-chip-moved">
            <span className="chip chip-warn">Moved</span>
            {thread.lastExactRev ? (
              <button type="button" className="rel-link" onClick={() => actions.setMode("revision")}>
                View at {thread.lastExactRev}
              </button>
            ) : null}
          </div>
        ) : null}
        {orphaned ? (
          <div className="thread-chip-orphaned">
            <span className="chip chip-danger">Orphaned</span>
            <span className="thread-orphan-note">Anchor no longer resolves. Re-anchor or resolve.</span>
          </div>
        ) : null}
        {thread.totalMembers && thread.resolvedMembers != null && thread.resolvedMembers < thread.totalMembers ? (
          <div className="thread-partial">
            Anchor changed: {thread.resolvedMembers} of {thread.totalMembers} objects resolved
            <button type="button" className="rel-link" onClick={() => actions.setMode("canvas")}>
              Show which
            </button>
          </div>
        ) : null}

        {thread.quote ? <blockquote className="thread-quote">{thread.quote}</blockquote> : null}

        <div className="thread-comments">
          {thread.comments.map((c) => (
            <div key={c.id} className="thread-comment">
              <span className="thread-comment-author">
                {c.role === "grogu" ? <span className="system-tag">Grogu</span> : c.author}
              </span>
              <Markdown text={c.body} className="thread-comment-body md" />
            </div>
          ))}
        </div>

        {thread.promotedDirective ? (
          <div className="thread-promoted">
            <span className="chip chip-accent">↗ {thread.promotedDirective}</span>
          </div>
        ) : null}

        {thread.status !== "resolved" && !orphaned ? (
          <div className="thread-composer">
            <textarea
              className="thread-reply"
              rows={2}
              value={reply}
              placeholder="Reply…"
              aria-label="Reply to thread"
              onChange={(e) => setReply(e.target.value)}
              onClick={(e) => e.stopPropagation()}
            />
          </div>
        ) : null}

        <footer className="thread-actions" onClick={(e) => e.stopPropagation()}>
          {thread.status !== "resolved" && !orphaned ? (
            <button type="button" className="btn btn-text" onClick={() => void post()}>
              Reply
            </button>
          ) : null}
          {thread.status !== "resolved" ? (
            <button type="button" className="btn btn-text" onClick={() => void resolve()}>
              Resolve
            </button>
          ) : (
            <button type="button" className="btn btn-text" onClick={() => void reopen()}>
              Reopen
            </button>
          )}
          <button
            type="button"
            className="btn btn-text"
            onClick={() => actions.openOverlay({ kind: "askGrogu", threadId: thread.id })}
          >
            Ask Grogu to revise
          </button>
          <Menu
            label="⋯"
            chevron={false}
            align="end"
            items={[
              ...(isArchitect && thread.kind !== "directive"
                ? [
                    {
                      label: "Promote to directive",
                      onSelect: () => actions.openOverlay({ kind: "directivePromotion", threadId: thread.id }),
                    },
                  ]
                : []),
              { label: "Copy thread id", onSelect: () => void navigator.clipboard?.writeText(thread.id) },
            ]}
          />
        </footer>
      </div>
    </article>
  );
}
