import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { AuditEvent, JsonValue, PlanNode } from "../../api/types";
import { useActions, useApp } from "../../state/store";
import { NODE_GLYPH, NODE_LABEL, EDGE_GLYPH } from "../../lib/selection";
import { ri } from "../../lib/geometry";
import { ago } from "../../lib/format";
import { Button } from "../../shell/ui";
import { HUMAN_EDGE_LABEL, humanTitle } from "../../lib/humanize";

function CommitInput({
  value,
  onCommit,
  label,
  multiline,
  numeric,
}: {
  value: string;
  onCommit: (v: string) => void;
  label: string;
  multiline?: boolean;
  numeric?: boolean;
}) {
  const [local, setLocal] = useState(value);
  useEffect(() => setLocal(value), [value]);
  const commit = () => {
    if (local !== value) onCommit(local);
  };
  if (multiline) {
    return (
      <textarea
        className="inspector-input"
        aria-label={label}
        value={local}
        rows={4}
        onChange={(e) => setLocal(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            commit();
          }
        }}
      />
    );
  }
  return (
    <input
      className="inspector-input"
      aria-label={label}
      type={numeric ? "number" : "text"}
      value={local}
      onChange={(e) => setLocal(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          commit();
        }
      }}
    />
  );
}

function Section({
  title,
  children,
  defaultOpen = true,
}: {
  title: string;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="inspector-section">
      <button type="button" className="inspector-section-head" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className="inspector-caret" aria-hidden="true">{open ? "▾" : "▸"}</span>
        {title}
      </button>
      {open ? <div className="inspector-section-body">{children}</div> : null}
    </section>
  );
}

