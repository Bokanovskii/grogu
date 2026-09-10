import { api } from "../api/client";
import { PLAN_MODES, useActions, useApp, type Mode } from "../state/store";
import { STAGES, stageLabel, type Stage } from "../api/types";
import { readableStage } from "../state/store";
import { AgentsChip } from "./AgentsChip";
import { Menu } from "./Menu";

const MODE_LABEL: Record<Mode, string> = {
  control: "Control room",
  document: "Document",
  canvas: "Canvas",
  dependencies: "Dependencies",
  revision: "Revision",
};

const STAGE_CHORD: Record<Stage, string> = {
  design: "⌘⇧D",
  implementation: "⌘⇧I",
  testing: "⌘⇧T",
  evaluation: "⌘⇧E",
};

// Interactive chrome row (visually the 40px second row). DOM order matches the
// design's focus order: Control room tab → stage tabs → mode tabs → search →
// agents chip → user menu. The presence slot is a non-focusable stub.
export function ModeBar() {
  const state = useApp();
  const actions = useActions();
  const planScoped = !!state.plan;

  return (
    <div className="modebar" role="toolbar" aria-label="Modes and stages">
      <button
        type="button"
        className={`mode-tab mode-tab-control${state.mode === "control" ? " is-active" : ""}`}
        aria-current={state.mode === "control" ? "page" : undefined}
        onClick={() => actions.setMode("control")}
        title="Control room (⌘0)"
      >
        <span aria-hidden="true">⌾</span> Control room
      </button>

      <span className="modebar-divider" aria-hidden="true" />

      <div className="tablist" role="tablist" aria-label="Stages">
        {STAGES.map((s) => {
          const status = readableStage(state.stages, s);
          const disabled = !planScoped;
          return (
            <button
              key={s}
              type="button"
              role="tab"
              aria-selected={planScoped && state.stage === s && state.mode !== "control"}
              aria-disabled={disabled}
              disabled={disabled}
              className={`stage-tab stage-tab-${status}${state.stage === s ? " is-active" : ""}`}
              onClick={() => {
                if (disabled) return;
                actions.setStage(s);
                if (state.mode === "control") actions.setMode("document");
              }}
              title={`${stageLabel(s)} · ${STAGE_CHORD[s]}${status !== "readable" ? ` (${status})` : ""}`}
            >
              {stageLabel(s)}
              {status === "sealed" ? <span className="stage-lock" aria-hidden="true"> 🔒</span> : null}
            </button>
          );
        })}
      </div>

      <span className="modebar-divider" aria-hidden="true" />

      <div className="tablist" role="tablist" aria-label="Modes">
        {PLAN_MODES.map((m, i) => (
          <button
            key={m}
            type="button"
            role="tab"
            aria-selected={state.mode === m}
            aria-disabled={!planScoped}
            disabled={!planScoped}
            className={`mode-tab${state.mode === m ? " is-active" : ""}`}
            onClick={() => actions.setMode(m)}
            title={`${MODE_LABEL[m]} · ⌘${i + 1}`}
          >
            {MODE_LABEL[m]}
          </button>
        ))}
      </div>

      <div className="modebar-spacer" />

      <button
        type="button"
        className="search-trigger"
        onClick={() => actions.openOverlay({ kind: "searchPalette" })}
        title="Search (⌘P)"
      >
        <span aria-hidden="true">⌕</span>
        <span className="search-trigger-text">Search nodes, threads, revisions…</span>
        <kbd className="search-kbd">⌘P</kbd>
      </button>

      <AgentsChip onClick={() => actions.setMode("control")} />

      <div className="presence-slot" aria-hidden="true" title="Only your session is here">
        <span className="presence-pill">{(state.role || "you").slice(0, 1).toUpperCase()}</span>
      </div>

      <Menu
        label="Menu"
        glyph="≡"
        align="end"
        items={[
          { label: "System theme", onSelect: () => actions.setTheme("system") },
          { label: "Light theme", onSelect: () => actions.setTheme("light") },
          { label: "Dark theme", onSelect: () => actions.setTheme("dark") },
          { label: "Normal contrast", onSelect: () => actions.setContrast("normal") },
          { label: "High contrast", onSelect: () => actions.setContrast("high") },
          { label: "Command palette", hint: "⌘K", onSelect: () => actions.openOverlay({ kind: "commandPalette" }) },
          { label: "Keyboard shortcuts", hint: "⌘/", onSelect: () => actions.openOverlay({ kind: "shortcuts" }) },
          { label: "Delivery ledger", hint: "⌘⇧L", onSelect: () => actions.openOverlay({ kind: "deliveryLedger" }) },
          { label: "Compiled Markdown preview", hint: "⌘E", onSelect: () => actions.openOverlay({ kind: "compiledPreview" }) },
          {
            label: "End session",
            danger: true,
            onSelect: () => {
              void api.shutdown();
              actions.toast("Session ended. You can close this tab.", "warn");
            },
          },
        ]}
      />
    </div>
  );
}
