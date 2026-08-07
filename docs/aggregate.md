# Bounded context aggregation

`grogu aggregate` gives a session bounded, cacheable summaries instead of raw
dumps. Each `grogu aggregate <op>` call batches, filters, and aggregates data
from one existing subsystem — Git, the repository knowledge graph (see
`docs/memory.md`), tasks, telemetry, or the cross-repository relationship
catalog — and returns a small, stable JSON envelope. Nothing here replaces
those subsystems; `aggregate` is a read-only view over them shaped for how
much context a model actually needs. It does not execute agent-written code;
see `docs/codemode.md` for that.

## Why

Reading `git status --porcelain` on a large tree, listing every task record,
or exporting the whole knowledge graph works, but it puts an unbounded amount
of text in front of the model for a question that usually has a small answer
("what changed", "what's open", "what does this repository depend on"). Every
aggregation operation instead:

* caps the number of items it returns (`MAX_ITEMS` in `grogu_context.py`);
* returns counts and identifiers before full records;
* excludes diff bodies, file contents, task bodies, and telemetry payloads;
* redacts anything that looks like a secret (reusing the telemetry redactor);
* never shells out beyond read-only Git plumbing, and never walks outside the
  resolved repository root.

## Envelope

Every operation returns the same shape:

```json
{
  "schema_version": 1,
  "kind": "grogu.context_summary",
  "op": "tasks",
  "repository_id": "…",
  "generated_at": "2026-01-01T00:00:00+00:00",
  "signature": "…",
  "data": { }
}
```

`signature` is a short hash of `data` only (`generated_at` and other envelope
fields are excluded), so two calls that observe the same underlying state
produce the same signature and can be cached or compared without re-reading
the payload.

## Operations

```sh
grogu aggregate git                  # branch, upstream drift, changed-file counts by role
grogu aggregate graph --query auth   # bounded knowledge-graph neighborhood (see `grogu memory context`)
grogu aggregate tasks --limit 20     # counts by status/label, most recently updated tasks
grogu aggregate traces               # telemetry event counts by event and outcome
grogu aggregate relationships        # this repository's cross-repository relationship edges
grogu aggregate service              # name/version/description from top-level manifests only
```

All operations accept `--repo` to target a repository other than the current
working tree. `graph` and `tasks` additionally accept `--limit`; `graph` also
accepts `--query`, `--node`, and `--depth`, matching `grogu memory context`.
`relationships` accepts `--limit` to cap the returned edge list; the `by_kind`
counts always reflect the full set.

## Adding an operation

New aggregation operations belong in `grogu_context.py` as a plain function
that takes already-resolved inputs (a repository root, an open database
connection, or an existing store) and returns a bounded `dict`. Keep the
function testable without argparse or process state; `grogu_cli.py` only
resolves `--repo`/database connections and wraps the result with
`grogu_context._envelope`.
