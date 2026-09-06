# Typed `.plan` document packages

Grogu can store a plan as a typed semantic graph in
`.grogu/plans/<id>.plan/`. The graph is the writable source of truth. Markdown
remains the compatibility and review artifact consumed by existing plan
commands, agents, and pull requests, but it is produced only by the compiler.

## Package layout

```text
.grogu/plans/<id>.plan/
  manifest.json
  design.md
  implementation.md
  testing.sealed
  evaluation.sealed
  record.md
  attachments/
  review.json
  HEAD
  graph/open.json
  graph/testing.sealed
  graph/evaluation.sealed
  log/000001.json
  snapshots/
  proposals/
  projections/
  legacy/pre-migration/
  recovery/<revision>/
```

`manifest.json`, `HEAD`, graph partitions, logs, snapshots, proposals,
projection caches, recovery material, sealed files, registrations, legacy
copies, and `review.json` are local-only. `PlanStore` upgrades the existing
`.grogu/plans/.gitignore` additively; it never rewrites custom entries.

The tracked set stays familiar: compiled stage Markdown, `record.md`, and
attachments. `plan finalize` still decides what is staged for review.

## One layout per plan

`<id>/` is the legacy layout and `<id>.plan/` is the package layout. They are
mutually exclusive. If both exist, every `PlanStore.plan_dir()` caller refuses,
names both paths, and asks you to remove the copy you do not want. Grogu never
chooses between two writable truths.

## CLI

Create and open a package:

```sh
grogu plan doc create "Typed plan" --design --eval
grogu plan doc open <id> [--mode document|canvas|dependencies|revision|control]
                         [--stage S] [--port N] [--no-open] [--timeout SECS]
```

Read role-bounded context:

```sh
grogu plan doc show <id> [--stage S] [--kind K] [--node ID] [--text TEXT]
grogu plan doc query <id> ...
grogu plan doc context <id> [--stage S] [--include normative|all]
                            [--budget N] [--since REV] [--format md|json]
grogu plan doc projection <id> ...       # same compiler surface
grogu plan doc export <id> ... [--output PATH]
```

Every read, compile, cache hit, diff, and export is role-bound. Pass `--role`
or set `GROGU_ROLE`; an undeclared user-facing workspace reads as `reviewer`.
An engineer cannot request a testing or evaluation projection, including from
a warm cache.

Edit and inspect history:

```sh
grogu plan doc node add <id> --kind task --title "..." --stage implementation
grogu plan doc node set <id> <node-id> [--title T] [--body -] [--attr k=v]
grogu plan doc node rm <id> <node-id>
grogu plan doc link <id> <from> <kind> <to>
grogu plan doc unlink <id> <edge-id>
grogu plan doc patch <id> --file - [--base REV] [--dry-run] [--intent "..."]
grogu plan doc revisions <id>
grogu plan doc diff <id> --from REV --to REV
```

`--body`, `--intent`, and proposal reasons accept `-` for stdin. A patch or
stage source path inside the package is refused: compiled and private package
parts cannot become an accidental input to their own rewrite.

Proposals and dependency impact:

```sh
grogu plan doc revise <id> --file patch.json --why "..."
grogu plan doc propose <id> --file patch.json --why "..."
grogu plan doc proposals <id>
grogu plan doc accept <id> <proposal-id>
grogu plan doc reject <id> <proposal-id> --why "..."
grogu plan doc impact <id> --select <node-id|selector-json> [--depth N]
```

Accepting a proposal applies its operations as one ordinary revision with
`origin: proposal:<id>` and writes the accepted proposal record in the same
generation. Rejection requires a reason and preserves the proposal.

Validate derived state:

```sh
grogu plan doc lint <id>
grogu plan doc verify <id>
grogu plan doc compile <id> [--stage S] [--check]
```

Verification checks the immutable log, `HEAD`, materialized partition
digests, every readable compiler identity, and each compiled artifact against
the revision declared in its provenance. An unrelated revision does not make
an unchanged stage artifact stale.

## Existing plan and review commands

Existing commands remain the compatibility surface:

```sh
grogu plan show <id> --stage implementation --role engineer
grogu plan write <id> implementation --role architect --file -
grogu review list <id>
grogu review comment <id> --stage implementation --quote "..." --body "..."
```

