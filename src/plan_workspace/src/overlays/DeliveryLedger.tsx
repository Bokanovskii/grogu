import { useState } from "react";
import type { FeedbackRecord } from "../api/types";
import { Modal, ModalBody } from "../shell/Modal";
import { useActions } from "../state/store";
import { useControl } from "../state/control";
import { ago, clock } from "../lib/format";

function stateReached(f: FeedbackRecord, target: FeedbackRecord["state"]): string {
  const order: FeedbackRecord["state"][] = ["sent", "routed", "delivered", "acknowledged"];
  const hit = f.history.find((h) => h.state === target);
  if (hit) return clock(hit.at);
  if (order.indexOf(f.state) >= order.indexOf(target) && target !== "acknowledged") return "✓";
  return "—";
}

export function DeliveryLedger() {
  const actions = useActions();
  const control = useControl();
  const [open, setOpen] = useState<string | null>(null);
  const feedback = [...control.feedback].sort((a, b) => b.at - a.at);

  return (
    <Modal title="Delivery ledger" onClose={actions.closeOverlay} width={860} className="ledger-modal">
      <ModalBody>
        {feedback.length === 0 ? (
          <div className="ledger-empty">
            <p className="state-panel-title">No feedback sent yet.</p>
            <p>Feedback you send to agents and roles appears here as a durable record.</p>
          </div>
        ) : (
          <table className="ledger-table">
            <thead>
              <tr>
                <th scope="col">Id</th>
                <th scope="col">Target</th>
                <th scope="col">Scope</th>
                <th scope="col">Binding</th>
                <th scope="col">Sent</th>
                <th scope="col">Delivered</th>
                <th scope="col">Acknowledged</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {feedback.map((f) => (
                <>
                  <tr key={f.id} className={f.state === "withdrawn" ? "ledger-withdrawn" : ""}>
                    <td>
                      <button type="button" className="rel-link" onClick={() => setOpen((o) => (o === f.id ? null : f.id))}>
                        {f.id}
                      </button>
                    </td>
                    <td>{f.scope.label}</td>
                    <td>{f.scope.kind}</td>
                    <td>{f.binding ? "binding" : "advisory"}</td>
                    <td>{ago(f.at, Date.now())}</td>
                    <td>{stateReached(f, "delivered")}</td>
                    <td>
                      {f.state === "undeliverable"
                        ? `undeliverable · ${f.reason ?? ""}`
                        : stateReached(f, "acknowledged")}
                    </td>
                    <td className="ledger-actions">
                      <button type="button" className="rel-link" onClick={() => void navigator.clipboard?.writeText(f.id)}>
                        Copy id
                      </button>
                      {f.binding && f.state !== "acknowledged" && f.state !== "withdrawn" ? (
                        <button type="button" className="rel-link" onClick={() => void control.withdrawFeedback(f.id)}>
                          Withdraw
                        </button>
                      ) : null}
                      <button type="button" className="rel-link" onClick={() => actions.setMode("control")}>
                        Open in Control room
                      </button>
                    </td>
                  </tr>
                  {open === f.id ? (
                    <tr key={f.id + "-detail"} className="ledger-detail-row">
                      <td colSpan={8}>
                        <div className="ledger-detail">
                          <p className="ledger-body">{f.text}</p>
                          {f.ack_event ? <p className="ledger-ack">Acknowledged: event {f.ack_event}</p> : null}
                          {f.gate ? (
                            <p className="ledger-gate">
                              Gate {f.gate.stage}: {f.gate.state}
                            </p>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  ) : null}
                </>
              ))}
            </tbody>
          </table>
        )}
      </ModalBody>
    </Modal>
  );
}
