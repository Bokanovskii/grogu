import copy
import dataclasses
import json
import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401

import grogu_plandoc as plandoc  # noqa: E402
import grogu_plandoc_canon as canon  # noqa: E402
import grogu_plandoc_compile as compiler  # noqa: E402
import grogu_plandoc_revision as revision  # noqa: E402
import grogu_plandoc_schema as schema  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "plandoc"
GOLDEN = FIXTURES / "golden"


def graph_fixture() -> dict:
    return json.loads((FIXTURES / "graph.json").read_text(encoding="utf8"))


def spec(**updates) -> dict:
    value = {
        "role": "engineer",
        "stages": ["implementation"],
        "include": "normative",
        "budget_chars": None,
        "since": "",
        "compiler": 1,
    }
    value.update(updates)
    return value


def _node_kind_graph() -> dict:
    graph = plandoc.new_document("p-golden-kinds", "Every node kind", revision="r0001")
    manifest = {"plandoc": {"counters": {}}}
    order = 1000
    for kind in schema.NODE_KINDS:
        node_id = plandoc.allocate_id(
            manifest, kind, existing_ids=graph["nodes"]
        )
        attrs = {}
        body = f"Body for {kind}."
        geometry = None
        ext = None
        if kind == "directive":
            attrs = {
                "binding": "must",
                "audience": ["reviewer", "engineer"],
                "status": "active",
            }
            ext = {"com.example.node": {"kind": kind}}
        elif kind == "thread":
            attrs = {
                "selector": {"type": "node", "id": "goal-1"},
                "comments": [
                    {
                        "id": "c1",
                        "at": "2026-09-05T20:00:00Z",
                        "author": "reviewer",
                        "body": "Thread body.",
                    }
                ],
                "status": "open",
                "anchor_state": "resolved",
            }
        elif kind == "diagram":
            body = "flowchart LR\n  A --> B\n"
            attrs = {"source": body}
        elif kind == "reference":
            attrs = {"url": "https://example.com"}
        elif kind == "region":
            geometry = {"x": 0, "y": 0, "w": 640, "h": 480, "z": 0}
        graph["nodes"][node_id] = plandoc.make_node(
            node_id,
            kind,
            f"{kind.title()} example",
            stage="implementation",
            body=body,
            attrs=attrs,
            order=order,
            revision="r0001",
            geometry=geometry,
            ext=ext,
        )
        order += 1000
    return schema.validate_document(graph)


def _edge_kind_graph() -> dict:
    graph = plandoc.new_document("p-golden-edges", "Every edge kind", revision="r0001")
    manifest = {"plandoc": {"counters": {}}}
    node_count = len(schema.EDGE_KINDS) * 2
    for number in range(1, node_count + 1):
        node_id = f"note-{number}"
        graph["nodes"][node_id] = plandoc.make_node(
            node_id,
            "note",
            f"Endpoint {number}",
            stage="implementation",
            order=number * 1000,
            revision="r0001",
        )
    for index, kind in enumerate(schema.EDGE_KINDS):
        edge_id = plandoc.allocate_id(
            manifest, "edge", existing_ids=graph["edges"]
        )
        source = f"note-{index * 2 + 1}"
        target = source if kind == "diagram_edge" else f"note-{index * 2 + 2}"
        attrs = (
            {"operator": "-->", "label": "loop", "pair_ordinal": 0, "edge_index": 0}
            if kind == "diagram_edge"
            else {}
        )
        graph["edges"][edge_id] = plandoc.make_edge(
            edge_id,
            kind,
            source,
            target,
            attrs=attrs,
            revision="r0001",
            ext=(
                {"com.example.edge": {"kind": kind}}
                if kind == "depends_on"
                else None
            ),
        )
    return schema.validate_document(graph)


