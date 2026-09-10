import { useActions, useApp } from "../state/store";

// The semantic-equivalence banner. Amber, not red — a compiler bug, not a user
// mistake. The primary action copies a diagnostic (field, revision, base, plan)
// to the clipboard as JSON for a bug report.
export function EquivalenceBanner() {
  const state = useApp();
  const actions = useActions();
  const eq = state.equivalence;
  if (!eq) return null;

  const diagnostic = {
    field: eq.field,
    revision: eq.revision,
    base: eq.base,
    plan: state.plan,
  };

  return (
    <div className="equivalence-banner" role="alert">
      <span className="equivalence-text">
        The compiled Markdown does not round-trip. Your change was not saved. Copy
        diagnostic for a bug report.
      </span>
      <div className="equivalence-actions">
        <button
          type="button"
          className="btn btn-warn"
          onClick={() => {
            void navigator.clipboard?.writeText(JSON.stringify(diagnostic, null, 2));
            actions.toast("Diagnostic copied.", "info");
          }}
        >
          Copy diagnostic
        </button>
        <button
          type="button"
          className="btn btn-text"
          aria-label="Dismiss"
          onClick={() => actions.clearEquivalence()}
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}
