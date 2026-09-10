import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
} from "react";
import { api, ApiFailure, connectEvents, StaleError } from "../api/client";
import type {
  ApiError,
  Counters,
  DocResponse,
  PatchOp,
  PlanEdge,
  PlanNode,
  Proposal,
  Role,
  Stage,
  StagesInfo,
  Thread,
} from "../api/types";
import { STAGES } from "../api/types";
import { applyOps, type Graph, invertOps, opsApply } from "../lib/patch";
import { loadPref, loadQueue, type QueuedIntent, savePref, saveQueue } from "./queue";

export type Mode = "control" | "document" | "canvas" | "dependencies" | "revision";
export const PLAN_MODES: Mode[] = ["document", "canvas", "dependencies", "revision"];

export type RightTab = "comments" | "inspector" | "agents";

export type Theme = "light" | "dark" | "system";
export type Contrast = "normal" | "high";

export type Connection = "connecting" | "connected" | "offline" | "reconnecting";
export type SaveState = "idle" | "saving" | "saved" | "rebasing" | "failed";

export interface Toast {
  id: string;
  text: string;
  kind: "info" | "warn" | "danger" | "success";
  action?: { label: string; event: string; arg?: string };
  ttl?: number;
}

export interface ConflictCard {
  id: string;
  title: string;
  ops: PatchOp[];
  base: string;
  serverRevision: string;
}

export type Overlay =
  | { kind: "commandPalette" }
  | { kind: "searchPalette" }
  | { kind: "compiledPreview" }
  | { kind: "shortcuts" }
  | { kind: "deliveryLedger" }
  | { kind: "askGrogu"; threadId: string }
  | { kind: "directivePromotion"; threadId: string }
  | { kind: "proposalPreview"; proposalId: string }
  | { kind: "impactPreview" }
  | { kind: "migration" }
  | { kind: "newComment"; nodeId: string; quote?: string }
  | { kind: "feedbackComposer"; agentKey?: string; nudge?: boolean }
  | { kind: "restoreQueue" };

export interface SelItem {
  type: "node" | "edge" | "region" | "handle" | "annotation" | "mark";
  id: string;
}

export interface Panels {
  leftCollapsed: boolean;
  rightCollapsed: boolean;
  leftWidth: number;
  rightWidth: number;
  rightTab: RightTab;
  density: "grid" | "list";
}

export interface EquivalenceError {
  field: string;
  revision: string;
  base: string;
}

export interface AppState {
  boot: "skeleton" | "loading" | "ready" | "error";
  bootError: ApiError | null;
  plan: string;
  title: string;
  role: Role | "";
  revision: string;
  base: string;
  nodes: Record<string, PlanNode>;
  edges: Record<string, PlanEdge>;
  counters: Counters;
  stages: StagesInfo;
  sealedEdgeCount: number;
  modes: string[];

  mode: Mode;
  stage: Stage;
  selection: SelItem[];
  panels: Panels;
  theme: Theme;
  contrast: Contrast;

  connection: Connection;
  save: SaveState;
  queue: QueuedIntent[];
  restoreQueue: QueuedIntent[];
  pendingIds: string[];
  reconnectIn: number | null;

  toasts: Toast[];
  live: string;
  liveAssertive: string;
  overlay: Overlay | null;
  conflicts: ConflictCard[];
  equivalence: EquivalenceError | null;

  threads: Thread[];
  proposals: Proposal[];

  // A modeless notice shown once (e.g. simplified rendering at low zoom).
  notices: string[];
}