def _nested_region_graph() -> dict:
    graph = plandoc.new_document(
        "p-golden-regions", "Nested regions", revision="r0001"
    )
    for number in range(1, 5):
        graph["nodes"][f"reg-{number}"] = plandoc.make_node(
            f"reg-{number}",
            "region",
            f"Region {number}",
            stage="implementation",
            order=number * 1000,
            revision="r0001",
            geometry={
                "x": number * 20,
                "y": number * 20,
                "w": 800 - number * 100,
                "h": 600 - number * 80,
                "z": number,
            },
        )
    graph["nodes"]["task-1"] = plandoc.make_node(
        "task-1",
        "task",
        "Nested task",
        stage="implementation",
        order=5000,
        revision="r0001",
    )
    for number, (source, target) in enumerate(
        (
            ("reg-1", "reg-2"),
            ("reg-2", "reg-3"),
            ("reg-3", "reg-4"),
            ("reg-4", "task-1"),
        ),
        1,
    ):
        graph["edges"][f"edge-{number}"] = plandoc.make_edge(
            f"edge-{number}",
            "contains",
            source,
            target,
            revision="r0001",
        )
    return schema.validate_document(graph)


def golden_cases() -> dict[str, tuple[dict, dict]]:
    empty = plandoc.new_document("p-golden-empty", "Empty graph", revision="r0001")
    one = plandoc.new_document("p-golden-one", "One node", revision="r0001")
    one["nodes"]["goal-1"] = plandoc.make_node(
        "goal-1",
        "goal",
        "One goal",
        stage="implementation",
        body="A single body.",
        revision="r0001",
    )
    self_loop = plandoc.new_document(
        "p-golden-loop", "Diagram self-loop", revision="r0001"
    )
    self_loop["nodes"]["dia-1"] = plandoc.make_node(
        "dia-1",
        "diagram",
        "Loop",
        stage="implementation",
        body="flowchart LR\n  A --> A\n",
        attrs={"source": "flowchart LR\n  A --> A\n"},
        revision="r0001",
    )
    self_loop["nodes"]["note-1"] = plandoc.make_node(
        "note-1",
        "note",
        "A",
        stage="implementation",
        attrs={"source": "mermaid", "mermaid_id": "A"},
        order=2000,
        revision="r0001",
    )
    self_loop["edges"]["edge-1"] = plandoc.make_edge(
        "edge-1", "contains", "dia-1", "note-1", revision="r0001"
    )
    self_loop["edges"]["edge-2"] = plandoc.make_edge(
        "edge-2",
        "diagram_edge",
        "note-1",
        "note-1",
        attrs={"operator": "-->", "label": "", "pair_ordinal": 0, "edge_index": 0},
        revision="r0001",
    )
    bounded = graph_fixture()
    bounded["nodes"]["note-1"]["body"] = "Long discussion. " * 300
    migrated = compiler.import_markdown(
        (
            ROOT
            / "tests"
            / "fixtures"
            / "review"
            / "stage_with_diagram.md"
        ).read_text(encoding="utf8"),
        plan_id="p-golden-migrated",
    )
    return {
        "empty": (empty, spec(role="reviewer", include="all")),
        "one-node": (one, spec(role="reviewer", include="all")),
        "every-node-kind": (
            _node_kind_graph(),
            spec(role="reviewer", include="all"),
        ),
        "every-edge-kind": (
            _edge_kind_graph(),
            spec(role="reviewer", include="all"),
        ),
        "diagram-self-loop": (
            schema.validate_document(self_loop),
            spec(role="reviewer", include="all"),
        ),
        "nested-region": (
            _nested_region_graph(),
            spec(role="reviewer", include="all"),
        ),
        "bounded-elision": (
            bounded,
            spec(role="reviewer", include="all", budget_chars=2400),
        ),
        "migrated-real-plan": (
            migrated,
            spec(role="reviewer", include="all"),
        ),
    }


