import copy
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401

import grogu_plandoc as plandoc  # noqa: E402
import grogu_plandoc_compile as compiler  # noqa: E402
import grogu_plandoc_schema as schema  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "plandoc"


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


class CompilerEquivalenceTests(unittest.TestCase):
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
        finally:
            if previous is None:
                os.environ.pop(compiler.STRICT_ENV, None)
            else:
                os.environ[compiler.STRICT_ENV] = previous


if __name__ == "__main__":
    unittest.main()
