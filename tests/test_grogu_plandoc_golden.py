import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401

import grogu_plandoc_compile as compiler  # noqa: E402
import grogu_plandoc_schema as schema  # noqa: E402
import test_grogu_plandoc_compile as support  # noqa: E402


class GoldenCompilerCorpusTests(unittest.TestCase):
    def test_corpus_matches_current_compiler_byte_for_byte(self):
        expected_names = set(support.golden_cases())
        self.assertEqual(
            {
                path.name
                for path in support.GOLDEN.iterdir()
                if path.is_dir()
            },
            expected_names,
        )
        node_graph = json.loads(
            (
                support.GOLDEN
                / "every-node-kind"
                / "graph.json"
            ).read_text(encoding="utf8")
        )
        edge_graph = json.loads(
            (
                support.GOLDEN
                / "every-edge-kind"
                / "graph.json"
            ).read_text(encoding="utf8")
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
                directory = support.GOLDEN / name
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


if __name__ == "__main__":
    unittest.main()
