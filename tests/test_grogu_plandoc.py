import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401

import grogu_plandoc as plandoc  # noqa: E402
import grogu_plandoc_anchor as anchor  # noqa: E402
import grogu_plandoc_canon as canon  # noqa: E402
import grogu_plandoc_schema as schema  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "plandoc"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf8"))


class CanonicalJsonTests(unittest.TestCase):
    def test_utf16_key_order_nfc_and_utf8(self):
        value = {
            "\ue000": "private",
            "\U00010000": "astral",
            "e\u0301": "normalized",
        }
        encoded = canon.dumps(value)
        self.assertEqual(
            encoded,
            '{"é":"normalized","𐀀":"astral","":"private"}',
        )
        self.assertEqual(canon.loads(encoded), canon.normalize(value))
        self.assertTrue(canon.is_canonical(encoded))

    def test_duplicate_keys_are_rejected_before_and_after_normalization(self):
        with self.assertRaises(canon.DuplicateKeyError):
            canon.loads('{"same":1,"same":2}')
        with self.assertRaises(canon.DuplicateKeyError):
            canon.loads('{"e\\u0301":1,"\\u00e9":2}')

    def test_floats_nonfinite_and_out_of_range_integers_are_rejected(self):
        for source in ("1.0", "1e2", "NaN", "Infinity", "-Infinity"):
            with self.subTest(source=source):
                with self.assertRaises(canon.CanonicalError):
                    canon.loads(source)
        for value in (1.5, -(2**53) - 1, 2**53 + 1):
            with self.subTest(value=value):
                with self.assertRaises(canon.CanonicalError):
                    canon.dumps(value)
        self.assertEqual(canon.loads(str(2**53)), 2**53)
        self.assertEqual(canon.loads(str(-(2**53))), -(2**53))

    def test_profile_matches_jcs_for_restricted_fixture_when_available(self):
        restricted_fixture = {
            "string": "€$\u000f\nA'B\"\\\"/",
            "integer": 42,
            "literals": [None, True, False],
        }
        self.assertEqual(
            canon.dumps(restricted_fixture),
            "{\"integer\":42,\"literals\":[null,true,false],"
            "\"string\":\"€$\\u000f\\nA'B\\\"\\\\\\\"/\"}",
        )
        try:
            import rfc8785
        except ImportError:
            self.skipTest("rfc8785 is optional")
        values = [
            None,
            True,
            42,
            "NFC café",
            ["a", 7, False],
            {"z": 1, "a": "value", "\U00010000": "astral"},
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(canon.dumpb(value), rfc8785.dumps(value))

    def test_digest_is_stable_across_mapping_insertion_order(self):
        left = {"b": 2, "a": {"d": 4, "c": 3}}
        right = {"a": {"c": 3, "d": 4}, "b": 2}
        self.assertEqual(canon.digest(left), canon.digest(right))


class PlanDocumentSchemaTests(unittest.TestCase):
    def test_fixture_satisfies_runtime_schema_and_published_schema(self):
        graph = fixture("graph.json")
        self.assertEqual(schema.validate_document(graph), graph)
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is optional")
        published = json.loads(
            (ROOT / "schemas" / "plan-document.schema.json").read_text()
        )
        jsonschema.Draft202012Validator(published).validate(graph)

    def test_schema_rejects_wrong_prefix_self_edges_cycles_and_two_parents(self):
        graph = fixture("graph.json")
        wrong_prefix = copy.deepcopy(graph)
        wrong_prefix["nodes"]["task-1"]["id"] = "goal-9"
        with self.assertRaises(schema.SchemaError):
            schema.validate_document(wrong_prefix)

        self_edge = copy.deepcopy(graph)
        self_edge["edges"]["edge-6"] = {
            "id": "edge-6",
            "kind": "depends_on",
            "from": "task-1",
            "to": "task-1",
            "attrs": {},
            "created_rev": "r0007",
        }
        with self.assertRaisesRegex(schema.SchemaError, "self-edges"):
            schema.validate_document(self_edge)

        cycle = copy.deepcopy(graph)
        cycle["edges"]["edge-6"] = {
            "id": "edge-6",
            "kind": "depends_on",
            "from": "task-1",
            "to": "task-2",
            "attrs": {},
            "created_rev": "r0007",
        }
        with self.assertRaisesRegex(schema.SchemaError, "depends_on cycle"):
            schema.validate_document(cycle)

        two_parents = copy.deepcopy(graph)
        two_parents["edges"]["edge-6"] = {
            "id": "edge-6",
            "kind": "contains",
            "from": "goal-1",
            "to": "dia-1",
            "attrs": {},
            "created_rev": "r0007",
        }
        with self.assertRaisesRegex(schema.SchemaError, "already has parent"):
            schema.validate_document(two_parents)

    def test_cross_seal_edges_are_references_only(self):
        graph = fixture("graph.json")
        graph["edges"]["edge-5"]["kind"] = "validates"
        with self.assertRaisesRegex(schema.SchemaError, "open/sealed"):
            schema.validate_document(graph)

    def test_deleted_selector_targets_require_explicit_orphan_state(self):
        graph = fixture("graph.json")
        graph["nodes"]["thr-1"]["attrs"]["selector"] = {
            "type": "edge",
            "id": "edge-99",
        }
        with self.assertRaisesRegex(schema.SchemaError, "missing edge"):
            schema.validate_document(graph)
        graph["nodes"]["thr-1"]["attrs"]["anchor_state"] = "orphaned"
        self.assertEqual(
            schema.validate_document(graph)["nodes"]["thr-1"]["attrs"][
                "anchor_state"
            ],
            "orphaned",
        )

    def test_floats_are_rejected_anywhere_in_graph(self):
        graph = fixture("graph.json")
        graph["nodes"]["task-1"]["attrs"]["ratio"] = 0.5
        with self.assertRaises(schema.SchemaError):
            schema.validate_document(graph)
        with self.assertRaises(canon.DuplicateKeyError):
            schema.load_document('{"schema_version":1,"schema_version":1}')

    def test_dashboard_fixture_matches_runtime_and_published_schema(self):
        snapshot = fixture("dashboard-snapshot.json")
        self.assertEqual(schema.validate_dashboard_snapshot(snapshot), snapshot)
        event_page = fixture("dashboard-event-page.json")
        request = fixture("dashboard-feedback-request.json")
        receipt = fixture("dashboard-feedback-receipt.json")
        object_ref = fixture("dashboard-object-ref.json")
        self.assertEqual(schema.validate_event_page(event_page), event_page)
        self.assertEqual(schema.validate_feedback_request(request), request)
        self.assertEqual(schema.validate_feedback_receipt(receipt), receipt)
        self.assertEqual(schema.validate_object_ref(object_ref), object_ref)
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is optional")
        published = json.loads(
            (
                ROOT / "schemas" / "plan-document-agent-dashboard.json"
            ).read_text()
        )
        validator = jsonschema.Draft202012Validator(published)
        for value in (snapshot, event_page, request, receipt):
            validator.validate(value)
        object_ref_schema = {
            "$schema": published["$schema"],
            "$defs": published["$defs"],
            "$ref": "#/$defs/ObjectRef",
        }
        jsonschema.Draft202012Validator(object_ref_schema).validate(object_ref)

    def test_dashboard_rejects_unknown_fields_floats_and_oversized_pages(self):
        snapshot = fixture("dashboard-snapshot.json")
        snapshot["agents"][0]["raw_tool_arguments"] = "must not cross the boundary"
        with self.assertRaisesRegex(schema.SchemaError, "unknown field"):
            schema.validate_dashboard_snapshot(snapshot)

        snapshot = fixture("dashboard-snapshot.json")
        snapshot["agents"][0]["elapsed_ms"] = 1.5
        with self.assertRaises(schema.SchemaError):
            schema.validate_dashboard_snapshot(snapshot)

        page = {
            "schema_version": 1,
            "agent_key": "agent-1",
            "events": [],
            "next_cursor": None,
            "gap": False,
            "truncated": False,
        }
        event = {
            "id": "event-1",
            "sequence": 1,
            "occurred_at": "2026-09-05T20:00:00Z",
            "collected_at": "2026-09-05T20:00:01Z",
            "source": "session",
            "basis": "observed",
            "kind": "tool.execution_start",
            "action": None,
            "refs": [],
            "safe_code": None,
            "summary": None,
        }
        page["events"] = [copy.deepcopy(event) for _ in range(101)]
        with self.assertRaisesRegex(schema.SchemaError, "at most 100"):
            schema.validate_event_page(page)


class GraphCoreTests(unittest.TestCase):
    def test_stable_ids_advance_and_are_never_reused(self):
        manifest = {"plandoc": {"counters": {"task": 2}}}
        allocated = plandoc.allocate_id(
            manifest, "task", existing_ids={"task-3", "task-8"}
        )
        self.assertEqual(allocated, "task-4")
        self.assertEqual(manifest["plandoc"]["counters"]["task"], 4)
        self.assertEqual(
            plandoc.allocate_id(manifest, "task", existing_ids={"task-5"}),
            "task-6",
        )

    def test_partition_loader_never_opens_engineer_sealed_partitions(self):
        graph = fixture("graph.json")
        partitions = plandoc.split_partitions(graph)
        self.assertEqual(plandoc.merge_partitions(partitions), graph)
        calls = []

        def load(name):
            calls.append(name)
            return partitions[name]

        visible = plandoc.load_visible(load, "engineer", sealed_relationship_count=1)
        self.assertEqual(calls, ["open"])
        self.assertNotIn("crit-2", visible["nodes"])
        self.assertNotIn("SEALED_FIXTURE_TEXT", canon.dumps(visible))
        self.assertEqual(visible["provenance"]["sealed_relationships"], 1)

    def test_tester_open_partition_is_filtered_to_readable_design_nodes(self):
        graph = fixture("graph.json")
        graph["nodes"]["note-2"] = plandoc.make_node(
            "note-2",
            "note",
            "Visible design note",
            stage="design",
            revision="r0007",
        )
        partitions = plandoc.split_partitions(graph)
        visible = plandoc.load_visible(lambda name: partitions[name], "tester")
        self.assertIn("note-2", visible["nodes"])
        self.assertIn("crit-2", visible["nodes"])
        self.assertNotIn("task-1", visible["nodes"])

    def test_all_selector_variants_and_text_reanchoring(self):
        graph = fixture("graph.json")
        self.assertEqual(
            plandoc.resolve_selector(graph, {"type": "node", "id": "task-1"})[
                "state"
            ],
            "resolved",
        )
        self.assertEqual(
            plandoc.resolve_selector(
                graph, {"type": "object", "id": "dir-1", "part": "attrs.binding"}
            )["nodes"],
            ["dir-1"],
        )
        self.assertEqual(
            plandoc.resolve_selector(graph, {"type": "edge", "id": "edge-1"})[
                "edges"
            ],
            ["edge-1"],
        )
        self.assertEqual(
            plandoc.resolve_selector(
                graph,
                {
                    "type": "edge",
                    "from": "task-2",
                    "to": "task-1",
                    "ordinal": 0,
                },
            )["edges"],
            ["edge-1"],
        )
        self.assertEqual(
            plandoc.resolve_selector(graph, {"type": "region", "id": "reg-1"})[
                "nodes"
            ],
            ["reg-1"],
        )

        body = graph["nodes"]["task-1"]["body"]
        start = body.index("canonical")
        legacy = anchor.text_anchor(
            body,
            start,
            start + len("canonical"),
            stage="implementation",
            revision=7,
        )
        selector = anchor.selector_from_text_anchor("task-1", legacy)
        moved = copy.deepcopy(graph)
        moved["nodes"]["task-1"]["body"] = "Prefix. " + body
        outcome = plandoc.resolve_selector(moved, selector)
        self.assertEqual(outcome["state"], "shifted")
        self.assertEqual(
            outcome["selector"]["position"]["start"], start + len("Prefix. ")
        )

        removed = copy.deepcopy(graph)
        del removed["nodes"]["task-1"]
        removed["edges"] = {
            key: edge
            for key, edge in removed["edges"].items()
            if "task-1" not in {edge["from"], edge["to"]}
        }
        outcome = plandoc.resolve_selector(removed, selector)
        self.assertEqual(outcome["state"], "orphaned")
        self.assertEqual(outcome["selector"], selector)

    def test_composite_selectors_are_bounded(self):
        graph = fixture("graph.json")
        selector = {
            "type": "composite",
            "op": "any",
            "members": [
                {"type": "node", "id": "task-1"},
                {"type": "edge", "id": "edge-1"},
            ],
        }
        outcome = plandoc.resolve_selector(graph, selector)
        self.assertEqual(outcome["nodes"], ["task-1", "task-2"])
        nested = {"type": "node", "id": "task-1"}
        for _ in range(5):
            nested = {"type": "composite", "op": "any", "members": [nested]}
        with self.assertRaisesRegex(schema.SchemaError, "nesting exceeds"):
            schema.validate_selector(nested)

    def test_dependency_impact_returns_shortest_deterministic_paths(self):
        graph = fixture("graph.json")
        result = plandoc.impact(graph, {"type": "node", "id": "task-2"})
        self.assertEqual(result["direct"], ["task-1"])
        by_id = {item["id"]: item for item in result["transitive"]}
        self.assertEqual(by_id["task-1"]["path"], ["task-2", "task-1"])
        self.assertEqual(by_id["crit-1"]["path"], ["task-2", "task-1", "crit-1"])
        self.assertEqual(result["cycles"], [])

        proposed = copy.deepcopy(graph)
        proposed["nodes"]["task-1"]["body"] = "Changed"
        self.assertEqual(
            plandoc.impact(
                graph,
                {"type": "node", "id": "task-2"},
                proposed=proposed,
            )["compiled_delta"],
            ["task-1"],
        )

    def test_object_references_are_role_visible_or_generic(self):
        graph = fixture("graph.json")
        ref = {
            "plan_id": graph["plan_id"],
            "revision": graph["revision"],
            "object_id": "task-1",
            "edge_id": None,
            "frame_id": None,
            "artifact_id": None,
            "relation": "context",
        }
        self.assertEqual(plandoc.authorize_object_ref(graph, ref), ref)
        current_sealed = {
            **ref,
            "object_id": "crit-2",
        }
        self.assertIsNone(
            plandoc.authorize_object_ref(
                graph, current_sealed, role="engineer"
            )["object_id"]
        )
        hidden = {**ref, "object_id": "crit-99"}
        authorized = plandoc.authorize_object_ref(graph, hidden)
        self.assertIsNone(authorized["object_id"])
        artifact = {
            **ref,
            "object_id": None,
            "artifact_id": "/private/plan.txt",
        }
        with self.assertRaises(schema.SchemaError):
            plandoc.authorize_object_ref(
                graph, artifact, artifact_ids={"/private/plan.txt"}
            )
        artifact["artifact_id"] = "plan.txt"
        authorized = plandoc.authorize_object_ref(
            graph, artifact, artifact_ids={"other.txt"}
        )
        self.assertIsNone(authorized["artifact_id"])

        historical = copy.deepcopy(graph)
        historical["revision"] = "r0006"
        current = copy.deepcopy(graph)
        del current["nodes"]["note-1"]
        old_ref = {
            **ref,
            "revision": "r0006",
            "object_id": "note-1",
        }
        self.assertEqual(
            plandoc.authorize_object_ref(
                current,
                old_ref,
                visible_revisions={"r0006"},
                revision_documents={"r0006": historical},
                role="engineer",
            ),
            old_ref,
        )
        sealed_ref = {
            **old_ref,
            "object_id": "crit-2",
        }
        self.assertIsNone(
            plandoc.authorize_object_ref(
                current,
                sealed_ref,
                visible_revisions={"r0006"},
                revision_documents={"r0006": historical},
                role="engineer",
            )["object_id"]
        )


if __name__ == "__main__":
    unittest.main()
