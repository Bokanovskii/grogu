import { useEffect } from "react";
import { PLAN_MODES, useActions, useApp, type Mode } from "../state/store";
import { STAGES, type Stage } from "../api/types";

// Global (shell-level) keyboard chords. Mode-specific chords (canvas tools,
// nudge, etc.) are handled inside their mode components. A "cmd" chord matches
// metaKey on macOS and ctrlKey elsewhere.
function isTyping(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  const tag = el.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    el.isContentEditable
  );
}

export function useGlobalKeys() {
  const state = useApp();
  const actions = useActions();

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const cmd = e.metaKey || e.ctrlKey;
      const typing = isTyping(e.target);

      // Escape handled by overlays/gestures themselves; only clear selection here.
      if (e.key === "Escape" && !typing) {
        if (!state.overlay && state.selection.length > 0) {
          actions.clearSelection();
        }
        return;
      }

      if (!cmd) return;

      const k = e.key.toLowerCase();

      // Stage switches: ⌘⇧D / I / T / E
      if (e.shiftKey && ["d", "i", "t", "e"].includes(k)) {
        const map: Record<string, Stage> = {
          d: "design",
          i: "implementation",
          t: "testing",
          e: "evaluation",
        };
        const stage = map[k]!;
        if (state.plan && STAGES.includes(stage)) {
          e.preventDefault();
          actions.setStage(stage);
          if (state.mode === "control") actions.setMode("document");
        }
        return;
      }

      // ⌘⇧M feedback composer, ⌘⇧L ledger, ⌘⇧F cross-plan search
      if (e.shiftKey && k === "m") {
        e.preventDefault();
        actions.openOverlay({ kind: "feedbackComposer" });
        return;
      }
      if (e.shiftKey && k === "l") {
        e.preventDefault();
        actions.openOverlay({ kind: "deliveryLedger" });
        return;
      }
      if (e.shiftKey && k === "f") {
        e.preventDefault();
        actions.openOverlay({ kind: "searchPalette" });
        return;
      }

      // Undo / redo
      if (k === "z") {
        e.preventDefault();
        if (e.shiftKey) void actions.redo();
        else void actions.undo();
        return;
      }

      // Mode digits: ⌘0 control, ⌘1-4 plan modes
      if (k === "0") {
        e.preventDefault();
        actions.setMode("control");
        return;
      }
      if (["1", "2", "3", "4"].includes(k) && state.plan) {
        e.preventDefault();
        const mode = PLAN_MODES[Number(k) - 1] as Mode;
        actions.setMode(mode);
        return;
      }

      // Palettes
      if (k === "k") {
        e.preventDefault();
        actions.openOverlay({ kind: "commandPalette" });
        return;
      }
      if (k === "p" && !e.shiftKey) {
        e.preventDefault();
        actions.openOverlay({ kind: "searchPalette" });
        return;
      }
      if (k === "/") {
        e.preventDefault();
        actions.openOverlay({ kind: "shortcuts" });
        return;
      }
      if (k === "e" && !e.shiftKey && state.plan) {
        e.preventDefault();
        actions.openOverlay({ kind: "compiledPreview" });
        return;
      }
      if (k === "s") {
        e.preventDefault();
        void actions.flushQueue();
        return;
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [state.overlay, state.selection.length, state.plan, state.mode, actions]);
}