type Action =
  | { t: "boot/loading" }
  | { t: "boot/ready"; doc: DocResponse; modes: string[] }
  | { t: "boot/error"; error: ApiError }
  | { t: "doc/replace"; doc: DocResponse }
  | { t: "graph/set"; nodes: Record<string, PlanNode>; edges: Record<string, PlanEdge>; counters: Counters; revision: string; base: string }
  | { t: "revision/advance"; revision: string }
  | { t: "mode/set"; mode: Mode }
  | { t: "stage/set"; stage: Stage }
  | { t: "selection/set"; selection: SelItem[] }
  | { t: "panels/patch"; patch: Partial<Panels> }
  | { t: "theme/set"; theme: Theme }
  | { t: "contrast/set"; contrast: Contrast }
  | { t: "connection/set"; connection: Connection; reconnectIn?: number | null }
  | { t: "save/set"; save: SaveState }
  | { t: "queue/set"; queue: QueuedIntent[]; pendingIds: string[] }
  | { t: "restore/set"; queue: QueuedIntent[] }
  | { t: "toast/push"; toast: Toast }
  | { t: "toast/dismiss"; id: string }
  | { t: "live/set"; text: string; assertive?: boolean }
  | { t: "overlay/set"; overlay: Overlay | null }
  | { t: "conflict/push"; card: ConflictCard }
  | { t: "conflict/dismiss"; id: string }
  | { t: "equivalence/set"; error: EquivalenceError | null }
  | { t: "threads/set"; threads: Thread[] }
  | { t: "proposals/set"; proposals: Proposal[] }
  | { t: "notice/push"; text: string };

function docToGraph(doc: DocResponse): {
  nodes: Record<string, PlanNode>;
  edges: Record<string, PlanEdge>;
} {
  const nodes: Record<string, PlanNode> = {};
  const edges: Record<string, PlanEdge> = {};
  for (const n of doc.nodes) nodes[n.id] = n;
  for (const e of doc.edges) edges[e.id] = e;
  return { nodes, edges };
}

function reducer(state: AppState, a: Action): AppState {
  switch (a.t) {
    case "boot/loading":
      return { ...state, boot: "loading" };
    case "boot/ready": {
      const { nodes, edges } = docToGraph(a.doc);
      return {
        ...state,
        boot: "ready",
        plan: a.doc.plan,
        title: a.doc.title,
        role: a.doc.role,
        revision: a.doc.revision,
        base: a.doc.revision,
        nodes,
        edges,
        counters: a.doc.counters,
        stages: a.doc.stages,
        sealedEdgeCount: a.doc.sealed_edge_count,
        modes: a.modes,
      };
    }
    case "boot/error":
      return { ...state, boot: "error", bootError: a.error };
    case "doc/replace": {
      const { nodes, edges } = docToGraph(a.doc);
      return {
        ...state,
        nodes,
        edges,
        counters: a.doc.counters,
        stages: a.doc.stages,
        sealedEdgeCount: a.doc.sealed_edge_count,
        revision: a.doc.revision,
        base: a.doc.revision,
        role: a.doc.role,
        title: a.doc.title,
      };
    }
    case "graph/set":
      return {
        ...state,
        nodes: a.nodes,
        edges: a.edges,
        counters: a.counters,
        revision: a.revision,
        base: a.base,
      };
    case "revision/advance":
      return { ...state, revision: a.revision };
    case "mode/set":
      return { ...state, mode: a.mode };
    case "stage/set":
      return { ...state, stage: a.stage };
    case "selection/set":
      return { ...state, selection: a.selection };
    case "panels/patch":
      return { ...state, panels: { ...state.panels, ...a.patch } };
    case "theme/set":
      return { ...state, theme: a.theme };
    case "contrast/set":
      return { ...state, contrast: a.contrast };
    case "connection/set":
      return {
        ...state,
        connection: a.connection,
        reconnectIn: a.reconnectIn === undefined ? state.reconnectIn : a.reconnectIn,
      };
    case "save/set":
      return { ...state, save: a.save };
    case "queue/set":
      return { ...state, queue: a.queue, pendingIds: a.pendingIds };
    case "restore/set":
      return { ...state, restoreQueue: a.queue };
    case "toast/push":
      return { ...state, toasts: [...state.toasts, a.toast] };
    case "toast/dismiss":
      return { ...state, toasts: state.toasts.filter((t) => t.id !== a.id) };
    case "live/set":
      return a.assertive
        ? { ...state, liveAssertive: a.text }
        : { ...state, live: a.text };
    case "overlay/set":
      return { ...state, overlay: a.overlay };
    case "conflict/push":
      return { ...state, conflicts: [...state.conflicts, a.card] };
    case "conflict/dismiss":
      return { ...state, conflicts: state.conflicts.filter((c) => c.id !== a.id) };
    case "equivalence/set":
      return { ...state, equivalence: a.error };
    case "threads/set":
      return { ...state, threads: a.threads };
    case "proposals/set":
      return { ...state, proposals: a.proposals };
    case "notice/push":
      return state.notices.includes(a.text)
        ? state
        : { ...state, notices: [...state.notices, a.text] };
    default:
      return state;
  }
}

