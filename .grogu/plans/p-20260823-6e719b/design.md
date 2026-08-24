# Confirmation-gated local iMessage attachments — design

## Surfaces
1. `grogu imessage draft --help`: documents optional, repeatable `--attachment PATH` on draft only.
2. `grogu imessage draft`: writes one immutable local draft and prints one JSON review object.
3. `grogu imessage send <draft-id>` without `--confirm`: prints one stderr error and performs no staging or Messages action.
4. `grogu imessage send <draft-id> --confirm`: validates the saved review payload, creates private Messages-accessible staging copies for attachments, makes one submission attempt, and prints one JSON result only after every ordered operation is accepted by Messages.
5. `GROGU_IMESSAGE_STAGING_ROOT`: optional environment configuration used only by confirmed attachment sends. Its default base directory is `~/Library/Messages/.grogu-send-staging`.
6. Validation, staging, and submission errors: print one actionable stderr line, no stdout, and exit 2.
7. README and the iMessage skill: describe the same draft-review-confirm-stage-submit sequence. Plan approval is explicitly not message confirmation.

## Hierarchy
- Draft output puts the reviewed payload first by schema: `attachments`, `body`, then recipient identity within the same JSON object; `created_at`, `id`, and `status` are secondary lifecycle metadata.
- Each attachment row exposes its 1-based order, Messages-visible filename, canonical source path, byte count, and SHA-256. Immutable snapshot paths and post-confirmation staging paths are not displayed because neither is a user-selected payload value.
- `--confirm` exists only on `send`. Drafting, local snapshotting, and hashing never imply confirmation. Staging beneath the Messages-accessible root starts only after `--confirm` and complete validation.
- Success distinguishes submission from delivery with `submitted: true` and `delivery_confirmed: false`. Neither copy nor field naming may use “sent” or “delivered” for a successful adapter return.
- Errors state the failed draft/attachment, whether no Messages action occurred or partial submission is possible, and the required next step. A possibly partial attempt is never presented beside a retry action.

## States
- Help: one `--attachment PATH` row says it snapshots one local file and may be repeated in send order.
- Empty attachment set: new text-only draft JSON contains `"attachments": []`. This is the sole additive change to draft output; all existing fields and the text-only command remain unchanged.
- Draft success: stdout is exactly one JSON object; stderr is empty; exit 0. Recipient resolution finishes before any draft or attachment directory is written.
- Draft validation failure: stdout is empty; stderr uses the copy below; exit 2; no draft record or per-draft snapshot remains.
- Awaiting confirmation: stdout is empty; stderr says no message, attachment, or staging copy was submitted/created; exit 2; draft status remains `draft`.
- Pre-staging source or immutable-snapshot validation failure: stdout is empty; stderr requires a replacement draft; exit 2; no staging directory or `osascript` call occurs and status remains `draft`.
- Staging configuration/copy failure: stdout is empty; stderr names `GROGU_IMESSAGE_STAGING_ROOT`; exit 2; partial attempt staging is removed, no `osascript` call occurs, and the unchanged draft remains `draft` for a new explicitly confirmed attempt after configuration is fixed.
- Submission success: stdout is exactly one JSON object; stderr is empty; exit 0; status becomes `submitted`; the complete per-attempt staging directory is retained.
- Submission failure after the attempt is claimed or `osascript` is launched: stdout is empty; stderr says partial submission is possible; exit 2; status becomes `submission_unknown`, the complete staging directory is retained, and the draft cannot be retried.
- Reuse of `submitted` or `submission_unknown`: stdout is empty; stderr reports the terminal state; exit 2; no new staging or `osascript` call occurs.

