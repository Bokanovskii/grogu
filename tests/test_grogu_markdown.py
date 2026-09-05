import html.parser
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401

import grogu_markdown  # noqa: E402
import grogu_plans  # noqa: E402

FIXTURES_REVIEW = ROOT / "tests" / "fixtures" / "review"


class _SpanCollector(html.parser.HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source
        self.spans = []
        self._current_span = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr_dict = dict(attrs)
        if "data-s" in attr_dict and "data-e" in attr_dict:
            start = int(attr_dict["data-s"])
            end = int(attr_dict["data-e"])
            atomic = attr_dict.get("data-atomic") == "1"
            self._current_span = (start, end, atomic, [])
            self.spans.append(self._current_span)

    def handle_endtag(self, tag: str) -> None:
        if self._current_span is not None:
            self._current_span = None

    def handle_data(self, data: str) -> None:
        if self._current_span is not None:
            self._current_span[3].append(data)


class _TagCollector(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags = set()
        self.attrs = set()

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self.tags.add(tag)
        for name, _value in attrs:
            self.attrs.add(name)


class GroguMarkdownTests(unittest.TestCase):
    def _assert_byte_identity(self, source: str, document: dict | None = None) -> None:
        doc = document if document is not None else grogu_markdown.render_document(source)
        collector = _SpanCollector(source)
        collector.feed(doc["html"])
        self.assertTrue(len(collector.spans) > 0, "no spans collected from document")
        for start, end, _atomic, chunks in collector.spans:
            text_content = "".join(chunks)
            expected = source[start:end]
            self.assertEqual(
                text_content,
                expected,
                f"Byte mismatch at [{start}:{end}]: {text_content!r} != {expected!r}",
            )

    # 1. Byte identity
    def test_01_byte_identity(self) -> None:
        # Fixture files in tests/fixtures/review/
        for fixture_path in FIXTURES_REVIEW.glob("*.md"):
            source = fixture_path.read_text(encoding="utf8")
            self._assert_byte_identity(source)

        # Output of grogu_plans.design_template("X")
        template_source = grogu_plans.design_template("Review Feature")
        self._assert_byte_identity(template_source)

        # Implementation stage body of a plan created in test
        with tempfile.TemporaryDirectory() as temp_dir:
            store = grogu_plans.PlanStore(Path(temp_dir))
            plan = store.create("Test Plan")
            plan_id = plan["id"]
            impl_body = (
                "# Implementation\n\n"
                "This is the **implementation** plan.\n\n"
                "- Item 1 with `code`\n"
                "- Item 2 with [link](https://example.com)\n\n"
                "```python\n"
                "def test():\n"
                "    return True\n"
                "```\n"
            )
            store.write_stage(plan_id, "implementation", impl_body, role="architect")
            read_impl = store.read_stage(plan_id, "implementation", role="architect")
            self._assert_byte_identity(read_impl)

    # 2. Atomic escapes
    def test_02_atomic_escapes(self) -> None:
        doc_source = 'a < b & c > d "e"'
        doc = grogu_markdown.render_document(doc_source)
        self._assert_byte_identity(doc_source, doc)

        collector = _SpanCollector(doc_source)
        collector.feed(doc["html"])
        atomic_spans = [s for s in collector.spans if s[2]]
        self.assertEqual(len(atomic_spans), 5)  # <, &, >, ", "
        for start, end, atomic, chunks in atomic_spans:
            self.assertTrue(atomic)
            self.assertEqual(end - start, 1)
            char = doc_source[start:end]
            self.assertIn(char, '&<>"')

    # 3. Closed tag set
    def test_03_closed_tag_set(self) -> None:
        adversarial = (
            "# Adversarial\n\n"
            "<script>alert(1)</script>\n\n"
            '<img src=x onerror=alert(1)>\n\n'
            '<iframe src="https://evil.com"></iframe>\n\n'
            "<!-- HTML comment -->\n\n"
            "A stray < character here.\n"
        )
        sources = [adversarial]
        for fixture_path in FIXTURES_REVIEW.glob("*.md"):
            sources.append(fixture_path.read_text(encoding="utf8"))

        for source in sources:
            html_out = grogu_markdown.render(source)
            collector = _TagCollector()
            collector.feed(html_out)
            for tag in collector.tags:
                self.assertIn(
                    tag,
                    grogu_markdown.ALLOWED_TAGS,
                    f"Disallowed tag <{tag}> emitted in output",
                )
            self.assertNotIn("script", collector.tags)
            self.assertNotIn("iframe", collector.tags)
            self.assertNotIn("img", collector.tags)
            self.assertNotIn("onerror", collector.attrs)

        adv_html = grogu_markdown.render(adversarial)
        self.assertNotIn("<script", adv_html.lower())
        self.assertNotIn("<iframe", adv_html.lower())
        self.assertNotIn("<img", adv_html.lower())
        # The literal text is present as escaped text so the reader sees what the plan said
        collector = _SpanCollector(adversarial)
        collector.feed(adv_html)
        text_content = "".join("".join(chunks) for _, _, _, chunks in collector.spans)
        self.assertIn("<script>alert(1)</script>", text_content)

    # 4. No images
    def test_04_no_images(self) -> None:
        source = "Here is an image: ![alt text](https://example.com/x.png) in text."
        html_out = grogu_markdown.render(source)
        self.assertNotIn("<img", html_out)
        self.assertIn("https://example.com/x.png", html_out)
        self.assertIn('class="image-link"', html_out)

    # 5. Link schemes
    def test_05_link_schemes(self) -> None:
        allowed = "[a](https://x) and [b](mailto:a@b) and [c](#frag)"
        html_allowed = grogu_markdown.render(allowed)
        self.assertIn('href="https://x"', html_allowed)
        self.assertIn('href="mailto:a@b"', html_allowed)
        self.assertIn('href="#frag"', html_allowed)
        self.assertIn('rel="noreferrer noopener"', html_allowed)

        rejected = (
            "[d](javascript:alert(1)) and [e](data:text/html,abc) and [f](vbscript:foo)"
        )
        html_rejected = grogu_markdown.render(rejected)
        self.assertNotIn("<a", html_rejected)
        self.assertNotIn("href", html_rejected)

    # 6. Subset coverage
    def test_06_subset_coverage(self) -> None:
        source = (
            "# Heading 1\n"
            "## Heading 2\n"
            "### Heading 3\n"
            "#### Heading 4\n"
            "##### Heading 5\n"
            "###### Heading 6\n\n"
            "A plain paragraph with **strong** text, *emphasis*, `inline code`, "
            "[a link](https://example.com), and an autolink <https://grogu.example>.\n\n"
            "- List item 1\n"
            "- List item 2\n"
            "  - Nested unordered item\n"
            "* Star item\n"
            "+ Plus item\n\n"
            "1. Ordered item 1\n"
            "2. Ordered item 2\n"
            "   1. Nested ordered item\n\n"
            "```python\n"
            "def hello():\n"
            "    return 'world'\n"
            "```\n\n"
            "```\n"
            "plain fence with no lang\n"
            "```\n\n"
            "    indented code block line 1\n"
            "    indented code block line 2\n\n"
            "> A block quote\n"
            "> second line of quote\n\n"
            "---\n\n"
            "| Header 1 | Header 2 |\n"
            "| --- | --- |\n"
            "| Cell 1 | Cell 2 |\n"
        )
        doc = grogu_markdown.render_document(source)
        html_out = doc["html"]
        for h in range(1, 7):
            self.assertIn(f"<h{h}", html_out)
        self.assertIn("<p", html_out)
        self.assertIn("<strong", html_out)
        self.assertIn("<em", html_out)
        self.assertIn("<code", html_out)
        self.assertIn("<a", html_out)
        self.assertIn("<ul", html_out)
        self.assertIn("<ol", html_out)
        self.assertIn("<li", html_out)
        self.assertIn("<pre", html_out)
        self.assertIn("<blockquote", html_out)
        self.assertIn("<hr", html_out)
        self.assertIn("<table", html_out)
        self.assertIn("<thead", html_out)
        self.assertIn("<tbody", html_out)
        self.assertIn("<tr", html_out)
        self.assertIn("<th", html_out)
        self.assertIn("<td", html_out)
        self._assert_byte_identity(source, doc)

    # 7. Unsupported constructs render literally
    def test_07_unsupported_constructs_render_literally(self) -> None:
        source = (
            "Setext Heading Title\n"
            "===\n\n"
            "Footnote reference [^1] in a sentence.\n\n"
            "Term\n"
            ": Definition line\n"
        )
        html_out = grogu_markdown.render(source)
        self.assertIn("Setext Heading Title", html_out)
        self.assertIn("===", html_out)
        self.assertIn("[^1]", html_out)
        self.assertIn(": Definition line", html_out)

    # 8. code_blocks accuracy
    def test_08_code_blocks_accuracy(self) -> None:
        source = (
            "Intro paragraph.\n\n"
            "```python\n"
            "def foo():\n"
            "    return 42\n"
            "```\n\n"
            "```mermaid\n"
            "flowchart TD\n"
            "  A --> B\n"
            "```\n\n"
            "```sh\n"
            "echo hello\n"
            "```\n\n"
            "End paragraph.\n"
        )
        doc = grogu_markdown.render_document(source)
        code_blocks = doc["code_blocks"]
        self.assertEqual(len(code_blocks), 3)

        self.assertEqual(code_blocks[0]["lang"], "python")
        self.assertEqual(code_blocks[1]["lang"], "mermaid")
        self.assertEqual(code_blocks[2]["lang"], "sh")

        for block in code_blocks:
            body_start = block["body_start"]
            body_end = block["body_end"]
            expected_body = block["body"]
            self.assertEqual(source[body_start:body_end], expected_body)
            self.assertFalse(expected_body.startswith("```"))
            self.assertFalse(expected_body.endswith("```"))

    # 9. Idempotent and total
    def test_09_idempotent_and_total(self) -> None:
        cases = [
            ("", False),
            ("   \n\t  \n  ", False),
            ("Paragraph\n\n" * 10000, False),  # ~200 KB document
            ("Line 1\r\nLine 2\r\nLine 3\r\n", False),  # CRLF
            ("Lone\rReturn", False),  # lone \r
            ("\ufeff# Document with BOM\n\nContent", False),  # BOM
            ("Unterminated fence\n\n```python\ndef foo(): pass\n", False),  # Unterminated
            (
                "# Astral Characters\n\nEmoji 🎉 and CJK ideograph 𠜎 and text.",
                True,
            ),  # Astral plane
        ]
        for source, check_spans in cases:
            res = grogu_markdown.render(source)
            self.assertIsInstance(res, str)
            doc = grogu_markdown.render_document(source)
            self.assertIsInstance(doc, dict)
            if check_spans:
                self._assert_byte_identity(source, doc)


if __name__ == "__main__":
    unittest.main()
