# Interactive plan review workspace — testing plan

You are testing a feature whose whole value is that a user's words about a plan
survive contact with the plan changing, and whose whole risk is that a web
server on the user's machine is reachable by a web page they did not open. Test
those two things hardest.

Assume the implementation is wrong until its behaviour says otherwise. In
particular, do not accept "the code does X" as evidence for anything below; run
it.

## How to run everything

```
python3 -m unittest discover -s tests
```

is the whole-suite command and it must be green at the end. Targeted runs while
working:

```
python3 -m unittest tests.test_grogu_markdown -v
python3 -m unittest tests.test_grogu_mermaid -v
python3 -m unittest tests.test_grogu_review -v
python3 -m unittest tests.test_grogu_review_server -v
python3 -m unittest tests.test_grogu_cli -v
python3 -m unittest tests.test_grogu_plans -v
```

`tests/_sandbox.py` is imported before any test module and redirects
`GROGU_HOME` and `HOME`. Every new test module must import it the same way the
existing ones do, and must build its plan store inside a
`tempfile.TemporaryDirectory` git repository. A test that touches the
developer's real `~/.grogu` is a failure regardless of its assertions — harness
friction #71 is exactly this, and it is already in the record once.

Server tests bind an ephemeral port on `127.0.0.1` and must always shut the
server down in `addCleanup`, not at the end of the test body. A leaked
`ThreadingHTTPServer` makes the rest of the run non-deterministic.

## Termination rule — read this before writing a single assertion

The list below is **closed**. It is 46 numbered checks. This stage is complete
when every numbered check has a named test that exercises it and the whole
suite passes. It is not complete-by-degree and it does not grow by inspection:
if you find a real defect outside the list, file it with
`grogu plan defect` and let the architect decide whether the list changes —
do not silently extend the list and do not treat "another assertion could be
added" as an incomplete stage.

Do not assert on the exact wording of prose, error messages or copy. Assert on
behaviour, structure, status codes, and the presence of the specific
identifiers named below. This repository has already paid for the other
approach once: a testing plan that asked for an assertion per prose clause ran
three defect rounds without the gap list shrinking, against a correct
implementation. Where a message matters, assert that it names a required
*token* — a thread id, a stage name, a version string, a count — not that it
reads a particular way.

## A. Markdown rendering and source mapping — `tests/test_grogu_markdown.py`

The source-span invariant is the foundation of every text anchor. If it is
wrong by one character, every comment in the product lands in the wrong place,
and nothing else in the suite would notice.

1. **Byte identity.** For each fixture document, parse the rendered HTML,
   collect every element carrying `data-s`/`data-e`, and assert
   `source[int(s):int(e)] == element.text_content()` for every one of them.
   Run this over: each file in `tests/fixtures/review/`, the output of
   `grogu_plans.design_template("X")`, and the implementation stage body of a
   plan created in the test. Use `html.parser` from the standard library; do
   not add a parser dependency to make the test convenient.
2. **Atomic escapes.** A document containing `a < b & c > d "e"` renders each
   escaped character in its own element with `data-atomic="1"`, and the byte
   identity check in (1) still passes over the whole document.
3. **Closed tag set.** For every fixture plus an adversarial document
   containing `<script>alert(1)</script>`, `<img src=x onerror=alert(1)>`,
   `<iframe src=…>`, an HTML comment, and a stray `<` — every tag name in the
   output is in `grogu_markdown.ALLOWED_TAGS`. Assert specifically that the
   strings `<script`, `<iframe`, `<img` and `onerror` do not appear in the
   output, and that the literal text `<script>alert(1)</script>` *is* present
   as escaped text so the reader can see what the plan said.
4. **No images.** `![alt](https://example.com/x.png)` produces no `img`
   element, and the URL appears as text.
5. **Link schemes.** `[a](https://x)`, `[b](mailto:a@b)`, `[c](#frag)` render
   as `a` elements; `[d](javascript:alert(1))`, `[e](data:text/html,…)` and
   `[f](vbscript:…)` render with no `a` element and no `href` attribute
   anywhere in that block. Every emitted `a` carries `rel` containing both
   `noreferrer` and `noopener`.
6. **Subset coverage.** A document exercising every supported construct —
   headings h1–h6, paragraphs, `-`/`*`/`+` and ordered lists with one nested
   level, fenced code with and without an info string, indented code, block
   quotes, thematic breaks, a pipe table with a header separator, inline code,
   strong, emphasis, links, autolinks — renders each to its documented element,
   and byte identity from (1) holds throughout.
7. **Unsupported constructs render literally.** A setext heading, a footnote
   reference and a definition list appear as ordinary text, not as dropped
   content. Assert the source text is present in the output.
