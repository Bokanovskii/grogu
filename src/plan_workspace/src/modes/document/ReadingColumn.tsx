import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { PlanNode } from "../../api/types";
import { useActions, useApp } from "../../state/store";
import { NODE_GLYPH, NODE_LABEL } from "../../lib/selection";
import { Markdown } from "../../lib/markdown";
import { activeDirectives, sectionsForStage } from "../../lib/sections";
import { deriveThreads, THREAD_KIND_META } from "../shared/threads";
import { NewThreadComposer } from "../shared/NewThreadComposer";

function NodeBlock({
  node,
  registerRef,
}: {
  node: PlanNode;
  registerRef: (id: string, el: HTMLElement | null) => void;
}) {
  const state = useApp();
  const actions = useActions();
  const [editingTitle, setEditingTitle] = useState(false);
  const [editingBody, setEditingBody] = useState(false);
  const [titleDraft, setTitleDraft] = useState(node.title);
  const [bodyDraft, setBodyDraft] = useState(node.body);
  const [composing, setComposing] = useState(false);
  const selected = state.selection.some((s) => s.id === node.id);

  useEffect(() => setTitleDraft(node.title), [node.title]);
  useEffect(() => setBodyDraft(node.body), [node.body]);

  const threads = deriveThreads(state.nodes).filter((t) => t.anchorNode === node.id);

  const commitTitle = () => {
    setEditingTitle(false);
    if (titleDraft !== node.title) {
      void actions.writeGesture(
        [{ op: "replace", path: `/nodes/${node.id}/title`, value: titleDraft }],
        `rename ${node.id}`,
        "workspace",
        `rename ${node.id}`,
      );
    }
  };
  const commitBody = () => {
    setEditingBody(false);
    if (bodyDraft !== node.body) {
      void actions.writeGesture(
        [{ op: "replace", path: `/nodes/${node.id}/body`, value: bodyDraft }],
        `edit body ${node.id}`,
        "workspace",
        `edit body ${node.id}`,
      );
    }
  };

  const startComment = () => {
    const sel = window.getSelection();
    const quote = sel && !sel.isCollapsed ? sel.toString().trim() : "";
    setComposing(true);
    // store the quote on the composer via state
    setComposeQuote(quote);
  };
  const [composeQuote, setComposeQuote] = useState("");

  return (
    <article
      id={`doc-node-${node.id}`}
      ref={(el) => registerRef(node.id, el)}
      className={`doc-node doc-node-${node.kind}${selected ? " is-selected" : ""}`}
      aria-label={`${NODE_LABEL[node.kind]} ${node.id}`}
      onClick={() => actions.select(node.id, "node")}
    >
      <div className="doc-node-head">
        <span className="doc-node-glyph" aria-hidden="true">
          {NODE_GLYPH[node.kind]}
        </span>
        <span className="doc-node-kind">{NODE_LABEL[node.kind]}</span>
        <span className="doc-node-id">{node.id}</span>
        {node.attrs?.["status"] ? <span className="doc-node-status chip chip-neutral">{String(node.attrs["status"])}</span> : null}
        <button
          type="button"
          className="doc-node-comment"
          onClick={(e) => {
            e.stopPropagation();
            startComment();
          }}
          title="Comment (C)"
        >
          Comment
        </button>
      </div>

      {editingTitle ? (
        <input
          className="doc-title-edit"
          value={titleDraft}
          autoFocus
          aria-label={`Edit title of ${node.id}`}
          onChange={(e) => setTitleDraft(e.target.value)}
          onBlur={commitTitle}
          onKeyDown={(e) => {
            if (e.key === "Enter") commitTitle();
            if (e.key === "Escape") {
              setTitleDraft(node.title);
              setEditingTitle(false);
            }
          }}
        />
      ) : (
        <h3
          className={`doc-title${threads.some((t) => t.anchorState === "resolved" && t.status === "open") ? " has-mark" : ""}`}
          onDoubleClick={() => setEditingTitle(true)}
          tabIndex={0}
        >
          {node.title || <span className="doc-untitled">Untitled</span>}
        </h3>
      )}

      {editingBody ? (
        <textarea
          className="doc-body-edit"
          value={bodyDraft}
          autoFocus
          rows={Math.max(3, bodyDraft.split("\n").length + 1)}
          aria-label={`Edit body of ${node.id}`}
          onChange={(e) => setBodyDraft(e.target.value)}
          onBlur={commitBody}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) commitBody();
            if (e.key === "Escape") {
              setBodyDraft(node.body);
              setEditingBody(false);
            }
          }}
        />
      ) : node.body ? (
        <div className="doc-body" onDoubleClick={() => setEditingBody(true)}>
          <Markdown text={node.body} />
        </div>
      ) : (
        <p className="doc-body-empty" onDoubleClick={() => setEditingBody(true)}>
          Double-click to add a body.
        </p>
      )}

      {threads.length ? (
        <div className="doc-marks" aria-label="Comment marks">
          {threads.map((t) => (
            <button
              key={t.id}
              type="button"
              className={`doc-mark doc-mark-${THREAD_KIND_META[t.kind].tone}`}
              title={`${THREAD_KIND_META[t.kind].label} thread ${t.id}`}
              onClick={(e) => {
                e.stopPropagation();
                actions.select(t.id, "mark");
                actions.patchPanels({ rightTab: "comments" });
              }}
            >
              {THREAD_KIND_META[t.kind].glyph}
            </button>
          ))}
        </div>
      ) : null}

      {composing ? (
        <NewThreadComposer node={node} quote={composeQuote} onClose={() => setComposing(false)} />
      ) : null}
    </article>
  );
}

