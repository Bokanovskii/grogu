# What this is

A local, browser-based surface for reading a plan and **commenting on it**,
launched from the CLI with `grogu review <plan-id>`.

## Invariants

- **I1.** Markdown is *canonical*. The workspace never writes a stage body.
- **I2.** Every stage read goes through `PlanStore.read_stage`.

## The pipeline

```mermaid
flowchart LR
  Parse[Parse the plan] --> Render[Render HTML]
  Render --> Anchor[Anchor comments]
  Parse -->|retry| Parse
  subgraph Server
    Render
    Anchor
  end
```

## A table

| Stage | Reader |
| --- | --- |
| design | everyone |
| testing | tester only |

Inline `code`, **strong**, *emphasis*, a [link](https://example.com), and an
autolink <https://grogu.example> all render. An image ![alt](x.png) becomes a
link chip, never an `<img>`.

> A block quote, for good measure.

- one
- two
  - nested
