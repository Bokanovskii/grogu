import { useControl } from "../state/control";
import { useActions, useApp } from "../state/store";
import { AgentsChip } from "./AgentsChip";

// Bottom status bar: plan · revision · autosave state · connection · agents chip.
export function StatusBar() {
  const state = useApp();
  const actions = useActions();
  const { feedback } = useControl();

  const bindingPending = feedback.filter(
    (f) => f.binding && (f.state === "sent" || f.state === "routed" || f.state === "delivered"),
  ).length;

  let saveLabel = "";
  if (state.save === "saving") saveLabel = "Saving…";
  else if (state.save === "rebasing") saveLabel = "Rebasing…";
  else if (state.save === "saved") saveLabel = `Saved · ${state.revision}`;
  else if (state.save === "failed") saveLabel = "Save failed · retry queued";
  else saveLabel = "autosaved";

  let connLabel = "Connected";
  let connTone = "ok";
  if (state.connection === "offline") {
    connLabel = `Offline · edits queued (${state.queue.length})`;
    connTone = "warn";
  } else if (state.connection === "reconnecting") {
    connLabel = state.reconnectIn != null ? `Reconnecting in ${state.reconnectIn}s` : "Reconnecting…";
    connTone = "warn";
  } else if (state.connection === "connecting") {
    connLabel = "Connecting…";
  }

  return (
    <footer className="statusbar" role="contentinfo">
      <div className="statusbar-left">
        <span className="status-plan">{state.plan || "— no plan selected —"}</span>
        <span className="status-sep">·</span>
        <span className="status-rev">{state.revision}</span>
        <span className="status-sep">·</span>
        <span className={`status-save status-save-${state.save}`}>{saveLabel}</span>
      </div>
      <div className="statusbar-right">
        {bindingPending > 0 ? (
          <span className="status-feedback">Feedback · {bindingPending} binding pending</span>
        ) : null}
        <AgentsChip onClick={() => actions.setMode("control")} />
        <span className={`status-conn status-conn-${connTone}`} aria-live="polite">
          {connLabel}
        </span>
      </div>
    </footer>
  );
}
