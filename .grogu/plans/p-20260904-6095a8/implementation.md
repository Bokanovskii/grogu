# Interactive plan review workspace — implementation plan

## What this is

A local, browser-based surface for reading a Grogu plan and commenting on it,
launched from the CLI with `grogu review <plan-id>`. It exists because complex
multi-stage plans are unreviewable as terminal prose (harness friction #76), and
because a review-required plan currently has no user-visible interaction path
(harness friction #55).

The workspace is a **reader and a comment channel**. It is not an editor. Plan
Markdown under `.grogu/plans/<id>/` stays canonical and is written by exactly
one path — `grogu plan write`, architect only. Everything the workspace adds
lives beside it in a new, git-ignored `review.json`, and every effect it has on
the pipeline goes through existing `PlanStore` methods (`steer`, `approve`).

## Invariants — these are load-bearing

**I1. Markdown is canonical.** The server never writes a stage body, never
calls `write_stage`, and never edits a `.md`/`.sealed` file. `review.json` is
the only file it writes inside `.grogu/plans/<id>/`.

**I2. Every stage read goes through `PlanStore.read_stage`.** The server never
opens a stage file directly and never calls `grogu_plans.unseal`. Role
isolation is therefore enforced by exactly the code that enforces it today; the
workspace adds no second read path.

**I3. The effective role is `grogu_plans.current_role()` when set, otherwise
`grogu_plans.REVIEWER`.** An agent that launches the workspace sees only what
its role may read: an engineer gets `implementation` and `design`, and the
sealed testing/evaluation bodies must not appear anywhere in any HTTP response
body. The user (no `GROGU_ROLE`) reads as `reviewer`, which
`ROLE_READABLE_STAGES` already defines as all four stages.

**I4. The workspace grants no approval authority.** `POST /api/approve` calls
`PlanStore.approve` unchanged. That method refuses any caller with
`current_role()` set, so an agent-launched workspace cannot approve. Do not add
an `--as-user` flag to any `review` subcommand, and do not unset `GROGU_ROLE`
anywhere in this feature.

**I5. Nothing leaves the machine at review time.** The server binds
`127.0.0.1` only. The page's CSP forbids every outbound fetch. No `<img>` tag
is ever emitted from plan Markdown. The one optional network operation in the
whole feature is `grogu review assets --install`, which is a separate, explicit
command that downloads one pinned file and nothing else.

**I6. A comment is never destroyed by a plan rewrite.** Re-anchoring may move a
thread or mark it orphaned; it never deletes one. An orphaned thread keeps its
quoted text so the comment still reads.

**I7. Review state is local-only.** `review.json` carries the user's unfiltered
words about the plan. It must be git-ignored, must never be staged by
`plan finalize`, and must never be copied into a plan stage, a steering note
verbatim without the user's action, telemetry, or the activity log.

**I8. No required third-party dependency, no build step, no package manager.**
Python standard library only, hand-written ES modules served from disk.

## Research and the decisions it settled

Checked 2026-09-04.

* **Anchoring is a solved problem; do not invent one.** The W3C Web Annotation
  Data Model's `TextQuoteSelector` (exact + prefix + suffix) and
  `TextPositionSelector` (start/end offsets) are the standard pair, used
  together precisely because positions are fast and brittle while quotes with
  context survive edits. Hypothesis anchors this way and marks unrecoverable
  annotations "orphaned" rather than deleting them.
  <https://www.w3.org/TR/annotation-model/#selectors>,
  <https://web.hypothes.is/blog/fuzzy-anchoring/>. **Decision:** store both
  selectors, 32 characters of context each side (Hypothesis's figure), and
  reproduce the anchored/shifted/orphaned vocabulary. Fuzzy matching uses
  `difflib.SequenceMatcher` from the standard library rather than
  diff-match-patch, because plan bodies are tens of kilobytes and a dependency
  buys nothing at that size.

* **Markdown rendering: render it in Python, do not ship a JS parser.**
  markdown-it 14.2.1 (MIT, ~42 kB gzipped, <https://www.npmjs.com/package/markdown-it>)
  is the usual answer and gives only line-level `token.map`, not the character
  offsets an inline text selection needs; pairing it with DOMPurify 3.4.14
  (Apache-2.0 or MPL-2.0, <https://github.com/cure53/DOMPurify>) would add two
  vendored bundles to a repository that today has zero. **Decision:** a small
  first-party block/inline renderer in `grogu_markdown.py` that escapes
  everything, emits a closed tag allowlist, passes no raw HTML through, and
  stamps exact source character offsets on every emitted run of literal text.
  Sanitisation is by construction rather than by filtering, the offsets are
  exact by construction rather than approximated from line maps, and the whole
  thing is testable in Python with no browser. The cost is that we support a
  defined Markdown subset, which is stated below and is a superset of what
  `design_template` and every existing plan stage in this repository uses.

* **Mermaid: current, alive, MIT, self-hostable, and optional here.** Latest
  release 11.17.2 (August 2026), MIT, ships a UMD `dist/mermaid.min.js` that
  loads from a `<script>` tag with no bundler
  (<https://www.npmjs.com/package/mermaid>,
  <https://mermaid.js.org/intro/getting-started.html>). The UMD bundle is
  roughly 3 MB, which is not something to commit to this repository.
  **Decision:** the bundle is an *optional, pinned, hash-verified* asset
  installed on request into `$GROGU_HOME/assets/review/`, never vendored and
  never fetched implicitly. Semantic node/edge anchors do **not** depend on it,
  because they are derived by parsing the Mermaid source in Python; without the
  asset the workspace shows the diagram source plus a commentable list of its
  nodes and edges, and everything else works.

* **Mermaid's DOM identifiers, read from the source rather than recalled.** In
  `packages/mermaid/src/diagrams/flowchart/flowDb.ts` a vertex gets
  `domId = 'flowchart-' + id + '-' + vertexCounter`, and `lookUpDomId` prefixes
  it with the diagram element id when one is set
  (`${diagramId}-${vertex.domId}`). In
  `packages/mermaid/src/rendering-util/rendering-elements/edges.js` the edge
  path is given `data-edge="true"`, `data-et="edge"`, `data-id="<edge.id>"` and
  `id="${diagramId}-${edge.id}"`. **Decision:** resolve nodes by matching the
  regular expression `(?:^|-)flowchart-<id>-\d+$` against element ids and edges
  by `[data-id]`, with documented fallbacks, and pin the version so the
  convention cannot move underneath us.

* **Localhost servers are not private by default.** A page on the open web can
  reach `http://127.0.0.1:<port>` through DNS rebinding and permissive CORS;
  the standard defences are a `Host` header allowlist, an `Origin` allowlist,
  rejecting `Sec-Fetch-Site: cross-site`, emitting no CORS headers at all, and
  a per-session credential.
  <https://github.blog/security/application-security/localhost-dangers-cors-and-dns-rebinding/>.
  **Decision:** all of them, listed concretely under "Server security" below.

* **Open question, stated rather than guessed.** Whether Mermaid 11.17.2
  renders cleanly under `script-src 'self'` with no `'unsafe-eval'` was not
  settled by reading. Build it strict, verify with the browser check, and if a
  directive genuinely must be relaxed, relax exactly one, keep `script-src`
  free of `'unsafe-inline'`, and write down what forced it. Do not relax it
  pre-emptively.

## Files and ownership

The two declared workstreams are the only statement of ownership. Read them
with `grogu plan workstreams <id> --json`; the `--path` globs there are
authoritative and this section does not restate them.

**Workstream `core`** owns three new modules and nothing else:
`grogu_markdown.py` (Markdown subset → HTML with exact source spans),
`grogu_mermaid.py` (Mermaid source → semantic nodes and edges), and
`grogu_review.py` (anchors, re-anchoring, `ReviewStore`, rounds).

**Workstream `surface`** owns everything that touches the user: the loopback
server and asset installer in `grogu_review_server.py`; the workspace front end
(`index.html`, `app.js`, `anchors.js`, `mermaid_anchors.js`, `app.css`); the
CLI wiring in `grogu_cli.py` (the `review` subparser family and its handlers,
`review` added to `GROGU_COMMANDS`, and review state folded into `plan status`
and `plan brief`); the two-line change to `grogu_plans.py` (`_LOCAL_ONLY` gains
`review.json`, and `_protect_working_state` upgrades an existing marker instead
of leaving it alone); the review fixtures including the committed golden
flowchart SVG; and the documentation — `docs/review.md`, plus one paragraph
each in `README.md` and `.github/AGENTS.md` on how the user reviews a plan.

Test modules under `tests/test_*.py` belong to the tester and are in neither
workstream. Do not write them from the implementation plan; they are written
against the sealed testing plan, which is the point of the split.

The two workstreams are genuinely parallel because the module contracts below
are fixed here. `surface` builds against those signatures without waiting for
`core`. If a signature turns out to be wrong, raise it with `grogu plan amend`
rather than changing it on one side.

## Storage schema

`.grogu/plans/<id>/review.json`, written atomically (temp file + `os.replace`,
the existing `PlanStore._write_json` pattern) under `PlanStore.locked()`. Do
not introduce a second lock file; reuse the plans lock so there is no lock
ordering to get wrong.

```json
{
  "schema_version": 1,
  "plan": "p-20260904-6095a8",
  "created_at": "2026-09-04T22:10:00+00:00",
  "updated_at": "2026-09-04T22:41:00+00:00",
  "stage_digests": {"implementation": "sha256:...", "design": "sha256:..."},
  "stage_revisions": {"implementation": 3, "design": 1},
  "rounds": [
    {
      "number": 1,
      "state": "changes_requested",
      "opened_at": "...",
      "requested_at": "...",
      "answered_at": "",
      "steering_seq": 7,
      "thread_ids": ["c1", "c3"],
      "note": "the user's covering note, verbatim"
    }
  ],
  "threads": [
    {
      "id": "c1",
      "stage": "implementation",
      "round": 1,
      "status": "open",
      "created_at": "...",
      "author": "charlie",
      "anchor": { "...": "see below" },
      "anchor_state": "anchored",
      "anchor_confidence": 1.0,
      "anchor_revision": 3,
      "anchor_history": [
        {"at": "...", "from_revision": 2, "to_revision": 3,
         "state": "shifted", "confidence": 0.86}
      ],
      "comments": [
        {"seq": 1, "at": "...", "author": "charlie", "body": "..."}
      ],
      "resolved_at": "",
      "resolved_by": ""
    }
  ]
}
```

Thread ids are `c1`, `c2`, … monotonic and never reused, matching the existing
`a1`/`a2` amendment convention. Comment `seq` is 1-based within a thread.

### Anchors

A text anchor is a W3C selector pair over the **canonical Markdown source**,
never over the DOM:

```json
{
  "kind": "text",
  "stage": "implementation",
  "revision": 3,
  "start": 1284,
  "end": 1339,
  "exact": "the engineer must not read the testing plan",
  "prefix": "…up to 32 characters before…",
  "suffix": "…up to 32 characters after…",
  "body_digest": "sha256:…"
}
```

A Mermaid anchor is semantic — it names parts of the diagram source, not pixels
and not DOM ids:

```json
{
  "kind": "mermaid",
  "stage": "design",
  "revision": 1,
  "block_index": 0,
  "block_start": 902,
  "block_end": 1411,
  "block_digest": "sha256:…",
  "target": "node",
  "node_id": "Parse",
  "edge": {"from": "Parse", "to": "Render", "pair_ordinal": 0, "edge_index": 2},
  "label": "Parse the plan",
  "body_digest": "sha256:…"
}
```

`target` is `node`, `edge`, `subgraph` or `diagram`. `diagram` anchors the whole
fenced block and is the only target offered for diagram types outside
`flowchart`/`graph` in this version. `edge` is absent for node targets and vice
versa; keep both ordinals because Mermaid's own edge identifier suffix is a
counter whose exact basis must be confirmed empirically against the pinned
build (see "Stable selectors").

## Module contracts

These signatures are the contract between the two workstreams. Write them
first; do not renegotiate them in prose.

### `grogu_markdown.py`

```python
ALLOWED_TAGS: frozenset  # closed set, see below
def render(source: str) -> str
def render_document(source: str) -> dict
```

`render_document` returns:

```python
{
  "html": str,
  "blocks": [{"kind": "heading|paragraph|list|code|quote|rule|table",
              "level": int, "start": int, "end": int}],
  "code_blocks": [{"lang": str, "start": int, "end": int,
                   "body_start": int, "body_end": int, "body": str}],
}
```

Supported subset: ATX headings `#`–`######`; paragraphs; fenced code with an
info string (``` and ~~~); indented code; unordered lists (`-`, `*`, `+`) and
ordered lists, one nesting level beyond the top; block quotes; thematic breaks;
pipe tables with a header separator row; and inline `code`, `**strong**`,
`*emphasis*`, `[text](url)`, and autolinks. Anything else is emitted as literal
text. Setext headings, footnotes, definition lists, and HTML blocks are out of
scope and render literally.

`ALLOWED_TAGS` is exactly: `h1 h2 h3 h4 h5 h6 p ul ol li pre code blockquote hr
em strong a span div table thead tbody tr th td`. Nothing else may appear in
the output, ever, for any input.

Source mapping — the invariant the whole selection mechanism rests on:

* Every run of literal source text is wrapped in
  `<span data-s="{start}" data-e="{end}">`.
* **`source[start:end]` is byte-identical to that span's text content.** When a
  character must be HTML-escaped (`& < > "`), it is emitted as its own span
  carrying `data-atomic="1"`, because its escaped form has a different length
  from its source form. Selection inside an atomic span selects the whole span.
* Structural elements carry `data-block-start`/`data-block-end` for the block
  they came from, so a click on a heading can anchor the heading.
* Fenced code blocks carry `data-code-index`; Mermaid blocks additionally carry
  `data-mermaid-index` and `data-mermaid-src-start`/`-end`.

Links: `href` is kept only when the scheme is `http`, `https` or `mailto`, or
the target is a fragment; anything else renders as literal text. Every emitted
`<a>` gets `rel="noreferrer noopener"` and `target="_blank"`. **Images are
never rendered as `<img>`.** `![alt](url)` becomes a link chip showing the URL
as text, because an image tag is a silent outbound request the moment the page
loads. The CSP blocks it too; do both.

### `grogu_mermaid.py`

```python
FLOWCHART_TYPES = ("flowchart", "graph")
def parse(source: str) -> dict
def find_node(parsed: dict, node_id: str) -> dict | None
def find_edge(parsed: dict, source_id: str, target_id: str,
              pair_ordinal: int = 0) -> dict | None
```

`parse` returns:

```python
{
  "type": "flowchart",      # or "sequenceDiagram", "classDiagram", … or ""
  "supported": True,        # semantic anchors available (flowchart/graph only)
  "partial": False,         # >20% of non-empty, non-directive lines unparsed
  "nodes": [{"id": "A", "label": "Parse", "shape": "round",
             "subgraph": "", "line": 3}],
  "edges": [{"from": "A", "to": "B", "label": "ok", "kind": "arrow",
             "edge_index": 0, "pair_ordinal": 0, "line": 4}],
  "subgraphs": [{"id": "s1", "title": "Server", "line": 8}],
  "lines": 12,
  "unparsed": 0,
}
```

Parsing rules, deliberately conservative rather than a Mermaid grammar:

* Strip `%% …` comments and `%%{ … }%%` init directives before anything else.
* The first non-empty line matching
  `^\s*(flowchart|graph)\s+(TB|TD|BT|RL|LR)\b` sets `type` and
  `supported = True`. Any other recognised diagram keyword sets `type` and
  `supported = False`. An unrecognised first line leaves `type` empty.
* Node shapes: `A[text]`, `A(text)`, `A((text))`, `A{text}`, `A>text]`,
  `A[[text]]`, `A[(text)]`, `A([text])`, and a bare identifier appearing in an
  edge. Labels may be quoted; keep the label as written minus the quotes.
* Edges: `-->`, `---`, `-.->`, `-.-`, `==>`, `===`, `--x`, `--o` and their
  labelled forms `A -->|text| B` and `A -- text --> B`. Chains
  `A --> B --> C` produce one edge per hop. A node is registered the first time
  it is seen, whether declared or only referenced.
* `subgraph <id> [title]` … `end` sets `subgraph` on the nodes inside.
* `classDef`, `class`, `style`, `linkStyle`, `click`, `direction` and
  `accTitle`/`accDescr` are recognised and skipped, not counted as unparsed.
* `edge_index` is declaration order across the diagram; `pair_ordinal` is the
  index among edges sharing the same `(from, to)`.

`parse` must never raise on arbitrary input. Unparseable lines increment
`unparsed`; nothing else happens.

### `grogu_review.py`

```python
SCHEMA_VERSION = 1
CONTEXT_CHARS = 32
FUZZY_MIN_RATIO = 0.75
FUZZY_MAX_BODY = 200_000
MAX_COMMENT_CHARS = 8000

ANCHORED, SHIFTED, ORPHANED = "anchored", "shifted", "orphaned"
OPEN, RESOLVED = "open", "resolved"
ROUND_OPEN, ROUND_REQUESTED, ROUND_ANSWERED = "open", "changes_requested", "answered"

class ReviewError(Exception): ...

def digest(body: str) -> str                      # "sha256:<hex>"
def text_anchor(body: str, start: int, end: int, *, stage: str, revision: int) -> dict
def mermaid_anchor(*, stage: str, revision: int, body: str, block: dict,
                   parsed: dict, target: str, node_id: str = "",
                   edge: dict | None = None) -> dict
def reanchor_text(anchor: dict, body: str) -> dict     # {"anchor","state","confidence"}
def reanchor_mermaid(anchor: dict, body: str, blocks: list) -> dict

class ReviewStore:
    def __init__(self, plans: "grogu_plans.PlanStore") -> None: ...
    def path(self, plan_id: str) -> Path: ...
    def load(self, plan_id: str) -> dict: ...
    def stage_view(self, plan_id: str, stage: str, *, role: str) -> dict: ...
    def sync(self, plan_id: str, *, role: str) -> dict: ...
    def add_thread(self, plan_id, *, stage, anchor, body, author="") -> dict: ...
    def reply(self, plan_id, thread_id, body, *, author="") -> dict: ...
    def resolve_thread(self, plan_id, thread_id, *, note="", author="") -> dict: ...
    def reopen_thread(self, plan_id, thread_id) -> dict: ...
    def threads(self, plan_id, *, stage="", status="") -> list: ...
    def request_changes(self, plan_id, *, note="", role="") -> dict: ...
    def approve(self, plan_id, *, confirm_open=False, note="") -> dict: ...
    def summary(self, plan_id: str) -> dict: ...
```

`stage_view(plan_id, stage, role)` returns
`{"stage","state","written","revision","digest","markdown","html","blocks",
"mermaid":[{"index","start","end","digest","source","parsed"}]}` and obtains
`markdown` by calling `self.plans.read_stage(plan_id, stage, role=role)` — see
I2.

`summary(plan_id)` returns
`{"round": int, "round_state": str, "open": int, "resolved": int,
"orphaned": int, "threads": int}` and is the shape the CLI folds into
`plan status` and `plan brief`.

`grogu_review` imports `grogu_plans`. **`grogu_plans` must not import
`grogu_review`** — the CLI composes the two. Keep the dependency one-way.

### Re-anchoring

`sync` runs on every workspace load and on every CLI read of review state. For
each stage the role may read: compute `digest(body)`; if it equals
`stage_digests[stage]`, do nothing. Otherwise re-anchor every thread on that
stage and record the outcome, then update `stage_digests` and
`stage_revisions`.

Text re-anchoring, in order, first hit wins:

1. **Unmoved.** `body[start:end] == exact` and the recorded prefix and suffix
   still surround it → `anchored`, confidence `1.0`.
2. **Unique quote.** `exact` occurs exactly once in the new body → re-anchor to
   it, `anchored`, confidence `1.0`.
3. **Disambiguated quote.** `exact` occurs more than once → score each
   occurrence by the length of the common suffix of `prefix` and the common
   prefix of `suffix`; take the single best. Ties, or a best score of zero, →
   the occurrence nearest the old `start`, marked `shifted`.
4. **Fuzzy.** `difflib.SequenceMatcher(None, exact, window)` over the new body,
   searching windows around the old offset outward; accept the best match with
   ratio `>= FUZZY_MIN_RATIO` (0.75) → `shifted`, confidence = the ratio. Skip
   this step when `len(body) > FUZZY_MAX_BODY` and go straight to step 5.
5. **Orphaned.** Keep `exact`, `prefix`, `suffix` and the last known offsets
   untouched, set `anchor_state = "orphaned"`, confidence `0.0`.

Mermaid re-anchoring: locate the block by `block_digest` first, then by
`block_index`, then by the first block whose parse contains the anchored node
or edge. Within the block: node id present → `anchored`; node id gone but
exactly one node carries the recorded `label` → `shifted` with that id
recorded; otherwise `orphaned`. Edges: exact `(from, to, pair_ordinal)` →
`anchored`; `(from, to)` with a different ordinal → `shifted`; same `label`
between two surviving nodes → `shifted`; otherwise `orphaned`.

Every transition appends to `anchor_history`. Threads are never removed (I6).

### Review rounds

* A round is `open` from the moment the first thread is created after the last
  round closed. `rounds` is never empty once a thread exists.
* **Request changes** closes the round: state becomes `changes_requested`, and
  **one** steering note is recorded through
  `PlanStore.steer(text, plan_id=…, role=ARCHITECT, requires_replan=True)`.
  One note per round, not one per thread — steering is append-only and read by
  every later agent, and N notes for one review is how the channel becomes
  unreadable. The note is composed as the user's covering note followed by one
  block per open thread:

  ```
  Review round 1 — 3 open comment(s). Read them with
  `grogu review list <plan> --json`.

  [c1 implementation] "…the exact quoted text, truncated to 160 chars…"
    the user's comment

  [c3 design node:Parse] "Parse the plan"
    the user's comment
  ```

  `requires_replan=True` already sets the plan status to `needs_review`, which
  `PlanStore.gate` already treats as blocking. No new gate logic is needed and
  none may be added.
* The round becomes `answered` when `sync` observes that every stage named by
  its threads has a new digest — that is, the architect rewrote the plan. Note
  that `write_stage` already flips a `needs_review` plan back to `draft` for
  the architect, and back to `draft` with `approved_at` cleared when
  `review_required` is set. Depend on that; do not duplicate it.
* **Approve** calls `ReviewStore.approve`, which refuses with a `ReviewError`
  naming the open thread ids when any thread is open and `confirm_open` is
  false, and otherwise calls `PlanStore.approve(plan_id, note=…)` and lets its
  own refusals (role set, missing stage bodies, pending amendments) pass
  through unchanged.

## The server

`grogu_review_server.serve(...) -> dict` builds a
`http.server.ThreadingHTTPServer` on `127.0.0.1`, port 0 by default, and
returns `{"url", "host", "port", "plan", "role", "assets"}`. `--json` prints
that and, with `--no-open`, the caller drives the browser itself.

### Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/?t=<token>` | exchange the launch token for a session cookie, then 303 to `/` |
| GET | `/` | `index.html` |
| GET | `/static/<name>` | `app.js`, `anchors.js`, `mermaid_anchors.js`, `app.css` |
| GET | `/vendor/mermaid.min.js` | the installed asset, or 404 |
| GET | `/api/plan` | the whole reviewable state (below) |
| POST | `/api/threads` | `{stage, anchor, body}` → the created thread |
| POST | `/api/threads/<id>/comments` | `{body}` |
| POST | `/api/threads/<id>/resolve` | `{note}` |
| POST | `/api/threads/<id>/reopen` | `{}` |
| POST | `/api/request-changes` | `{note}` |
| POST | `/api/approve` | `{confirm_open, note}` |
| POST | `/api/shutdown` | stop the server |
| GET | `/api/health` | `{"ok": true}` |

`GET /api/plan` returns:

```json
{
  "plan": "p-…", "title": "…", "status": "draft",
  "review_required": true, "approved_at": "",
  "role": "reviewer", "readable_stages": ["design", "implementation", "testing", "evaluation"],
  "stages": [ { "…": "the stage_view shape" } ],
  "threads": [ "…" ],
  "round": { "…": "the current round, or null" },
  "summary": { "…": "ReviewStore.summary" },
  "assets": {"mermaid": true, "version": "11.17.2"},
  "gate": {"implement": {"allowed": false, "blockers": ["…"]}}
}
```

Errors are `{"error": "<message>"}` with 400 (bad request), 403 (refused),
404 (unknown), 409 (`ReviewError`/`PlanError`). Never leak a traceback.

### Server security

Every one of these is required.

* Bind `127.0.0.1` literally. Never `0.0.0.0`, never a hostname, never a
  `--host` value other than `127.0.0.1` or `::1` — reject anything else with a
  usage error rather than binding it.
* **`Host` allowlist.** The request's `Host` must be exactly
  `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`. Anything else →
  403. This is the DNS-rebinding defence and it is not optional.
* **`Origin` allowlist.** When present, `Origin` must be
  `http://127.0.0.1:<port>` or `http://localhost:<port>` → otherwise 403.
* **`Sec-Fetch-Site`.** When present it must be `same-origin` or `none` →
  otherwise 403.
* **No CORS.** Never emit `Access-Control-Allow-*`. Do not implement `OPTIONS`.
* **Capability token.** `secrets.token_urlsafe(32)`, generated per run, valid
  for exactly one `GET /?t=…`. That request sets
  `Set-Cookie: grogu_review=<secrets.token_urlsafe(32)>; HttpOnly; SameSite=Strict; Path=/`
  and redirects to `/` so the token does not persist in history or a referrer.
  Compare both values with `hmac.compare_digest`.
* **Custom header on writes.** Every `/api/*` request must carry
  `X-Grogu-Review: 1`, which a cross-origin form or simple request cannot set.
* **Headers on every response.**
  `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'`,
  `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`,
  `Cache-Control: no-store`. `'unsafe-inline'` is present for `style-src` only,
  because Mermaid injects a `<style>` element; it is never added to
  `script-src`.
* **Static files.** Serve only from `src/review_workspace/` and the installed
  asset directory, by resolving the path and requiring
  `Path(resolved).is_file()` and `resolved.parent == root` — no `..`, no
  symlink escape, no directory listing.
* **Body limits.** Reject a request body over 64 KiB; reject a comment over
  `MAX_COMMENT_CHARS`.
* **Lifetime.** Shut down after `--timeout` seconds with no request (default
  3600), on `POST /api/shutdown`, and on `KeyboardInterrupt`. Use a watchdog
  thread and `shutdown()`; do not use `signal.alarm` — this must work on
  Windows.
* **Logging.** Override `log_message` to print nothing by default. The token,
  the cookie and comment text must never reach stdout, `grogu watch`'s activity
  log, telemetry, or a traceback.

### The agent-identity trap

`PlanStore.read_stage` calls `claim_agent_role`, which binds an *identified*
agent to one role for the life of the plan. The user's own shell in the
repository checkout may already be covered by a binding — the architect's, for
instance — and reading as `reviewer` from there would be refused with "this
agent is already bound to the architect role".

So, before the first read, the server sets a dedicated identity in its own
process environment **only when one is not already set**:

```python
os.environ.setdefault("GROGU_AGENT", f"grogu-review-{plan_id}")
```

It must never set, unset or change `GROGU_ROLE`. When an agent launched the
workspace, `GROGU_AGENT` is already theirs and their existing binding is used,
which is the correct behaviour. This is the first thing that will go wrong if
it is skipped; it is not optional and it is not a workaround to be simplified
away.

## The front end

`index.html` loads `app.css` and `app.js` as `<script type="module" src>`. No
inline script, no inline event handlers — the CSP forbids both, deliberately.

`anchors.js` owns the source-offset mapping and nothing else:

```js
export function offsetsFromSelection(root, selection)  // -> {start, end} | null
export function rangeForOffsets(root, start, end)      // -> Range | null
export function highlight(root, thread)                // wraps in <mark data-thread=…>
```

Offsets come from the nearest ancestor carrying `data-s`: for a selection
boundary inside a mapped span, the source offset is
`Number(span.dataset.s) + offsetWithinSpanText`; inside an atomic span it snaps
to the span's `data-s` or `data-e`. This is exact because of the byte-identity
invariant in `grogu_markdown`. Selections spanning several spans take the first
span's start and the last span's end.

`mermaid_anchors.js` owns diagram interaction:

```js
export async function renderDiagram(container, source, index)
export function elementForNode(svg, nodeId)
export function elementForEdge(svg, edge)
```

`mermaid.initialize({startOnLoad: false, securityLevel: 'strict',
flowchart: {htmlLabels: false}, deterministicIds: true,
deterministicIDSeed: '<block digest>'})`. `deterministicIds` is what makes the
generated element ids stable across renders, which is what makes a stored
anchor resolve to the same element tomorrow.

After `mermaid.render` returns SVG, **sanitise it before insertion**: parse it
with `DOMParser`, remove every `script`, `foreignObject`, `use` with an external
reference, every attribute beginning `on`, and every `href`/`xlink:href` whose
value is not a same-document fragment. That is a ~30 line first-party function
and it is why DOMPurify is not needed here. Then insert.

Degraded mode, when `/vendor/mermaid.min.js` is absent: render the fenced block
as source in a `<pre>`, and beneath it a list of chips built from the Python
parse — one per node, edge and subgraph — each of which opens the same comment
composer and produces the same semantic anchor. Anchoring must not depend on
the asset.

### Stable selectors

Node `A` in the SVG for block *i*: match element ids against
`/(?:^|-)flowchart-A-\d+$/`, preferring `g.node`. Fall back to a `g.node` whose
text content equals the node's parsed label. Fall back to unresolved, which is
a displayed state, not an error.

Edge: prefer `[data-id]` on a path within the diagram whose value matches the
Mermaid edge identifier; fall back to `[id$="L_A_B_0"]` and to the *n*-th
`path.flowchart-link` using `edge_index`. **Confirm the actual suffix
convention against the pinned build before choosing which ordinal to try
first**, and write what you found into `docs/review.md` — do not assume either
ordinal is the right one.

Guard the convention cheaply: commit one golden SVG fixture produced by the
pinned Mermaid build for a fixture flowchart, and add a Python test asserting
that fixture contains ids matching the documented node pattern and the
documented edge attributes. A Mermaid upgrade that changes the convention then
fails a fast test instead of silently unanchoring every diagram comment.

## Assets

```
grogu review assets              # what is installed
grogu review assets --install    # download the pinned bundle
grogu review assets --install --from ./mermaid.min.js   # from a local file
```

* `MERMAID_VERSION = "11.17.2"`, MIT.
* `MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@11.17.2/dist/mermaid.min.js"`.
* `MERMAID_SHA256` is pinned in `grogu_review_server.py`. Capture it once by
  downloading and hashing, record it in the source with a comment naming the
  date and URL, and make `--install` refuse on mismatch with a message that
  says the digest did not match and nothing was installed.
* Installs to `$GROGU_HOME/assets/review/mermaid-11.17.2/mermaid.min.js`, with
  the upstream `LICENSE` written alongside it.
* `GROGU_REVIEW_MERMAID` may point at an existing file, which is used as-is —
  this is the air-gapped path.
* No implicit download, ever. `grogu review` prints one line when the asset is
  missing and carries on.

## CLI surface

`review` is a new top-level command and must be added to `GROGU_COMMANDS` in
`grogu_cli.py`, or the launcher hands it to Copilot.

```
grogu review <plan-id>                          # same as `review open`
grogu review open <plan-id> [--stage S] [--port N] [--no-open]
                             [--timeout SECS] [--json]
grogu review list <plan-id> [--stage S] [--open] [--json]
grogu review comment <plan-id> --stage S (--quote TEXT | --node ID | --edge A>B)
                                --body TEXT [--diagram N]
grogu review reply <plan-id> <thread-id> --body TEXT
grogu review resolve <plan-id> <thread-id> [--note TEXT]
grogu review request-changes <plan-id> [--note TEXT]
grogu review status <plan-id> [--json]
grogu review assets [--install] [--from PATH] [--json]
```

The CLI mirror is not a convenience. It is how the architect reads a review
round without a browser and how the whole feature is tested without one. `review
list --json` is the machine-readable shape agents consume.

Conventions to follow, because they are this repository's and getting them
wrong is a failed first command for every agent: take the plan id positionally
*and* as `--plan`/`--id` via `_plan_id_argument`; exit 3 on `PlanError` and
`ReviewError` (add `ReviewError` to `main`'s handler chain); exit 2 on usage
errors; `print_json` for `--json`. Do **not** add `--role` to any `review`
subcommand — the effective role is the session's, per I3 — and do **not** add
`--as-user` anywhere (I4).

`grogu review comment --quote` finds the quoted text in the stage body and
refuses when it is absent or ambiguous, naming how many times it occurred.

Two compositions in `grogu_cli.py`, both answering harness friction #70 (a
verdict that routes nowhere):

* `plan status` gains one line when a review exists:
  `review: round 2, 3 open, 1 orphaned`.
* `plan brief` gains an open-review-threads section for the architect,
  assembled in the CLI handler from `ReviewStore.summary` and
  `ReviewStore.threads(status="open")`. Do not add it inside
  `PlanStore.brief`; that would invert the module dependency.

## Order of work

Within `core`:

1. `grogu_markdown.py`. Nothing else can be anchored until the source-mapping
   invariant holds.
2. `grogu_mermaid.py`.
3. `grogu_review.py`: anchors, then re-anchoring, then `ReviewStore`.

Within `surface`:

1. The two-line `grogu_plans.py` change, so `review.json` is git-ignored before
   any of it can be written.
2. `grogu_review_server.py`, security envelope first — `Host`, `Origin`,
   `Sec-Fetch-Site`, the token exchange, the response headers — and routes
   after. A route added before the envelope is a route that will be shipped
   without it.
3. `src/review_workspace/*`.
4. CLI wiring, then `docs/review.md`, `README.md` and `.github/AGENTS.md`.

## Migration and backward compatibility

* A plan with no `review.json` opens with zero threads; the file is created on
  first write, never on read.
* `.grogu/plans/.gitignore` already exists in repositories that have used
  Grogu, and `_protect_working_state` only writes it when it is absent. Change
  it to read the existing marker and append any of `manifest.json`,
  `revisions/`, `*.sealed`, `review.json` that are missing, preserving whatever
  else is there. An existing three-line marker that never gains `review.json`
  is a privacy failure (I7), not a cosmetic one.
* `review.json` with a `schema_version` this build does not know: raise a
  `ReviewError` naming the version and refuse. Never rewrite it, never drop
  fields, never silently migrate downwards.
* No change to `manifest.json`'s schema and no bump of
  `grogu_plans.SCHEMA_VERSION`. Rounds and threads live only in `review.json`;
  the only manifest mutations are those the existing public `steer` and
  `approve` methods already make.
* Every existing command keeps its current behaviour and output. `plan status`
  and `plan brief` gain lines only when a review exists.
* Python 3.10 is the floor (`grogu_cli.MIN_PYTHON`); the repository runs on
  3.14. No syntax past 3.10, and no `match` on structural patterns that 3.10
  cannot parse.
* Windows: no `fcntl`, no `signal.alarm`, no POSIX-only socket options. Reuse
  `grogu_platform.exclusive_lock` through `PlanStore.locked()`.

## Validation

`python3 -m unittest discover -s tests` must be green. Test modules are the
tester's to write; an engineer's own scratch checks are fine and are not the
proof. The browser behaviour is checked with the Playwright MCP capability in a
fresh automation-owned context, headless, per the operating contract — never
against the user's own browser.

## What is left to the engineer

Internal structure of the Markdown renderer, the exact HTML class names, the
JavaScript module internals beyond the three exported signatures, the wire
format of `anchor_history` entries beyond the fields named, and the copy and
layout of the workspace — the last of those belongs to the designer, whose spec
lands in the design stage and takes precedence over any placeholder you write.
