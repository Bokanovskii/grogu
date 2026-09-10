import { useActions, useApp } from "../state/store";
import { Button } from "./ui";

// Conflict cards from a 409 that could not be rebased. They name the node
// involved and let the user keep their edit, open the server's, or view both.
// Rendered as a floating stack anchored to the right so it reads as part of the
// rail without depending on the current mode's panel.
export function ConflictCards() {
  const { conflicts } = useApp();
  const actions = useActions();
  if (conflicts.length === 0) return null;
  return (
    <div className="conflict-stack" role="region" aria-label="Unresolved conflicts">
      {conflicts.map((c) => (
        <div key={c.id} className="conflict-card" role="alertdialog" aria-label={`Conflict on ${c.title}`}>
          <p className="conflict-title">Someone else changed {c.title}. Keep your edit or open theirs.</p>
          <div className="conflict-actions">
            <Button
              variant="primary"
              onClick={() => {
                // Retry our ops against the server's latest revision.
                void actions
                  .writeGesture(c.ops, `Keep mine: ${c.title}`, "workspace")
                  .then(() => actions.dismissConflict(c.id));
              }}
            >
              Keep mine
            </Button>
            <Button
              onClick={() => {
                void actions.refreshDoc();
                actions.dismissConflict(c.id);
              }}
            >
              Open theirs
            </Button>
            <Button variant="text" onClick={() => actions.openOverlay({ kind: "compiledPreview" })}>
              View both
            </Button>
          </div>
        </div>
      ))}
    </div>
  );
}
