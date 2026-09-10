"""Small, safe Markdown renderer with exact source offsets.

The review workspace anchors comments to the canonical Markdown source.  This
renderer therefore favours a deliberately bounded dialect and predictable
source mapping over permissive Markdown compatibility.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

ALLOWED_TAGS = frozenset(
    {
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "ul",
        "ol",
        "li",
        "pre",
        "code",
        "blockquote",
        "hr",
        "em",
        "strong",
        "a",
        "span",
        "div",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
    }
)

_ATOMIC = frozenset("&<>\"")
_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})([^\n]*)$")
_HEADING_RE = re.compile(r"^( {0,3})(#{1,6})(?:[ \t]+|$)(.*)$")
_UL_RE = re.compile(r"^( *)([-+*])[ \t]+(.*)$")
_OL_RE = re.compile(r"^( *)(\d+)[.)][ \t]+(.*)$")
_QUOTE_RE = re.compile(r"^( {0,3})>[ \t]?(.*)$")
_THEMATIC_RE = re.compile(
    r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$"
)
_TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
_AUTOLINK_RE = re.compile(
    r"<((?:https?://|mailto:)[^ <>\n]+|[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9.-]+\.[A-Za-z]{2,})>"
)


def _line_records(source: str) -> list:
    records = []
    offset = 0
    for raw in source.splitlines(keepends=True):
        content = raw.rstrip("\r\n")
        records.append(
            {
                "raw": raw,
                "text": content,
                "start": offset,
                "content_end": offset + len(content),
                "end": offset + len(raw),
            }
        )
        offset += len(raw)
    if offset < len(source) or not records:
        records.append(
            {
                "raw": source[offset:],
                "text": source[offset:],
                "start": offset,
                "content_end": len(source),
                "end": len(source),
            }
        )
    return records


def _attrs(start: int, end: int) -> str:
    return f' data-block-start="{start}" data-block-end="{end}"'


def _literal(source: str, start: int, end: int) -> str:
    """Render literal source text, isolating characters changed by escaping."""
    if start >= end:
        return ""
    pieces = []
    run_start = start
    for position in range(start, end):
        character = source[position]
        if character not in _ATOMIC:
            continue
        if run_start < position:
            value = source[run_start:position]
            pieces.append(
                f'<span data-s="{run_start}" data-e="{position}">{value}</span>'
            )
        pieces.append(
            f'<span data-s="{position}" data-e="{position + 1}" '
            f'data-atomic="1">{html.escape(character, quote=True)}</span>'
        )
        run_start = position + 1
    if run_start < end:
        value = source[run_start:end]
        pieces.append(f'<span data-s="{run_start}" data-e="{end}">{value}</span>')
    return "".join(pieces)


def _safe_href(target: str) -> bool:
    if target.startswith("#"):
        return True
    try:
        parsed = urlsplit(target)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https", "mailto"}


def _link_attributes(target: str) -> str:
    return (
        f' href="{html.escape(target, quote=True)}"'
        ' rel="noreferrer noopener" target="_blank"'
    )


def _find_closing(text: str, marker: str, start: int) -> int:
    position = text.find(marker, start)
    return position if position >= start else -1


def _inline(source: str, start: int, end: int) -> str:
    pieces = []
    position = start
    literal_start = start

    def flush(until: int) -> None:
        nonlocal literal_start
        if literal_start < until:
            pieces.append(_literal(source, literal_start, until))
        literal_start = until

    while position < end:
        # Images are intentionally links showing the URL, never outbound loads.
        if source.startswith("![", position):
            match = re.match(r"!\[([^\]\n]*)\]\(([^)\n]+)\)", source[position:end])
            if match:
                whole_end = position + match.end()
                target = match.group(2).strip()
                target_start = position + match.start(2)
                target_end = position + match.end(2)
                if _safe_href(target):
                    flush(position)
                    pieces.append(
                        f'<a class="image-link"{_link_attributes(target)}>'
                        f"{_literal(source, target_start, target_end)}</a>"
                    )
                    position = whole_end
                    literal_start = position
                    continue

        if source[position] == "[":
            match = re.match(r"\[([^\]\n]+)\]\(([^)\n]+)\)", source[position:end])
            if match:
                whole_end = position + match.end()
                target = match.group(2).strip()
                if _safe_href(target):
                    label_start = position + match.start(1)
                    label_end = position + match.end(1)
                    flush(position)
                    pieces.append(
                        f"<a{_link_attributes(target)}>"
                        f"{_inline(source, label_start, label_end)}</a>"
                    )
                    position = whole_end
                    literal_start = position
                    continue

        if source[position] == "<":
            match = _AUTOLINK_RE.match(source, position, end)
            if match:
                value = match.group(1)
                target = value if ":" in value.split("@", 1)[0] else f"mailto:{value}"
                if _safe_href(target):
                    text_start, text_end = match.span(1)
                    flush(position)
                    pieces.append(
                        f"<a{_link_attributes(target)}>"
                        f"{_literal(source, text_start, text_end)}</a>"
                    )
                    position = match.end()
                    literal_start = position
                    continue

        if source[position] == "`":
            run = 1
            while position + run < end and source[position + run] == "`":
                run += 1
            marker = "`" * run
            closing = _find_closing(source, marker, position + run)
            if 0 <= closing < end:
                flush(position)
                pieces.append(
                    f"<code>{_literal(source, position + run, closing)}</code>"
                )
                position = closing + run
                literal_start = position
                continue

        if source.startswith("**", position) or source.startswith("__", position):
            marker = source[position : position + 2]
            closing = _find_closing(source, marker, position + 2)
            if position + 2 < closing < end:
                flush(position)
                pieces.append(
                    f"<strong>{_inline(source, position + 2, closing)}</strong>"
                )
                position = closing + 2
                literal_start = position
                continue

        if source[position] in "*_":
            marker = source[position]
            closing = _find_closing(source, marker, position + 1)
            if position + 1 < closing < end:
                flush(position)
                pieces.append(f"<em>{_inline(source, position + 1, closing)}</em>")
                position = closing + 1
                literal_start = position
                continue

        position += 1

    flush(end)
    return "".join(pieces)


def _cells(line: dict) -> list:
    text = line["text"]
    left = len(text) - len(text.lstrip())
    right = len(text.rstrip())
    if left < right and text[left] == "|":
        left += 1
    if right > left and text[right - 1] == "|":
        right -= 1
    cells = []
    cursor = left
    while cursor <= right:
        separator = text.find("|", cursor, right)
        cell_end = right if separator < 0 else separator
        content_start = cursor
        while content_start < cell_end and text[content_start].isspace():
            content_start += 1
        content_end = cell_end
        while content_end > content_start and text[content_end - 1].isspace():
            content_end -= 1
        cells.append(
            (
                line["start"] + content_start,
                line["start"] + content_end,
                text[content_start:content_end],
            )
        )
        if separator < 0:
            break
        cursor = separator + 1
    return cells


def _is_table_separator(line: dict) -> bool:
    cells = _cells(line)
    return bool(cells) and all(_TABLE_SEPARATOR_RE.fullmatch(cell[2]) for cell in cells)


def _block_kind(line: dict, following: dict | None = None) -> bool:
    text = line["text"]
    return bool(
        not text.strip()
        or _FENCE_RE.match(text)
        or _HEADING_RE.match(text)
        or _UL_RE.match(text)
        or _OL_RE.match(text)
        or _QUOTE_RE.match(text)
        or _THEMATIC_RE.match(text)
        or text.startswith("    ")
        or (following is not None and "|" in text and _is_table_separator(following))
    )


def _list_item(line: dict, match: re.Match) -> dict:
    return {
        "start": line["start"],
        "end": line["end"],
        "parts": [
            {
                "kind": "content",
                "segments": [
                    (
                        line["start"] + match.start(3),
                        line["start"] + match.end(3),
                        line["end"],
                    )
                ],
            }
        ],
    }


def _append_continuation(item: dict, line: dict, content_start: int) -> None:
    segment = (
        line["start"] + content_start,
        line["content_end"],
        line["end"],
    )
    if item["parts"][-1]["kind"] == "content":
        item["parts"][-1]["segments"].append(segment)
    else:
        item["parts"].append({"kind": "content", "segments": [segment]})
    item["end"] = line["end"]


def _render_list_content(source: str, segments: list) -> str:
    content = []
    for position, (start, end, raw_end) in enumerate(segments):
        content.append(_inline(source, start, end))
        if position + 1 < len(segments) and end < raw_end:
            content.append(_literal(source, end, raw_end))
    return "".join(content)


def _render_list_group(source: str, group: dict) -> str:
    parts = [
        f"<{group['tag']}{_attrs(group['start'], group['end'])}>"
    ]
    for item in group["items"]:
        parts.append(f"<li{_attrs(item['start'], item['end'])}>")
        for item_part in item["parts"]:
            if item_part["kind"] == "content":
                parts.append(
                    _render_list_content(source, item_part["segments"])
                )
            else:
                parts.append(_render_list_group(source, item_part["group"]))
        parts.append("</li>")
    parts.append(f"</{group['tag']}>")
    return "".join(parts)


def _parse_list(source: str, lines: list, start_index: int) -> tuple:
    first_line = lines[start_index]
    first_unordered = _UL_RE.match(first_line["text"])
    first_ordered = _OL_RE.match(first_line["text"])
    first = first_unordered or first_ordered
    outer_ordered = first_ordered is not None
    base_indent = len(first.group(1))
    group = {
        "tag": "ol" if outer_ordered else "ul",
        "start": first_line["start"],
        "end": first_line["end"],
        "items": [],
    }
    current_top = None
    current_nested_group = None
    current_nested_item = None
    nested_indent = -1
    index = start_index

    while index < len(lines):
        line = lines[index]
        text = line["text"]
        unordered = _UL_RE.match(text)
        ordered = _OL_RE.match(text)
        match = unordered or ordered

        if match:
            indent = len(match.group(1))
            item_ordered = ordered is not None
            if indent == base_indent:
                if item_ordered != outer_ordered:
                    break
                current_top = _list_item(line, match)
                group["items"].append(current_top)
                current_nested_group = None
                current_nested_item = None
                nested_indent = -1
            elif base_indent < indent <= base_indent + 4 and current_top is not None:
                wanted_tag = "ol" if item_ordered else "ul"
                if nested_indent < 0:
                    nested_indent = indent
                if indent != nested_indent:
                    # A deeper list is outside the supported one-level nesting.
                    content_start = len(text) - len(text.lstrip())
                    target = current_nested_item or current_top
                    _append_continuation(target, line, content_start)
                    current_top["end"] = line["end"]
                    if current_nested_group is not None:
                        current_nested_group["end"] = line["end"]
                    index += 1
                    continue
                if (
                    current_nested_group is None
                    or current_nested_group["tag"] != wanted_tag
                ):
                    current_nested_group = {
                        "tag": wanted_tag,
                        "start": line["start"],
                        "end": line["end"],
                        "items": [],
                    }
                    current_top["parts"].append(
                        {"kind": "list", "group": current_nested_group}
                    )
                current_nested_item = _list_item(line, match)
                current_nested_group["items"].append(current_nested_item)
                current_nested_group["end"] = line["end"]
                current_top["end"] = line["end"]
            else:
                break
            group["end"] = line["end"]
            index += 1
            continue

        if not text.strip():
            break
        indent = len(text) - len(text.lstrip())
        if indent <= base_indent or current_top is None:
            break
        content_start = indent
        if current_nested_item is not None and indent > nested_indent:
            _append_continuation(current_nested_item, line, content_start)
            current_nested_group["end"] = line["end"]
        else:
            _append_continuation(current_top, line, content_start)
            current_nested_group = None
            current_nested_item = None
            nested_indent = -1
        current_top["end"] = line["end"]
        group["end"] = line["end"]
        index += 1

    return _render_list_group(source, group), index, group["end"]


def render_document(source: str) -> dict:
    """Render the supported Markdown subset and return its source map."""
    if not isinstance(source, str):
        raise TypeError("source must be a string")

    lines = _line_records(source)
    rendered = []
    blocks = []
    code_blocks = []
    code_index = 0
    mermaid_index = 0
    index = 0

    def add_block(kind: str, level: int, start: int, end: int) -> None:
        blocks.append({"kind": kind, "level": level, "start": start, "end": end})

    while index < len(lines):
        line = lines[index]
        text = line["text"]
        if not text.strip():
            index += 1
            continue

        fence = _FENCE_RE.match(text)
        if fence:
            marker = fence.group(2)
            language = fence.group(3).strip().split(None, 1)[0] if fence.group(3).strip() else ""
            body_start = line["end"]
            cursor = index + 1
            closing = None
            while cursor < len(lines):
                candidate = lines[cursor]["text"]
                match = re.match(r"^ {0,3}(`+|~+)[ \t]*$", candidate)
                if (
                    match
                    and match.group(1)[0] == marker[0]
                    and len(match.group(1)) >= len(marker)
                ):
                    closing = cursor
                    break
                cursor += 1
            body_end = lines[closing]["start"] if closing is not None else len(source)
            block_end = lines[closing]["end"] if closing is not None else len(source)
            attributes = (
                _attrs(line["start"], block_end)
                + f' data-code-index="{code_index}"'
            )
            if language.lower() == "mermaid":
                attributes += (
                    f' data-mermaid-index="{mermaid_index}"'
                    f' data-mermaid-src-start="{body_start}"'
                    f' data-mermaid-src-end="{body_end}"'
                )
                mermaid_index += 1
            class_name = (
                f' class="language-{html.escape(language, quote=True)}"'
                if language
                else ""
            )
            rendered.append(
                f"<pre{attributes}><code{class_name}>"
                f"{_literal(source, body_start, body_end)}</code></pre>"
            )
            add_block("code", 0, line["start"], block_end)
            code_blocks.append(
                {
                    "lang": language,
                    "start": line["start"],
                    "end": block_end,
                    "body_start": body_start,
                    "body_end": body_end,
                    "body": source[body_start:body_end],
                }
            )
            code_index += 1
            index = (closing + 1) if closing is not None else len(lines)
            continue

        heading = _HEADING_RE.match(text)
        if heading:
            level = len(heading.group(2))
            content_start = line["start"] + heading.start(3)
            content_end = line["start"] + heading.end(3)
            rendered.append(
                f"<h{level}{_attrs(line['start'], line['end'])}>"
                f"{_inline(source, content_start, content_end)}</h{level}>"
            )
            add_block("heading", level, line["start"], line["end"])
            index += 1
            continue

        if _THEMATIC_RE.match(text):
            rendered.append(f"<hr{_attrs(line['start'], line['end'])}>")
            add_block("rule", 0, line["start"], line["end"])
            index += 1
            continue

        if text.startswith("    "):
            start_index = index
            content = []
            while index < len(lines):
                current = lines[index]
                if current["text"].startswith("    "):
                    content.append((current["start"] + 4, current["end"]))
                    index += 1
                elif not current["text"].strip():
                    content.append((current["start"], current["end"]))
                    index += 1
                else:
                    break
            block_start = lines[start_index]["start"]
            block_end = lines[index - 1]["end"]
            rendered.append(
                f"<pre{_attrs(block_start, block_end)} data-code-index=\"{code_index}\">"
                f"<code>{''.join(_literal(source, start, end) for start, end in content)}"
                "</code></pre>"
            )
            add_block("code", 0, block_start, block_end)
            body_start = content[0][0]
            body_end = content[-1][1]
            code_blocks.append(
                {
                    "lang": "",
                    "start": block_start,
                    "end": block_end,
                    "body_start": body_start,
                    "body_end": body_end,
                    "body": "".join(source[start:end] for start, end in content),
                }
            )
            code_index += 1
            continue

        next_line = lines[index + 1] if index + 1 < len(lines) else None
        if "|" in text and next_line is not None and _is_table_separator(next_line):
            start_index = index
            header = _cells(line)
            index += 2
            rows = []
            while (
                index < len(lines)
                and "|" in lines[index]["text"]
                and lines[index]["text"].strip()
            ):
                rows.append(_cells(lines[index]))
                index += 1
            block_end = lines[index - 1]["end"]
            parts = [f"<table{_attrs(line['start'], block_end)}><thead><tr>"]
            for start, end, _ in header:
                parts.append(f"<th>{_inline(source, start, end)}</th>")
            parts.append("</tr></thead><tbody>")
            for row in rows:
                parts.append("<tr>")
                for start, end, _ in row:
                    parts.append(f"<td>{_inline(source, start, end)}</td>")
                parts.append("</tr>")
            parts.append("</tbody></table>")
            rendered.append("".join(parts))
            add_block("table", 0, lines[start_index]["start"], block_end)
            continue

        list_match = _UL_RE.match(text) or _OL_RE.match(text)
        if list_match:
            start_index = index
            list_html, index, block_end = _parse_list(source, lines, index)
            rendered.append(list_html)
            add_block("list", 0, lines[start_index]["start"], block_end)
            continue

        quote = _QUOTE_RE.match(text)
        if quote:
            start_index = index
            quote_lines = []
            while index < len(lines):
                match = _QUOTE_RE.match(lines[index]["text"])
                if not match:
                    break
                quote_lines.append(
                    (
                        lines[index]["start"] + match.start(2),
                        lines[index]["start"] + match.end(2),
                        lines[index]["end"],
                    )
                )
                index += 1
            block_end = lines[index - 1]["end"]
            content = []
            for position, (start, end, raw_end) in enumerate(quote_lines):
                content.append(_inline(source, start, end))
                if position + 1 < len(quote_lines) and end < raw_end:
                    content.append(_literal(source, end, raw_end))
            rendered.append(
                f"<blockquote{_attrs(lines[start_index]['start'], block_end)}>"
                f"<p>{''.join(content)}</p></blockquote>"
            )
            add_block("quote", 0, lines[start_index]["start"], block_end)
            continue

        start_index = index
        paragraph_lines = []
        while index < len(lines):
            current = lines[index]
            following = lines[index + 1] if index + 1 < len(lines) else None
            if index > start_index and _block_kind(current, following):
                if re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", current["text"]):
                    paragraph_lines.append(current)
                    index += 1
                    continue
                break
            if not current["text"].strip():
                break
            paragraph_lines.append(current)
            index += 1
        block_start = paragraph_lines[0]["start"]
        block_end = paragraph_lines[-1]["end"]
        content = []
        for position, current in enumerate(paragraph_lines):
            content.append(_inline(source, current["start"], current["content_end"]))
            if position + 1 < len(paragraph_lines) and current["content_end"] < current["end"]:
                content.append(_literal(source, current["content_end"], current["end"]))
        rendered.append(f"<p{_attrs(block_start, block_end)}>{''.join(content)}</p>")
        add_block("paragraph", 0, block_start, block_end)

    return {"html": "".join(rendered), "blocks": blocks, "code_blocks": code_blocks}


def render(source: str) -> str:
    """Render supported Markdown to safe HTML."""
    return render_document(source)["html"]


def _list_continuation_regression() -> None:
    """Keep wrapped list text and its child list inside the same list item.

    >>> sample = "- Parent text\\n  continues here\\n  - Child text\\n    continues too\\n- Next\\n"
    >>> output = render(sample)
    >>> output.count("<ul") == 2 and output.count("<li") == 3
    True
    >>> output.count("<p") == 0
    True
    >>> output.index("continues here") < output.index("<ul", output.index("<ul") + 1)
    True
    >>> after_nested = render("- Parent\\n  - Child\\n  after child\\n")
    >>> after_nested.index("Child") < after_nested.index("after child")
    True
    """
