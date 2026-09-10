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
  ControlScope,
  ControlResponse,
  FeedbackRecord,
  FeedbackResponse,
  FeedbackScope,
} from "../api/types";

// One server-side collector is sampled at most once per 2s across all clients
// (the server enforces that); the client polls every 5s while visible, pauses
// while hidden, and resyncs immediately on focus. Reading the board
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
  drillLoading: boolean;
  drillError: string | null;
  feedback: FeedbackRecord[];
  feedbackError: string | null;
  scope: ControlScope;
  setScope: (scope: ControlScope) => void;
  selectAgent: (key: string | null) => void;
  refresh: () => Promise<void>;
  sendFeedback: (
    scope: FeedbackScope,
    text: string,
    binding: boolean,
  ) => Promise<FeedbackResponse>;
  withdrawFeedback: (id: string, plan?: string) => Promise<void>;
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
  const [drillLoading, setDrillLoading] = useState(false);
  const [drillError, setDrillError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<FeedbackRecord[]>([]);
  const [feedbackError, setFeedbackError] = useState<string | null>(null);
  const [scope, setScopeState] = useState<ControlScope>(() =>
    localStorage.getItem("grogu.control.scope") === "current_plan"
      ? "current_plan"
      : "repository_program",
  );
  const selectedRef = useRef<string | null>(null);
  selectedRef.current = selectedAgent;

  const setScope = useCallback((next: ControlScope) => {
    localStorage.setItem("grogu.control.scope", next);
    setScopeState(next);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const snap = await api.control({
        scope,
        plan: scope === "current_plan" ? plan : undefined,
        window: 1440,
      });
      setSnapshot(snap);
      setError(null);
      const planIds = [...new Set([plan, ...Object.keys(snap.plans)])].filter(Boolean);
      try {
        const ledgers = await Promise.all(planIds.map((planId) => api.listFeedback(planId)));
        const records = ledgers.flatMap((ledger) => ledger.feedback);
        const unique = new Map<string, FeedbackRecord>();
        for (const record of records) {
          const key = record.receipt_key ?? `${record.plan ?? plan}:${record.id}`;
          unique.set(key, record);
        }
        setFeedback([...unique.values()]);
        setFeedbackError(null);
      } catch (feedbackFailure) {
        setFeedbackError(
          feedbackFailure instanceof Error
            ? feedbackFailure.message
            : "The delivery ledger is unavailable.",
        );
      }
      const sel = selectedRef.current;
      if (sel) {
        try {
          const d = await api.controlAgent(sel);
          setDrill(d);
          setDrillError(null);
        } catch {
          setDrillError("The selected agent's timeline could not be refreshed.");
        }
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [plan, scope]);

  useEffect(() => {
    let timer = 0;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      await refresh();
      if (document.visibilityState === "visible") {
        timer = window.setTimeout(tick, 5000);
      }
    };
    if (document.visibilityState === "visible") void tick();
    const onVis = () => {
      if (document.visibilityState === "visible") {
        window.clearTimeout(timer);
        void tick();
      } else {
        window.clearTimeout(timer);
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
      setDrillLoading(false);
      setDrillError(null);
      return;
    }
    setDrill(null);
    setDrillLoading(true);
    setDrillError(null);
    void api
      .controlAgent(key)
      .then((value) => {
        setDrill(value);
        setDrillError(null);
      })
      .catch((error) => {
        setDrill(null);
        setDrillError(
          error instanceof Error ? error.message : "The audit timeline is unavailable.",
        );
      })
      .finally(() => setDrillLoading(false));
  }, []);

  const sendFeedback = useCallback(
    async (scope: FeedbackScope, text: string, binding: boolean) => {
      let response: FeedbackResponse;
      if (scope.kind === "agent") {
        const targetPlan = snapshot?.agents.find(
          (agent) => agent.agent_key === scope.agent_key,
        )?.plan;
        response = await api.feedback(
          scope.agent_key,
          text,
          binding,
          "agent",
          targetPlan,
        );
      } else {
        response = await api.sendScopedFeedback(scope, text, binding);
      }
      setFeedback((current) => {
        const key =
          response.record.receipt_key ??
          `${response.record.plan ?? plan}:${response.record.id}`;
        return [
          response.record,
          ...current.filter(
            (record) =>
              (record.receipt_key ?? `${record.plan ?? plan}:${record.id}`) !== key,
          ),
        ];
      });
      await refresh();
      return response;
    },
    [plan, refresh, snapshot],
  );

  const withdrawFeedback = useCallback(
    async (id: string, targetPlan?: string) => {
      const record = feedback.find((item) => item.id === id && (!targetPlan || item.plan === targetPlan));
      await api.withdrawFeedback(id, targetPlan ?? record?.plan ?? plan);
      await refresh();
    },
    [feedback, plan, refresh],
  );

  const value = useMemo<ControlContextValue>(
    () => ({
      snapshot,
      freshAsOf: snapshot?.sampled_at ?? snapshot?.fresh_as_of ?? 0,
      loading,
      error,
      selectedAgent,
      drill,
      drillLoading,
      drillError,
      feedback,
      feedbackError,
      scope,
      setScope,
      selectAgent,
      refresh,
      sendFeedback,
      withdrawFeedback,
    }),
    [
      snapshot,
      loading,
      error,
      selectedAgent,
      drill,
      drillLoading,
      drillError,
      feedback,
      feedbackError,
      scope,
      setScope,
      selectAgent,
      refresh,
      sendFeedback,
      withdrawFeedback,
    ],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useControl(): ControlContextValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useControl outside provider");
  return v;
}
