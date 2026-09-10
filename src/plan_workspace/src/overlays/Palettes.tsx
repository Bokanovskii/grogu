import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { RevisionEnvelope } from "../api/types";
import { Modal, ModalBody } from "../shell/Modal";
import { PLAN_MODES, useActions, useApp, type Mode } from "../state/store";
import { STAGES, stageLabel } from "../api/types";
import { deriveThreads, THREAD_KIND_META } from "../modes/shared/threads";
import { NODE_GLYPH, NODE_LABEL } from "../lib/selection";

interface Command {
  id: string;
  name: string;
  chord?: string;
  run: () => void;
  enabled: boolean;
  reason?: string;
}

// Subsequence fuzzy match with a light score (earlier + contiguous is better).
function fuzzy(query: string, text: string): number | null {
  if (!query) return 0;
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  let qi = 0;
  let score = 0;
  let last = -1;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      score += last === ti - 1 ? 2 : 1;
      last = ti;
      qi++;
    }
  }
  return qi === q.length ? score : null;
}

export function CommandPalette() {
  const state = useApp();
  const actions = useActions();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);

  const commands = useMemo<Command[]>(() => {
    const planScoped = !!state.plan;
    const list: Command[] = [
      { id: "control", name: "Go to Control room", chord: "⌘0", run: () => actions.setMode("control"), enabled: true },
      ...PLAN_MODES.map((m, i) => ({
        id: `mode-${m}`,
        name: `Switch to ${m[0]!.toUpperCase() + m.slice(1)}`,
        chord: `⌘${i + 1}`,
        run: () => actions.setMode(m as Mode),
        enabled: planScoped,
        reason: planScoped ? undefined : "Open a plan first",
      })),
      ...STAGES.map((s) => ({
        id: `stage-${s}`,
        name: `Go to ${stageLabel(s)} stage`,
        run: () => actions.setStage(s),
        enabled: planScoped,
        reason: planScoped ? undefined : "Open a plan first",
      })),
      { id: "undo", name: "Undo", chord: "⌘Z", run: () => void actions.undo(), enabled: actions.canUndo(), reason: "Nothing to undo" },
      { id: "redo", name: "Redo", chord: "⌘⇧Z", run: () => void actions.redo(), enabled: actions.canRedo(), reason: "Nothing to redo" },
      { id: "compiled", name: "Compiled Markdown preview", chord: "⌘E", run: () => actions.openOverlay({ kind: "compiledPreview" }), enabled: planScoped },
      { id: "ledger", name: "Delivery ledger", chord: "⌘⇧L", run: () => actions.openOverlay({ kind: "deliveryLedger" }), enabled: true },
      { id: "feedback", name: "Send feedback", chord: "⌘⇧M", run: () => actions.openOverlay({ kind: "feedbackComposer" }), enabled: true },
      { id: "shortcuts", name: "Keyboard shortcuts", chord: "⌘/", run: () => actions.openOverlay({ kind: "shortcuts" }), enabled: true },
      { id: "theme-dark", name: "Dark theme", run: () => actions.setTheme("dark"), enabled: true },
      { id: "theme-light", name: "Light theme", run: () => actions.setTheme("light"), enabled: true },
      { id: "theme-system", name: "System theme", run: () => actions.setTheme("system"), enabled: true },
      { id: "contrast-high", name: "High contrast", run: () => actions.setContrast("high"), enabled: true },
      { id: "contrast-normal", name: "Normal contrast", run: () => actions.setContrast("normal"), enabled: true },
      { id: "flush", name: "Flush save queue", chord: "⌘S", run: () => void actions.flushQueue(), enabled: state.queue.length > 0, reason: "Queue is empty" },
    ];
    return list;
  }, [state.plan, state.queue.length, actions]);

  const results = useMemo(() => {
    const scored = commands
      .map((c) => ({ c, score: fuzzy(query, c.name) }))
      .filter((x) => x.score !== null)
      .sort((a, b) => (b.score! - a.score!));
    return scored.map((x) => x.c);
  }, [commands, query]);

  useEffect(() => setActive(0), [query]);

  return (
    <Modal title="Command palette" onClose={actions.closeOverlay} width={560} className="palette-modal">
      <ModalBody>
        <input
          className="palette-input"
          autoFocus
          placeholder="Type a command…"
          value={query}
          aria-label="Command"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") setActive((a) => Math.min(results.length - 1, a + 1));
            else if (e.key === "ArrowUp") setActive((a) => Math.max(0, a - 1));
            else if (e.key === "Enter") {
              const c = results[active];
              if (c?.enabled) {
                c.run();
                actions.closeOverlay();
              }
            }
          }}
        />
        <ul className="palette-list" role="listbox" aria-label="Commands">
          {results.map((c, i) => (
            <li
              key={c.id}
              role="option"
              aria-selected={i === active}
              className={`palette-row${i === active ? " is-active" : ""}${c.enabled ? "" : " is-disabled"}`}
              title={c.enabled ? undefined : c.reason}
              onMouseEnter={() => setActive(i)}
              onClick={() => {
                if (c.enabled) {
                  c.run();
                  actions.closeOverlay();
                }
              }}
            >
              <span className="palette-name">{c.name}</span>
              {c.chord ? <kbd className="palette-chord">{c.chord}</kbd> : null}
            </li>
          ))}
          {results.length === 0 ? <li className="palette-empty">Nothing matches “{query}”.</li> : null}
        </ul>
      </ModalBody>
    </Modal>
  );
}

