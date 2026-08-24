# Objective

Extend the existing macOS-only `grogu imessage` workflow so one immutable local draft can include local files, with HTML as the acceptance case. Resolve one direct recipient before drafting, snapshot/hash the reviewed bytes under `$GROGU_HOME`, show the exact recipient/body/attachment set, and perform no outbound action until `grogu imessage send <draft-id> --confirm`. At confirmed send time, stage verified bytes privately beneath a Messages-accessible local root and pass only those retained staged files to Messages. Do not add HTTP upload, cloud storage, or any non-Messages transfer path.

# Verified platform constraints

- The current prototype in `src/grogu_imessage.py` validates a source path and passes that same path directly to `send POSIX file ...`; it has no immutable snapshot, Messages-accessible staging, alias conversion, delay, or retained send artifact.
- Engineer evidence from macOS 15+ showed body submission and attachment bubbles followed by `Not Delivered` for HTML, ZIP, and PDF files outside the Messages-accessible tree. This is a real delivery defect, not merely a compile failure.
- Apple documents that a POSIX path can be converted to a file and then to an `alias`, and an alias requires the target to exist at runtime (checked 2026-08-23): https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/ReferenceFilesandFolders.html . Apple directs callers to the installed application's dictionary for Messages-specific vocabulary: https://support.apple.com/guide/script-editor/view-an-apps-scripting-dictionary-scpedt1126/mac .
- A static `on run argv` script that creates `(POSIX file path) as alias` values before the Messages `tell` block, sends body first, delays briefly, and sends aliases in order compiles successfully with `osacompile` on macOS 26.5.1. Compilation proves syntax/dictionary resolution only; evaluation must verify actual Messages acceptance.

# Required invariants

1. **Resolve before persistence:** resolve a name to the concrete handle of exactly one direct Messages conversation before writing a draft, immutable snapshot, or send-staging artifact. Unresolved, ambiguous, and group recipients leave none.
2. **Immutable reviewed payload:** draft and attachment values are immutable. Recipient, display name, body, attachment order, canonical source paths, immutable snapshot paths, filenames, sizes, and SHA-256 digests never change for a draft id; only lifecycle state may transition.
3. **Review snapshot:** after recipient resolution, require each source to be a readable regular file, atomically copy it beneath `$GROGU_HOME/imessage/drafts/<draft-id>/attachments/`, preserve its basename, hash the completed snapshot, and persist source path, snapshot path, size, and digest. Directories are `0700`; files are user-readable only. Failed capture leaves no draft or orphan directory.
4. **Confirmed-send validation:** without `--confirm`, do not hash again, stage, launch `osascript`, activate Messages, or perform any external side effect. After confirmation, require both source and immutable snapshot to exist as readable regular files and match recorded size/digest.
5. **Messages-accessible staging:** after confirmation and validation, copy only from the immutable snapshot into a fresh private attempt directory under `GROGU_IMESSAGE_STAGING_ROOT`, defaulting to `~/Library/Messages/.grogu-send-staging/`. Preserve the reviewed basename, isolate duplicate names by ordered subdirectories, use `0700` directories and `0600` files, verify staged size/digest, reject symlink traversal, and never pass the mutable source or `$GROGU_HOME` snapshot to Messages.
6. **Retained staged files:** Messages consumes attachments asynchronously. Once an `osascript` attempt begins, retain its staging directory on both success and unknown failure; do not delete or rewrite staged files automatically in this change. A future cleanup feature requires a separate design with delivery-safe retention. A failure before `osascript` may remove that attempt's staging directory and leave the draft reusable.
7. **One reviewed bundle and one attempt:** send only persisted recipient/body/attachments. The send command accepts no overrides. Atomically claim immediately before `osascript`; concurrent sends permit at most one attempt. Zero exit becomes `submitted`; launch/nonzero failure after claim becomes non-retryable `submission_unknown` because body/files are sequential and may be partial.
8. **Backward compatibility:** original text-only draft/send command shapes remain valid; `--message` remains required; `--attachment` is optional and repeatable; old JSONL records with no attachment/version fields load as empty attachments, create no snapshot/staging files, and retain existing text-only result fields.
9. **Private-data boundary:** recipient/body/source/snapshot/staging paths, filenames, hashes, and bytes stay in private local state or the intentional draft review. They never enter telemetry, activity arguments, repository plans/fixtures/logs, commits, issues, or pull requests. Public examples use placeholders only.

# Implementation sequence