8. **`code_blocks` accuracy.** For a document with three fenced blocks, one of
   them `mermaid`, `render_document()["code_blocks"]` has three entries in
   source order with correct `lang`, and `source[body_start:body_end]` equals
   the fence's inner text exactly, with no fence line and no trailing newline
   ambiguity.
9. **Idempotent and total.** `render()` returns a string and raises nothing for:
   the empty string, a string of only whitespace, a 200 KB document, a document
   with CRLF line endings, one with a lone `\r`, one with a BOM, one with
   astral-plane characters (an emoji and a CJK ideograph), and one with an
   unterminated fence. For the astral case, assert byte identity from (1) still
   holds — Python string offsets are code points and the browser's are UTF-16
   code units, so if the implementation ever grows a length calculation, this
   is where it breaks.

## B. Mermaid source parsing — `tests/test_grogu_mermaid.py`

10. **Node shapes.** A flowchart declaring `A[sq]`, `B(round)`, `C((circle))`,
    `D{rhombus}`, `E>flag]`, `F[[sub]]`, `G[(db)]`, `H([stadium])` yields eight
    nodes with those ids, the labels without brackets or quotes, and a `shape`
    value per node. Ids are exactly as written; labels are exactly the inner
    text.
11. **Edge kinds and labels.** `-->`, `---`, `-.->`, `-.-`, `==>`, `===`,
    `--x`, `--o`, `A -->|yes| B`, and `A -- text --> B` all parse, with `label`
    set where one was given and empty where it was not.
12. **Chains.** `A --> B --> C` yields exactly two edges, `A→B` and `B→C`, with
    `edge_index` 0 and 1, and registers three nodes.
13. **Ordinals.** A diagram with `A-->B` twice and `A-->C` once yields
    `pair_ordinal` 0 and 1 for the two `A→B` edges and 0 for `A→C`, with
    `edge_index` in declaration order across all three.
14. **Referenced-only nodes.** `graph LR` with only `A --> B` registers nodes
    `A` and `B` with empty labels.
15. **Subgraphs.** `subgraph s1 [Server]` … `end` records the subgraph and sets
    `subgraph: "s1"` on the nodes declared inside it and empty on those outside.
16. **Noise is skipped, not counted.** A diagram containing `%% comment`,
    `%%{init: {...}}%%`, `classDef`, `class`, `style`, `linkStyle`, `click`,
    `direction`, `accTitle` and `accDescr` parses with `unparsed == 0`.
17. **Unsupported diagram types.** `sequenceDiagram`, `classDiagram`,
    `stateDiagram-v2`, `erDiagram`, `gantt` and `journey` each set `type`
    correctly, `supported == False`, and return empty `nodes` and `edges`.
18. **Partial.** A flowchart where more than 20% of non-empty, non-directive
    lines are gibberish sets `partial == True`; one where fewer are does not.
19. **Never raises.** `parse()` returns a dict for: empty string, only
    whitespace, only `%%` comments, 5000 random lines, unbalanced brackets, a
    `subgraph` with no `end`, and a 500 KB input. Assert it returns within a
    couple of seconds — a catastrophic-backtracking regex here is a hang in the
    server's request thread.

## C. Anchors and re-anchoring — `tests/test_grogu_review.py`

This section is where a plausible-looking implementation is most likely to be
wrong, because every one of these cases *looks* like it works when you try it
by hand on the easy input.

20. **Anchor construction.** `text_anchor(body, start, end, …)` records
    `exact == body[start:end]`, `prefix` equal to the up-to-32 characters
    before `start`, `suffix` equal to the up-to-32 after `end`, and a
    `body_digest` matching `digest(body)`. At the very start and very end of a
    body the context is shorter and the call still succeeds.
21. **Unmoved.** Re-anchoring against a byte-identical body returns state
    `anchored`, confidence 1.0, and offsets unchanged.
22. **Shift by insertion.** Insert 500 characters before the anchored region;
    the anchor is `anchored` with new offsets satisfying
    `new_body[start:end] == exact`.
23. **Duplicate quote disambiguated.** A body where `exact` appears three
    times, with the original occurrence distinguishable only by its prefix and
    suffix: assert the chosen offsets are the *original* occurrence's, not the
    first one in the document. Then construct the pathological case where all
    three occurrences have identical surrounding context and assert the result
    is `shifted` (not `anchored`) and lands on the occurrence nearest the old
    offset.
24. **Fuzzy.** Change roughly one word in eight inside the anchored region;
    assert `shifted` with confidence between `FUZZY_MIN_RATIO` and 1.0, and
    that the new region overlaps the edited text. Then change most of it and
    assert `orphaned`.