## Flow
1. The caller supplies one required recipient, one required complete body, and zero or more `--attachment PATH` occurrences. Attachment occurrence order is preserved, including duplicate paths or filenames.
2. Resolve the recipient to exactly one direct Messages handle before generating persistent draft or immutable snapshot artifacts.
3. For each attachment, expand and canonicalize the source path, retain the user-selected basename as `filename`, verify a readable regular file, copy it to the private per-draft snapshot beneath `$GROGU_HOME`, then compute the snapshot byte count and lowercase SHA-256.
4. Persist only after every immutable attachment snapshot succeeds. Print the review JSON. This stage may read/copy/hash local files; it must not create a Messages staging directory, invoke Messages/`osascript`, make a network request, or upload anything.
5. The user reviews the exact resolved `recipient.identifier`, complete `body`, and every ordered attachment’s `index`, `filename`, `source_path`, `size_bytes`, and `sha256`. JSON escaping is authoritative for multiline or special-character body content.
6. Approval of this software plan, a topic, a tone, or a partial message is not confirmation. Only an unambiguous confirmation of that exact JSON payload permits `grogu imessage send <draft-id> --confirm`.
7. The confirmed send reloads values only from the draft id; it accepts no recipient, body, attachment, or staging overrides. Before staging, both each canonical source and immutable snapshot must match the recorded byte count and digest.
8. Attachment sends expand and canonicalize `GROGU_IMESSAGE_STAGING_ROOT`, defaulting to `~/Library/Messages/.grogu-send-staging`. The base and per-attempt directories must be owned by the current user with mode `0700`; staged files use mode `0600`. A configured existing base with any group/other permission bits is rejected rather than chmodding a caller-managed directory.
9. Create one fresh opaque `<attempt-id>` directory per confirmed attachment attempt. Copy each validated immutable snapshot byte-for-byte to `<attempt-id>/<index>/<filename>`, preserve attachment order and filename, then verify each staged file against the reviewed byte count and SHA-256. Never stage from the mutable source path.
10. If staging fails before `osascript`, remove only that incomplete attempt directory and leave the draft retryable. Once `osascript` is invoked, retain the complete attempt directory after both success and failure because Messages may consume file aliases asynchronously. Grogu does not automatically delete retained attempt directories.
11. Atomically claim the draft once staging is complete and immediately before `osascript`. The static AppleScript receives only `argv`: resolved recipient, complete body, then staged paths in review order. It converts every staged POSIX path to an AppleScript `alias` before entering the Messages `tell` block.
12. Inside the Messages `tell` block, submit the body first. For each prebuilt alias in ascending attachment order, wait exactly 0.5 seconds before submitting that alias. Text-only sends create no staging directory and incur no delay.
13. Body and attachments are separate, sequential, non-atomic Messages operations. A zero adapter exit marks the draft `submitted` and reports submission, never delivery. A launch or AppleScript failure after claim marks `submission_unknown`; the user inspects Messages and creates a replacement draft only for content still unsent.
14. Any change to recipient, body, attachment count, order, selected filename, canonical source path, size, or digest invalidates prior approval and requires a replacement draft, complete re-review, and fresh confirmation. Staging location and attempt id are transport details and do not change the reviewed payload.

## Copy
Voice uses direct factual statements. “Message sent” is forbidden; use “submitted” and state that delivery is not confirmed.

Help text for the new option:

```text
--attachment PATH     snapshot a local file; repeat in send order
```

No-confirm error:

```text
grogu: iMessage draft '<draft-id>' was not submitted because --confirm is required; no staging copy, message, or attachment was submitted; next: review the saved draft JSON, then run `grogu imessage send <draft-id> --confirm`
```

Invalid attachment while drafting:

```text
grogu: attachment <index> is not a readable regular file: <path>; no draft was created; next: fix the file and rerun the same `grogu imessage draft` command
```

Changed or missing reviewed source:

```text
grogu: iMessage draft '<draft-id>' was not submitted because attachment <index> no longer matches the reviewed source <canonical-source-path>; no staging or Messages action occurred; next: create and review a replacement draft, then confirm the new draft id
```

Changed or missing immutable snapshot:

```text
grogu: iMessage draft '<draft-id>' was not submitted because its immutable snapshot for attachment <index> is missing or changed; no staging or Messages action occurred; next: create and review a replacement draft, then confirm the new draft id
```

Unavailable or non-private staging root:

```text
grogu: iMessage draft '<draft-id>' was not submitted because GROGU_IMESSAGE_STAGING_ROOT is not a private writable directory: <configured-path>; no Messages action occurred; next: run `GROGU_IMESSAGE_STAGING_ROOT="<private-messages-root>" grogu imessage send <draft-id> --confirm`
```

Staging copy/verification failure:

```text
grogu: iMessage draft '<draft-id>' was not submitted because attachment <index> could not be staged and verified; no Messages action occurred and incomplete staging was removed; next: fix GROGU_IMESSAGE_STAGING_ROOT, then rerun `grogu imessage send <draft-id> --confirm`
```

Unknown result after a claimed attempt:

```text
grogu: iMessage draft '<draft-id>' may have been partially submitted and is now submission_unknown; private staged copies were retained for Messages; do not retry this draft; next: inspect the conversation in Messages, then create and review a replacement draft only for content still unsent
```

Subsequent use of an unknown draft:

```text
grogu: iMessage draft '<draft-id>' is submission_unknown and cannot be retried; next: inspect the conversation in Messages, then create and review a replacement draft only for content still unsent
```

Configuration documentation:

