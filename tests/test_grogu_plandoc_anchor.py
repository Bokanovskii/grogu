import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401

import grogu_markdown  # noqa: E402
import grogu_mermaid  # noqa: E402
import grogu_plandoc_anchor  # noqa: E402
import grogu_review  # noqa: E402


class LiftedAnchorParityTests(unittest.TestCase):
    def assert_text_parity(self, anchor, body):
        self.assertEqual(
            grogu_plandoc_anchor.reanchor_text(copy.deepcopy(anchor), body),
            grogu_review.reanchor_text(copy.deepcopy(anchor), body),
        )

    def test_text_anchor_shape_and_errors_match_existing_review_behavior(self):
        body = "0123456789" * 10
        for start, end in ((0, 10), (40, 60), (90, 100)):
            with self.subTest(start=start):
                self.assertEqual(
                    grogu_plandoc_anchor.text_anchor(
                        body,
                        start,
                        end,
                        stage="implementation",
                        revision=7,
                    ),
                    grogu_review.text_anchor(
                        body,
                        start,
                        end,
                        stage="implementation",
                        revision=7,
                    ),
                )
        invalid = [(-1, 2), (2, 2), (0, len(body) + 1)]
        for start, end in invalid:
            with self.subTest(start=start, end=end):
                with self.assertRaises(Exception) as lifted:
                    grogu_plandoc_anchor.text_anchor(
                        body,
                        start,
                        end,
                        stage="implementation",
                        revision=1,
                    )
                with self.assertRaises(Exception) as existing:
                    grogu_review.text_anchor(
                        body,
                        start,
                        end,
                        stage="implementation",
                        revision=1,
                    )
                self.assertEqual(str(lifted.exception), str(existing.exception))

    def test_unmoved_unique_context_ambiguous_fuzzy_and_orphan_ladder_match(self):
        body = "before target phrase after"
        start = body.index("target")
        base = grogu_review.text_anchor(
            body,
            start,
            start + len("target phrase"),
            stage="implementation",
            revision=1,
        )
        cases = [
            body,
            "inserted " + body,
            "target phrase elsewhere; " + body,
            "before target phrasing after",
            "the selected words are gone",
            ("x" * (grogu_review.FUZZY_MAX_BODY + 1)),
        ]
        for updated in cases:
            with self.subTest(updated=updated[:40]):
                self.assert_text_parity(base, updated)

        ambiguous_body = "same target same target"
        ambiguous = grogu_review.text_anchor(
            ambiguous_body,
            5,
            11,
            stage="implementation",
            revision=1,
        )
        self.assert_text_parity(ambiguous, "target xx target")

    def test_mermaid_anchor_and_reanchor_match_existing_review_behavior(self):
        body = (
            "# Diagram\n\n"
            "```mermaid\n"
            "flowchart LR\n"
            "  A[Alpha] -->|go| B[Beta]\n"
            "```\n"
        )
        document = grogu_markdown.render_document(body)
        block = document["code_blocks"][0]
        parsed = grogu_mermaid.parse(block["body"])
        edge = parsed["edges"][0]
        for target, arguments in (
            ("diagram", {}),
            ("node", {"node_id": "A"}),
            ("edge", {"edge": edge}),
        ):
            with self.subTest(target=target):
                lifted = grogu_plandoc_anchor.mermaid_anchor(
                    stage="implementation",
                    revision=1,
                    body=body,
                    block=block,
                    parsed=parsed,
                    target=target,
                    **arguments,
                )
                existing = grogu_review.mermaid_anchor(
                    stage="implementation",
                    revision=1,
                    body=body,
                    block=block,
                    parsed=parsed,
                    target=target,
                    **arguments,
                )
                self.assertEqual(lifted, existing)

                moved_body = "Preamble\n\n" + body.replace("A[Alpha]", "A[Alpha changed]")
                moved_blocks = grogu_markdown.render_document(moved_body)["code_blocks"]
                self.assertEqual(
                    grogu_plandoc_anchor.reanchor_mermaid(
                        copy.deepcopy(lifted), moved_body, moved_blocks
                    ),
                    grogu_review.reanchor_mermaid(
                        copy.deepcopy(existing), moved_body, moved_blocks
                    ),
                )

    def test_selector_adapters_round_trip_legacy_anchor_fields(self):
        body = "prefix selected suffix"
        legacy = grogu_plandoc_anchor.text_anchor(
            body,
            body.index("selected"),
            body.index("selected") + len("selected"),
            stage="implementation",
            revision=9,
        )
        selector = grogu_plandoc_anchor.selector_from_text_anchor("task-7", legacy)
        restored = grogu_plandoc_anchor.text_anchor_from_selector(
            selector, stage="implementation", revision=9
        )
        self.assertEqual(restored, legacy)


if __name__ == "__main__":
    unittest.main()