25. **Orphaned preserves evidence.** After the quoted text is deleted outright,
    the thread still exists, its `anchor_state` is `orphaned`, and `exact`,
    `prefix` and `suffix` are byte-identical to what they were before. Assert
    the thread count before and after is equal — nothing is ever removed (I6).
26. **Threshold boundary.** Construct an edit whose best `SequenceMatcher`
    ratio is just below `FUZZY_MIN_RATIO` and one just above; assert
    `orphaned` and `shifted` respectively. This pins the constant to a
    behaviour rather than to a number in the source.
27. **Large-body bypass.** With a body over `FUZZY_MAX_BODY`, a quote that
    would only match fuzzily returns `orphaned` and the call completes quickly
    (assert an elapsed-time bound, generously — the point is that step 4 was
    skipped, not that the machine is fast).
28. **History.** Each transition appends one entry to `anchor_history` with
    `from_revision`, `to_revision`, `state` and `confidence`; a re-sync that
    finds no change appends nothing.
29. **Mermaid node re-anchoring.** Rename a node's *label* and keep its id →
    `anchored`. Change its *id* and keep a unique matching label → `shifted`
    with the new id recorded. Delete the node → `orphaned`.
30. **Mermaid edge re-anchoring.** Same edge → `anchored`. Same `(from, to)`
    but a different ordinal → `shifted`. Endpoint deleted → `orphaned`. A
    second diagram inserted *before* the anchored one, shifting `block_index`,
    still resolves by `block_digest` → `anchored`.
31. **Cross-stage isolation.** A thread on `design` is unaffected when
    `implementation` is rewritten, and its `anchor_revision` does not move.

## D. Review store, rounds and privacy — `tests/test_grogu_review.py`

32. **Round lifecycle.** First thread opens round 1. `request_changes` sets
    `changes_requested` and records exactly **one** steering note — assert
    `len(manifest["steering"])` grew by exactly 1, that its `requires_replan`
    is true, that the plan status is `needs_review`, and that the note text
    contains every open thread id. Rewriting the affected stage and re-syncing
    moves the round to `answered`; the next new thread opens round 2.
33. **Gate coupling is inherited, not reimplemented.** After
    `request_changes`, `PlanStore.gate(plan, "implement")["allowed"]` is false.
    Assert `grogu_review` contains no gate logic of its own: the blocking must
    come from `requires_replan` → `needs_review`, which the existing gate
    already treats as blocking.
34. **Approval refusals.** `ReviewStore.approve` with an open thread and
    `confirm_open=False` raises and the plan status is unchanged; with
    `confirm_open=True` it approves. With `GROGU_ROLE=architect` set in the
    environment it raises and the plan is **not** approved — this is I4 and it
    is the single most important assertion in this section. With a pending
    amendment it raises, from `PlanStore.approve`'s own check.
35. **No new approval surface.** Assert the built `argparse` parser has no
    `--as-user` option on any `review` subcommand, and that no `review` handler
    mutates `GROGU_ROLE`. Grep the source of `grogu_review.py`,
    `grogu_review_server.py` and the `review` handlers for assignment to
    `os.environ["GROGU_ROLE"]` and assert there is none.
36. **Comment validation.** Empty or whitespace-only body raises; a body over
    `MAX_COMMENT_CHARS` raises; thread ids are `c1`, `c2`, … monotonic and are
    not reused after a thread is resolved; replying to an unknown thread id
    raises.
37. **Atomicity and concurrency.** Two `ReviewStore` instances over the same
    plan adding a thread each from two threads both survive: the file has two
    threads with distinct ids. Assert no `.tmp` file remains in the plan
    directory afterwards.
38. **Schema guard.** A `review.json` whose `schema_version` is 99 causes a
    `ReviewError` naming `99` on load, and the file on disk is byte-identical
    afterwards.
39. **Git-ignored, and the migration.** In a temp git repository: after any
    review write, `.grogu/plans/.gitignore` contains `review.json`, and
    `git status --porcelain` lists no `review.json`. Separately, pre-create the
    marker with only the old three lines (`manifest.json`, `revisions/`,
    `*.sealed`) plus an unrelated custom line, run a store operation, and
    assert the file now contains all four required entries **and** still
    contains the custom line. This is the migration in I7 and a repository that
    has ever run an older Grogu is the only place it matters.
40. **Finalize never ships it.** Create a plan with threads, complete the
    stages, run `PlanStore.finalize`, and assert `review.json` is not in
    `git diff --cached --name-only`.

## E. Server security and role isolation — `tests/test_grogu_review_server.py`

Drive these with `http.client` against the real running server. Do not test the
handler functions in isolation; the whole point is the envelope around them.