```text
Confirmed attachment sends stage fresh verified copies beneath `GROGU_IMESSAGE_STAGING_ROOT`; the default is `~/Library/Messages/.grogu-send-staging`. Each attempt uses a new mode-0700 directory and mode-0600 files. The staging path is never part of draft review or command output.
```

Retention documentation:

```text
After Messages submission starts, Grogu retains that attempt’s staged files because Messages may read aliases asynchronously. Grogu does not delete them automatically. Remove an old attempt directory only after checking Messages and deciding the attachment no longer needs it.
```

Safety documentation:

```text
Drafting snapshots and hashes local files only. It does not create Messages staging files, upload files, open Messages, or submit anything. Approving a Grogu software plan is not message confirmation. After reviewing the exact resolved recipient, complete body, and ordered attachment identities, the only send authorization is `grogu imessage send <draft-id> --confirm`.
```

Submission semantics documentation:

```text
Messages receives the body first, waits 0.5 seconds, then receives each attachment in reviewed order with 0.5 seconds between attachment submissions. These are separate operations. A successful command means Messages accepted all submission operations; delivery remains asynchronous and is not confirmed by Grogu.
```

## Tokens
- Encoding: UTF-8. JSON uses two-space indentation, alphabetically sorted object keys, one trailing newline, and no ANSI escape sequences.
- Streams: successful draft/send output goes only to stdout; errors go only to stderr.
- Progress: no spinner, status line, upload language, snapshot path, staging path, or intermediate file list is printed.
- Labels: lowercase snake_case JSON keys; 1-based integer attachment indexes; `size_bytes` is a base-10 integer; `sha256` is exactly 64 lowercase hexadecimal characters with no prefix.
- Review paths: `source_path` is an absolute canonical path. `filename` is the caller-selected basename and the name presented to Messages. Immutable snapshot and staging paths never appear in CLI output.
- Configuration: the environment variable is exactly `GROGU_IMESSAGE_STAGING_ROOT`; unset or blank means `~/Library/Messages/.grogu-send-staging`. It configures the base directory, not the per-attempt directory.
- Permissions: staging base/attempt/index directories are `0700`; staged files are `0600`.
- Timing: exactly 0.5 seconds occurs before each attachment submission; no delay follows the final operation and text-only sends have no added delay.
- Time field: `created_at` retains the existing UTC RFC 3339 representation.
- Styling: inherit terminal type size and colors; use no color, bold, borders, icons, or motion.

## Accessibility
- Every state is represented in text and by exit status; no meaning depends on color, animation, cursor position, or terminal width.
- JSON field names state units and digest algorithm. Attachment order is both array order and an explicit `index`, so screen readers and line-oriented tools expose the same sequence.
- The complete body remains one JSON string; newlines, quotes, backslashes, and non-ASCII characters must round-trip without truncation or source interpolation.
- Keyboard path is command entry only: draft command, review stdout, then a separate send command with explicit `--confirm`.
- Help and error output remains readable at 80 columns; long private values may extend rather than be truncated.
- No retained path is required to understand success or failure; the documented root and attempt-directory rule provide cleanup access without printing private path values.

## Layout
At an 80-column terminal, draft help includes:

```text
$ grogu imessage draft --help
usage: grogu imessage draft [-h] --recipient RECIPIENT
                            [--display-name DISPLAY_NAME] --message MESSAGE
                            [--attachment PATH]

options:
  -h, --help            show this help message and exit
  --recipient RECIPIENT
  --display-name DISPLAY_NAME
  --message MESSAGE
  --attachment PATH     snapshot a local file; repeat in send order
```

Text-only draft output intentionally adds an explicit empty attachment set while retaining all prior fields:

```json
$ grogu imessage draft --recipient "<recipient>" --message "<complete-body>"
{
  "attachments": [],
  "body": "<complete-body>",
  "created_at": "<RFC3339-UTC>",
  "id": "<draft-id>",
  "recipient": {
    "display_name": "<resolved-display-name-or-empty-string>",
    "identifier": "<resolved-recipient>"
  },
  "status": "draft"
}
```

Attachment draft output is the exact review contract. Array order is send order; immutable snapshot paths, staging paths, attempt ids, and storage version fields are omitted:

