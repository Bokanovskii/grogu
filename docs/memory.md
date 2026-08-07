# Repository intelligence

Grogu defines a standard contract for intelligence in every repository it
works on. The contract is implemented by `grogu memory` and is stored in the
target repository under `.grogu/intelligence/`; the Grogu source repository
contains only the implementation and schemas, never another project's context.

## Files and schemas

| File | Audience | Meaning |
| --- | --- | --- |
| `manifest.json` | Git and sessions | Stable repository identity, remote, schema, and derivation version |
| `index.json` | Sessions and tools | Incremental file inventory, roles, Git cursor, and summary counts |
| `insights.jsonl` | Sessions and reviewers | Future model-derived facts with explicit provenance and derivation |

The index is deterministic. It records relative paths, SHA-256 digests, sizes,
roles, and file timestamps, plus the current branch, commit, remote, and recent
commits. Build output, dependencies, virtual environments, Git internals, and
Grogu volatile state are excluded. Unchanged files are reused from the previous
index, and changed or removed files are reported.

## Commands

```sh
grogu memory index
grogu memory status
grogu memory context --limit 40
```

Normal Grogu launches refresh this index incrementally and export
`GROGU_MEMORY_DIR`, `GROGU_REPOSITORY_ID`, and `GROGU_REPOSITORY_HEAD` to
Copilot. The context command returns a bounded, machine-readable summary rather
than injecting an unbounded repository dump into every prompt.

## Cross-project relationships

The repository identity is derived from its origin remote when available, not
from a local filesystem path. The user-local catalog can map that identity to
local checkouts. Explicit relationships are stored separately in the catalog:

```sh
grogu project relate repo-a repo-b depends-on \
  --evidence '{"source":"package manifest"}'
grogu project graph
```

The catalog stores relationship identifiers, kind, evidence, and timestamps;
repository content remains in the repository-local intelligence directory.
Automatic relationship discovery is still a later capability, so relationships
must currently be declared by a user or a verified workflow.
