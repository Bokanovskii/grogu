import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401

import grogu_cli  # noqa: E402
import grogu_plans  # noqa: E402
import grogu_review  # noqa: E402


class GroguReviewTests(unittest.TestCase):
    def _create_git_repo(self, path: Path) -> grogu_plans.PlanStore:
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Review Tester"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=path, check=True, capture_output=True)
        return grogu_plans.PlanStore(path)

    # 20. Anchor construction
    def test_20_anchor_construction(self) -> None:
        body = "0123456789" * 10  # 100 chars
        start, end = 40, 60
        anchor = grogu_review.text_anchor(body, start, end, stage="implementation", revision=1)
        self.assertEqual(anchor["exact"], body[start:end])
        self.assertEqual(anchor["prefix"], body[start - 32 : start])
        self.assertEqual(anchor["suffix"], body[end : end + 32])
        self.assertEqual(anchor["body_digest"], grogu_review.digest(body))
        self.assertEqual(anchor["stage"], "implementation")
        self.assertEqual(anchor["revision"], 1)

        # Start of body
        start_anchor = grogu_review.text_anchor(body, 0, 10, stage="implementation", revision=1)
        self.assertEqual(start_anchor["prefix"], "")
        self.assertEqual(start_anchor["exact"], body[0:10])

        # End of body
        end_anchor = grogu_review.text_anchor(body, 90, 100, stage="implementation", revision=1)
        self.assertEqual(end_anchor["suffix"], "")
        self.assertEqual(end_anchor["exact"], body[90:100])

    # 21. Unmoved
    def test_21_unmoved(self) -> None:
        body = "Here is the implementation body with a unique quote inside it."
        start = body.index("unique quote")
        end = start + len("unique quote")
        anchor = grogu_review.text_anchor(body, start, end, stage="implementation", revision=1)
        reanchored = grogu_review.reanchor_text(anchor, body)
        self.assertEqual(reanchored["state"], grogu_review.ANCHORED)
        self.assertEqual(reanchored["confidence"], 1.0)
        self.assertEqual(reanchored["anchor"]["start"], start)
        self.assertEqual(reanchored["anchor"]["end"], end)

    # 22. Shift by insertion
    def test_22_shift_by_insertion(self) -> None:
        prefix_body = "Intro text.\n\n"
        target = "TARGET ANCHOR PHRASE"
        suffix_body = "\n\nTrailing text."
        original_body = prefix_body + target + suffix_body
        start = len(prefix_body)
        end = start + len(target)
        anchor = grogu_review.text_anchor(original_body, start, end, stage="implementation", revision=1)

        insertion = "x" * 500 + "\n\n"
        new_body = insertion + original_body
        reanchored = grogu_review.reanchor_text(anchor, new_body)
        self.assertEqual(reanchored["state"], grogu_review.ANCHORED)
        new_start = reanchored["anchor"]["start"]
        new_end = reanchored["anchor"]["end"]
        self.assertEqual(new_body[new_start:new_end], target)
        self.assertEqual(new_start, start + len(insertion))

    # 23. Duplicate quote disambiguated
    def test_23_duplicate_quote_disambiguated(self) -> None:
        # 3 identical quotes with distinct context
        quote = "DUPLICATE_PHRASE"
        body = (
            f"Alpha context before {quote} and alpha context after.\n\n"
            f"Beta context before {quote} and beta context after.\n\n"
            f"Gamma context before {quote} and gamma context after.\n\n"
        )
        second_start = body.index(f"Beta context before {quote}") + len("Beta context before ")
        second_end = second_start + len(quote)
        anchor = grogu_review.text_anchor(body, second_start, second_end, stage="implementation", revision=1)

        # Re-anchor against body shifted by prefix
        new_body = "Prefix insertion at top\n\n" + body
        reanchored = grogu_review.reanchor_text(anchor, new_body)
        self.assertEqual(reanchored["state"], grogu_review.ANCHORED)
        expected_start = second_start + len("Prefix insertion at top\n\n")
        self.assertEqual(reanchored["anchor"]["start"], expected_start)

        # Pathological: 3 occurrences with identical surrounding context
        block = f"same context prefix {quote} same context suffix\n"
        pathological_body = block * 3
        # Anchor second block
        p_second_start = len(block) + len("same context prefix ")
        p_second_end = p_second_start + len(quote)
        p_anchor = grogu_review.text_anchor(pathological_body, p_second_start, p_second_end, stage="implementation", revision=1)
        p_reanchored = grogu_review.reanchor_text(p_anchor, pathological_body)
        # Should be shifted (or anchored) and land on the nearest offset
        self.assertEqual(p_reanchored["anchor"]["start"], p_second_start)

    # 24. Fuzzy
    def test_24_fuzzy(self) -> None:
        original = "The quick brown fox jumps over the lazy dog and runs away fast."
        start = original.index("fox jumps over the lazy dog and runs")
        end = start + len("fox jumps over the lazy dog and runs")
        anchor = grogu_review.text_anchor(original, start, end, stage="implementation", revision=1)

        # Change ~1 word in 8: "fox leaps over the lazy dog and runs"
        edited = "The quick brown fox leaps over the lazy dog and runs away fast."
        reanchored = grogu_review.reanchor_text(anchor, edited)
        self.assertEqual(reanchored["state"], grogu_review.SHIFTED)
        self.assertGreaterEqual(reanchored["confidence"], grogu_review.FUZZY_MIN_RATIO)
        self.assertLessEqual(reanchored["confidence"], 1.0)
        new_start = reanchored["anchor"]["start"]
        new_end = reanchored["anchor"]["end"]
        # Overlaps edited text
        self.assertIn("lazy dog", edited[new_start:new_end])

        # Change most of it -> orphaned
        heavily_edited = "The quick brown elephant sleeps under a giant red maple tree completely quietly."
        re_orphaned = grogu_review.reanchor_text(anchor, heavily_edited)
        self.assertEqual(re_orphaned["state"], grogu_review.ORPHANED)

    # 25. Orphaned preserves evidence
    def test_25_orphaned_preserves_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            body = "# Implementation\n\nQuoted text line to delete.\n\nSecond paragraph.\n"
            store.write_stage(plan_id, "implementation", body, role="architect")
            rev_store = grogu_review.ReviewStore(store)
            start = body.index("Quoted text line to delete.")
            end = start + len("Quoted text line to delete.")
            anchor = grogu_review.text_anchor(body, start, end, stage="implementation", revision=1)
            rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="Comment on deleted line")

            threads_before = rev_store.threads(plan_id)
            self.assertEqual(len(threads_before), 1)
            orig_exact = threads_before[0]["anchor"]["exact"]
            orig_prefix = threads_before[0]["anchor"]["prefix"]
            orig_suffix = threads_before[0]["anchor"]["suffix"]

            # Delete the quoted text outright
            new_body = "# Implementation\n\nSecond paragraph.\n"
            store.write_stage(plan_id, "implementation", new_body, role="architect", replace=True)
            synced = rev_store.sync(plan_id, role="reviewer")

            threads_after = synced["threads"]
            self.assertEqual(len(threads_after), len(threads_before))
            orphan_thread = threads_after[0]
            self.assertEqual(orphan_thread["anchor_state"], grogu_review.ORPHANED)
            self.assertEqual(orphan_thread["anchor"]["exact"], orig_exact)
            self.assertEqual(orphan_thread["anchor"]["prefix"], orig_prefix)
            self.assertEqual(orphan_thread["anchor"]["suffix"], orig_suffix)

    # 26. Threshold boundary
    def test_26_threshold_boundary(self) -> None:
        # Construct an edit whose best SequenceMatcher ratio is just below FUZZY_MIN_RATIO and one just above
        exact = "abcdefghij" * 4  # 40 chars
        body_orig = f"prefix {exact} suffix"
        start = len("prefix ")
        end = start + len(exact)
        anchor = grogu_review.text_anchor(body_orig, start, end, stage="implementation", revision=1)

        # 40 chars, 30 matching = 75% ratio (just above/at threshold)
        just_above = "prefix " + exact[:31] + "X" * 9 + " suffix"
        res_above = grogu_review.reanchor_text(anchor, just_above)
        self.assertEqual(res_above["state"], grogu_review.SHIFTED)

        # 40 chars, 20 matching = 50% ratio (well below threshold)
        just_below = "prefix " + exact[:20] + "X" * 20 + " suffix"
        res_below = grogu_review.reanchor_text(anchor, just_below)
        self.assertEqual(res_below["state"], grogu_review.ORPHANED)

    # 27. Large-body bypass
    def test_27_large_body_bypass(self) -> None:
        large_body = ("Lorem ipsum dolor sit amet. " * 8000) + "Unique anchor text here. " + ("Tail lorem ipsum dolor. " * 8000)
        self.assertGreater(len(large_body), grogu_review.FUZZY_MAX_BODY)
        start = large_body.index("Unique anchor text here.")
        end = start + len("Unique anchor text here.")
        anchor = grogu_review.text_anchor(large_body, start, end, stage="implementation", revision=1)

        # Modify quote so it would only match fuzzily
        modified_large_body = large_body.replace("Unique anchor text here.", "Unique MODIFIED text here.")
        t0 = time.monotonic()
        outcome = grogu_review.reanchor_text(anchor, modified_large_body)
        elapsed = time.monotonic() - t0
        self.assertEqual(outcome["state"], grogu_review.ORPHANED)
        self.assertLess(elapsed, 1.0, f"Bypass took too long: {elapsed:.3f}s")

    # 28. History
    def test_28_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            body1 = "# Implementation\n\nFirst body with TARGET quote here.\n"
            store.write_stage(plan_id, "implementation", body1, role="architect")
            rev_store = grogu_review.ReviewStore(store)
            start = body1.index("TARGET quote")
            end = start + len("TARGET quote")
            anchor = grogu_review.text_anchor(body1, start, end, stage="implementation", revision=1)
            rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="Initial comment")

            # Revision 2: shift
            body2 = "# Implementation\n\nInsertion line.\n\nFirst body with TARGET quote here.\n"
            store.write_stage(plan_id, "implementation", body2, role="architect", replace=True)
            s2 = rev_store.sync(plan_id, role="reviewer")
            t = s2["threads"][0]
            self.assertEqual(len(t["anchor_history"]), 1)
            self.assertEqual(t["anchor_history"][0]["from_revision"], 1)
            self.assertEqual(t["anchor_history"][0]["to_revision"], 2)
            self.assertEqual(t["anchor_history"][0]["state"], grogu_review.ANCHORED)

            # Re-sync without change: appends nothing
            s2_again = rev_store.sync(plan_id, role="reviewer")
            self.assertEqual(len(s2_again["threads"][0]["anchor_history"]), 1)

    # 29. Mermaid node re-anchoring
    def test_29_mermaid_node_reanchoring(self) -> None:
        block_source = "flowchart LR\n  A[Original Label] --> B[Target]\n"
        parsed = grogu_plans_mermaid = grogu_review.grogu_mermaid.parse(block_source)
        block = {"index": 0, "start": 0, "end": len(block_source), "source": block_source, "body": block_source}
        anchor = grogu_review.mermaid_anchor(
            stage="implementation",
            revision=1,
            body=block_source,
            block=block,
            parsed=parsed,
            target="node",
            node_id="A",
        )

        # Rename label, keep id -> anchored
        renamed_source = "flowchart LR\n  A[Renamed Label] --> B[Target]\n"
        renamed_blocks = [{"index": 0, "start": 0, "end": len(renamed_source), "body": renamed_source, "lang": "mermaid"}]
        res_rename = grogu_review.reanchor_mermaid(anchor, renamed_source, renamed_blocks)
        self.assertEqual(res_rename["state"], grogu_review.ANCHORED)
        self.assertEqual(res_rename["anchor"]["node_id"], "A")

        # Change id, keep unique matching label -> shifted
        shifted_source = "flowchart LR\n  A2[Original Label] --> B[Target]\n"
        shifted_blocks = [{"index": 0, "start": 0, "end": len(shifted_source), "body": shifted_source, "lang": "mermaid"}]
        res_shift = grogu_review.reanchor_mermaid(anchor, shifted_source, shifted_blocks)
        self.assertEqual(res_shift["state"], grogu_review.SHIFTED)
        self.assertEqual(res_shift["anchor"]["node_id"], "A2")

        # Delete node -> orphaned
        deleted_source = "flowchart LR\n  C[Other] --> B[Target]\n"
        deleted_blocks = [{"index": 0, "start": 0, "end": len(deleted_source), "body": deleted_source, "lang": "mermaid"}]
        res_delete = grogu_review.reanchor_mermaid(anchor, deleted_source, deleted_blocks)
        self.assertEqual(res_delete["state"], grogu_review.ORPHANED)

    # 30. Mermaid edge re-anchoring
    def test_30_mermaid_edge_reanchoring(self) -> None:
        block_source = "flowchart LR\n  A -->|first| B\n  A -->|second| B\n"
        parsed = grogu_review.grogu_mermaid.parse(block_source)
        block = {"index": 0, "start": 0, "end": len(block_source), "source": block_source, "body": block_source}
        edge = parsed["edges"][0]
        anchor = grogu_review.mermaid_anchor(
            stage="implementation",
            revision=1,
            body=block_source,
            block=block,
            parsed=parsed,
            target="edge",
            edge=edge,
        )

        # Same edge -> anchored
        blocks1 = [{"index": 0, "start": 0, "end": len(block_source), "body": block_source, "lang": "mermaid"}]
        res1 = grogu_review.reanchor_mermaid(anchor, block_source, blocks1)
        self.assertEqual(res1["state"], grogu_review.ANCHORED)

        # Same (from, to) but different ordinal -> shifted
        anchor_second = grogu_review.mermaid_anchor(
            stage="implementation",
            revision=1,
            body=block_source,
            block=block,
            parsed=parsed,
            target="edge",
            edge=parsed["edges"][1],  # ordinal 1
        )
        single_edge_source = "flowchart LR\n  A --> B\n"
        blocks_single = [{"index": 0, "start": 0, "end": len(single_edge_source), "body": single_edge_source, "lang": "mermaid"}]
        res_single = grogu_review.reanchor_mermaid(anchor_second, single_edge_source, blocks_single)
        self.assertEqual(res_single["state"], grogu_review.SHIFTED)

        # Endpoint deleted -> orphaned
        deleted_source = "flowchart LR\n  C --> D\n"
        blocks_del = [{"index": 0, "start": 0, "end": len(deleted_source), "body": deleted_source, "lang": "mermaid"}]
        res_del = grogu_review.reanchor_mermaid(anchor, deleted_source, blocks_del)
        self.assertEqual(res_del["state"], grogu_review.ORPHANED)

        # Second diagram inserted BEFORE anchored one -> block_digest resolves -> anchored
        diag1 = "flowchart TD\n  X --> Y\n"
        diag2 = block_source
        combined_body = f"```mermaid\n{diag1}```\n\n```mermaid\n{diag2}```\n"
        blocks_combined = [
            {"index": 0, "start": 0, "end": len(diag1), "body": diag1, "lang": "mermaid"},
            {"index": 1, "start": len(diag1) + 20, "end": len(combined_body), "body": diag2, "lang": "mermaid"},
        ]
        res_combined = grogu_review.reanchor_mermaid(anchor, combined_body, blocks_combined)
        self.assertEqual(res_combined["state"], grogu_review.ANCHORED)
        self.assertEqual(res_combined["anchor"]["block_index"], 1)

    # 31. Cross-stage isolation
    def test_31_cross_stage_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            impl_body1 = "# Implementation\n\nInitial implementation body.\n"
            testing_body = "# Testing\n\nInitial testing body.\n"
            store.write_stage(plan_id, "implementation", impl_body1, role="architect")
            store.write_stage(plan_id, "testing", testing_body, role="architect")

            rev_store = grogu_review.ReviewStore(store)
            anchor = grogu_review.text_anchor(testing_body, 12, 26, stage="testing", revision=1)
            rev_store.add_thread(plan_id, stage="testing", anchor=anchor, body="Comment on testing stage")

            # Rewrite implementation stage
            impl_body2 = "# Implementation\n\nRewritten implementation body completely.\n"
            store.write_stage(plan_id, "implementation", impl_body2, role="architect", replace=True)
            synced = rev_store.sync(plan_id, role="tester")

            testing_thread = next(t for t in synced["threads"] if t["stage"] == "testing")
            self.assertEqual(testing_thread["anchor_state"], grogu_review.ANCHORED)
            self.assertEqual(testing_thread["anchor_revision"], 1)

    # 32. Round lifecycle
    def test_32_round_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            body1 = "# Implementation\n\nFirst line of plan.\n"
            store.write_stage(plan_id, "implementation", body1, role="architect")
            rev_store = grogu_review.ReviewStore(store)

            anchor = grogu_review.text_anchor(body1, 18, 28, stage="implementation", revision=1)
            t1 = rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="Please fix this.")
            self.assertEqual(t1["round"], 1)

            # Request changes
            req_round = rev_store.request_changes(plan_id, note="Review round note")
            self.assertEqual(req_round["state"], grogu_review.ROUND_REQUESTED)

            manifest = store.load(plan_id)
            self.assertEqual(len(manifest.get("steering", [])), 1)
            steering_note = manifest["steering"][0]
            self.assertTrue(steering_note.get("requires_replan"))
            self.assertEqual(manifest.get("status"), grogu_plans.NEEDS_REVIEW)
            self.assertIn("c1", steering_note.get("text", ""))

            # Rewrite stage and sync moves round to answered
            body2 = "# Implementation\n\nFixed line of plan.\n"
            store.write_stage(plan_id, "implementation", body2, role="architect", replace=True)
            synced = rev_store.sync(plan_id, role="reviewer")
            self.assertEqual(synced["rounds"][0]["state"], grogu_review.ROUND_ANSWERED)

            # Next new thread opens round 2
            anchor2 = grogu_review.text_anchor(body2, 18, 26, stage="implementation", revision=2)
            t2 = rev_store.add_thread(plan_id, stage="implementation", anchor=anchor2, body="Round 2 comment")
            self.assertEqual(t2["round"], 2)

    # 33. Gate coupling is inherited, not reimplemented
    def test_33_gate_coupling_is_inherited_not_reimplemented(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            body = "# Implementation\n\nBody content.\n"
            store.write_stage(plan_id, "implementation", body, role="architect")
            rev_store = grogu_review.ReviewStore(store)
            anchor = grogu_review.text_anchor(body, 18, 30, stage="implementation", revision=1)
            rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="Comment")
            rev_store.request_changes(plan_id, note="Please replan")

            gate_result = store.gate(plan_id, "implement")
            self.assertFalse(gate_result["allowed"])

            # Assert grogu_review contains no gate logic of its own
            self.assertFalse(hasattr(grogu_review.ReviewStore, "gate"))
            self.assertFalse(hasattr(grogu_review, "GATE_IMPLEMENT"))

    # 34. Approval refusals
    def test_34_approval_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            body = "# Implementation\n\nBody content.\n"
            store.write_stage(plan_id, "implementation", body, role="architect")
            store.write_stage(plan_id, "testing", "# Testing\n", role="architect")
            rev_store = grogu_review.ReviewStore(store)
            anchor = grogu_review.text_anchor(body, 18, 30, stage="implementation", revision=1)
            rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="Open comment")

            # Unconfirmed open threads raises
            with self.assertRaises(grogu_review.ReviewError):
                rev_store.approve(plan_id, confirm_open=False)
            self.assertNotEqual(store.load(plan_id).get("status"), grogu_plans.APPROVED)

            # Role-bearing agent (architect) raises
            with mock.patch.dict(os.environ, {"GROGU_ROLE": "architect"}):
                with self.assertRaises(Exception):
                    rev_store.approve(plan_id, confirm_open=True)
                self.assertNotEqual(store.load(plan_id).get("status"), grogu_plans.APPROVED)

            # Pending amendment raises
            store.amend(plan_id, claim="Pending claim", evidence="Some evidence", raised_by="engineer")
            with self.assertRaises(Exception):
                rev_store.approve(plan_id, confirm_open=True)

    # 35. No new approval surface
    def test_35_no_new_approval_surface(self) -> None:
        parser = grogu_cli.build_parser()
        # Find all review subparser actions
        review_subparsers = None
        for action in parser._actions:
            if hasattr(action, "choices") and isinstance(action.choices, dict) and "review" in action.choices:
                review_parser = action.choices["review"]
                for subaction in review_parser._actions:
                    if hasattr(subaction, "choices") and isinstance(subaction.choices, dict):
                        review_subparsers = subaction.choices
                        break
        self.assertIsNotNone(review_subparsers)
        for name, subp in review_subparsers.items():
            options = [opt for act in subp._actions for opt in act.option_strings]
            self.assertNotIn("--as-user", options, f"review subcommand {name} must not have --as-user")

        # Grep source files for assignment to os.environ["GROGU_ROLE"]
        for filename in ["grogu_review.py", "grogu_review_server.py"]:
            filepath = ROOT / "src" / filename
            content = filepath.read_text(encoding="utf8")
            self.assertNotIn('os.environ["GROGU_ROLE"] =', content)
            self.assertNotIn("os.environ['GROGU_ROLE'] =", content)

    # 36. Comment validation
    def test_36_comment_validation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            body = "# Implementation\n\nValid body.\n"
            store.write_stage(plan_id, "implementation", body, role="architect")
            rev_store = grogu_review.ReviewStore(store)
            anchor = grogu_review.text_anchor(body, 18, 28, stage="implementation", revision=1)

            # Empty or whitespace-only raises
            with self.assertRaises(grogu_review.ReviewError):
                rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="")
            with self.assertRaises(grogu_review.ReviewError):
                rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="   \n\t  ")

            # Over MAX_COMMENT_CHARS raises
            with self.assertRaises(grogu_review.ReviewError):
                rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="x" * (grogu_review.MAX_COMMENT_CHARS + 1))

            # Thread ids monotonic c1, c2 and not reused after resolve
            t1 = rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="Comment 1")
            self.assertEqual(t1["id"], "c1")
            rev_store.resolve_thread(plan_id, "c1")
            t2 = rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="Comment 2")
            self.assertEqual(t2["id"], "c2")

            # Reply to unknown thread raises
            with self.assertRaises(grogu_review.ReviewError):
                rev_store.reply(plan_id, "c999", "Reply body")

    # 37. Atomicity and concurrency
    def test_37_atomicity_and_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            body = "# Implementation\n\nConcurrency test body.\n"
            store.write_stage(plan_id, "implementation", body, role="architect")

            store1 = grogu_review.ReviewStore(store)
            store2 = grogu_review.ReviewStore(store)
            anchor = grogu_review.text_anchor(body, 18, 30, stage="implementation", revision=1)

            def add_t(rs, text):
                return rs.add_thread(plan_id, stage="implementation", anchor=anchor, body=text)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(add_t, store1, "Thread from worker 1")
                f2 = executor.submit(add_t, store2, "Thread from worker 2")
                r1 = f1.result()
                r2 = f2.result()

            self.assertNotEqual(r1["id"], r2["id"])
            threads = store1.threads(plan_id)
            self.assertEqual(len(threads), 2)

            plan_dir = store.plan_dir(plan_id)
            tmp_files = list(plan_dir.glob("*.tmp"))
            self.assertEqual(tmp_files, [])

    # 38. Schema guard
    def test_38_schema_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            rev_store = grogu_review.ReviewStore(store)
            rev_path = rev_store.path(plan_id)
            invalid_data = {
                "schema_version": 99,
                "plan": plan_id,
                "created_at": "2026-09-04T00:00:00Z",
                "threads": [],
            }
            raw_text = json.dumps(invalid_data, indent=2)
            rev_path.write_text(raw_text, encoding="utf8")

            with self.assertRaises(grogu_review.ReviewError) as ctx:
                rev_store.load(plan_id)
            self.assertIn("99", str(ctx.exception))
            self.assertEqual(rev_path.read_text(encoding="utf8"), raw_text)

    # 39. Git-ignored and migration
    def test_39_git_ignored_and_migration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            body = "# Implementation\n\nBody.\n"
            store.write_stage(plan_id, "implementation", body, role="architect")
            rev_store = grogu_review.ReviewStore(store)
            anchor = grogu_review.text_anchor(body, 18, 22, stage="implementation", revision=1)
            rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="Comment")

            gitignore_path = Path(td) / ".grogu" / "plans" / ".gitignore"
            self.assertTrue(gitignore_path.exists())
            gitignore_text = gitignore_path.read_text(encoding="utf8")
            self.assertIn("review.json", gitignore_text)

            res = subprocess.run(["git", "status", "--porcelain"], cwd=td, capture_output=True, text=True, check=True)
            self.assertNotIn("review.json", res.stdout)

        # Migration test
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plans_dir = Path(td) / ".grogu" / "plans"
            plans_dir.mkdir(parents=True, exist_ok=True)
            gitignore_path = plans_dir / ".gitignore"
            old_gitignore = "manifest.json\nrevisions/\n*.sealed\n# custom rule\n"
            gitignore_path.write_text(old_gitignore, encoding="utf8")

            plan = store.create("Test Plan 2")
            plan_id = plan["id"]
            body = "# Implementation\n\nBody.\n"
            store.write_stage(plan_id, "implementation", body, role="architect")
            rev_store = grogu_review.ReviewStore(store)
            anchor = grogu_review.text_anchor(body, 18, 22, stage="implementation", revision=1)
            rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="Comment")

            updated_gitignore = gitignore_path.read_text(encoding="utf8")
            for req in ["manifest.json", "revisions/", "*.sealed", "review.json"]:
                self.assertIn(req, updated_gitignore)
            self.assertIn("# custom rule", updated_gitignore)

    # 40. Finalize never ships it
    def test_40_finalize_never_ships_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            body = "# Implementation\n\nBody.\n"
            store.write_stage(plan_id, "implementation", body, role="architect")
            store.write_stage(plan_id, "testing", "# Testing\n", role="architect")
            rev_store = grogu_review.ReviewStore(store)
            anchor = grogu_review.text_anchor(body, 18, 22, stage="implementation", revision=1)
            rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="Comment")

            store.set_stage_state(plan_id, "implementation", grogu_plans.COMPLETE, role="engineer")
            store.set_stage_state(plan_id, "testing", grogu_plans.COMPLETE, role="tester")
            store.finalize(plan_id, role="architect")

            res = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=td, capture_output=True, text=True, check=True)
            self.assertNotIn("review.json", res.stdout)

    # Designer's observation: rewrite stage back to earlier byte-identical body and away again
    def test_designer_observation_reanchor_cycle_to_earlier_body_and_away(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._create_git_repo(Path(td))
            plan = store.create("Test Plan")
            plan_id = plan["id"]

            body1 = "# Implementation\n\nThis is paragraph one with original target text in it.\n\nParagraph two.\n"
            store.write_stage(plan_id, "implementation", body1, role="architect")
            rev_store = grogu_review.ReviewStore(store)

            start = body1.index("original target text")
            end = start + len("original target text")
            anchor = grogu_review.text_anchor(body1, start, end, stage="implementation", revision=1)
            t0 = rev_store.add_thread(plan_id, stage="implementation", anchor=anchor, body="Comment on target")
            self.assertEqual(t0["anchor_state"], grogu_review.ANCHORED)

            # 1. Rewrite to body2: quote removed -> orphaned
            body2 = "# Implementation\n\nThis is paragraph one without the target text.\n\nParagraph two.\n"
            store.write_stage(plan_id, "implementation", body2, role="architect", replace=True)
            s2 = rev_store.sync(plan_id, role="reviewer")
            t_s2 = s2["threads"][0]
            self.assertEqual(t_s2["anchor_state"], grogu_review.ORPHANED)
            self.assertEqual(len(t_s2["anchor_history"]), 1)
            self.assertEqual(t_s2["anchor_history"][0]["from_revision"], 1)
            self.assertEqual(t_s2["anchor_history"][0]["to_revision"], 2)
            self.assertEqual(t_s2["anchor_history"][0]["state"], grogu_review.ORPHANED)

            # 2. Rewrite back to byte-identical body1
            store.write_stage(plan_id, "implementation", body1, role="architect", replace=True)
            s3 = rev_store.sync(plan_id, role="reviewer")
            t_s3 = s3["threads"][0]
            self.assertEqual(t_s3["anchor_state"], grogu_review.ANCHORED)
            self.assertEqual(t_s3["anchor"]["start"], start)
            self.assertEqual(t_s3["anchor"]["end"], end)
            self.assertEqual(len(t_s3["anchor_history"]), 2)
            self.assertEqual(t_s3["anchor_history"][1]["from_revision"], 1)
            self.assertEqual(t_s3["anchor_history"][1]["to_revision"], 3)
            self.assertEqual(t_s3["anchor_history"][1]["state"], grogu_review.ANCHORED)

            # 3. Rewrite away to body3: quote completely removed
            body3 = "# Implementation\n\nCompletely different content with nothing in common.\n"
            store.write_stage(plan_id, "implementation", body3, role="architect", replace=True)
            s4 = rev_store.sync(plan_id, role="reviewer")
            t_s4 = s4["threads"][0]
            self.assertEqual(t_s4["anchor_state"], grogu_review.ORPHANED)
            self.assertEqual(len(t_s4["anchor_history"]), 3)
            self.assertEqual(t_s4["anchor_history"][2]["from_revision"], 3)
            self.assertEqual(t_s4["anchor_history"][2]["to_revision"], 4)
            self.assertEqual(t_s4["anchor_history"][2]["state"], grogu_review.ORPHANED)


class PlanDocumentReviewAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.store = grogu_plans.PlanStore(self.root)
        self.plan = grogu_plans.PlanDocumentStore.create(
            self.store, "Review package"
        )["id"]
        self.source = (
            "# Review package — implementation plan\n\n"
            "## Work\n\nA unique phrase belongs to one graph node.\n"
        )
        self.store.write_stage(
            self.plan,
            "implementation",
            self.source,
            role="architect",
        )
        self.review = grogu_review.ReviewStore(self.store)

    def _anchor(self):
        compiled = self.store.read_stage(
            self.plan, "implementation", role="reviewer"
        )
        start = compiled.index("unique phrase")
        return grogu_review.text_anchor(
            compiled,
            start,
            start + len("unique phrase"),
            stage="implementation",
            revision=2,
        )

    def test_legacy_comment_command_writes_a_graph_thread_not_review_json(self):
        thread = self.review.add_thread(
            self.plan,
            stage="implementation",
            anchor=self._anchor(),
            body="Please make this clearer.",
        )
        self.assertFalse(self.review.path(self.plan).exists())
        self.assertTrue(thread["graph_id"].startswith("thr-"))
        document = grogu_plans.PlanDocumentStore.for_plan(
            self.store, self.plan
        ).load(role="reviewer")
        self.assertEqual(
            document["nodes"][thread["graph_id"]]["kind"], "thread"
        )

    def test_request_changes_routes_one_binding_note_through_plan_store(self):
        self.review.add_thread(
            self.plan,
            stage="implementation",
            anchor=self._anchor(),
            body="Please revise.",
        )
        review_round = self.review.request_changes(
            self.plan, note="Address the open comment", role="reviewer"
        )
        self.assertEqual(review_round["state"], grogu_review.ROUND_REQUESTED)
        steering = self.store.load(self.plan)["steering"]
        self.assertEqual(len(steering), 1)
        self.assertTrue(steering[0]["requires_replan"])

        retried = self.review.request_changes(
            self.plan, note="Address the open comment", role="reviewer"
        )
        self.assertEqual(retried["steering_seq"], review_round["steering_seq"])
        self.assertEqual(len(self.store.load(self.plan)["steering"]), 1)

    def test_concurrent_request_changes_is_idempotent(self):
        self.review.add_thread(
            self.plan,
            stage="implementation",
            anchor=self._anchor(),
            body="Please revise concurrently.",
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _index: self.review.request_changes(
                        self.plan,
                        note="Address the open comment",
                        role="reviewer",
                    ),
                    range(2),
                )
            )
        self.assertEqual(
            {result["steering_seq"] for result in results},
            {results[0]["steering_seq"]},
        )
        self.assertEqual(len(self.store.load(self.plan)["steering"]), 1)

    def test_package_reply_and_resolve_are_append_only_revisions(self):
        service = grogu_plans.PlanDocumentStore.for_plan(
            self.store, self.plan
        )
        document = service.load(role="reviewer")
        note = next(
            node
            for node in document["nodes"].values()
            if node["stage"] == "implementation" and node["kind"] == "note"
        )
        created = service.add_thread(
            role="reviewer",
            selector={"type": "node", "id": note["id"]},
            body="Initial",
        )
        self.review.reply(self.plan, created["id"], "Reply")
        self.review.resolve_thread(self.plan, created["id"], note="Done")
        revisions = service.revisions(role="reviewer")
        self.assertEqual(
            [item["revision"] for item in revisions[-3:]],
            ["r0003", "r0004", "r0005"],
        )
        self.assertEqual(
            service.threads(role="reviewer")[0]["status"], "resolved"
        )


if __name__ == "__main__":
    unittest.main()