def write_golden_corpus() -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for name, (graph, projection_spec) in golden_cases().items():
        directory = GOLDEN / name
        directory.mkdir(parents=True, exist_ok=True)
        result = compiler.compile_checked(graph, projection_spec)
        (directory / "graph.json").write_text(
            json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
            encoding="utf8",
        )
        (directory / "spec.json").write_text(
            json.dumps(projection_spec, ensure_ascii=False, indent=2) + "\n",
            encoding="utf8",
        )
        (directory / "expected.md").write_text(
            result["markdown"], encoding="utf8"
        )
        (directory / "expected.json").write_text(
            json.dumps(
                result["json"],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf8",
        )


class CompilerEquivalenceTests(unittest.TestCase):
    def test_golden_corpus_matches_current_compiler_byte_for_byte(self):
        expected_names = set(golden_cases())
        self.assertEqual(
            {path.name for path in GOLDEN.iterdir() if path.is_dir()},
            expected_names,
        )
        node_graph = json.loads(
            (GOLDEN / "every-node-kind" / "graph.json").read_text()
        )
        edge_graph = json.loads(
            (GOLDEN / "every-edge-kind" / "graph.json").read_text()
        )
        self.assertEqual(
            {node["kind"] for node in node_graph["nodes"].values()},
            set(schema.NODE_KINDS),
        )
        self.assertEqual(
            {edge["kind"] for edge in edge_graph["edges"].values()},
            set(schema.EDGE_KINDS),
        )
        for name in sorted(expected_names):
            with self.subTest(case=name):
                directory = GOLDEN / name
                graph = json.loads(
                    (directory / "graph.json").read_text(encoding="utf8")
                )
                projection_spec = json.loads(
                    (directory / "spec.json").read_text(encoding="utf8")
                )
                result = compiler.compile_checked(graph, projection_spec)
                self.assertEqual(
                    result["markdown"],
                    (directory / "expected.md").read_text(encoding="utf8"),
                )
                self.assertEqual(
                    result["json"],
                    json.loads(
                        (directory / "expected.json").read_text(encoding="utf8")
                    ),
                )
                self.assertIn(
                    f"compiler {compiler.COMPILER_VERSION}",
                    result["markdown"].splitlines()[2],
                )
                self.assertEqual(
                    result["json"]["provenance"]["compiler"],
                    compiler.COMPILER_VERSION,
                )

    def test_all_three_compiler_identities_hold(self):
        graph = graph_fixture()
        original = copy.deepcopy(graph)
        fragment = compiler.scope(graph, spec())
        ir = compiler.project(fragment, spec())
        self.assertEqual(graph, original)
        self.assertEqual(
            compiler.verify_identities(fragment, ir),
            {
                "identity_1": True,
                "identity_2": True,
                "identity_3": True,
                "projection_digest": ir.projection_digest,
            },
        )
        self.assertEqual(
            compiler.lift(compiler.parse(compiler.render(ir))),
            compiler.subtract_elided(fragment, ir.elided),
        )
        self.assertEqual(
            compiler.elements(ir)
            | compiler.elided(fragment, ir),
            compiler.elements(fragment),
        )

    def test_identity_two_can_pass_while_identity_three_fails(self):
        fragment = compiler.scope(graph_fixture(), spec())
        ir = compiler.project(fragment, spec())
        nodes = list(ir.nodes)
        task_index = next(
            index for index, node in enumerate(nodes) if node.id == "task-1"
        )
        nodes[task_index] = dataclasses.replace(
            nodes[task_index], body="A self-consistent but false body"
        )
        tampered = dataclasses.replace(
            ir, nodes=tuple(nodes), projection_digest=""
        )
        tampered = dataclasses.replace(
            tampered,
            projection_digest=compiler.projection_digest(tampered),
        )
        self.assertEqual(
            compiler.parse(compiler.render(tampered)),
            tampered,
            "Identity 2 deliberately still passes",
        )
        with self.assertRaisesRegex(
            compiler.CompileError, "identity 3 graph equivalence"
        ):
            compiler.verify_graph_equivalence(fragment, tampered)

    def test_identity_one_names_a_silently_missing_element(self):
        fragment = compiler.scope(graph_fixture(), spec())
        ir = compiler.project(fragment, spec())
        missing = dataclasses.replace(
            ir,
            nodes=tuple(node for node in ir.nodes if node.id != "risk-1"),
            projection_digest="",
        )
        missing = dataclasses.replace(
            missing,
            projection_digest=compiler.projection_digest(missing),
        )
        with self.assertRaises(compiler.CompileError) as caught:
            compiler.verify_completeness(fragment, missing)
        self.assertIn("risk-1", caught.exception.field)

    def test_forged_elision_cannot_authorize_dropping_required_nodes(self):
        fragment = compiler.scope(graph_fixture(), spec())
        ir = compiler.project(fragment, spec())
        for node_id in ("dir-1", "risk-1", "inv-1"):
            with self.subTest(node_id=node_id):
                node = next(item for item in ir.nodes if item.id == node_id)
                forged = dataclasses.replace(
                    ir,
                    nodes=tuple(item for item in ir.nodes if item.id != node_id),
                    elided=(
                        *ir.elided,
                        compiler.Elision(node_id, node.kind, "node"),
                    ),
                    projection_digest="",
                )
                forged = dataclasses.replace(
                    forged,
                    projection_digest=compiler.projection_digest(forged),
                )
                self.assertEqual(
                    compiler.parse(compiler.render(forged)),
                    forged,
                    "the forged artifact remains internally invertible",
                )
                with self.assertRaisesRegex(
                    compiler.CompileError, "deterministic projection policy"
                ):
                    compiler.verify_identities(fragment, forged)

    def test_unknown_node_and_edge_kinds_fail_closed_in_scope_and_render(self):
        graph = graph_fixture()
        graph["nodes"]["future-1"] = {
            **copy.deepcopy(graph["nodes"]["note-1"]),
            "id": "future-1",
            "kind": "future",
        }
        with mock.patch.dict(
            schema.NODE_KINDS,
            {"future": {"prefix": "future", "normative": False}},
        ):
            validated = schema.validate_document(graph)
            with self.assertRaisesRegex(compiler.CompileError, "no dispatch"):
                compiler.scope(graph, spec())
            fragment = compiler.GraphFragment(
                document_json=canon.dumps(validated),
                spec_json=canon.dumps(
                    schema.validate_projection_spec(spec())
                ),
                source_digest=canon.digest(validated),
            )
            with self.assertRaisesRegex(compiler.CompileError, "no dispatch"):
                compiler.project(fragment, spec())

        graph = graph_fixture()
        graph["edges"]["edge-99"] = {
            "id": "edge-99",
            "kind": "future_edge",
            "from": "task-2",
            "to": "task-1",
            "attrs": {},
            "created_rev": "r0007",
        }
        with mock.patch.object(
            schema, "EDGE_KINDS", (*schema.EDGE_KINDS, "future_edge")
        ):
            with self.assertRaisesRegex(compiler.CompileError, "no dispatch"):
                compiler.scope(graph, spec())

        ir = compiler.project(graph_fixture(), spec())
        bad_node = dataclasses.replace(ir.nodes[0], kind="future")
        bad = dataclasses.replace(
            ir, nodes=(bad_node, *ir.nodes[1:]), projection_digest=""
        )
        bad = dataclasses.replace(
            bad, projection_digest=compiler.projection_digest(bad)
        )
        with self.assertRaisesRegex(compiler.CompileError, "no dispatch"):
            compiler.render(bad)

    def test_reverse_dns_extensions_round_trip_through_every_compiler_layer(self):
        graph = graph_fixture()
        fragment = compiler.scope(graph, spec())
        self.assertEqual(
            fragment.nodes["dir-1"]["ext"]["com.example.tool"]["opaque"],
            ["preserved", 7],
        )
        ir = compiler.project(fragment, spec())
        markdown = compiler.render(ir)
        self.assertIn("## Extensions", markdown)
        self.assertIn('"com.example.tool"', markdown)
        self.assertIn('"dev.grogu.review"', markdown)
        parsed = compiler.parse(markdown)
        lifted = compiler.lift(parsed)
        expected = compiler.subtract_elided(fragment, ir.elided)
        self.assertEqual(lifted, expected)
        twin = compiler.render_json(parsed)
        directive = next(node for node in twin["nodes"] if node["id"] == "dir-1")
        edge = next(item for item in twin["edges"] if item["id"] == "edge-1")
        self.assertEqual(directive["ext"], graph["nodes"]["dir-1"]["ext"])
        self.assertEqual(edge["ext"], graph["edges"]["edge-1"]["ext"])

    def test_parse_render_identity_and_json_twin_fact_identity(self):
        ir = compiler.project(graph_fixture(), spec())
        markdown = compiler.render(ir)
        self.assertEqual(compiler.parse(markdown), ir)
        self.assertTrue(compiler.verify_equivalence(ir))

        twin = compiler.render_json(ir)
        self.assertEqual(twin["projection_digest"], ir.projection_digest)
        self.assertEqual(
            [
                (
                    item["id"],
                    item["kind"],
                    item["title"],
                    item["body"],
                    item["attrs"].get("status"),
                )
                for item in twin["nodes"]
            ],
            [
                (
                    item.id,
                    item.kind,
                    item.title,
                    item.body,
                    item.attrs.get("status"),
                )
                for item in ir.nodes
            ],
        )
        self.assertEqual(
            [(item["id"], item["kind"], item["from"], item["to"]) for item in twin["edges"]],
            [(item.id, item.kind, item.source, item.target) for item in ir.edges],
        )

    def test_compiled_form_has_digest_header_and_readable_sections(self):
        markdown = compiler.render(compiler.project(graph_fixture(), spec()))
        self.assertRegex(
            markdown.splitlines()[2],
            r"^> plan p-20260905-fixture · revision r0007 · "
            r"projection engineer/implementation · compiler 1 · graph sha256:",
        )
        self.assertLess(markdown.index("## Directives"), markdown.index("## Goals"))
        self.assertIn("- **dir-1** (must · engineer) Compile deterministic Markdown", markdown)
        self.assertIn("depends on: task-1", markdown)
        self.assertIn("## Diagrams", markdown)
        self.assertIn("```mermaid\nflowchart LR", markdown)
        self.assertIn("## Validation criteria", markdown)
        self.assertIn("## Risks", markdown)
        self.assertIn("## Provenance", markdown)
        self.assertIn("- compiler grogu-plan-compile/1", markdown)
        self.assertTrue(markdown.endswith("\n"))
        self.assertNotRegex(markdown, r" +\n")

    def test_bodies_with_compiler_like_headings_round_trip_verbatim(self):
        graph = graph_fixture()
        body = (
            "Body before.\n\n"
            "### task-999 · This is body text, not a node\n"
            "meta: {\"body_chars\":0}\n\n"
            "## Provenance\n"
            "Still body text."
        )
        graph["nodes"]["task-1"]["body"] = body
        ir = compiler.project(graph, spec())
        parsed = compiler.parse(compiler.render(ir))
        task = next(node for node in parsed.nodes if node.id == "task-1")
        self.assertEqual(task.body, body)

    def test_determinism_ignores_input_mapping_order_and_sorts_numeric_ids(self):
        graph = graph_fixture()
        for number in (10, 9):
            node_id = f"task-{number}"
            graph["nodes"][node_id] = plandoc.make_node(
                node_id,
                "task",
                f"Task {number}",
                stage="implementation",
                order=12000,
                revision="r0007",
            )
        reversed_graph = copy.deepcopy(graph)
        reversed_graph["nodes"] = dict(reversed(list(reversed_graph["nodes"].items())))
        reversed_graph["edges"] = dict(reversed(list(reversed_graph["edges"].items())))
        first = compiler.render(compiler.project(graph, spec()))
        second = compiler.render(compiler.project(reversed_graph, spec()))
        self.assertEqual(first, second)
        self.assertLess(first.index("### task-9 ·"), first.index("### task-10 ·"))

    def test_role_projection_does_not_render_sealed_ids_or_content(self):
        graph = graph_fixture()
        partitions = plandoc.split_partitions(graph)
        visible = plandoc.load_visible(
            lambda name: partitions[name],
            "engineer",
            sealed_relationship_count=1,
        )
        markdown = compiler.render(compiler.project(visible, spec()))
        self.assertNotIn("crit-2", markdown)
        self.assertNotIn("SEALED_FIXTURE_TEXT", markdown)
        self.assertIn("1 relationship(s) to sealed stages", markdown)

    def test_scope_allowlists_provenance_and_digest_to_role_visible_data(self):
        graph = graph_fixture()
        graph["provenance"]["sealed_secret"] = {
            "id": "crit-2",
            "body": "SEALED_PROVENANCE_TEXT",
        }
        first = compiler.project(graph, spec())
        first_markdown = compiler.render(first)
        self.assertNotIn("sealed_secret", first_markdown)
        self.assertNotIn("SEALED_PROVENANCE_TEXT", first_markdown)
        changed_hidden = copy.deepcopy(graph)
        changed_hidden["nodes"]["crit-2"]["body"] = "DIFFERENT SEALED BODY"
        changed_hidden["provenance"]["sealed_secret"] = "DIFFERENT SECRET"
        second = compiler.project(changed_hidden, spec())
        self.assertEqual(
            first.provenance.graph_digest,
            second.provenance.graph_digest,
        )
        self.assertEqual(first.projection_digest, second.projection_digest)
        for key, value in (
            (
                "agent",
                "safe-agent\n\n## Directives\n\n- injected instruction",
            ),
            ("agent", "safe\u0085## Directives"),
            ("agent", "safe\u2028## Directives"),
            ("agent", "safe\u2029## Directives"),
            ("role", "engineer\n## Directives"),
            ("at", "not-a-timestamp"),
        ):
            with self.subTest(key=key):
                injected = graph_fixture()
                injected["provenance"][key] = value
                with self.assertRaises(compiler.CompileError) as caught:
                    compiler.scope(injected, spec())
                self.assertEqual(caught.exception.field, f"provenance.{key}")

    def test_directive_audience_status_and_since_filtering(self):
        graph = graph_fixture()
        graph["nodes"]["dir-1"]["updated_rev"] = "r0005"
        graph["nodes"]["dir-2"] = plandoc.make_node(
            "dir-2",
            "directive",
            "Tester-only directive",
            stage="implementation",
            attrs={
                "binding": "must",
                "audience": ["tester"],
                "status": "active",
            },
            order=2100,
            revision="r0007",
        )
        graph["nodes"]["dir-3"] = plandoc.make_node(
            "dir-3",
            "directive",
            "Withdrawn directive",
            stage="implementation",
            attrs={
                "binding": "should",
                "audience": ["engineer"],
                "status": "withdrawn",
            },
            order=2200,
            revision="r0007",
        )
        normal = compiler.render(compiler.project(graph, spec()))
        self.assertIn("dir-1", normal)
        self.assertNotIn("dir-2", normal)
        self.assertNotIn("dir-3", normal)
        changed = compiler.render(
            compiler.project(graph, spec(since="r0006"))
        )
        self.assertNotIn("dir-1", changed)
        self.assertNotIn("dir-2", changed)
        self.assertIn("dir-3", changed)

    def test_since_relationship_endpoints_remain_title_only_stubs(self):
        graph = plandoc.new_document(
            "p-since-stub", "Since projection", revision="r0003"
        )
        graph["nodes"]["goal-1"] = plandoc.make_node(
            "goal-1",
            "goal",
            "Changed goal",
            stage="implementation",
            body="New changed body.",
            revision="r0003",
        )
        graph["nodes"]["goal-2"] = plandoc.make_node(
            "goal-2",
            "goal",
            "Unchanged related goal",
            stage="implementation",
            body="OLD UNCHANGED BODY THAT MUST STAY OUT",
            revision="r0001",
        )
        graph["edges"]["edge-1"] = plandoc.make_edge(
            "edge-1",
            "depends_on",
            "goal-1",
            "goal-2",
            revision="r0003",
        )
        projection_spec = spec(since="r0002")
        fragment = compiler.scope(graph, projection_spec)
        self.assertEqual(fragment.relationship_stub_ids, ("goal-2",))
        ir = compiler.project(fragment, projection_spec)
        old = next(node for node in ir.nodes if node.id == "goal-2")
        self.assertEqual(old.body_state, "projection")
        self.assertEqual(old.body, "")
        self.assertNotIn(
            "OLD UNCHANGED BODY THAT MUST STAY OUT", compiler.render(ir)
        )
        self.assertIn(
            compiler.Elision("goal-2", "goal", "projection body"),
            ir.elided,
        )

    def test_budget_elision_is_deterministic_and_never_drops_required_kinds(self):
        graph = graph_fixture()
        graph["nodes"]["note-1"]["body"] = "discussion " * 500
        graph["nodes"]["ref-1"] = plandoc.make_node(
            "ref-1",
            "reference",
            "External reference",
            stage="implementation",
            body="reference body " * 300,
            attrs={"url": "https://example.com"},
            order=12500,
            revision="r0007",
        )
        ir = compiler.project(
            graph, spec(include="all", budget_chars=2200)
        )
        markdown = compiler.render(ir)
        self.assertTrue(ir.elided)
        self.assertIn("## Elided", markdown)
        self.assertIn("risk-1", {node.id for node in ir.nodes})
        self.assertIn("crit-1", {node.id for node in ir.nodes})
        self.assertIn("dir-1", {node.id for node in ir.nodes})
        self.assertEqual(compiler.parse(markdown), ir)

        impossible = compiler.project(graph, spec(budget_chars=200))
        self.assertTrue(impossible.provenance.budget_exceeded)
        self.assertIn("budget 200 characters (exceeded)", compiler.render(impossible))

    def test_tampering_reports_the_first_equivalence_boundary(self):
        markdown = compiler.render(compiler.project(graph_fixture(), spec()))
        tampered = markdown.replace("Build the graph core", "Build a different core", 1)
        with self.assertRaises(compiler.CompileError) as caught:
            compiler.parse(tampered)
        self.assertIn(caught.exception.field, {"projection_digest", "markdown"})

    def test_projection_cache_key_is_content_addressed(self):
        first = compiler.cache_key("r0007", spec())
        reordered = {
            "compiler": 1,
            "since": "",
            "budget_chars": None,
            "include": "normative",
            "stages": ["implementation"],
            "role": "engineer",
        }
        self.assertEqual(first, compiler.cache_key("r0007", reordered))
        self.assertNotEqual(first, compiler.cache_key("r0008", reordered))

    def test_warm_cache_hit_is_one_file_read_without_graph_load_or_parse(self):
        directory = ROOT / "tests" / f".plandoc-cache-{uuid.uuid4().hex}"
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory, True)
        projection_spec = spec()
        ir = compiler.project(graph_fixture(), projection_spec)
        cache_file = compiler.write_cache(directory, ir, projection_spec)
        original_read = revision.safe_read
        with (
            mock.patch.object(revision, "safe_read", wraps=original_read) as read_spy,
            mock.patch.object(
                compiler, "scope", side_effect=AssertionError("graph loaded")
            ),
            mock.patch.object(
                compiler, "parse", side_effect=AssertionError("Markdown parsed")
            ),
        ):
            cached = compiler.read_cache(
                directory, "r0007", projection_spec
            )
        self.assertEqual(read_spy.call_count, 1)
        self.assertEqual(cached.etag, ir.projection_digest)
        self.assertEqual(cached.payload.decode("utf8"), compiler.render(ir))
        with mock.patch.object(compiler, "COMPILER_VERSION", 2):
            self.assertIsNone(
                compiler.read_cache(directory, "r0007", projection_spec)
            )
        raw = cache_file.read_bytes()
        cache_file.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        with self.assertRaisesRegex(compiler.CompileError, "payload digest"):
            compiler.read_cache(directory, "r0007", projection_spec)


