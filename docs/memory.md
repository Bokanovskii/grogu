# Repository intelligence

Grogu defines a standard contract for intelligence in every repository it
works on. The contract is implemented by `grogu memory` and is stored in the
target repository under `.grogu/intelligence/`; the Grogu source repository
contains only the implementation and schemas, never another project's context.

## Files and schemas

| File | Audience | Meaning |
| --- | --- | --- |
| `manifest.json` | Sessions and tools | Stable repository identity, schema, and derivation version |
| `inventory.json` | Local indexer | Content digests and file roles used for incremental updates |
| `index.json` | Sessions and tools | Knowledge graph nodes, edges, confidence, and provenance |
| `insights.jsonl` | Sessions and reviewers | Reserved stream for future derived observations |

Git remains the source of truth for repository history. Grogu's inventory only
uses relative paths, SHA-256 digests, sizes, and file roles to update the
knowledge graph incrementally. File timestamps used to avoid re-hashing are
kept only in ignored local state. Build output, dependencies, virtual
environments, Git internals, and Grogu volatile state are excluded.

The graph stores typed nodes such as `architecture`, `component`, `decision`,
`convention`, `workflow`, `service`, and `file`. Each node has a compact
summary, paths, tags, confidence, and provenance. Edges express relationships
such as `contains`, `depends-on`, `calls`, `implements`, `constrains`, and
`verified-by`.

## Commands

```sh
grogu memory index
grogu memory status
grogu memory remember --type architecture --name api --summary "HTTP handlers live under src/api" --path src/api
grogu memory link architecture:api file:src/api/routes.py --kind implemented-by
grogu memory context --node architecture:api --depth 2 --limit 40
grogu memory context --query authentication --limit 20
grogu memory context --related --limit 20
```

Normal Grogu launches refresh this index incrementally and export
`GROGU_MEMORY_DIR` and `GROGU_REPOSITORY_ID` to Copilot. The context command
returns bounded, machine-readable graph neighborhoods rather than injecting an
unbounded repository dump into every prompt. `--related` follows the explicit
cross-repository catalog edges and loads bounded contexts from locally known
related repositories without merging their full indexes into the current one.

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