let counter = 0;
function uid(prefix: string): string {
  counter += 1;
  return `${prefix}-${Date.now().toString(36)}-${counter}`;
}

function initialState(): AppState {
  return {
    boot: "skeleton",
    bootError: null,
    plan: "",
    title: "",
    role: "",
    revision: "",
    base: "",
    nodes: {},
    edges: {},
    counters: {},
    stages: { readable: [], sealed: [], written: {} },
    sealedEdgeCount: 0,
    modes: [],
    mode: "control",
    stage: "design",
    selection: [],
    panels: {
      leftCollapsed: false,
      rightCollapsed: false,
      leftWidth: 280,
      rightWidth: 360,
      rightTab: "comments",
      density: "grid",
    },
    theme: "system",
    contrast: "normal",
    connection: "connecting",
    save: "idle",
    queue: [],
    restoreQueue: [],
    pendingIds: [],
    reconnectIn: null,
    toasts: [],
    live: "",
    liveAssertive: "",
    overlay: null,
    conflicts: [],
    equivalence: null,
    threads: [],
    proposals: [],
    notices: [],
  };
}

export interface UndoEntry {
  ops: PatchOp[];
  inverse: PatchOp[];
  label: string;
}

export interface Actions {
  boot: () => Promise<void>;
  setMode: (m: Mode) => void;
  setStage: (s: Stage) => void;
  select: (id: string, type: SelItem["type"], mode?: "replace" | "extend" | "toggle") => void;
  setSelection: (sel: SelItem[]) => void;
  clearSelection: () => void;
  patchPanels: (p: Partial<Panels>) => void;
  setTheme: (t: Theme) => void;
  setContrast: (c: Contrast) => void;
  openOverlay: (o: Overlay) => void;
  closeOverlay: () => void;
  toast: (text: string, kind?: Toast["kind"], action?: Toast["action"]) => void;
  dismissToast: (id: string) => void;
  live: (text: string, assertive?: boolean) => void;
  notice: (text: string) => void;
  /** The save engine: one gesture -> one revision. */
  writeGesture: (ops: PatchOp[], intent: string, origin?: string, label?: string) => Promise<boolean>;
  undo: () => Promise<void>;
  redo: () => Promise<void>;
  canUndo: () => boolean;
  canRedo: () => boolean;
  flushQueue: () => Promise<void>;
  applyRestoredQueue: () => void;
  discardRestoredQueue: () => void;
  refreshDoc: () => Promise<void>;
  reloadThreads: () => Promise<void>;
  reloadProposals: () => Promise<void>;
  dismissConflict: (id: string) => void;
  clearEquivalence: () => void;
  applyServerOps: (ops: PatchOp[], revision: string) => void;
}

