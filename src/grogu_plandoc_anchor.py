"""Shared text and Mermaid anchors for reviews and plan-document selectors.

The anchoring ladder is lifted from ``grogu_review`` without behavioural
changes so existing review threads keep their exact moved/shifted/orphaned
semantics during migration.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import re

import grogu_mermaid

CONTEXT_CHARS = 32
FUZZY_MIN_RATIO = 0.75
FUZZY_MAX_BODY = 200_000

ANCHORED, SHIFTED, ORPHANED = "anchored", "shifted", "orphaned"


class ReviewError(Exception):
    """An anchor operation cannot be completed safely."""


AnchorError = ReviewError


def digest(body: str) -> str:
    """Return the stable digest used by review anchors and stage snapshots."""
    if not isinstance(body, str):
        raise TypeError("body must be a string")
    return "sha256:" + hashlib.sha256(body.encode("utf8")).hexdigest()


def text_anchor(
    body: str,
    start: int,
    end: int,
    *,
    stage: str,
    revision: int,
) -> dict:
    """Create the quote and position selector pair for a text selection."""
    if not isinstance(body, str):
        raise TypeError("body must be a string")
    if not isinstance(start, int) or not isinstance(end, int):
        raise ReviewError("text anchor offsets must be integers")
    if start < 0 or end <= start or end > len(body):
        raise ReviewError(
            f"text anchor [{start}, {end}) is outside a body of {len(body)} characters"
        )
    if not stage:
        raise ReviewError("text anchor requires a stage")
    return {
        "kind": "text",
        "stage": stage,
        "revision": int(revision),
        "start": start,
        "end": end,
        "exact": body[start:end],
        "prefix": body[max(0, start - CONTEXT_CHARS) : start],
        "suffix": body[end : end + CONTEXT_CHARS],
        "body_digest": digest(body),
    }


def _mermaid_block_source(block: dict) -> str:
    return str(block.get("source", block.get("body", "")))


def mermaid_anchor(
    *,
    stage: str,
    revision: int,
    body: str,
    block: dict,
    parsed: dict,
    target: str,
    node_id: str = "",
    edge: dict | None = None,
) -> dict:
    """Create a semantic selector for a Mermaid block target."""
    if target not in {"node", "edge", "subgraph", "diagram"}:
        raise ReviewError(f"unknown Mermaid target {target!r}")
    source = _mermaid_block_source(block)
    block_start = int(block.get("start", block.get("body_start", 0)))
    block_end = int(block.get("end", block.get("body_end", block_start + len(source))))
    value = {
        "kind": "mermaid",
        "stage": stage,
        "revision": int(revision),
        "block_index": int(block.get("index", block.get("code_index", 0))),
        "block_start": block_start,
        "block_end": block_end,
        "block_digest": digest(source),
        "target": target,
        "body_digest": digest(body),
    }
    if target == "node":
        node = grogu_mermaid.find_node(parsed, node_id)
        if node is None:
            raise ReviewError(f"Mermaid node {node_id!r} was not found")
        value["node_id"] = node_id
        value["label"] = node.get("label", node_id)
    elif target == "edge":
        if not isinstance(edge, dict):
            raise ReviewError("Mermaid edge target requires an edge")
        found = grogu_mermaid.find_edge(
            parsed,
            str(edge.get("from", "")),
            str(edge.get("to", "")),
            int(edge.get("pair_ordinal", 0)),
        )
        if found is None:
            raise ReviewError("Mermaid edge was not found")
        value["edge"] = {
            "from": found["from"],
            "to": found["to"],
            "pair_ordinal": int(found.get("pair_ordinal", 0)),
            "edge_index": int(found.get("edge_index", 0)),
            "label": found.get("label", ""),
        }
        value["label"] = found.get("label", "")
    elif target == "subgraph":
        found = next(
            (item for item in parsed.get("subgraphs", []) if item.get("id") == node_id),
            None,
        )
        if found is None:
            raise ReviewError(f"Mermaid subgraph {node_id!r} was not found")
        value["subgraph_id"] = node_id
        value["label"] = found.get("title", node_id)
    else:
        value["label"] = parsed.get("type", "diagram") or "diagram"
    return value


def _common_prefix(left: str, right: str) -> int:
    count = 0
    for first, second in zip(left, right):
        if first != second:
            break
        count += 1
    return count


def _common_suffix(left: str, right: str) -> int:
    return _common_prefix(left[::-1], right[::-1])


def _updated_text_anchor(anchor: dict, body: str, start: int, end: int) -> dict:
    updated = copy.deepcopy(anchor)
    updated.update(
        {
            "start": start,
            "end": end,
            "exact": body[start:end],
            "prefix": body[max(0, start - CONTEXT_CHARS) : start],
            "suffix": body[end : end + CONTEXT_CHARS],
            "body_digest": digest(body),
        }
    )
    return updated


def _fuzzy_match(exact: str, body: str, old_start: int) -> tuple | None:
    if not exact or not body:
        return None
    matcher = difflib.SequenceMatcher(None, exact, body, autojunk=False)
    candidates = {max(0, min(old_start, len(body)))}
    for block in matcher.get_matching_blocks():
        candidates.add(max(0, min(len(body), block.b - block.a)))
    radius = max(2, min(32, len(exact) // 4))
    expanded = {
        max(0, min(len(body), candidate + delta))
        for candidate in candidates
        for delta in range(-radius, radius + 1)
    }
    length_delta = max(2, min(32, len(exact) // 4))
    lengths = {
        max(1, len(exact) + delta)
        for delta in range(-length_delta, length_delta + 1)
    }
    best = None
    for start in sorted(expanded, key=lambda item: (abs(item - old_start), item)):
        for length in lengths:
            end = min(len(body), start + length)
            if end <= start:
                continue
            ratio = difflib.SequenceMatcher(
                None, exact, body[start:end], autojunk=False
            ).ratio()
            candidate = (ratio, -abs(start - old_start), -start, start, end)
            if best is None or candidate > best:
                best = candidate
    if best is None or best[0] < FUZZY_MIN_RATIO:
        return None
    return best[3], best[4], best[0]


def reanchor_text(anchor: dict, body: str) -> dict:
    """Re-anchor text using positions, quote context, then fuzzy matching."""
    original = copy.deepcopy(anchor)
    exact = str(anchor.get("exact", ""))
    prefix = str(anchor.get("prefix", ""))
    suffix = str(anchor.get("suffix", ""))
    start = int(anchor.get("start", 0))
    end = int(anchor.get("end", start))

    if (
        0 <= start <= end <= len(body)
        and body[start:end] == exact
        and body[max(0, start - len(prefix)) : start] == prefix
        and body[end : end + len(suffix)] == suffix
    ):
        return {
            "anchor": _updated_text_anchor(anchor, body, start, end),
            "state": ANCHORED,
            "confidence": 1.0,
        }

    occurrences = (
        [match.start() for match in re.finditer(f"(?={re.escape(exact)})", body)]
        if exact
        else []
    )
    if len(occurrences) == 1:
        new_start = occurrences[0]
        return {
            "anchor": _updated_text_anchor(
                anchor, body, new_start, new_start + len(exact)
            ),
            "state": ANCHORED,
            "confidence": 1.0,
        }
    if len(occurrences) > 1:
        scored = []
        for occurrence in occurrences:
            occurrence_end = occurrence + len(exact)
            context_score = _common_suffix(prefix, body[:occurrence]) + _common_prefix(
                suffix, body[occurrence_end:]
            )
            scored.append((context_score, occurrence))
        best_score = max(score for score, _ in scored)
        winners = [position for score, position in scored if score == best_score]
        if best_score > 0 and len(winners) == 1:
            new_start = winners[0]
            state = ANCHORED
        else:
            new_start = min(occurrences, key=lambda item: (abs(item - start), item))
            state = SHIFTED
        return {
            "anchor": _updated_text_anchor(
                anchor, body, new_start, new_start + len(exact)
            ),
            "state": state,
            "confidence": 1.0,
        }

    if len(body) <= FUZZY_MAX_BODY:
        fuzzy = _fuzzy_match(exact, body, start)
        if fuzzy is not None:
            new_start, new_end, ratio = fuzzy
            return {
                "anchor": _updated_text_anchor(anchor, body, new_start, new_end),
                "state": SHIFTED,
                "confidence": ratio,
            }

    return {"anchor": original, "state": ORPHANED, "confidence": 0.0}


def _normalise_mermaid_block(block: dict, index: int) -> dict:
    source = _mermaid_block_source(block)
    parsed = block.get("parsed")
    if not isinstance(parsed, dict):
        parsed = grogu_mermaid.parse(source)
    return {
        **block,
        "index": int(block.get("index", index)),
        "start": int(block.get("start", block.get("body_start", 0))),
        "end": int(block.get("end", block.get("body_end", 0))),
        "source": source,
        "digest": block.get("digest") or digest(source),
        "parsed": parsed,
    }


def _block_match_level(anchor: dict, block: dict) -> int:
    parsed = block["parsed"]
    target = anchor.get("target")
    if target == "node":
        node_id = str(anchor.get("node_id", ""))
        label = str(anchor.get("label", ""))
        if grogu_mermaid.find_node(parsed, node_id) is not None:
            return 3
        return (
            1
            if sum(node.get("label") == label for node in parsed.get("nodes", []))
            == 1
            else 0
        )
    if target == "edge":
        edge = anchor.get("edge") or {}
        if (
            grogu_mermaid.find_edge(
                parsed,
                str(edge.get("from", "")),
                str(edge.get("to", "")),
                int(edge.get("pair_ordinal", 0)),
            )
            is not None
        ):
            return 3
        if any(
            item.get("from") == edge.get("from")
            and item.get("to") == edge.get("to")
            for item in parsed.get("edges", [])
        ):
            return 2
        label = edge.get("label")
        return (
            1
            if label
            and sum(item.get("label") == label for item in parsed.get("edges", []))
            == 1
            else 0
        )
    if target == "subgraph":
        wanted = anchor.get("subgraph_id")
        label = anchor.get("label")
        if any(item.get("id") == wanted for item in parsed.get("subgraphs", [])):
            return 3
        return (
            1
            if sum(
                item.get("title") == label for item in parsed.get("subgraphs", [])
            )
            == 1
            else 0
        )
    return 1 if target == "diagram" else 0


def _block_has_target(anchor: dict, block: dict) -> bool:
    return _block_match_level(anchor, block) > 0


def reanchor_mermaid(anchor: dict, body: str, blocks: list) -> dict:
    """Re-anchor a semantic Mermaid target without relying on rendered DOM ids."""
    original = copy.deepcopy(anchor)
    candidates = [
        _normalise_mermaid_block(block, index)
        for index, block in enumerate(blocks)
        if str(block.get("lang", "mermaid")).lower() == "mermaid"
    ]
    digest_matches = [
        block for block in candidates if block["digest"] == anchor.get("block_digest")
    ]
    selected = (
        digest_matches[0]
        if len(digest_matches) == 1
        else next(
            (
                block
                for block in digest_matches
                if block["index"] == int(anchor.get("block_index", -1))
            ),
            None,
        )
    )
    if selected is not None and not _block_has_target(anchor, selected):
        selected = None
    if selected is None:
        indexed = next(
            (
                block
                for block in candidates
                if block["index"] == int(anchor.get("block_index", -1))
            ),
            None,
        )
        levels = {
            block["index"]: _block_match_level(anchor, block) for block in candidates
        }
        best_level = max(levels.values(), default=0)
        if (
            indexed is not None
            and best_level > 0
            and levels.get(indexed["index"]) == best_level
        ):
            selected = indexed
        elif best_level > 0:
            strongest = [
                block
                for block in candidates
                if levels.get(block["index"]) == best_level
            ]
            if len(strongest) == 1:
                selected = strongest[0]
    if selected is None:
        return {"anchor": original, "state": ORPHANED, "confidence": 0.0}

    updated = copy.deepcopy(anchor)
    updated.update(
        {
            "block_index": selected["index"],
            "block_start": selected["start"],
            "block_end": selected["end"],
            "block_digest": selected["digest"],
            "body_digest": digest(body),
        }
    )
    parsed = selected["parsed"]
    target = anchor.get("target")
    state = ANCHORED

    if target == "node":
        node = grogu_mermaid.find_node(parsed, str(anchor.get("node_id", "")))
        if node is None:
            matches = [
                item
                for item in parsed.get("nodes", [])
                if item.get("label") == anchor.get("label")
            ]
            if len(matches) != 1:
                return {"anchor": original, "state": ORPHANED, "confidence": 0.0}
            node = matches[0]
            updated["node_id"] = node["id"]
            state = SHIFTED
        updated["label"] = node.get("label", updated.get("label", ""))
    elif target == "edge":
        old_edge = anchor.get("edge") or {}
        edge = grogu_mermaid.find_edge(
            parsed,
            str(old_edge.get("from", "")),
            str(old_edge.get("to", "")),
            int(old_edge.get("pair_ordinal", 0)),
        )
        if edge is None:
            same_pair = [
                item
                for item in parsed.get("edges", [])
                if item.get("from") == old_edge.get("from")
                and item.get("to") == old_edge.get("to")
            ]
            if same_pair:
                edge = min(
                    same_pair,
                    key=lambda item: abs(
                        int(item.get("pair_ordinal", 0))
                        - int(old_edge.get("pair_ordinal", 0))
                    ),
                )
            else:
                surviving = {item.get("id") for item in parsed.get("nodes", [])}
                labelled = [
                    item
                    for item in parsed.get("edges", [])
                    if old_edge.get("label")
                    and item.get("label") == old_edge.get("label")
                    and item.get("from") in surviving
                    and item.get("to") in surviving
                ]
                if len(labelled) == 1:
                    edge = labelled[0]
            if edge is None:
                return {"anchor": original, "state": ORPHANED, "confidence": 0.0}
            state = SHIFTED
        updated["edge"] = {
            "from": edge["from"],
            "to": edge["to"],
            "pair_ordinal": int(edge.get("pair_ordinal", 0)),
            "edge_index": int(edge.get("edge_index", 0)),
            "label": edge.get("label", ""),
        }
        updated["label"] = edge.get("label", "")
    elif target == "subgraph":
        subgraph = next(
            (
                item
                for item in parsed.get("subgraphs", [])
                if item.get("id") == anchor.get("subgraph_id")
            ),
            None,
        )
        if subgraph is None:
            matches = [
                item
                for item in parsed.get("subgraphs", [])
                if item.get("title") == anchor.get("label")
            ]
            if len(matches) != 1:
                return {"anchor": original, "state": ORPHANED, "confidence": 0.0}
            subgraph = matches[0]
            updated["subgraph_id"] = subgraph["id"]
            state = SHIFTED
        updated["label"] = subgraph.get("title", updated.get("label", ""))
    elif target != "diagram":
        return {"anchor": original, "state": ORPHANED, "confidence": 0.0}

    return {
        "anchor": updated,
        "state": state,
        "confidence": 1.0 if state == ANCHORED else 0.9,
    }


def selector_from_text_anchor(node_id: str, anchor: dict) -> dict:
    """Convert the legacy flat review anchor into the plan selector union."""
    return {
        "type": "text",
        "node": node_id,
        "quote": {
            "exact": str(anchor.get("exact", "")),
            "prefix": str(anchor.get("prefix", "")),
            "suffix": str(anchor.get("suffix", "")),
        },
        "position": {
            "start": int(anchor.get("start", 0)),
            "end": int(anchor.get("end", 0)),
        },
        "body_digest": str(anchor.get("body_digest", "")),
    }


def text_anchor_from_selector(selector: dict, *, stage: str = "", revision: int = 0) -> dict:
    """Convert a plan text selector to the unchanged legacy anchor shape."""
    quote = selector.get("quote") or {}
    position = selector.get("position") or {}
    return {
        "kind": "text",
        "stage": stage,
        "revision": int(revision),
        "start": int(position.get("start", 0)),
        "end": int(position.get("end", 0)),
        "exact": str(quote.get("exact", "")),
        "prefix": str(quote.get("prefix", "")),
        "suffix": str(quote.get("suffix", "")),
        "body_digest": str(selector.get("body_digest", "")),
    }