```json
$ grogu imessage draft --recipient "<recipient>" --message "<complete-body>" --attachment "<path-one>" --attachment "<path-two>"
{
  "attachments": [
    {
      "filename": "<basename-one>",
      "index": 1,
      "sha256": "<64-lowercase-hex-one>",
      "size_bytes": 1234,
      "source_path": "<canonical-absolute-path-one>"
    },
    {
      "filename": "<basename-two>",
      "index": 2,
      "sha256": "<64-lowercase-hex-two>",
      "size_bytes": 5678,
      "source_path": "<canonical-absolute-path-two>"
    }
  ],
  "body": "<complete-body>",
  "created_at": "<RFC3339-UTC>",
  "id": "<draft-id>",
  "recipient": {
    "display_name": "<resolved-display-name-or-empty-string>",
    "identifier": "<resolved-recipient>"
  },
  "status": "draft"
}
```

Text-only send output remains byte-for-byte compatible in field set and meaning:

```json
$ grogu imessage send <draft-id> --confirm
{
  "delivery_confirmed": false,
  "recipient": "<resolved-recipient>",
  "submitted": true
}
```

Attachment send output adds only the reviewed attachment count; staging details stay internal:

```json
$ grogu imessage send <draft-id> --confirm
{
  "attachment_count": 2,
  "delivery_confirmed": false,
  "recipient": "<resolved-recipient>",
  "submitted": true
}
```

`send --help` exposes no payload or staging override:

```text
$ grogu imessage send --help
usage: grogu imessage send [-h] [--confirm] draft

positional arguments:
  draft

options:
  -h, --help  show this help message and exit
  --confirm
```

## Acceptance criteria
- `--attachment PATH` appears only on `imessage draft`, accepts repeated occurrences, and preserves occurrence order.
- A text-only draft command remains valid with required `--recipient` and `--message`; its output retains every existing field and adds exactly `attachments: []`.
- A legacy JSONL record with no attachment or version field loads as an empty attachment tuple, sends through the prior text-only path, emits the prior three send-result fields, and creates no snapshot or staging directory.
- Draft output contains the full resolved recipient, complete body, and every attachment’s 1-based order, selected filename, canonical absolute source path, byte count, and lowercase SHA-256.
- Draft output never contains immutable snapshot paths, staging paths, attempt ids, file bytes, or an upload/cloud URL.
- Recipient resolution failure, attachment capture failure, or draft persistence failure leaves no new draft record or per-draft immutable snapshot artifacts.
- No-confirm and all pre-staging validation errors invoke neither staging nor `osascript`/Messages and do not consume the draft.
- Attachment staging occurs only after `--confirm`, source/snapshot validation, and resolution of `GROGU_IMESSAGE_STAGING_ROOT` (default `~/Library/Messages/.grogu-send-staging`).
- Every staged file is copied from the immutable snapshot, retains reviewed filename/order, matches reviewed size/SHA-256, and resides in a fresh opaque mode-0700 attempt directory with mode-0600 file permissions.
- Staging failure removes the incomplete attempt directory, invokes no Messages action, and leaves the draft retryable only through another explicit `--confirm` command.
- Once `osascript` is invoked, all staged files for that attempt are retained after success or failure and are never automatically deleted.
- AppleScript source is static and contains no recipient, body, or path interpolation; those values arrive only through `argv`. Staged paths become aliases before the Messages `tell` block.
- Confirmed submission sends the body first and staged aliases afterward in reviewed order, with exactly 0.5 seconds before every attachment send; no source or immutable snapshot path is passed to Messages.
- One successful attempt emits `submitted: true` and `delivery_confirmed: false`; attachment sends additionally emit only `attachment_count`.
- A failure after claim records `submission_unknown`, reports that partial submission is possible and staged files were retained, and blocks retries.
- Editing, replacing, reordering, adding, or removing any reviewed payload value requires a replacement draft and fresh exact confirmation. Changing staging configuration alone does not alter reviewed payload identity.
- Documentation states that plan approval is not send confirmation, drafting performs no Messages staging/upload/send, staging is retained for asynchronous consumption, body/files are separate operations, and successful submission does not confirm delivery.
- Commands and artifacts use placeholders in public examples and never place recipient, body, source path, immutable snapshot path, staging path, digest, attempt id, or file bytes into plans, telemetry, logs, commits, issues, or pull requests.

## Left to the engineer
- Dataclass names, JSONL storage version number, immutable snapshot index directory format, temporary filename, locking primitive, and atomic-write implementation.
- Opaque attempt-id format and internal claimed-state representation, provided every claimed-but-unresolved attempt is non-retryable and surfaced as `submission_unknown`.
- Exact exception class names and helper boundaries.
- Test fixture contents and synthetic paths, provided they contain no real recipient, message, or user path data.
- AppleScript variable names and runner wiring, provided aliases are constructed before `tell application "Messages"`, all private values arrive only through `argv`, the script compiles, and the specified order/delay is preserved.