export function ReadingColumn({
  onMarkOffsets,
}: {
  onMarkOffsets?: (offsets: Record<string, number>) => void;
}) {
  const state = useApp();
  const containerRef = useRef<HTMLDivElement>(null);
  const nodeEls = useRef<Record<string, HTMLElement | null>>({});
  const lastOffsets = useRef<string>("");
  const sections = sectionsForStage(state.nodes, state.stage);
  const leadDirectives = activeDirectives(state.nodes, state.role || "reviewer");

  const registerRef = (id: string, el: HTMLElement | null) => {
    nodeEls.current[id] = el;
  };

  // Report the vertical offset of each thread's anchor node so the rail can
  // align cards to their marks with an 8px minimum gap. Only push up when the
  // measured offsets actually change, otherwise the parent's setState would
  // re-render us and loop.
  useLayoutEffect(() => {
    if (!onMarkOffsets || !containerRef.current) return;
    const base = containerRef.current.getBoundingClientRect().top;
    const threads = deriveThreads(state.nodes);
    const offsets: Record<string, number> = {};
    for (const t of threads) {
      const el = t.anchorNode ? nodeEls.current[t.anchorNode] : null;
      if (el) offsets[t.id] = Math.round(el.getBoundingClientRect().top - base);
    }
    const serialized = JSON.stringify(offsets);
    if (serialized !== lastOffsets.current) {
      lastOffsets.current = serialized;
      onMarkOffsets(offsets);
    }
  });

  return (
    <div className="reading-column" ref={containerRef}>
      <div className="reading-inner">
        {leadDirectives.length ? (
          <section className="doc-section doc-section-directives" aria-label="Directives">
            <h2 className="doc-section-title">Directives</h2>
            {leadDirectives.map((n) => (
              <NodeBlock key={n.id} node={n} registerRef={registerRef} />
            ))}
          </section>
        ) : null}

        {sections
          .filter((s) => s.kind !== "directive")
          .map((section) => (
            <section key={section.kind} className="doc-section" aria-label={section.label}>
              <h2 className="doc-section-title">{section.label}</h2>
              {section.nodes.map((n) => (
                <NodeBlock key={n.id} node={n} registerRef={registerRef} />
              ))}
            </section>
          ))}

        {sections.length === 0 && leadDirectives.length === 0 ? (
          <p className="doc-empty">This stage has no content yet.</p>
        ) : null}
      </div>
    </div>
  );
}
