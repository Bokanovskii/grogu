# Test objective

Prove that attachment support preserves resolve-draft-review-confirm-send while binding reviewed bytes to an immutable `$GROGU_HOME` snapshot and using a separate, private Messages-accessible staging copy only after confirmation. Automated tests must never execute a live Messages send or network upload and must use synthetic data only.

# Draft storage, snapshot, and compatibility

1. Add focused `DraftStore` tests in `tests/test_grogu_cli.py`.
   - Draft a small synthetic HTML source and verify an independent regular-file snapshot is atomically created beneath the draft's private `$GROGU_HOME` directory, preserving basename and exact bytes while recording canonical source path, snapshot path, size, SHA-256, and order.
   - Assert immutable draft/attachment values and that lifecycle changes preserve every reviewed field.
   - On POSIX, assert draft directories are `0700` and files user-readable only.
   - Cover missing, directory, unreadable, broken-link, copy, hash, and persistence failures; no draft or orphan snapshot may remain.
   - Prove recipient resolution completes before snapshot/persistence; unresolved, ambiguous, and group recipients create nothing.
   - Load a pre-change JSONL text-only draft with no attachment/version fields. It must have empty attachments, send through the old path, and create neither snapshot nor Messages-staging artifacts.
   - Reject unverified string-only attachment records from the unmerged prototype with replacement guidance unless they identify a valid immutable snapshot.

2. Verify immutable identity.
   - Source and snapshot start byte-identical with the recorded digest.
   - Modifying the source does not modify the snapshot, but confirmed send still refuses because the reviewed source identity changed.
   - Missing/changed source or missing/changed/replaced/symlinked snapshot fails before staging/runner. Include same-size changed bytes so SHA-256, not only size, detects it.

# Confirmed-send staging

3. Test the staging-root contract with a temporary configured root.
   - Default resolution is `~/Library/Messages/.grogu-send-staging`; `GROGU_IMESSAGE_STAGING_ROOT` overrides it without changing review output or CLI shape.
   - No-confirm, invalid-draft, and failed source/snapshot validation cases create no staging directory and invoke no runner.
   - After confirmation and validation, create one fresh attempt directory strictly beneath the configured root. Reject traversal, symlink-root/subdirectory escape, non-directory roots, and unreadable/unwritable roots before `osascript`.
   - Copy only from immutable snapshots, never sources. Preserve each reviewed basename in ordered subdirectories so duplicate names remain distinct. Verify staged bytes/size/digest before runner invocation and assert private modes.
   - A staging failure before runner invocation removes that attempt directory, leaves draft status `draft`, and makes zero runner calls.
   - Once runner invocation begins, retain staged files unchanged on both success and nonzero/exception outcomes. Do not assert cleanup after a fixed delay; asynchronous Messages consumption is the reason retention is required.

4. Test single-attempt lifecycle.
   - Competing sends permit at most one stage-and-run sequence.
   - Successful runner result marks `submitted`; launch/nonzero failure after claim marks non-retryable `submission_unknown` and blocks a second send.
   - Payload, snapshot, and retained staged bytes remain unchanged across status transitions.

# AppleScript contract

5. Make the script source independently inspectable.
   - Assert the runner receives static source plus `--` and separate argv entries for persisted recipient, complete body, and ordered staged paths. No private value is interpolated into source.
   - Assert alias creation occurs before `tell application "Messages"` and uses `((POSIX file (attachmentPath as text)) as alias)`.
   - Assert the tell block resolves one iMessage service/direct buddy, sends body first, then delays exactly `0.5` seconds before each alias send, preserving attachment order.
   - Assert only staged paths reach `osascript`; source and `$GROGU_HOME` snapshot paths do not.
   - Cover zero, one HTML, and multiple attachments plus spaces, quotes, backslashes, Unicode, leading hyphens, and multiline body argv.
   - On macOS CI, compile the static script with `osacompile`; never execute it.

# CLI and documentation contracts

6. Exercise parser/handlers with isolated `GROGU_HOME`, configured staging root, and mocked adapter/runner.
   - Original text-only commands remain valid with no new required flag and preserve prior send result fields.
   - Optional repeated `--attachment` preserves order. Draft output contains exact resolved recipient, full body, source path, filename, size, digest, and index, but no snapshot/staging path.
   - `send` without `--confirm` exits 2 without validation/staging/claim/runner. Confirmed send accepts no payload overrides and uses persisted data only.
   - Attachment success adds only the design-approved count; it does not claim delivery.

7. Extend `tests/test_grogu_skills.py` and README contract checks.
   - Pin exact attachment review, immutable local snapshot/hash, confirmed-only local Messages staging, configurable staging root, retained staged files, alias/ordered-send behavior, separate body/file submissions, replacement after changes, and no upload.
   - Keep all examples placeholders; do not add real-looking message, recipient, user path, or captured output.

# Privacy and scope checks

8. In isolated homes, inspect filesystem/process/telemetry effects.
   - Drafting writes only private draft snapshots beneath `GROGU_HOME`; confirmed sending additionally writes retained private staging beneath the configured Messages root.
   - No HTTP/network helper, cloud artifact, repository copy, or live `osascript` runs in automated tests.
   - Activity/telemetry contain at most command names/non-content metadata, never recipient/body/source/snapshot/staging path, filename, digest, or bytes.

# Validation commands

```sh
python -m unittest \
  tests.test_grogu_cli.GroguCliTests \
  tests.test_grogu_skills.RepositoryMessagingSkillContractTests
python -m unittest discover -s tests
```

On macOS, explicitly run the compile-only AppleScript test if selectors skip it. Then run `git diff --check`, inspect `git diff --name-only`, and run the privacy guard. Automated validation must not perform a live confirmed send.

# Pass criteria

All focused/full tests pass; macOS compilation passes; recipient resolution precedes persistence; reviewed bytes are snapshot/hash-bound; confirmed sends stage verified copies under the configurable Messages root and pass only retained aliases in body-then-file order with the specified delay; no external action occurs without confirmation; uncertain attempts cannot be retried; old text-only drafts/commands remain compatible; and no upload or private-data leakage exists.