export function SearchPalette() {
  const state = useApp();
  const actions = useActions();
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<"nodes" | "threads" | "revisions">("nodes");
  const [revisions, setRevisions] = useState<RevisionEnvelope[]>([]);

  useEffect(() => {
    api.revisions().then((r) => setRevisions(r.revisions)).catch(() => setRevisions([]));
  }, []);

  const nodes = useMemo(
    () =>
      Object.values(state.nodes)
        .filter((n) => n.kind !== "thread")
        .filter((n) => !query || (n.title + " " + n.id).toLowerCase().includes(query.toLowerCase())),
    [state.nodes, query],
  );
  const threads = useMemo(
    () => deriveThreads(state.nodes).filter((t) => !query || (t.quote + " " + t.id).toLowerCase().includes(query.toLowerCase())),
    [state.nodes, query],
  );
  const revs = useMemo(
    () => revisions.filter((r) => !query || (r.intent + " " + r.revision).toLowerCase().includes(query.toLowerCase())),
    [revisions, query],
  );

  return (
    <Modal title="Search" onClose={actions.closeOverlay} width={560} className="palette-modal">
      <ModalBody>
        <input
          className="palette-input"
          autoFocus
          placeholder="Search nodes, threads and revisions…"
          value={query}
          aria-label="Search"
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="palette-tabs" role="tablist" aria-label="Search scope">
          {(["nodes", "threads", "revisions"] as const).map((t) => (
            <button key={t} type="button" role="tab" aria-selected={tab === t} className={`palette-tab${tab === t ? " is-active" : ""}`} onClick={() => setTab(t)}>
              {t[0]!.toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>
        <ul className="palette-list" role="listbox">
          {tab === "nodes"
            ? nodes.map((n) => (
                <li
                  key={n.id}
                  role="option"
                  aria-selected={false}
                  className="palette-row"
                  onClick={() => {
                    actions.select(n.id, "node");
                    if (state.mode === "control") actions.setMode("document");
                    actions.closeOverlay();
                  }}
                >
                  <span className="palette-icon" aria-hidden="true">{NODE_GLYPH[n.kind]}</span>
                  <span className="palette-name">{n.title || n.id}</span>
                  <span className="palette-crumb">{n.stage || "plan"} · {NODE_LABEL[n.kind]}</span>
                </li>
              ))
            : tab === "threads"
              ? threads.map((t) => (
                  <li
                    key={t.id}
                    role="option"
                    aria-selected={false}
                    className="palette-row"
                    onClick={() => {
                      if (t.anchorNode) actions.select(t.anchorNode, "node");
                      actions.setMode("document");
                      actions.closeOverlay();
                    }}
                  >
                    <span className="palette-icon" aria-hidden="true">{THREAD_KIND_META[t.kind].glyph}</span>
                    <span className="palette-name">{t.quote || t.id}</span>
                    <span className="palette-crumb">{t.status}</span>
                  </li>
                ))
              : revs.map((r) => (
                  <li
                    key={r.revision}
                    role="option"
                    aria-selected={false}
                    className="palette-row"
                    onClick={() => {
                      actions.setMode("revision");
                      actions.closeOverlay();
                    }}
                  >
                    <span className="palette-name">{r.revision} · {r.intent}</span>
                    <span className="palette-crumb">{r.origin}</span>
                  </li>
                ))}
          {(tab === "nodes" && nodes.length === 0) ||
          (tab === "threads" && threads.length === 0) ||
          (tab === "revisions" && revs.length === 0) ? (
            <li className="palette-empty">Nothing matches “{query}”.</li>
          ) : null}
        </ul>
      </ModalBody>
    </Modal>
  );
}

const SHORTCUTS: [string, string][] = [
  ["⌘K", "Command palette"],
  ["⌘P", "Search"],
  ["⌘0", "Control room"],
  ["⌘1–⌘4", "Document, Canvas, Dependencies, Revision"],
  ["⌘⇧D/I/T/E", "Switch stage"],
  ["⌘Z / ⌘⇧Z", "Undo / redo"],
  ["⌘E", "Compiled Markdown preview"],
  ["⌘⇧M", "Send feedback"],
  ["⌘⇧L", "Delivery ledger"],
  ["C", "New comment on selection"],
  ["V H R O A P F L", "Canvas tools"],
  ["← ↑ → ↓", "Nudge 1px (⇧ 8px, ⌘ 32px)"],
  ["[ ] / ⌘[ ⌘]", "Z-order"],
  ["⌘G / ⌘⇧G", "Group / ungroup"],
  ["⌘D", "Duplicate"],
  [". / ,", "Toggle grid / snapping"],
  ["Esc", "Close overlay / cancel gesture / clear selection"],
];

export function ShortcutsOverlay() {
  const actions = useActions();
  const ref = useRef<HTMLDivElement>(null);
  void ref;
  return (
    <Modal title="Keyboard" onClose={actions.closeOverlay} width={520}>
      <ModalBody>
        <dl className="kbd-table">
          {SHORTCUTS.map(([k, d]) => (
            <div key={k} className="kbd-row">
              <dt>
                <kbd>{k}</kbd>
              </dt>
              <dd>{d}</dd>
            </div>
          ))}
        </dl>
      </ModalBody>
    </Modal>
  );
}