1. **Immutable draft capture in `src/grogu_imessage.py`.**
   - Add an immutable attachment value with canonical source path, immutable snapshot path, Messages-visible filename, byte size, and SHA-256; make `MessageDraft` immutable with an ordered immutable attachment collection.
   - Generate the draft id before capture. For each attachment, copy source bytes to a temporary file inside a private per-draft directory, hash/size the completed copy, atomically rename it, then persist the draft only after every attachment succeeds. Clean only new-id artifacts on failure.
   - Parse persisted JSON by name/version. Missing attachment fields mean a legacy text-only draft. Reject the unmerged prototype's string-only attachment records with replacement-draft guidance unless they demonstrably reference a valid private snapshot; never bless mutable source bytes silently.
   - Preserve reviewed fields exactly across status changes. Keep atomic JSONL writes and user-only permissions.

2. **Confirmed-send staging in `src/grogu_imessage.py`.**
   - Add a configurable staging root resolved from `GROGU_IMESSAGE_STAGING_ROOT`; default to `~/Library/Messages/.grogu-send-staging`. Configuration changes only internal file placement and does not alter CLI arguments or review payload.
   - After `confirmed` and all source/snapshot integrity checks, create a fresh attempt directory beneath that root. Copy from each immutable snapshot into its ordered private subdirectory, preserving basename; atomically finalize and hash-check each staged file. Ensure no path can escape the configured root.
   - Hold the single-attempt store lock/claim boundary so a competing process cannot stage and submit the same draft. Staging failures before runner invocation clean the attempt directory and leave status `draft`; once runner invocation starts, retain staging and use `submitted`/`submission_unknown` terminal state.
   - Preserve the adapter's existing text-only call compatibility. Text-only sends skip all attachment staging and use the prior result schema.

3. **Static AppleScript and ordering in `src/grogu_imessage.py`.**
   - Use one static `on run argv` source. Invoke `osascript -e <static-script> -- <recipient> <body> <staged-path>...`; never interpolate private values into source.
   - Before entering `tell application "Messages"`, convert each staged path with `((POSIX file (attachmentPath as text)) as alias)` into an ordered alias list. This both proves existence at AppleScript runtime and avoids resolving generic file terminology inside the application tell context.
   - Inside the tell block, resolve the existing iMessage service/direct buddy, send the body first, then delay `0.5` seconds before each attachment and send aliases in review order to the same buddy. Keep the delay as one named constant/script literal covered by design/tests.
   - Report only submission, never delivery. On runner error, include no private payload in telemetry/public logs and leave retained staging for Messages inspection/possible asynchronous consumption.

4. **CLI wiring in `src/grogu_cli.py`.**
   - Add repeatable optional `--attachment PATH` only to `imessage draft`; no send-time payload overrides.
   - Keep recipient resolution first, then snapshot/hash and persist once. Print the exact review schema from design: resolved recipient, full body, and ordered source attachment identity; do not expose immutable snapshot or Messages-staging paths.
   - On confirmed send, reload by id, validate, stage, claim, and submit using persisted values only. Preserve exit code 2 for actionable validation/staging failures and old text-only output.

5. **Documentation and skill contract.**
   - Update `.github/skills/imessage/SKILL.md` to require exact attachment identity review and fresh draft/confirmation after any recipient/body/attachment change. Explain that draft snapshotting and confirmed-send staging are local copies, not uploads; only the confirmed Messages invocation is outbound.
   - Update `README.md` for optional repeated attachments, immutable local snapshots, configurable private Messages staging, retained staged files, body/files as separate ordered submissions, and asynchronous delivery. Use placeholders only.
   - Update repository skill contract tests without embedding realistic private data.

6. **Scope and sequencing.**
   - No dependency, HTTP uploader, cloud service, recipient-resolution change, attachment-only mode, Messages database write, delivery polling, or automatic staging cleanup.
   - One sequential workstream across `src/grogu_imessage.py`, `src/grogu_cli.py`, `.github/skills/imessage/SKILL.md`, `README.md`, and tests. Storage, staging, CLI output, and safety copy are coupled.

# Existing prototype defects to correct

- Mutable draft/attachment values do not represent an immutable review payload.
- It stores and sends the original source path, allowing reviewed bytes to change and exposing delivery to Messages sandbox/path restrictions.
- It has no private hash-bound draft snapshot, no confirmed-send Messages staging, no retained staging for asynchronous consumption, and no traversal/permission rules.
- It interpolates values into AppleScript and constructs file references inside the Messages tell block; tests only assert substrings.
- It has no explicit alias coercion, ordering delay, live acceptance evidence, legacy JSONL/CLI compatibility test, or precise attachment review/reconfirmation documentation.
- Sequential body/file failure leaves the draft reusable, risking duplicate content on retry.
