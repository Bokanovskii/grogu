import { useEffect } from "react";
import { useActions, useApp } from "./state/store";
import { AppShell } from "./shell/AppShell";
import { BootSkeleton } from "./shell/BootSkeleton";
import { BootError } from "./shell/StatePanels";

export function App() {
  const state = useApp();
  const actions = useActions();

  useEffect(() => {
    void actions.boot();
    // boot once on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (state.boot === "skeleton" || state.boot === "loading") {
    return <BootSkeleton />;
  }
  if (state.boot === "error") {
    return <BootError error={state.bootError} />;
  }
  return <AppShell />;
}
