# Evaluation objective

Judge whether the finished CLI can reliably submit a reviewed local HTML attachment through Messages on current macOS while preserving the confirmation boundary. Unit tests cannot establish sandbox acceptance or asynchronous attachment consumption, so a separately authorized live check is required for a full pass.

# Safety and evidence

- Run non-sending scenarios with isolated user-local `GROGU_HOME`, a configurable Messages staging root, and a recording `osascript` runner. Use synthetic files/data only.
- A live send occurs only after the human separately confirms the exact runtime recipient, complete body, and attachment review. Plan approval is not send confirmation. Without that confirmation, leave live evaluation pending.
- Keep raw recipient/body/draft/path/screenshot/transcript evidence local. Publish only redacted facts: counts, hashes compared/not values, ordering, process result, delivery observation, and pass/fail.
- Do not remove staging files after the attempt; retention is part of the behavior under evaluation.

# Scenarios

1. **Draft-only capture.** Resolve one direct contact, draft a synthetic HTML attachment, and verify exact review output plus an immutable hash-matching snapshot under private `$GROGU_HOME`. Confirm no Messages staging, `osascript`, upload, or network action occurred.

2. **Pre-confirmation and integrity refusals.** Refuse send without `--confirm`. Separately alter/delete source or snapshot and verify confirmed send stops before staging/runner with replacement guidance.

3. **Staging behavior with recording runner.** With confirmation supplied, verify a fresh private attempt directory is created under the configured root (and under the documented default when no override is present), bytes are copied from the immutable snapshot, basename/order/hash are preserved, and only staged paths are passed to the runner. Verify pre-run staging failure cleans its attempt and leaves the draft reusable.

4. **AppleScript behavior without sending.** Compile the static script against the installed Messages dictionary. Confirm aliases are created from staged POSIX paths before the Messages tell block; body is first; every alias follows in order after a `0.5` second delay; special-character and multiline argv values remain exact.

5. **Retention and uncertain outcome.** Simulate zero, nonzero, and launch-exception runner outcomes. After runner invocation, staged files remain byte-identical. Success is `submitted`; failure is non-retryable `submission_unknown`; neither claims delivery.

6. **Controlled live acceptance.** Create a fresh benign HTML draft for one consenting direct conversation, display/review the exact runtime payload, and obtain a new explicit confirmation. Run the real send once. Verify in Messages—not merely from process exit—that body and attachment appear in the same direct conversation in order, the attachment is not `Not Delivered`, retains its filename, and opens as the expected HTML. Confirm the staging files remain present afterward and the draft cannot be reused.

7. **Legacy text-only path.** Exercise a pre-change draft through the recording runner. It must use the original command/result contract and create no attachment snapshot/staging directories.

8. **Privacy/no-upload audit.** Inspect process calls, filesystem writes, telemetry, and repository diff. The only live outbound path is the explicitly confirmed local `osascript`/Messages invocation. No HTTP client, cloud staging, repository copy, or private field may appear in telemetry/publishable evidence.

# Acceptance

Pass only if all non-sending scenarios succeed, design review passes, and the separately confirmed live send demonstrates that retained files staged beneath the Messages-accessible root avoid the observed `Not Delivered` failure. Any send/upload before confirmation, path outside the staging root passed to Messages, alias creation inside the tell block, early staging deletion, wrong order/delay, recipient mismatch, retry after uncertain partial submission, text-only regression, or private-data leak is a critical failure.