On a package, `PlanStore.write_stage` imports the supplied Markdown into graph
nodes and then recompiles. It never writes the caller's bytes directly to the
stage artifact. `PlanStore.read_stage` remains the only stage read and seal
authority.

Legacy review commands adapt to graph `thread` nodes. Replies, resolution, and
reopening are graph revisions; `review.json` is retained only as the verbatim
pre-migration rollback copy. There is no second writable comment store.

## Migration and rollback

```sh
grogu plan doc migrate <id> [--dry-run]
grogu plan doc revert <id> [--dry-run]
```

Migration:

1. Refuses an existing package or dual layout.
2. Copies the entire legacy directory verbatim to
   `legacy/pre-migration/`.
3. Reads each written stage through `PlanStore.read_stage` as the declared
   role.
4. Imports every recognized section and keeps unrecognized prose as a `note`.
5. Imports Mermaid diagrams using `diagram_edge`, including cycles and
   self-loops.
6. Converts review threads into graph `thread` nodes.
7. Compiles all stages, runs the three compiler identities, and verifies every
   imported source span.
8. Installs the package and removes the legacy path only after verification.

The complete legacy bytes remain available for rollback. `revert` is permitted
only while `HEAD` is still `r0001`; once a new package revision exists, rollback
would destroy work and is refused.

## Revisions, recovery, and concurrency

Every durable graph mutation states its base revision. A write:

1. authorizes pointer paths against the caller's readable stages before patch
   application;
2. applies all operations to an isolated copy;
3. validates the complete role-visible graph;
4. compiles affected projections and runs completeness, invertibility, and
   graph-equivalence checks;
5. writes the immutable log, partitions, compiled artifacts, recovery copies,
   and finally `HEAD`.

A stale base returns `409` and only role-visible operations since that base.
The browser and CLI therefore coexist without silent overwrite.

If `HEAD` points at an incomplete generation, it is repaired to the newest
complete immutable log entry. A missing or corrupt materialized partition is
restored from the role-safe recovery copy for that revision. Sealed recovery
parts remain sealed.

## Compiler cache and HTTP ETags

Projection caches are keyed by `(revision, projection id, compiler version)`.
A warm hit reads one cache file and does not load the graph. The HTTP projection
route returns the projection digest as a strong `ETag` and honors
`If-None-Match` with `304`. `--since REV` compiles only the changed directives
and elements selected by the compiler contract.

## Local browser security

The workspace server:

* binds only `127.0.0.1` or `::1`;
* exchanges a single-use launch token for an `HttpOnly`,
  `SameSite=Strict` session cookie and immediately burns the token;
* requires the session cookie, exact same-origin `Origin`, and matching
  `X-Grogu-Token` on every mutation;
* validates `Host` and `Sec-Fetch-Site`, emits no CORS headers, and logs no
  requests;
* caps request bodies at 256 KiB and patches at 500 operations;
* serves only regular, non-symlink files from the committed
  `src/plan_workspace/dist/`;
* uses a CSP with no inline/eval script and no non-self network destination.

Node is a build-time dependency only. The committed React distribution runs
from the Python standard-library server when `node` is absent.

## HTTP contract

The stable routes are:

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/health` | plan, revision, role, and available modes |
| GET | `/api/doc` | role-visible document/canvas DTO |
| GET | `/api/projection` | Markdown or JSON projection with ETag |
| POST | `/api/patch` | one atomic revision |
| GET/POST | `/api/proposals...` | list, preview, create, accept, reject |
| POST | `/api/impact` | direct, transitive, cycles, compiled delta |
| POST | `/api/layout` | deterministic integer geometry patch |
| GET | `/api/revisions`, `/api/diff` | role-visible history |
| POST | `/api/threads...` | comment, reply, resolve, reopen, revise |
| GET | `/api/control...` | normalized registered-agent board and drill-in |
| GET/POST | `/api/feedback...` | receipts, scoped feedback, withdrawal |
| GET | `/api/events` | revision/proposal/thread/control/shutdown SSE |
| POST | `/api/request-changes`, `/api/approve` | existing review behavior |
| POST | `/api/shutdown` | end the local session |

All error responses use `{"error":"machine_token","message":"human text"}`.
The document and control-room routes never return an unreadable node, sealed
identifier, raw tool argument/result, assistant body, task description, model
reasoning, or trace payload.
