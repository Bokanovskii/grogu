import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api } from "../api/client";
import type {
  AgentDrillResponse,
  ControlResponse,
  FeedbackRecord,
  FeedbackScope,
} from "../api/types";

// One server-side collector is sampled at most once per 2s across all clients
// (the server enforces that); the client polls every 2s while visible, backs
// off to 10s while hidden, and resyncs immediately on focus. Reading the board
// must not create Grogu activity — that is the server's concern; here we simply
// GET. A single unfiltered poll feeds the mode-bar chip, the status-bar chip
// and the Control room, so state/role/workstream filters are applied in the UI.

interface ControlContextValue {
  snapshot: ControlResponse | null;
  freshAsOf: number;
  loading: boolean;
  error: string | null;
  selectedAgent: string | null;
  drill: AgentDrillResponse | null;
  feedback: FeedbackRecord[];
  selectAgent: (key: string | null) => void;
  refresh: () => Promise<void>;
  sendFeedback: (scope: FeedbackScope, text: string, binding: boolean) => Promise<void>;
  withdrawFeedback: (id: string) => Promise<void>;
}

const Ctx = createContext<ControlContextValue | null>(null);

export function ControlProvider({
  plan,
  children,
}: {
  plan: string;
  children: React.ReactNode;
}) {
  const [snapshot, setSnapshot] = useState<ControlResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);
  const [drill, setDrill] = useState<AgentDrillResponse | null>(null);
  const [feedback, setFeedback] = useState<FeedbackRecord[]>([]);
  const selectedRef = useRef<string | null>(null);
  selectedRef.current = selectedAgent;

  const refresh = useCallback(async () => {
    try {
      const [snap, fb] = await Promise.all([api.control({ window: 60 }), api.listFeedback()]);
      setSnapshot(snap);
      setFeedback(fb.feedback);
      setError(null);
      const sel = selectedRef.current;
      if (sel) {
        try {
          const d = await api.controlAgent(sel);
          setDrill(d);
        } catch {
          /* keep last drill */
        }
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let timer = 0;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      await refresh();
      const interval = document.visibilityState === "hidden" ? 10000 : 2000;
      timer = window.setTimeout(tick, interval);
    };
    void tick();
    const onVis = () => {
      if (document.visibilityState === "visible") {
        window.clearTimeout(timer);
        void tick();
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [refresh]);

  const selectAgent = useCallback((key: string | null) => {
    setSelectedAgent(key);
    if (!key) {
      setDrill(null);
      return;
    }
    void api
      .controlAgent(key)
      .then(setDrill)
      .catch(() => setDrill(null));
  }, []);

  const sendFeedback = useCallback(
    async (scope: FeedbackScope, text: string, binding: boolean) => {
      if (scope.kind === "agent") {
        await api.feedback(scope.agent_key, text, binding, "agent");
      } else {
        await api.sendScopedFeedback(scope, text, binding);
      }
      await refresh();
    },
    [refresh],
  );

  const withdrawFeedback = useCallback(
    async (id: string) => {
      await api.withdrawFeedback(id);
      await refresh();
    },
    [refresh],
  );

  const value = useMemo<ControlContextValue>(
    () => ({
      snapshot,
      freshAsOf: snapshot?.fresh_as_of ?? 0,
      loading,
      error,
      selectedAgent,
      drill,
      feedback,
      selectAgent,
      refresh,
      sendFeedback,
      withdrawFeedback,
    }),
    [snapshot, loading, error, selectedAgent, drill, feedback, selectAgent, refresh, sendFeedback, withdrawFeedback],
  );

  // plan currently only scopes which agents the user usually cares about; the
  // board itself is workspace-wide. Kept in the signature for future scoping.
  void plan;

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useControl(): ControlContextValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useControl outside provider");
  return v;
}