41. **Rejects hostile framing.** Each of the following returns 403 and no body
    containing plan text: `Host: evil.example.com`; `Host: 127.0.0.1:1`
    (wrong port); `Origin: https://evil.example.com`;
    `Sec-Fetch-Site: cross-site`; a `POST /api/threads` without the
    `X-Grogu-Review` header; any `/api/*` request with no session cookie; a
    second `GET /?t=<token>` reusing an already-spent token. Also assert that a
    *valid* request with `Host: localhost:<port>` and no `Origin` succeeds, so
    the allowlist is not simply refusing everything.
42. **No CORS, and the right headers.** No response ever carries a header
    beginning `Access-Control-`. Every response — the index, a static file, a
    JSON API response, and a 403 — carries `Content-Security-Policy`,
    `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff` and
    `Cache-Control: no-store`. Assert the CSP contains `default-src 'none'`,
    `connect-src 'self'` and `frame-ancestors 'none'`, and that `script-src`
    does **not** contain `'unsafe-inline'` or `'unsafe-eval'`.
43. **Loopback only.** The bound address is `127.0.0.1`. Assert the server is
    not reachable on a non-loopback address of this machine (resolve one via
    `socket`; skip the test rather than fail it when the host has none). Assert
    `--host 0.0.0.0` is refused with a usage error and no socket is opened.
44. **Path traversal.** `GET /static/../../../../etc/passwd`,
    `GET /static/..%2f..%2fgrogu_plans.py`, `GET /static/` and
    `GET /vendor/../../../../etc/passwd` all return 403 or 404 and never the
    contents of a file outside the workspace directory. Assert the response
    body does not contain `PlanStore` (which would mean `src/` leaked).
45. **Role isolation end to end — the seal.** Start the server with
    `GROGU_ROLE=engineer` on a plan whose testing and evaluation stages contain
    a distinctive sentinel string. Assert: `GET /api/plan` returns stages
    `design` and `implementation` only; `readable_stages` does not contain
    `testing`; and **the sentinel does not appear anywhere in the raw response
    bytes**. Then assert the same for `role=tester` (implementation absent) and
    that with no `GROGU_ROLE` all four stages are present. Also assert the
    server never sets or clears `GROGU_ROLE` in its own environment, and that
    it does set `GROGU_AGENT` only when it was previously unset.
46. **Lifecycle and limits.** A body over 64 KiB is rejected with 400 and the
    server stays up. `POST /api/shutdown` stops it and the port stops
    accepting. A server constructed with a 1-second idle timeout exits on its
    own. The token, the cookie value and a comment's text never appear in
    captured stdout or stderr for a normal session.

## F. CLI surface — `tests/test_grogu_cli.py`

Fold these into the existing CLI test conventions (invoking
`grogu_cli.main([...])` with captured output, the pattern already used there).

* `review` is in `GROGU_COMMANDS`, so `main(["review", …])` is parsed rather
  than handed to Copilot.
* Every subcommand named in the implementation plan parses, accepts the plan id
  both positionally and as `--plan`, and resolves it from `GROGU_PLAN`.
* `review list --json` and `review status --json` emit valid JSON whose top
  level contains the keys named in the implementation plan.
* `ReviewError` exits 3, matching `PlanError`; a usage error exits 2.
* `review comment --quote` with text absent from the stage exits non-zero and
  names the stage; with text occurring twice it exits non-zero and names the
  count `2`.
* `plan status` on a plan with threads includes a `review:` line with the round
  number and open count; on a plan with none, output is byte-identical to
  before the feature (capture it from a plan created without any review).
* `plan brief --role architect` on a plan with open threads includes their
  thread ids; without any, the brief is unchanged.
* `review assets` with nothing installed reports so and exits 0; `--install
  --from <file>` with a file whose digest is wrong refuses, installs nothing,
  and exits non-zero; with the correct digest it installs and a second run
  reports it present. Do **not** hit the network in any test — use `--from`
  with a fixture file and a monkeypatched expected digest.

## G. Regression

* The full suite passes: `python3 -m unittest discover -s tests`. Record the
  test count and the command in the completion note.
* No existing test was modified to accommodate the new code. If one had to
  change, say which and why in the completion note; an existing assertion
  weakened to make new code pass is a defect, not a test update.

## What counts as proof

For each numbered check, the test method name and the module it lives in. For
G, the captured tail of the suite run showing the count and `OK`. A statement
that something "was verified manually" is not proof for anything in A–F; every
one of them is mechanisable and must be mechanised.

Report anything the plan left ambiguous with `grogu plan defect` rather than
choosing an interpretation and testing that.
