import { useActions, useApp } from "../state/store";

// Toast rail (bottom, above status bar) and the two live regions. Autosave,
// offline, save-failed, conflict states surface here. Each toast may carry one
// action. Live regions carry the polite and assertive announcements.
export function Toasts() {
  const { toasts } = useApp();
  const actions = useActions();
  return (
    <div className="toast-rail" aria-live="off">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.kind}`} role="status">
          <span className="toast-text">{t.text}</span>
          {t.action ? (
            <button
              type="button"
              className="toast-action"
              onClick={() => {
                if (t.action!.event === "flush-queue") void actions.flushQueue();
                actions.dismissToast(t.id);
              }}
            >
              {t.action.label}
            </button>
          ) : null}
          <button
            type="button"
            className="toast-dismiss"
            aria-label="Dismiss"
            onClick={() => actions.dismissToast(t.id)}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

export function LiveRegions() {
  const { live, liveAssertive } = useApp();
  return (
    <>
      <div className="visually-hidden" aria-live="polite" aria-atomic="true">
        {live}
      </div>
      <div className="visually-hidden" aria-live="assertive" aria-atomic="true" role="alert">
        {liveAssertive}
      </div>
    </>
  );
}
