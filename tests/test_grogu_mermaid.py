import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401

import grogu_mermaid  # noqa: E402


class GroguMermaidTests(unittest.TestCase):
    # 10. Node shapes
    def test_10_node_shapes(self) -> None:
        source = (
            "flowchart TD\n"
            "  A[sq]\n"
            "  B(round)\n"
            "  C((circle))\n"
            "  D{rhombus}\n"
            "  E>flag]\n"
            "  F[[sub]]\n"
            "  G[(db)]\n"
            "  H([stadium])\n"
        )
        parsed = grogu_mermaid.parse(source)
        self.assertTrue(parsed["supported"])
        nodes = {node["id"]: node for node in parsed["nodes"]}
        self.assertEqual(len(nodes), 8)

        self.assertEqual(nodes["A"]["label"], "sq")
        self.assertEqual(nodes["A"]["shape"], "rectangle")

        self.assertEqual(nodes["B"]["label"], "round")
        self.assertEqual(nodes["B"]["shape"], "round")

        self.assertEqual(nodes["C"]["label"], "circle")
        self.assertEqual(nodes["C"]["shape"], "circle")

        self.assertEqual(nodes["D"]["label"], "rhombus")
        self.assertEqual(nodes["D"]["shape"], "diamond")

        self.assertEqual(nodes["E"]["label"], "flag")
        self.assertEqual(nodes["E"]["shape"], "asymmetric")

        self.assertEqual(nodes["F"]["label"], "sub")
        self.assertEqual(nodes["F"]["shape"], "subroutine")

        self.assertEqual(nodes["G"]["label"], "db")
        self.assertEqual(nodes["G"]["shape"], "cylinder")

        self.assertEqual(nodes["H"]["label"], "stadium")
        self.assertEqual(nodes["H"]["shape"], "stadium")

    # 11. Edge kinds and labels
    def test_11_edge_kinds_and_labels(self) -> None:
        source = (
            "flowchart LR\n"
            "  A --> B\n"
            "  C --- D\n"
            "  E -.-> F\n"
            "  G -.- H\n"
            "  I ==> J\n"
            "  K === L\n"
            "  M --x N\n"
            "  O --o P\n"
            "  Q -->|yes| R\n"
            "  S -- text --> T\n"
        )
        parsed = grogu_mermaid.parse(source)
        edges = parsed["edges"]
        self.assertEqual(len(edges), 10)

        # Labels
        self.assertEqual(edges[0]["label"], "")
        self.assertEqual(edges[0]["kind"], "arrow")
        self.assertEqual(edges[0]["operator"], "-->")

        self.assertEqual(edges[1]["label"], "")
        self.assertEqual(edges[1]["kind"], "line")

        self.assertEqual(edges[2]["label"], "")
        self.assertEqual(edges[2]["kind"], "arrow")

        self.assertEqual(edges[3]["label"], "")
        self.assertEqual(edges[3]["kind"], "line")

        self.assertEqual(edges[4]["label"], "")
        self.assertEqual(edges[4]["kind"], "arrow")

        self.assertEqual(edges[5]["label"], "")
        self.assertEqual(edges[5]["kind"], "line")

        self.assertEqual(edges[6]["label"], "")
        self.assertEqual(edges[6]["kind"], "cross")

        self.assertEqual(edges[7]["label"], "")
        self.assertEqual(edges[7]["kind"], "circle")

        self.assertEqual(edges[8]["label"], "yes")
        self.assertEqual(edges[8]["kind"], "arrow")
        self.assertEqual(edges[8]["operator"], "-->")

        self.assertEqual(edges[9]["label"], "text")
        self.assertEqual(edges[9]["kind"], "arrow")
        self.assertEqual(edges[9]["operator"], "-->")

    # 12. Chains
    def test_12_chains(self) -> None:
        source = "flowchart LR\n  A --> B --> C\n"
        parsed = grogu_mermaid.parse(source)
        self.assertEqual(len(parsed["edges"]), 2)
        self.assertEqual(len(parsed["nodes"]), 3)

        e0 = parsed["edges"][0]
        self.assertEqual(e0["from"], "A")
        self.assertEqual(e0["to"], "B")
        self.assertEqual(e0["edge_index"], 0)

        e1 = parsed["edges"][1]
        self.assertEqual(e1["from"], "B")
        self.assertEqual(e1["to"], "C")
        self.assertEqual(e1["edge_index"], 1)

    # 13. Ordinals
    def test_13_ordinals(self) -> None:
        source = (
            "flowchart LR\n"
            "  A -->|first| B\n"
            "  A -->|only| C\n"
            "  A -->|second| B\n"
        )
        parsed = grogu_mermaid.parse(source)
        edges = parsed["edges"]
        self.assertEqual(len(edges), 3)

        self.assertEqual(edges[0]["from"], "A")
        self.assertEqual(edges[0]["to"], "B")
        self.assertEqual(edges[0]["pair_ordinal"], 0)
        self.assertEqual(edges[0]["edge_index"], 0)

        self.assertEqual(edges[1]["from"], "A")
        self.assertEqual(edges[1]["to"], "C")
        self.assertEqual(edges[1]["pair_ordinal"], 0)
        self.assertEqual(edges[1]["edge_index"], 1)

        self.assertEqual(edges[2]["from"], "A")
        self.assertEqual(edges[2]["to"], "B")
        self.assertEqual(edges[2]["pair_ordinal"], 1)
        self.assertEqual(edges[2]["edge_index"], 2)

    # 14. Referenced-only nodes
    def test_14_referenced_only_nodes(self) -> None:
        source = "graph LR\n  A --> B\n"
        parsed = grogu_mermaid.parse(source)
        self.assertEqual(len(parsed["nodes"]), 2)
        node_ids = {n["id"] for n in parsed["nodes"]}
        self.assertEqual(node_ids, {"A", "B"})
        for node in parsed["nodes"]:
            self.assertEqual(node["shape"], "bare")

    # 15. Subgraphs
    def test_15_subgraphs(self) -> None:
        source = (
            "flowchart LR\n"
            "  subgraph s1 [Server]\n"
            "    A[App]\n"
            "    B[Database]\n"
            "  end\n"
            "  C[Client]\n"
        )
        parsed = grogu_mermaid.parse(source)
        self.assertEqual(len(parsed["subgraphs"]), 1)
        self.assertEqual(parsed["subgraphs"][0]["id"], "s1")
        self.assertEqual(parsed["subgraphs"][0]["title"], "Server")

        nodes = {n["id"]: n for n in parsed["nodes"]}
        self.assertEqual(nodes["A"]["subgraph"], "s1")
        self.assertEqual(nodes["B"]["subgraph"], "s1")
        self.assertEqual(nodes["C"]["subgraph"], "")

    # 16. Noise is skipped, not counted
    def test_16_noise_skipped_not_counted(self) -> None:
        source = (
            "flowchart TD\n"
            "  %% comment line\n"
            "  %%{init: {'theme': 'dark'}}%%\n"
            "  classDef default fill:#f9f,stroke:#333;\n"
            "  class A default\n"
            "  style A fill:#f9f\n"
            "  linkStyle 0 stroke:#ff3\n"
            "  click A href \"https://example.com\"\n"
            "  direction TB\n"
            "  accTitle: Title of diagram\n"
            "  accDescr: Long description of diagram\n"
            "  A[Node A] --> B[Node B]\n"
        )
        parsed = grogu_mermaid.parse(source)
        self.assertEqual(parsed["unparsed"], 0)
        self.assertFalse(parsed["partial"])
        self.assertEqual(len(parsed["nodes"]), 2)
        self.assertEqual(len(parsed["edges"]), 1)

    # 17. Unsupported diagram types
    def test_17_unsupported_diagram_types(self) -> None:
        types = [
            "sequenceDiagram",
            "classDiagram",
            "stateDiagram-v2",
            "erDiagram",
            "gantt",
            "journey",
        ]
        for dtype in types:
            source = f"{dtype}\n  Alice->>Bob: Hello\n"
            parsed = grogu_mermaid.parse(source)
            self.assertEqual(parsed["type"], dtype)
            self.assertFalse(parsed["supported"])
            self.assertEqual(parsed["nodes"], [])
            self.assertEqual(parsed["edges"], [])

    # 18. Partial
    def test_18_partial(self) -> None:
        # Fewer than or equal to 20% unparsed lines -> partial is False
        source_clean = (
            "flowchart TD\n"
            "  A --> B\n"
            "  B --> C\n"
            "  C --> D\n"
            "  D --> E\n"
            "  E --> F\n"
            "  F --> G\n"
            "  G --> H\n"
            "  H --> I\n"
            "  ???\n"
            "  !!!\n"
        )
        p1 = grogu_mermaid.parse(source_clean)
        self.assertFalse(p1["partial"])

        # More than 20% unparsed lines -> partial is True
        source_dirty = (
            "flowchart TD\n"
            "  A --> B\n"
            "  B --> C\n"
            "  C --> D\n"
            "  D --> E\n"
            "  E --> F\n"
            "  F --> G\n"
            "  G --> H\n"
            "  ???\n"
            "  !!!\n"
            "  ###\n"
        )
        p2 = grogu_mermaid.parse(source_dirty)
        self.assertTrue(p2["partial"])

    # 19. Never raises
    def test_19_never_raises(self) -> None:
        cases = [
            "",
            "   \n\t  \n  ",
            "%% comment only\n%% another comment\n",
            "\n".join(f"gibberish line {i}" for i in range(5000)),
            "flowchart TD\n  A[[[unbalanced brackets\n  B(((unclosed circle\n",
            "flowchart TD\n  subgraph unclosed [Unclosed Subgraph]\n  A --> B\n",
            "flowchart TD\n" + ("  A --> B\n" * 25000),  # ~500 KB input
        ]
        start_time = time.monotonic()
        for case in cases:
            res = grogu_mermaid.parse(case)
            self.assertIsInstance(res, dict)
            self.assertIn("nodes", res)
            self.assertIn("edges", res)
            self.assertIn("supported", res)
        elapsed = time.monotonic() - start_time
        self.assertLess(elapsed, 3.0, f"parse() took {elapsed:.2f}s, expected < 3.0s")


if __name__ == "__main__":
    unittest.main()
