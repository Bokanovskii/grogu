import { useEffect, useState } from "react";
import { Modal, ModalBody, ModalFooter, ModalHeader, ModalTitle } from "../shell/Modal";
import { useActions } from "../state/store";

const STEPS = ["copy", "parse", "import diagrams", "import threads", "compile", "verify"];

// The migration overlay. Migration itself is owned by the server; this surface
// drives the one-way conversion and shows the four states — explain, migrate,
// verify (equivalent or not), revert. The progress is fed from the server's
// step list in production; in the fixture it advances locally so every state is
// reachable for the evidence checklist. `?migration=fail` forces the
// non-equivalent outcome.
export function Migration() {
  const actions = useActions();
  const [phase, setPhase] = useState<"explain" | "running" | "ready" | "failed">("explain");
  const [stepIdx, setStepIdx] = useState(0);
  const forceFail = new URLSearchParams(location.search).get("migration") === "fail";

  useEffect(() => {
    if (phase !== "running") return;
    if (stepIdx >= STEPS.length) {
      setPhase(forceFail ? "failed" : "ready");
      return;
    }
    const t = window.setTimeout(() => setStepIdx((i) => i + 1), 400);
    return () => window.clearTimeout(t);
  }, [phase, stepIdx, forceFail]);

  const pct = Math.round((Math.min(stepIdx, STEPS.length) / STEPS.length) * 100);

  return (
    <Modal title="Migrate plan" onClose={actions.closeOverlay} width={640} className={phase === "failed" ? "migration-failed" : ""}>
      <ModalHeader>
        <ModalTitle>Migrate to the .plan package</ModalTitle>
      </ModalHeader>
      <ModalBody>
        {phase === "explain" ? (
          <p>
            This plan uses the old on-disk layout. Migrating converts it to the .plan package in
            place. Every file is preserved. You can revert until you make a new revision.
          </p>
        ) : phase === "running" ? (
          <div className="migration-progress">
            <div className="progress-track" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
              <div className="progress-fill" style={{ width: `${pct}%` }} />
            </div>
            <ol className="migration-steps">
              {STEPS.map((s, i) => (
                <li key={s} className={i < stepIdx ? "step-done" : i === stepIdx ? "step-active" : ""}>
                  {s}
                </li>
              ))}
            </ol>
          </div>
        ) : phase === "ready" ? (
          <div className="migration-ready">
            <p className="state-panel-title">Ready.</p>
            <p>The graph and the compiled artifact are equivalent. You can revert until your next revision.</p>
          </div>
        ) : (
          <div className="migration-failed-body">
            <p className="state-panel-title tone-danger">Migration would change the meaning of this plan.</p>
            <p>The legacy directory is untouched. Diff below shows what would differ.</p>
          </div>
        )}
      </ModalBody>
      <ModalFooter>
        {phase === "explain" ? (
          <>
            <button type="button" className="btn btn-text" onClick={actions.closeOverlay}>
              Cancel
            </button>
            <button type="button" className="btn btn-primary" onClick={() => setPhase("running")}>
              Migrate
            </button>
          </>
        ) : phase === "ready" ? (
          <>
            <button type="button" className="btn btn-text" onClick={() => actions.toast("Reverted to the legacy directory.", "info")}>
              Revert to legacy…
            </button>
            <button type="button" className="btn btn-primary" onClick={actions.closeOverlay}>
              Done
            </button>
          </>
        ) : phase === "failed" ? (
          <>
            <button type="button" className="btn btn-text" onClick={() => actions.toast("Showing diff…", "info")}>
              Show diff
            </button>
            <button type="button" className="btn btn-primary" onClick={actions.closeOverlay}>
              Cancel
            </button>
          </>
        ) : null}
      </ModalFooter>
    </Modal>
  );
}
