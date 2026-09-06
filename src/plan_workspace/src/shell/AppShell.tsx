import { ControlProvider } from "../state/control";
import { useActions, useApp } from "../state/store";
import { useGlobalKeys } from "./useGlobalKeys";
import { TopBar } from "./TopBar";
import { ModeBar } from "./ModeBar";
import { StatusBar } from "./StatusBar";
import { LiveRegions, Toasts } from "./Toasts";
import { ControlRoom } from "../modes/controlroom/ControlRoom";
import { DocumentMode } from "../modes/document/DocumentMode";
import { CanvasMode } from "../modes/canvas/CanvasMode";
import { DependenciesMode } from "../modes/dependencies/DependenciesMode";
import { RevisionMode } from "../modes/revision/RevisionMode";
import { Overlays } from "../overlays/Overlays";
import { ConflictCards } from "./ConflictCards";
import { EquivalenceBanner } from "./EquivalenceBanner";
import { StatePanel } from "./StatePanels";
import { Button } from "./ui";

function CurrentMode() {
  const { mode, plan } = useApp();
  const actions = useActions();
  if (mode === "control") return <ControlRoom />;
  if (!plan) {
    return (
      <div className="mode-layout">
        <main className="workspace-body" id="workspace-body" tabIndex={-1}>
          <StatePanel title="No plan open." tone="neutral">
            <p>
              Run <code className="kbd-inline">grogu plan doc open &lt;id&gt;</code> in your
              terminal, or head to Control room to watch running agents.
            </p>
            <Button variant="primary" onClick={() => actions.setMode("control")}>
              Open Control room
            </Button>
          </StatePanel>
        </main>
      </div>
    );
  }
  switch (mode) {
    case "document":
      return <DocumentMode />;
    case "canvas":
      return <CanvasMode />;
    case "dependencies":
      return <DependenciesMode />;
    case "revision":
      return <RevisionMode />;
    default:
      return <DocumentMode />;
  }
}

export function AppShell() {
  const state = useApp();
  useGlobalKeys();

  return (
    <ControlProvider plan={state.plan}>
      <div className="shell" data-mode={state.mode}>
        <header className="shell-header" role="banner">
          <a className="skip-link" href="#workspace-body">
            Skip to workspace
          </a>
          <a className="skip-link" href="#right-panel">
            Skip to comments
          </a>
          <a className="skip-link" href="#control-agents">
            Skip to agents
          </a>
          <TopBar />
          <ModeBar />
        </header>

        {state.equivalence ? <EquivalenceBanner /> : null}

        <div className="shell-body">
          <CurrentMode />
        </div>

        <StatusBar />
        <ConflictCards />
        <Toasts />
        <LiveRegions />
        <Overlays />
      </div>
    </ControlProvider>
  );
}
