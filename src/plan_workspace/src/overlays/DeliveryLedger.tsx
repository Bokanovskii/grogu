import { Fragment, useState } from "react";
import type { FeedbackRecord } from "../api/types";
import { Modal, ModalBody } from "../shell/Modal";
import { useActions } from "../state/store";
import { useControl } from "../state/control";
import { ago, clock } from "../lib/format";

export function DeliveryLedger() {
  const actions = useActions();
  const control = useControl();
  const [open, setOpen] = useState<string | null>(null);
  const feedback = [...control.feedback].sort((a, b) => b.at - a.at);

  return (
    <Modal
      title="Delivery ledger"
      onClose={actions.closeOverlay}
      width={960}
      className="ledger-modal"
    >
      <ModalBody>
        {control.feedbackError ? (
          <p className="ledger-error" role="alert">
            Delivery ledger unavailable for one or more plans. Previously loaded
            receipts remain visible.
          </p>
        ) : null}
        {feedback.length === 0 ? (
          <div className="ledger-empty">
            <p className="state-panel-title">No feedback sent yet.</p>
            <p>
              Feedback sent to agents, roles, and plans appears here as a durable,
              plan-qualified receipt.
            </p>
          </div>
        ) : (
          <div className="ledger-scroll">
            <table className="ledger-table">
              <thead>
                <tr>
                  <th scope="col">Receipt</th>
                  <th scope="col">Plan</th>
                  <th scope="col">Target</th>
                  <th scope="col">Kind</th>
                  <th scope="col">State</th>
                  <th scope="col">Sent</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {feedback.map((record) => {
                  const receipt =
                    record.receipt_key ??
                    `${record.plan ?? "plan unavailable"}:${record.id}`;
                  const state = record.delivery_state ?? record.state;
                  return (
                    <Fragment key={receipt}>
                      <tr className={state === "withdrawn" ? "ledger-withdrawn" : ""}>
                        <td>
                          <button
                            type="button"
                            className="rel-link ledger-receipt"
                            onClick={() =>
                              setOpen((current) =>
                                current === receipt ? null : receipt,
                              )
                            }
                          >
                            {receipt}
                          </button>
                        </td>
                        <td>{record.plan ?? "Unavailable"}</td>
                        <td>{record.scope.label}</td>
                        <td>{record.binding ? "Binding" : "Advisory"}</td>
                        <td>{deliveryLabel(record)}</td>
                        <td>{ago(record.at, Date.now())}</td>
                        <td className="ledger-actions">
                          <button
                            type="button"
                            className="rel-link"
                            onClick={() =>
                              void navigator.clipboard?.writeText(receipt)
                            }
                          >
                            Copy receipt
                          </button>
                          {record.binding &&
                          state !== "acknowledged" &&
                          state !== "withdrawn" ? (
                            <button
                              type="button"
                              className="rel-link ledger-withdraw"
                              onClick={() =>
                                void control.withdrawFeedback(record.id, record.plan)
                              }
                            >
                              Withdraw
                            </button>
                          ) : null}
                        </td>
                      </tr>
                      {open === receipt ? (
                        <tr className="ledger-detail-row">
                          <td colSpan={7}>
                            <div className="ledger-detail">
                              <p className="ledger-body">{record.text}</p>
                              <p>{consequenceLabel(record)}</p>
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </ModalBody>
    </Modal>
  );
}

function deliveryLabel(record: FeedbackRecord): string {
  const state = record.delivery_state ?? record.state;
  if (state === "sent") return "Sent";
  if (state === "routed") {
    return `Routed to ${record.scope.label} on ${record.plan ?? "plan unavailable"}`;
  }
  if (state === "delivered") return "Delivered";
  if (state === "acknowledged") {
    const at = record.history.find((item) => item.state === "acknowledged")?.at;
    return at == null ? "Acknowledged" : `Acknowledged ${clock(at)}`;
  }
  if (state === "undeliverable") {
    return "Not delivered · no agent matches this scope yet";
  }
  return "Withdrawn";
}

function consequenceLabel(record: FeedbackRecord): string {
  const consequence = record.consequence;
  if (!consequence?.binding) return "Advisory · closes no gate";
  if (consequence.requires_replan) return "Binding · requires replan";
  const gates = consequence.gates.map((gate) => `${gate} gate`).join(", ");
  if (consequence.release === "acknowledgement_or_withdrawal") {
    return `Binding · ${gates || "recorded gate"} closed until acknowledgement or withdrawal`;
  }
  return `Binding · ${gates || "recorded gate"} consequence`;
}