class MarkdownImporterTests(unittest.TestCase):
    def test_legacy_sections_and_nested_markdown_are_not_dropped(self):
        source = (FIXTURES / "legacy.md").read_text(encoding="utf8")
        document = compiler.import_markdown(
            source,
            plan_id="p-legacy",
            stage="implementation",
            revision="r0001",
        )
        note_bodies = [
            node["body"]
            for node in document["nodes"].values()
            if node["kind"] == "note"
            and node["attrs"].get("source") == "markdown-import"
        ]
        self.assertTrue(any("Introductory prose" in body for body in note_bodies))
        self.assertTrue(any("### A nested heading" in body for body in note_bodies))
        self.assertTrue(any("This heading is not part" in body for body in note_bodies))

    def test_heading_like_text_inside_code_fences_does_not_split_sections(self):
        source = (
            "# Legacy\n\n"
            "## Real section\n\n"
            "```text\n"
            "## Not a section\n"
            "```\n\n"
            "After the fence.\n"
        )
        document = compiler.import_markdown(source, plan_id="p-fenced")
        imported = [
            node
            for node in document["nodes"].values()
            if node["attrs"].get("source") == "markdown-import"
        ]
        self.assertEqual([node["title"] for node in imported], ["Real section"])
        self.assertIn("## Not a section", imported[0]["body"])
        self.assertIn("After the fence.", imported[0]["body"])

    def test_legacy_mermaid_populates_diagram_nodes_and_dependency_edges(self):
        source = (FIXTURES / "legacy.md").read_text(encoding="utf8")
        document = compiler.import_markdown(source, plan_id="p-legacy")
        diagrams = [
            node for node in document["nodes"].values() if node["kind"] == "diagram"
        ]
        self.assertEqual(len(diagrams), 1)
        self.assertTrue(diagrams[0]["attrs"]["parsed"]["supported"])
        contains = [
            edge for edge in document["edges"].values() if edge["kind"] == "contains"
        ]
        diagram_edges = [
            edge
            for edge in document["edges"].values()
            if edge["kind"] == "diagram_edge"
        ]
        self.assertGreaterEqual(len(contains), 4)
        self.assertEqual(len(diagram_edges), 2)

    def test_parse_legacy_then_render_is_a_stable_semantic_projection(self):
        source = (FIXTURES / "legacy.md").read_text(encoding="utf8")
        ir = compiler.parse(source)
        compiled = compiler.render(ir)
        self.assertEqual(compiler.parse(compiled), ir)
        self.assertEqual(compiler.render(compiler.parse(compiled)), compiled)

    def test_diagram_self_edges_are_preserved_as_non_normative_diagram_edges(self):
        source = (
            ROOT / "tests" / "fixtures" / "review" / "stage_with_diagram.md"
        ).read_text(encoding="utf8")
        document = compiler.import_markdown(source, plan_id="p-review-fixture")
        self_edges = [
            edge
            for edge in document["edges"].values()
            if edge["kind"] == "diagram_edge" and edge["from"] == edge["to"]
        ]
        self.assertEqual(len(self_edges), 1)
        self.assertEqual(self_edges[0]["attrs"]["operator"], "-->")
        self.assertEqual(self_edges[0]["attrs"]["label"], "retry")
        self.assertEqual(
            plandoc.impact(
                document,
                {"type": "node", "id": self_edges[0]["from"]},
            )["cycles"],
            [[self_edges[0]["from"], self_edges[0]["from"]]],
        )
        with self.assertRaisesRegex(plandoc.PlanDocumentError, "cannot promote"):
            plandoc.promote_diagram_edge(document, self_edges[0]["id"])
        ordinary = next(
            edge
            for edge in document["edges"].values()
            if edge["kind"] == "diagram_edge" and edge["from"] != edge["to"]
        )
        promoted = plandoc.promote_diagram_edge(document, ordinary["id"])
        self.assertEqual(promoted["edges"][ordinary["id"]]["kind"], "depends_on")
        self.assertEqual(
            document["edges"][ordinary["id"]]["kind"], "diagram_edge"
        )

    def test_strict_equivalence_is_default_and_can_be_disabled_explicitly(self):
        previous = os.environ.pop(compiler.STRICT_ENV, None)
        try:
            self.assertTrue(compiler.strict_enabled())
            os.environ[compiler.STRICT_ENV] = "0"
            self.assertFalse(compiler.strict_enabled())
            with mock.patch.object(
                compiler, "parse", wraps=compiler.parse
            ) as parse_spy:
                compiler.project(graph_fixture(), spec())
            self.assertGreaterEqual(
                parse_spy.call_count,
                1,
                "Identity 2 always runs even when identities 1 and 3 are skipped",
            )
        finally:
            if previous is None:
                os.environ.pop(compiler.STRICT_ENV, None)
            else:
                os.environ[compiler.STRICT_ENV] = previous


if __name__ == "__main__":
    unittest.main()