const StateCtx = createContext<AppState | null>(null);
const ActionsCtx = createContext<Actions | null>(null);

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [state, dispatch] = useReducer(reducer, undefined, initialState);
  const stateRef = useRef(state);
  stateRef.current = state;

  // Undo/redo stacks live in refs; they never need to re-render on their own.
  const undoStack = useRef<UndoEntry[]>([]);
  const redoStack = useRef<UndoEntry[]>([]);

  const graphOf = (s: AppState): Graph => ({
    nodes: s.nodes as unknown as Record<string, never>,
    edges: s.edges as unknown as Record<string, never>,
    counters: s.counters as unknown as Record<string, never>,
  });

  const commitGraph = useCallback((g: Graph, revision: string, base: string) => {
    dispatch({
      t: "graph/set",
      nodes: g.nodes as unknown as Record<string, PlanNode>,
      edges: g.edges as unknown as Record<string, PlanEdge>,
      counters: g.counters as unknown as Counters,
      revision,
      base,
    });
  }, []);

  const toast = useCallback<Actions["toast"]>((text, kind = "info", action) => {
    const id = uid("toast");
    const toastObj: Toast = { id, text, kind, ...(action ? { action } : {}) };
    dispatch({ t: "toast/push", toast: toastObj });
    const ttl = kind === "info" || kind === "success" ? 2600 : 6000;
    window.setTimeout(() => dispatch({ t: "toast/dismiss", id }), ttl);
  }, []);

  const live = useCallback<Actions["live"]>((text, assertive) => {
    dispatch({ t: "live/set", text, ...(assertive ? { assertive } : {}) });
  }, []);

  const notice = useCallback<Actions["notice"]>((text) => {
    dispatch({ t: "notice/push", text });
  }, []);

  const persistQueue = useCallback((plan: string, queue: QueuedIntent[]) => {
    saveQueue(plan, queue);
    const pendingIds = new Set<string>();
    for (const item of queue) {
      for (const op of item.ops) {
        const m = ("path" in op ? op.path : "").match(/^\/(?:nodes|edges)\/([^/]+)/);
        if (m) pendingIds.add(m[1]!);
      }
    }
    dispatch({ t: "queue/set", queue, pendingIds: [...pendingIds] });
  }, []);

  // The core save engine.
  const writeGesture = useCallback<Actions["writeGesture"]>(
    async (ops, intent, origin = "workspace", label) => {
      if (ops.length === 0) return true;
      const s = stateRef.current;
      const pre = graphOf(s);
      let optimistic: Graph;
      try {
        optimistic = applyOps(pre, ops);
      } catch {
        toast("That change could not be applied locally.", "danger");
        return false;
      }
      const inverse = invertOps(pre, ops);
      // optimistic local apply
      commitGraph(optimistic, s.revision, s.base);
      dispatch({ t: "save/set", save: "saving" });

      try {
        const res = await api.patch({ base: s.base, ops, intent, origin });
        commitGraph(optimistic, res.revision, res.revision);
        dispatch({ t: "save/set", save: "saved" });
        dispatch({ t: "connection/set", connection: "connected", reconnectIn: null });
        if (origin !== "undo" && origin !== "redo") {
          undoStack.current.push({ ops, inverse, label: label ?? intent });
          redoStack.current = [];
          toast("Autosaved", "success");
        }
        live(`Saved ${res.revision}`);
        return true;
      } catch (err) {
        if (err instanceof StaleError) {
          return await rebase(ops, intent, origin, label, inverse, pre, err);
        }
        if (err instanceof ApiFailure) {
          if (err.status === 0 || err.status >= 500) {
            // offline / server error: queue the intent, keep the optimistic edit
            const item: QueuedIntent = {
              id: uid("q"),
              ops,
              base: s.base,
              intent,
              origin,
              at: Date.now(),
            };
            const q = [...stateRef.current.queue, item];
            persistQueue(s.plan, q);
            dispatch({ t: "connection/set", connection: "offline" });
            dispatch({ t: "save/set", save: "failed" });
            if (err.status === 0) {
              toast(
                "Network unavailable. Your edits are saved locally and will upload when the server is reachable.",
                "warn",
              );
              live("Offline. Edits kept locally.", true);
            } else {
              toast("Could not save. Your edits are kept.", "warn", {
                label: "Retry",
                event: "flush-queue",
              });
            }
            return false;
          }
          if (err.status === 403) {
            commitGraph(pre, s.revision, s.base); // roll back
            dispatch({ t: "save/set", save: "idle" });
            toast(err.body?.message ?? "That stage is out of your role's reach.", "danger");
            return false;
          }
          if (err.status === 422) {
            commitGraph(pre, s.revision, s.base);
            dispatch({ t: "save/set", save: "idle" });
            if (err.body?.error === "nonequivalent") {
              dispatch({
                t: "equivalence/set",
                error: { field: err.body.field ?? "?", revision: s.revision, base: s.base },
              });
              live("Compiler bug: not saved", true);
            } else {
              toast(err.body?.why ?? err.body?.message ?? "That change is not valid.", "danger");
            }
            return false;
          }
        }
        commitGraph(pre, s.revision, s.base);
        dispatch({ t: "save/set", save: "failed" });
        toast("Could not save. Your edits are kept.", "warn", { label: "Retry", event: "flush-queue" });
        return false;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [commitGraph, toast, live, persistQueue],
  );

  // Rebase against server ops since our base, then retry once.
  const rebase = useCallback(
    async (
      ops: PatchOp[],
      intent: string,
      origin: string,
      label: string | undefined,
      inverse: PatchOp[],
      pre: Graph,
      stale: StaleError,
    ): Promise<boolean> => {
      dispatch({ t: "save/set", save: "rebasing" });
      // Apply the server's ops to our pre-state to get the new base state.
      let rebased: Graph;
      try {
        rebased = applyOps(pre, stale.opsSince);
      } catch {
        // We can't compute the new base locally; fetch fresh.
        await refreshDoc();
        rebased = graphOf(stateRef.current);
      }
      if (opsApply(rebased, ops)) {
        const applied = applyOps(rebased, ops);
        commitGraph(applied, stale.revision, stale.revision);
        try {
          const res = await api.patch({ base: stale.revision, ops, intent, origin });
          commitGraph(applied, res.revision, res.revision);
          dispatch({ t: "save/set", save: "saved" });
          if (origin !== "undo" && origin !== "redo") {
            undoStack.current.push({ ops, inverse, label: label ?? intent });
            redoStack.current = [];
          }
          toast(`Rebased on ${res.revision}`, "info", { label: "View changes", event: "noop" });
          live(`Rebased on ${res.revision}`);
          return true;
        } catch {
          // fall through to conflict
        }
      }
      // Could not rebase: keep the local edit and raise a conflict card.
      commitGraph(applyOps(rebased, []), stale.revision, stale.revision);
      const firstId =
        (ops[0] && "path" in ops[0] ? ops[0].path : "").match(/\/(?:nodes|edges)\/([^/]+)/)?.[1] ??
        "an item";
      const node = stateRef.current.nodes[firstId];
      const card: ConflictCard = {
        id: uid("conflict"),
        title: node?.title ?? firstId,
        ops,
        base: stale.revision,
        serverRevision: stale.revision,
      };
      dispatch({ t: "conflict/push", card });
      dispatch({ t: "save/set", save: "idle" });
      live(`Someone else changed ${card.title}`, true);
      return false;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [commitGraph, toast, live],
  );

  const refreshDoc = useCallback<Actions["refreshDoc"]>(async () => {
    const s = stateRef.current;
    try {
      const doc = await api.doc(s.stage, "all");
      dispatch({ t: "doc/replace", doc });
    } catch {
      /* leave state as is */
    }
  }, []);

  const flushQueue = useCallback<Actions["flushQueue"]>(async () => {
    if (stateRef.current.queue.length === 0) return;
    dispatch({ t: "connection/set", connection: "reconnecting" });
    live("Reconnecting…");
    let rebases = 0;
    // Drain one revision at a time, in order. The item's optimistic edit is
    // already in local state; a clean send just advances the base.
    while (stateRef.current.queue.length > 0) {
      const s = stateRef.current;
      const item = s.queue[0]!;
      try {
        const res = await api.patch({
          base: item.base,
          ops: item.ops,
          intent: item.intent,
          origin: item.origin,
        });
        commitGraph(graphOf(stateRef.current), res.revision, res.revision);
        persistQueue(s.plan, stateRef.current.queue.filter((q) => q.id !== item.id));
        dispatch({ t: "save/set", save: "saved" });
        live(`Synced ${res.revision}`);
      } catch (err) {
        if (err instanceof StaleError) {
          // The server moved under us. Rebase the WHOLE remaining queue onto its
          // latest document, replaying local intent, so nothing is silently
          // discarded. A replay that no longer applies raises a conflict card
          // and is dropped from the queue (its intent is preserved on the card).
          if (rebases++ > 50) {
            dispatch({ t: "connection/set", connection: "offline" });
            return;
          }
          let fresh: Graph;
          let freshRev: string;
          try {
            const doc = await api.doc(s.stage, "all");
            const { nodes, edges } = docToGraph(doc);
            fresh = {
              nodes: nodes as unknown as Record<string, never>,
              edges: edges as unknown as Record<string, never>,
              counters: doc.counters as unknown as Record<string, never>,
            };
            freshRev = doc.revision;
          } catch {
            dispatch({ t: "connection/set", connection: "offline" });
            return;
          }
          let g = fresh;
          const survivors: QueuedIntent[] = [];
          for (const q of stateRef.current.queue) {
            if (opsApply(g, q.ops)) {
              g = applyOps(g, q.ops);
              survivors.push({ ...q, base: freshRev });
            } else {
              const firstId =
                (q.ops[0] && "path" in q.ops[0] ? q.ops[0].path : "").match(/\/(?:nodes|edges)\/([^/]+)/)?.[1] ??
                "an item";
              dispatch({
                t: "conflict/push",
                card: {
                  id: uid("conflict"),
                  title: stateRef.current.nodes[firstId]?.title ?? firstId,
                  ops: q.ops,
                  base: freshRev,
                  serverRevision: freshRev,
                },
              });
              live(`Someone else changed ${firstId}`, true);
            }
          }
          commitGraph(g, freshRev, freshRev);
          persistQueue(s.plan, survivors);
          continue;
        }
        dispatch({ t: "connection/set", connection: "offline" });
        return;
      }
    }
    dispatch({ t: "connection/set", connection: "connected", reconnectIn: null });
  }, [commitGraph, live, persistQueue]);

  const applyRestoredQueue = useCallback<Actions["applyRestoredQueue"]>(() => {
    const s = stateRef.current;
    let graph = graphOf(s);
    const accepted: QueuedIntent[] = [];
    for (const item of s.restoreQueue) {
      if (!opsApply(graph, item.ops)) {
        const firstId =
          (item.ops[0] && "path" in item.ops[0] ? item.ops[0].path : "")
            .match(/\/(?:nodes|edges)\/([^/]+)/)?.[1] ?? "an item";
        dispatch({
          t: "conflict/push",
          card: {
            id: uid("conflict"),
            title: s.nodes[firstId]?.title ?? firstId,
            ops: item.ops,
            base: item.base,
            serverRevision: s.revision,
          },
        });
        continue;
      }
      graph = applyOps(graph, item.ops);
      accepted.push(item);
    }
    commitGraph(graph, s.revision, s.base);
    dispatch({ t: "restore/set", queue: [] });
    persistQueue(s.plan, accepted);
  }, [commitGraph, persistQueue]);

  const discardRestoredQueue = useCallback<Actions["discardRestoredQueue"]>(() => {
    const s = stateRef.current;
    saveQueue(s.plan, []);
    dispatch({ t: "restore/set", queue: [] });
  }, []);

  const undo = useCallback<Actions["undo"]>(async () => {
    const entry = undoStack.current.pop();
    if (!entry) return;
    const ok = await writeGesture(entry.inverse, `Undo: ${entry.label}`, "undo", entry.label);
    if (ok) {
      // writeGesture leaves the stacks alone for undo/redo origins; record the
      // redo here so earlier redo entries survive a second consecutive undo.
      redoStack.current.push(entry);
      live(`Undid: ${entry.label}`);
      toast(`Undid: ${entry.label}`, "info");
    } else {
      undoStack.current.push(entry);
    }
  }, [writeGesture, live, toast]);

  const redo = useCallback<Actions["redo"]>(async () => {
    const entry = redoStack.current.pop();
    if (!entry) return;
    const ok = await writeGesture(entry.ops, `Redo: ${entry.label}`, "redo", entry.label);
    if (ok) {
      undoStack.current.push(entry);
      live(`Redid: ${entry.label}`);
    } else {
      redoStack.current.push(entry);
    }
  }, [writeGesture, live]);

  const applyServerOps = useCallback<Actions["applyServerOps"]>(
    (ops, revision) => {
      const s = stateRef.current;
      try {
        const next = applyOps(graphOf(s), ops);
        commitGraph(next, revision, revision);
      } catch {
        // Our local view diverged; pull a fresh copy.
        void refreshDoc();
      }
    },
    [commitGraph, refreshDoc],
  );

  const reloadThreads = useCallback<Actions["reloadThreads"]>(async () => {
    // Threads are nodes of kind "thread"; the server also exposes them richly
    // via the doc. We derive from the current graph so a single fetch suffices.
    const s = stateRef.current;
    const threads = Object.values(s.nodes)
      .filter((n) => n.kind === "thread")
      .map((n) => (n.ext?.["dev.grogu.thread"] as unknown as Thread) ?? null)
      .filter((x): x is Thread => !!x);
    dispatch({ t: "threads/set", threads });
  }, []);

  const reloadProposals = useCallback<Actions["reloadProposals"]>(async () => {
    try {
      const res = await api.proposals();
      dispatch({ t: "proposals/set", proposals: res.proposals });
    } catch {
      /* ignore */
    }
  }, []);

  const boot = useCallback<Actions["boot"]>(async () => {
    // A spent/invalid launch token leaves a readable notice cookie; show the
    // session-spent panel without touching the API.
    if (/(?:^|;\s*)grogu_notice=token_spent/.test(document.cookie)) {
      document.cookie = "grogu_notice=; Path=/; Max-Age=0";
      dispatch({ t: "boot/error", error: { error: "token_spent", message: "This session link has already been used." } });
      return;
    }
    dispatch({ t: "boot/loading" });
    try {
      const health = await api.health();
      const doc = await api.doc(undefined, "all");
      dispatch({ t: "boot/ready", doc, modes: health.modes });
      // Restore persisted UI prefs for this plan.
      const plan = doc.plan;
      const panels = loadPref<Partial<Panels>>("panels", plan, {});
      dispatch({ t: "panels/patch", patch: panels });
      const theme = loadPref<Theme>("theme", plan, "system");
      dispatch({ t: "theme/set", theme });
      const contrast = loadPref<Contrast>("contrast", plan, "normal");
      dispatch({ t: "contrast/set", contrast });
      // Default mode: control room when no plan, else document.
      dispatch({ t: "mode/set", mode: plan ? "document" : "control" });
      if (doc.stages.readable.length) {
        dispatch({ t: "stage/set", stage: doc.stages.readable[0]! });
      }
      // Crash recovery: restore queued edits.
      const q = loadQueue(plan);
      if (q.length) {
        dispatch({ t: "restore/set", queue: q });
        dispatch({ t: "overlay/set", overlay: { kind: "restoreQueue" } });
      }
      void reloadProposals();
    } catch (err) {
      const body = err instanceof ApiFailure ? err.body : null;
      dispatch({ t: "boot/error", error: body ?? { error: "boot", message: String(err) } });
    }
  }, [persistQueue, reloadProposals]);

  // SSE wiring: a second window or a CLI write updates the open editor.
  useEffect(() => {
    if (state.boot !== "ready") return;
    const dispose = connectEvents(
      (type) => {
        dispatch({ t: "connection/set", connection: "connected", reconnectIn: null });
        if (type === "revision") {
          // A write happened elsewhere; refresh if it is ahead of our base.
          void refreshDoc();
        } else if (type === "proposal") {
          void reloadProposals();
        } else if (type === "shutdown") {
          toast("The server is shutting down. Your work is saved.", "warn");
        }
      },
      () => {
        if (stateRef.current.queue.length > 0) {
          dispatch({ t: "connection/set", connection: "offline" });
        } else {
          dispatch({ t: "connection/set", connection: "reconnecting" });
        }
      },
    );
    return dispose;
  }, [state.boot, refreshDoc, reloadProposals, toast]);

  // When connectivity returns, drain the queue.
  useEffect(() => {
    if (state.connection === "connected" && state.queue.length > 0) {
      void flushQueue();
    }
  }, [state.connection, state.queue.length, flushQueue]);

  // Persist prefs.
  useEffect(() => {
    if (state.boot === "ready") savePref("panels", state.plan, state.panels);
  }, [state.panels, state.plan, state.boot]);
  useEffect(() => {
    if (state.boot === "ready") savePref("theme", state.plan, state.theme);
  }, [state.theme, state.plan, state.boot]);
  useEffect(() => {
    if (state.boot === "ready") savePref("contrast", state.plan, state.contrast);
  }, [state.contrast, state.plan, state.boot]);

  // Apply theme + contrast to the document root.
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", state.theme);
    document.documentElement.setAttribute("data-contrast", state.contrast);
  }, [state.theme, state.contrast]);

  const actions = useMemo<Actions>(
    () => ({
      boot,
      setMode: (m) => dispatch({ t: "mode/set", mode: m }),
      setStage: (s) => dispatch({ t: "stage/set", stage: s }),
      select: (id, type, mode = "replace") => {
        const cur = stateRef.current.selection;
        let next: SelItem[];
        if (mode === "replace") next = [{ id, type }];
        else if (mode === "extend") next = cur.some((x) => x.id === id) ? cur : [...cur, { id, type }];
        else next = cur.some((x) => x.id === id) ? cur.filter((x) => x.id !== id) : [...cur, { id, type }];
        dispatch({ t: "selection/set", selection: next });
      },
      setSelection: (sel) => dispatch({ t: "selection/set", selection: sel }),
      clearSelection: () => dispatch({ t: "selection/set", selection: [] }),
      patchPanels: (p) => dispatch({ t: "panels/patch", patch: p }),
      setTheme: (t) => dispatch({ t: "theme/set", theme: t }),
      setContrast: (c) => dispatch({ t: "contrast/set", contrast: c }),
      openOverlay: (o) => dispatch({ t: "overlay/set", overlay: o }),
      closeOverlay: () => dispatch({ t: "overlay/set", overlay: null }),
      toast,
      dismissToast: (id) => dispatch({ t: "toast/dismiss", id }),
      live,
      notice,
      writeGesture,
      undo,
      redo,
      canUndo: () => undoStack.current.length > 0,
      canRedo: () => redoStack.current.length > 0,
      flushQueue,
      applyRestoredQueue,
      discardRestoredQueue,
      refreshDoc,
      reloadThreads,
      reloadProposals,
      dismissConflict: (id) => dispatch({ t: "conflict/dismiss", id }),
      clearEquivalence: () => dispatch({ t: "equivalence/set", error: null }),
      applyServerOps,
    }),
    [
      boot,
      toast,
      live,
      notice,
      writeGesture,
      undo,
      redo,
      flushQueue,
      applyRestoredQueue,
      discardRestoredQueue,
      refreshDoc,
      reloadThreads,
      reloadProposals,
      applyServerOps,
    ],
  );

  return (
    <StateCtx.Provider value={state}>
      <ActionsCtx.Provider value={actions}>{children}</ActionsCtx.Provider>
    </StateCtx.Provider>
  );
}

export function useApp(): AppState {
  const s = useContext(StateCtx);
  if (!s) throw new Error("useApp outside provider");
  return s;
}

export function useActions(): Actions {
  const a = useContext(ActionsCtx);
  if (!a) throw new Error("useActions outside provider");
  return a;
}

export function readableStage(stages: StagesInfo, stage: Stage): "readable" | "sealed" | "unwritten" {
  if (stages.sealed.includes(stage)) return "sealed";
  if (stages.readable.includes(stage)) return "readable";
  return "unwritten";
}

export { STAGES };
export type { Stage };