export function Inspector() {
  const state = useApp();
  const actions = useActions();
  const sel = state.selection[0];
  const node = sel && sel.type !== "edge" ? state.nodes[sel.id] : undefined;
  const edge = sel && sel.type === "edge" ? state.edges[sel.id] : undefined;

  const [activity, setActivity] = useState<AuditEvent[]>([]);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    setConfirmDelete(false);
    if (!node) {
      setActivity([]);
      return;
    }
    let cancelled = false;
    api
      .activityForObject(node.id, 1440)
      .then((r) => {
        if (!cancelled) setActivity(r.events.slice(0, 5));
      })
      .catch(() => {
        if (!cancelled) setActivity([]);
      });
    return () => {
      cancelled = true;
    };
  }, [node?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!node && !edge) {
    return <p className="inspector-empty">Select an item to see its properties.</p>;
  }

  if (edge) {
    const from = state.nodes[edge.from];
    const to = state.nodes[edge.to];
    return (
      <div className="inspector">
        <Section title="Technical identity" defaultOpen={false}>
          <dl className="inspector-dl">
            <dt>id</dt>
            <dd>
              <button type="button" className="mono-copy" onClick={() => navigator.clipboard?.writeText(edge.id)}>
                {edge.id}
              </button>
            </dd>
            <dt>kind</dt>
            <dd>
              <span aria-hidden="true">{EDGE_GLYPH[edge.kind]} </span>
              {edge.kind}
            </dd>
          </dl>
        </Section>
        <Section title="Endpoints">
          <p className="inspector-edge">
            {humanTitle(from?.title ?? edge.from)} → {humanTitle(to?.title ?? edge.to)}
          </p>
        </Section>
        <div className="inspector-delete">
          {!confirmDelete ? (
            <Button variant="text" onClick={() => setConfirmDelete(true)}>
              Remove edge
            </Button>
          ) : (
            <div className="confirm-row">
              <span>Remove this edge?</span>
              <Button variant="text" onClick={() => setConfirmDelete(false)}>
                Keep
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  void actions.writeGesture(
                    [{ op: "remove", path: `/edges/${edge.id}` }],
                    `unlink ${edge.id}`,
                    "workspace",
                    `unlink ${edge.id}`,
                  );
                  actions.clearSelection();
                }}
              >
                Remove
              </Button>
            </div>
          )}
        </div>
      </div>
    );
  }

  const n = node!;
  const edgesFrom = Object.values(state.edges).filter((e) => e.from === n.id);
  const edgesTo = Object.values(state.edges).filter((e) => e.to === n.id);
  const attrKeys = Object.keys(n.attrs ?? {});
  const threadsOnNode = Object.values(state.nodes).filter(
    (t) => t.kind === "thread" && t.attrs?.["anchor_node"] === n.id,
  );

  const setField = (field: keyof PlanNode, value: JsonValue, label: string) => {
    void actions.writeGesture(
      [{ op: "replace", path: `/nodes/${n.id}/${field}`, value }],
      label,
      "workspace",
      label,
    );
  };

  const setGeom = (axis: "x" | "y" | "w" | "h" | "z", value: number) => {
    const geom = { ...(n.geometry ?? { x: 0, y: 0, w: 240, h: 120, z: 0 }), [axis]: ri(value) };
    void actions.writeGesture(
      [{ op: n.geometry ? "replace" : "add", path: `/nodes/${n.id}/geometry`, value: geom }],
      `move ${n.id}`,
      "workspace",
      `move ${n.id}`,
    );
  };

  return (
    <div className="inspector">
      <Section title="Technical identity" defaultOpen={false}>
        <dl className="inspector-dl">
          <dt>id</dt>
          <dd>
            <button type="button" className="mono-copy" title="Copy id" onClick={() => navigator.clipboard?.writeText(n.id)}>
              {n.id}
            </button>
          </dd>
          <dt>kind</dt>
          <dd>
            <span aria-hidden="true">{NODE_GLYPH[n.kind]} </span>
            {NODE_LABEL[n.kind]}
          </dd>
          <dt>stage</dt>
          <dd>{n.stage || "plan-level"}</dd>
        </dl>
      </Section>

      <Section title="Title and body">
        <label className="inspector-label">Title</label>
        <CommitInput value={n.title} label="Title" onCommit={(v) => setField("title", v, `rename ${n.id}`)} />
        <label className="inspector-label">Body</label>
        <CommitInput value={n.body} label="Body" multiline onCommit={(v) => setField("body", v, `edit body ${n.id}`)} />
      </Section>

      <Section title="Attributes">
        {attrKeys.length === 0 ? (
          <p className="inspector-muted">No attributes.</p>
        ) : (
          <dl className="inspector-attrs">
            {attrKeys.map((k) => (
              <div key={k} className="inspector-attr">
                <dt>
                  {k}
                  {!["binding", "audience", "status", "shape", "origin", "source", "anchor_node", "thread_kind", "anchor_state", "round"].includes(k) ? (
                    <span className="attr-custom">custom</span>
                  ) : null}
                </dt>
                <dd>{JSON.stringify(n.attrs[k])}</dd>
              </div>
            ))}
          </dl>
        )}
      </Section>

      <Section title="Relationships">
        {edgesFrom.length === 0 && edgesTo.length === 0 ? (
          <p className="inspector-muted">No relationships.</p>
        ) : (
          <ul className="inspector-rels">
            {edgesFrom.map((e) => (
              <li key={e.id}>
                <button type="button" className="rel-link" onClick={() => actions.select(e.to, "node")}>
                  {HUMAN_EDGE_LABEL[e.kind] ?? e.kind} → {humanTitle(state.nodes[e.to]?.title ?? e.to)}
                </button>
              </li>
            ))}
            {edgesTo.map((e) => (
              <li key={e.id}>
                <button type="button" className="rel-link" onClick={() => actions.select(e.from, "node")}>
                  {humanTitle(state.nodes[e.from]?.title ?? e.from)} → {HUMAN_EDGE_LABEL[e.kind] ?? e.kind}
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>

      {n.geometry ? (
        <Section title="Geometry">
          <div className="inspector-geom">
            {(["x", "y", "w", "h", "z"] as const).map((axis) => (
              <label key={axis} className="geom-field">
                <span>{axis.toUpperCase()}</span>
                <CommitInput
                  value={String(n.geometry![axis])}
                  numeric
                  label={`Geometry ${axis}`}
                  onCommit={(v) => setGeom(axis, Number(v))}
                />
              </label>
            ))}
          </div>
        </Section>
      ) : null}

      <Section title="Provenance" defaultOpen={false}>
        <dl className="inspector-dl">
          <dt>created</dt>
          <dd>
            <button type="button" className="rel-link" onClick={() => actions.setMode("revision")}>
              {n.created_rev}
            </button>
          </dd>
          <dt>updated</dt>
          <dd>
            <button type="button" className="rel-link" onClick={() => actions.setMode("revision")}>
              {n.updated_rev}
            </button>
          </dd>
          <dt>source</dt>
          <dd>{String(n.attrs?.["origin"] ?? n.attrs?.["source"] ?? "user")}</dd>
        </dl>
      </Section>

      <Section title="Comments">
        <p className="inspector-muted">
          {threadsOnNode.length} thread{threadsOnNode.length === 1 ? "" : "s"} on this item.
        </p>
      </Section>

      <Section title="Agent activity" defaultOpen={false}>
        {activity.length === 0 ? (
          <p className="inspector-muted">No agent activity on this item in the last 24 hours.</p>
        ) : (
          <ul className="inspector-activity">
            {activity.map((ev) => (
              <li key={ev.id}>
                <span className="activity-summary">{ev.summary}</span>
                <span className="activity-age">{ago(ev.at, Date.now())}</span>
                <button
                  type="button"
                  className="rel-link"
                  onClick={() => {
                    actions.setMode("control");
                  }}
                >
                  jump to timeline
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <div className="inspector-delete">
        {!confirmDelete ? (
          <Button variant="text" onClick={() => setConfirmDelete(true)}>
            Delete node…
          </Button>
        ) : (
          <div className="confirm-row">
            <span>Delete {n.id}? This writes a remove op; earlier revisions keep it.</span>
            <Button variant="text" onClick={() => setConfirmDelete(false)}>
              Keep
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                void actions.writeGesture(
                  [{ op: "remove", path: `/nodes/${n.id}` }],
                  `remove ${n.id}`,
                  "workspace",
                  `remove ${n.id}`,
                );
                actions.clearSelection();
              }}
            >
              Delete
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
