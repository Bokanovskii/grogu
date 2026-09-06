"""Local review state, semantic anchors, and plan re-anchoring."""

from __future__ import annotations

import copy
import datetime as dt
import difflib
import hashlib
import json
import re
import threading
from pathlib import Path

import grogu_markdown
import grogu_mermaid
import grogu_plandoc_anchor as _plandoc_anchor
import grogu_plans

SCHEMA_VERSION = 1
CONTEXT_CHARS = 32
FUZZY_MIN_RATIO = 0.75
FUZZY_MAX_BODY = 200_000
MAX_COMMENT_CHARS = 8000

ANCHORED, SHIFTED, ORPHANED = "anchored", "shifted", "orphaned"
OPEN, RESOLVED = "open", "resolved"
ROUND_OPEN, ROUND_REQUESTED, ROUND_ANSWERED = "open", "changes_requested", "answered"


class ReviewError(Exception):
    """A review operation that cannot be completed safely."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


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
        [
            match.start()
            for match in re.finditer(f"(?={re.escape(exact)})", body)
        ]
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
        if grogu_mermaid.find_edge(
            parsed,
            str(edge.get("from", "")),
            str(edge.get("to", "")),
            int(edge.get("pair_ordinal", 0)),
        ) is not None:
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
        if any(
            item.get("id") == wanted for item in parsed.get("subgraphs", [])
        ):
            return 3
        return (
            1
            if sum(
                item.get("title") == label
                for item in parsed.get("subgraphs", [])
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
    selected = digest_matches[0] if len(digest_matches) == 1 else next(
        (
            block
            for block in digest_matches
            if block["index"] == int(anchor.get("block_index", -1))
        ),
        None,
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
            block["index"]: _block_match_level(anchor, block)
            for block in candidates
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
                surviving = {
                    item.get("id") for item in parsed.get("nodes", [])
                }
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


# The package compiler owns these anchor primitives now. Keep the public names
# here as compatibility adapters so existing callers and tests exercise the
# exact shared implementation rather than a second copy that can drift.
digest = _plandoc_anchor.digest
text_anchor = _plandoc_anchor.text_anchor
mermaid_anchor = _plandoc_anchor.mermaid_anchor
reanchor_text = _plandoc_anchor.reanchor_text
reanchor_mermaid = _plandoc_anchor.reanchor_mermaid


class ReviewStore:
    """Read and mutate one repository's local-only plan review state."""

    def __init__(self, plans: "grogu_plans.PlanStore") -> None:
        self.plans = plans
        self._mutex = threading.RLock()

    def _document(self, plan_id: str):
        if not self.plans.is_document_plan(plan_id):
            return None
        return grogu_plans.PlanDocumentStore.for_plan(self.plans, plan_id)

    @staticmethod
    def _document_role(role: str = "") -> str:
        return role or grogu_plans.current_role() or grogu_plans.REVIEWER

    def _document_state(self, plan_id: str, *, role: str = "") -> dict:
        service = self._document(plan_id)
        if service is None:
            raise ReviewError(f"{plan_id} is not a plan document package")
        effective = self._document_role(role)
        manifest = self.plans.load(plan_id)
        thread_values = service.threads(role=effective)
        aliases = {
            thread["id"]: thread.get("legacy_id") or thread["id"]
            for thread in thread_values
        }
        threads = []
        for value in thread_values:
            selector = value.get("selector", {})
            if selector.get("type") == "text":
                quote = selector.get("quote", {})
                position = selector.get("position", {})
                anchor = {
                    "kind": "text",
                    "stage": value.get("stage", ""),
                    "revision": int(service.head()[1:]),
                    "start": int(position.get("start", 0)),
                    "end": int(position.get("end", 0)),
                    "exact": str(quote.get("exact", "")),
                    "prefix": str(quote.get("prefix", "")),
                    "suffix": str(quote.get("suffix", "")),
                    "body_digest": str(selector.get("body_digest", "")),
                }
            else:
                anchor = {
                    "kind": "object",
                    "stage": value.get("stage", ""),
                    "selector": copy.deepcopy(selector),
                }
            comments = [
                {
                    "seq": index,
                    "at": comment.get("at", ""),
                    "author": comment.get("author", ""),
                    "body": comment.get("body", ""),
                }
                for index, comment in enumerate(value.get("comments", []), 1)
            ]
            threads.append(
                {
                    "id": aliases[value["id"]],
                    "graph_id": value["id"],
                    "stage": value.get("stage", ""),
                    "round": value.get("round", 1),
                    "status": value.get("status", OPEN),
                    "created_at": value.get("created_at", ""),
                    "author": comments[0]["author"] if comments else "",
                    "anchor": anchor,
                    "anchor_state": (
                        ANCHORED
                        if value.get("anchor_state") == "resolved"
                        else value.get("anchor_state", ANCHORED)
                    ),
                    "anchor_confidence": (
                        1.0 if value.get("anchor_state") == "resolved" else 0.9
                    ),
                    "anchor_revision": int(service.head()[1:]),
                    "anchor_history": copy.deepcopy(
                        value.get("anchor_history", [])
                    ),
                    "comments": comments,
                    "resolved_at": value.get("resolved_at", ""),
                    "resolved_by": value.get("resolved_by", ""),
                }
            )
        rounds = copy.deepcopy(manifest.get("review_rounds", []))
        for item in rounds:
            item["thread_ids"] = [
                aliases.get(thread_id, thread_id)
                for thread_id in item.get("thread_ids", [])
            ]
            if "requested_thread_ids" in item:
                item["requested_thread_ids"] = [
                    aliases.get(thread_id, thread_id)
                    for thread_id in item.get("requested_thread_ids", [])
                ]
        return {
            "schema_version": SCHEMA_VERSION,
            "plan": plan_id,
            "created_at": manifest.get("created_at", ""),
            "updated_at": manifest.get("updated_at", ""),
            "stage_digests": {},
            "stage_revisions": {},
            "rounds": rounds,
            "threads": threads,
        }

    def _selector_from_legacy_anchor(
        self,
        plan_id: str,
        stage: str,
        anchor: dict,
        *,
        role: str,
    ) -> dict:
        service = self._document(plan_id)
        if service is None:
            raise ReviewError(f"{plan_id} is not a plan document package")
        current_body = self.plans.read_stage(
            plan_id, stage, role=role, record=False
        )
        if anchor.get("body_digest") and anchor.get("body_digest") != digest(
            current_body
        ):
            raise ReviewError("the plan changed; reload it before adding this comment")
        document = service.load(role=role, stages=[stage], record=False)
        if anchor.get("kind") == "text":
            exact = str(anchor.get("exact", ""))
            body_matches = []
            title_matches = []
            for node in document["nodes"].values():
                if node.get("kind") == "thread":
                    continue
                start = node.get("body", "").find(exact)
                if exact and start >= 0:
                    body_matches.append((node, start))
                if exact and exact in node.get("title", ""):
                    title_matches.append(node)
            if len(body_matches) == 1:
                node, start = body_matches[0]
                return {
                    "type": "text",
                    "node": node["id"],
                    "quote": {
                        "exact": exact,
                        "prefix": node["body"][max(0, start - 32) : start],
                        "suffix": node["body"][
                            start + len(exact) : start + len(exact) + 32
                        ],
                    },
                    "position": {
                        "start": start,
                        "end": start + len(exact),
                    },
                    "body_digest": grogu_plans.grogu_plandoc_anchor.digest(
                        node["body"]
                    ),
                }
            if len(title_matches) == 1:
                return {
                    "type": "object",
                    "id": title_matches[0]["id"],
                    "part": "title",
                }
            raise ReviewError(
                "the selected text does not map to exactly one graph node"
            )
        if anchor.get("kind") == "mermaid":
            diagrams = sorted(
                (
                    node
                    for node in document["nodes"].values()
                    if node.get("kind") == "diagram"
                ),
                key=grogu_plans.grogu_plandoc.node_sort_key,
            )
            index = int(anchor.get("block_index", 0) or 0)
            if index >= len(diagrams):
                raise ReviewError("the selected diagram no longer exists")
            diagram = diagrams[index]
            if anchor.get("target") == "node":
                wanted = str(anchor.get("node_id", ""))
                child = next(
                    (
                        node
                        for node in document["nodes"].values()
                        if node.get("attrs", {}).get("diagram") == diagram["id"]
                        and node.get("attrs", {}).get("mermaid_id") == wanted
                    ),
                    None,
                )
                if child:
                    return {"type": "node", "id": child["id"]}
            return {"type": "node", "id": diagram["id"]}
        raise ReviewError("thread requires a text or Mermaid anchor")

    def path(self, plan_id: str) -> Path:
        return self.plans.plan_dir(plan_id) / "review.json"

    def _empty(self, plan_id: str) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan": plan_id,
            "created_at": "",
            "updated_at": "",
            "stage_digests": {},
            "stage_revisions": {},
            "rounds": [],
            "threads": [],
        }

    def _load_unlocked(self, plan_id: str) -> dict:
        path = self.path(plan_id)
        if not path.exists():
            return self._empty(plan_id)
        try:
            value = json.loads(path.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReviewError(f"could not read {path}: {error}") from error
        if not isinstance(value, dict):
            raise ReviewError(f"{path} does not contain a review object")
        version = value.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ReviewError(
                f"review schema version {version!r} is not supported; "
                f"this build supports version {SCHEMA_VERSION}"
            )
        if value.get("plan") not in ("", plan_id):
            raise ReviewError(
                f"{path} belongs to plan {value.get('plan')!r}, not {plan_id!r}"
            )
        result = self._empty(plan_id)
        result.update(value)
        for key in ("stage_digests", "stage_revisions"):
            if not isinstance(result.get(key), dict):
                raise ReviewError(f"{path} has invalid {key}")
        for key in ("rounds", "threads"):
            if not isinstance(result.get(key), list):
                raise ReviewError(f"{path} has invalid {key}")
        return result

    def load(self, plan_id: str) -> dict:
        self.plans.load(plan_id)
        if self._document(plan_id) is not None:
            return self._document_state(plan_id)
        with self._mutex:
            state = self._load_unlocked(plan_id)
            role = grogu_plans.current_role() or grogu_plans.REVIEWER
            return self._visible_state(state, role)

    def _write_unlocked(self, state: dict) -> dict:
        stamp = _now()
        if not state.get("created_at"):
            state["created_at"] = stamp
        state["updated_at"] = stamp
        self.plans._write_json(self.path(state["plan"]), state)
        return copy.deepcopy(state)

    @staticmethod
    def _revision(manifest: dict, stage: str) -> int:
        plandoc = manifest.get("plandoc") or {}
        head = str(plandoc.get("head", ""))
        if re.fullmatch(r"r[0-9]{4,}", head):
            return int(head[1:])
        return len(
            [
                item
                for item in manifest.get("revisions", [])
                if item.get("stage") == stage
            ]
        )

    @classmethod
    def _stage_token(cls, manifest: dict, stage: str) -> tuple:
        return (
            bool(manifest.get("stage_written", {}).get(stage)),
            cls._revision(manifest, stage),
        )

    @staticmethod
    def _readable(role: str, stage: str) -> bool:
        return stage in grogu_plans.ROLE_READABLE_STAGES.get(role, frozenset())

    @classmethod
    def _visible_state(cls, state: dict, role: str) -> dict:
        visible = copy.deepcopy(state)
        visible["stage_digests"] = {
            stage: value
            for stage, value in visible["stage_digests"].items()
            if cls._readable(role, stage)
        }
        visible["stage_revisions"] = {
            stage: value
            for stage, value in visible["stage_revisions"].items()
            if cls._readable(role, stage)
        }
        visible["threads"] = [
            thread
            for thread in visible["threads"]
            if cls._readable(role, str(thread.get("stage", "")))
        ]
        visible_ids = {thread.get("id") for thread in visible["threads"]}
        for review_round in visible["rounds"]:
            review_round["thread_ids"] = [
                thread_id
                for thread_id in review_round.get("thread_ids", [])
                if thread_id in visible_ids
            ]
            if "requested_thread_ids" in review_round:
                review_round["requested_thread_ids"] = [
                    thread_id
                    for thread_id in review_round.get("requested_thread_ids", [])
                    if thread_id in visible_ids
                ]
            if "stage_digests" in review_round:
                review_round["stage_digests"] = {
                    stage: value
                    for stage, value in review_round["stage_digests"].items()
                    if cls._readable(role, stage)
                }
        return visible

    @classmethod
    def _require_readable_thread(cls, thread: dict) -> None:
        role = grogu_plans.current_role() or grogu_plans.REVIEWER
        if not cls._readable(role, str(thread.get("stage", ""))):
            raise ReviewError(
                f"role {role!r} may not access thread {thread.get('id', '')!r}"
            )

    def _read_stage_snapshot(self, plan_id: str, stage: str, role: str) -> tuple:
        for _ in range(4):
            manifest = self.plans.load(plan_id)
            token = self._stage_token(manifest, stage)  # grogu-allow-secret
            body = self.plans.read_stage(plan_id, stage, role=role)
            current = self.plans.load(plan_id)
            if self._stage_token(current, stage) == token:
                return current, body
        raise ReviewError(f"{stage} changed repeatedly while it was being read")

    def stage_view(self, plan_id: str, stage: str, *, role: str) -> dict:
        manifest = self.plans.load(plan_id)
        if stage not in grogu_plans.STAGES:
            raise ReviewError(f"unknown plan stage {stage!r}")
        written = bool(manifest.get("stage_written", {}).get(stage))
        if written:
            manifest, markdown = self._read_stage_snapshot(plan_id, stage, role)
            written = bool(manifest.get("stage_written", {}).get(stage))
            if not written:
                markdown = ""
        else:
            markdown = ""
        declined = manifest.get("declined_stages", {}).get(stage)
        state = (
            "declined"
            if stage not in manifest.get("stages", []) and declined is not None
            else manifest.get("stage_state", {}).get(stage, "declined")
        )
        document = grogu_markdown.render_document(markdown)
        mermaid = []
        mermaid_index = 0
        for block in document["code_blocks"]:
            if block["lang"].lower() != "mermaid":
                continue
            source = block["body"]
            mermaid.append(
                {
                    "index": mermaid_index,
                    "start": block["start"],
                    "end": block["end"],
                    "digest": digest(source),
                    "source": source,
                    "parsed": grogu_mermaid.parse(source),
                }
            )
            mermaid_index += 1
        return {
            "stage": stage,
            "state": state,
            "written": written,
            "revision": self._revision(manifest, stage),
            "digest": digest(markdown),
            "markdown": markdown,
            "html": document["html"],
            "blocks": document["blocks"],
            "mermaid": mermaid,
        }

    def sync(self, plan_id: str, *, role: str) -> dict:
        """Re-anchor changed readable stages, preserving every thread."""
        if self._document(plan_id) is not None:
            return self._document_state(plan_id, role=role)
        readable = grogu_plans.ROLE_READABLE_STAGES.get(role)
        if readable is None:
            raise ReviewError(f"unknown role {role!r}")
        if not self.path(plan_id).exists():
            return self._empty(plan_id)

        for _ in range(4):
            manifest = self.plans.load(plan_id)
            bodies = {}
            revisions = {}
            mermaid_blocks = {}
            tokens = {}
            for stage in manifest.get("stages", []):
                if stage not in readable or not manifest.get("stage_written", {}).get(stage):
                    continue
                snapshot, body = self._read_stage_snapshot(plan_id, stage, role)
                if not snapshot.get("stage_written", {}).get(stage):
                    continue
                bodies[stage] = body
                revisions[stage] = self._revision(snapshot, stage)
                tokens[stage] = self._stage_token(snapshot, stage)
                document = grogu_markdown.render_document(body)
                mermaid_code_blocks = [
                    block
                    for block in document["code_blocks"]
                    if block["lang"].lower() == "mermaid"
                ]
                mermaid_blocks[stage] = [
                    {
                        "index": index,
                        "start": block["start"],
                        "end": block["end"],
                        "source": block["body"],
                        "digest": digest(block["body"]),
                        "parsed": grogu_mermaid.parse(block["body"]),
                    }
                    for index, block in enumerate(mermaid_code_blocks)
                ]

            with self._mutex, self.plans.locked():
                current_manifest = self.plans.load(plan_id)
                if any(
                    self._stage_token(current_manifest, stage) != token
                    for stage, token in tokens.items()
                ):
                    continue
                state = self._load_unlocked(plan_id)
                changed_stages = {
                    stage
                    for stage, body in bodies.items()
                    if state["stage_digests"].get(stage) != digest(body)
                }
                if not changed_stages:
                    return self._visible_state(state, role)

                for thread in state["threads"]:
                    stage = thread.get("stage")
                    if stage not in changed_stages:
                        continue
                    old_revision = int(thread.get("anchor_revision", 0))
                    anchor = thread.get("anchor") or {}
                    if anchor.get("kind") == "text":
                        outcome = reanchor_text(anchor, bodies[stage])
                    elif anchor.get("kind") == "mermaid":
                        outcome = reanchor_mermaid(
                            anchor, bodies[stage], mermaid_blocks.get(stage, [])
                        )
                    else:
                        outcome = {
                            "anchor": copy.deepcopy(anchor),
                            "state": ORPHANED,
                            "confidence": 0.0,
                        }
                    new_revision = revisions[stage]
                    if outcome["state"] != ORPHANED:
                        outcome["anchor"]["revision"] = new_revision
                    thread["anchor"] = outcome["anchor"]
                    thread["anchor_state"] = outcome["state"]
                    thread["anchor_confidence"] = round(float(outcome["confidence"]), 6)
                    if outcome["state"] != ORPHANED:
                        thread["anchor_revision"] = new_revision
                    thread.setdefault("anchor_history", []).append(
                        {
                            "at": _now(),
                            "from_revision": old_revision,
                            "to_revision": new_revision,
                            "state": outcome["state"],
                            "confidence": round(float(outcome["confidence"]), 6),
                        }
                    )

                for stage in changed_stages:
                    state["stage_digests"][stage] = digest(bodies[stage])
                    state["stage_revisions"][stage] = revisions[stage]

                for review_round in state["rounds"]:
                    if review_round.get("state") != ROUND_REQUESTED:
                        continue
                    baseline = review_round.get("stage_digests", {})
                    requested = review_round.get(
                        "requested_thread_ids", review_round.get("thread_ids", [])
                    )
                    stages = {
                        thread.get("stage")
                        for thread in state["threads"]
                        if thread.get("id") in requested
                    }
                    if stages and all(
                        stage in state["stage_digests"]
                        and state["stage_digests"].get(stage) != baseline.get(stage)
                        for stage in stages
                    ):
                        review_round["state"] = ROUND_ANSWERED
                        review_round["answered_at"] = _now()

                saved = self._write_unlocked(state)
                return self._visible_state(saved, role)
        raise ReviewError("plan stages changed repeatedly while review state was syncing")

    @staticmethod
    def _comment(body: str, author_name: str, seq: int) -> dict:
        if not isinstance(body, str) or not body.strip():
            raise ReviewError("comment body may not be empty")
        if len(body) > MAX_COMMENT_CHARS:
            raise ReviewError(
                f"comment is {len(body)} characters; maximum is {MAX_COMMENT_CHARS}"
            )
        return {
            "seq": seq,
            "at": _now(),
            "author": author_name or grogu_plans.actor(),
            "body": body,
        }

    @staticmethod
    def _thread(state: dict, thread_id: str) -> dict:
        thread = next(
            (item for item in state["threads"] if item.get("id") == thread_id),
            None,
        )
        if thread is None:
            raise ReviewError(f"review has no thread {thread_id!r}")
        return thread

    @staticmethod
    def _open_round(state: dict) -> dict:
        if state["rounds"] and state["rounds"][-1].get("state") == ROUND_OPEN:
            return state["rounds"][-1]
        review_round = {
            "number": len(state["rounds"]) + 1,
            "state": ROUND_OPEN,
            "opened_at": _now(),
            "requested_at": "",
            "answered_at": "",
            "steering_seq": 0,
            "thread_ids": [],
            "note": "",
        }
        state["rounds"].append(review_round)
        return review_round

    def add_thread(
        self,
        plan_id,
        *,
        stage,
        anchor,
        body,
        author="",
    ) -> dict:
        service = self._document(plan_id)
        if service is not None:
            role = self._document_role()
            selector = self._selector_from_legacy_anchor(
                plan_id, stage, anchor, role=role
            )
            value = service.add_thread(
                role=role,
                selector=selector,
                body=body,
                kind="discussion",
            )
            state = self._document_state(plan_id, role=role)
            return next(
                item
                for item in state["threads"]
                if item.get("graph_id") == value["id"]
            )
        if not isinstance(anchor, dict) or anchor.get("kind") not in {"text", "mermaid"}:
            raise ReviewError("thread requires a text or Mermaid anchor")
        if anchor.get("stage") != stage:
            raise ReviewError("thread stage does not match its anchor")
        role = grogu_plans.current_role() or grogu_plans.REVIEWER
        comment = self._comment(body, author, 1)
        for _ in range(4):
            manifest, current_body = self._read_stage_snapshot(plan_id, stage, role)
            token = self._stage_token(manifest, stage)  # grogu-allow-secret
            with self._mutex, self.plans.locked():
                current_manifest = self.plans.load(plan_id)
                if self._stage_token(current_manifest, stage) != token:
                    continue
                if anchor.get("body_digest") != digest(current_body):
                    raise ReviewError(
                        "the plan changed; reload it before adding this comment"
                    )
                state = self._load_unlocked(plan_id)
                if state.get("approval_pending"):
                    raise ReviewError("plan approval is in progress; retry the comment")
                numbers = [
                    int(match.group(1))
                    for item in state["threads"]
                    if (match := re.fullmatch(r"c(\d+)", str(item.get("id", ""))))
                ]
                thread_id = f"c{max(numbers, default=0) + 1}"
                review_round = self._open_round(state)
                revision = int(anchor.get("revision", 0))
                thread = {
                    "id": thread_id,
                    "stage": stage,
                    "round": review_round["number"],
                    "status": OPEN,
                    "created_at": _now(),
                    "author": author or grogu_plans.actor(),
                    "anchor": copy.deepcopy(anchor),
                    "anchor_state": ANCHORED,
                    "anchor_confidence": 1.0,
                    "anchor_revision": revision,
                    "anchor_history": [],
                    "comments": [comment],
                    "resolved_at": "",
                    "resolved_by": "",
                }
                state["threads"].append(thread)
                review_round["thread_ids"].append(thread_id)
                state["stage_digests"].setdefault(stage, anchor["body_digest"])
                state["stage_revisions"].setdefault(stage, revision)
                self._write_unlocked(state)
                return copy.deepcopy(thread)
        raise ReviewError(f"{stage} changed repeatedly while adding the comment")

    def reply(self, plan_id, thread_id, body, *, author="") -> dict:
        service = self._document(plan_id)
        if service is not None:
            role = self._document_role()
            value = service.reply_thread(thread_id, body, role=role)
            state = self._document_state(plan_id, role=role)
            return next(
                item
                for item in state["threads"]
                if item.get("graph_id") == value["id"]
                or item.get("id") == thread_id
            )
        with self._mutex, self.plans.locked():
            state = self._load_unlocked(plan_id)
            thread = self._thread(state, thread_id)
            self._require_readable_thread(thread)
            thread.setdefault("comments", []).append(
                self._comment(body, author, len(thread.get("comments", [])) + 1)
            )
            self._write_unlocked(state)
            return copy.deepcopy(thread)

    def resolve_thread(self, plan_id, thread_id, *, note="", author="") -> dict:
        service = self._document(plan_id)
        if service is not None:
            role = self._document_role()
            value = service.set_thread_status(
                thread_id, "resolved", role=role, note=note
            )
            state = self._document_state(plan_id, role=role)
            return next(
                item
                for item in state["threads"]
                if item.get("graph_id") == value["id"]
                or item.get("id") == thread_id
            )
        with self._mutex, self.plans.locked():
            state = self._load_unlocked(plan_id)
            thread = self._thread(state, thread_id)
            self._require_readable_thread(thread)
            if note:
                thread.setdefault("comments", []).append(
                    self._comment(note, author, len(thread.get("comments", [])) + 1)
                )
            thread["status"] = RESOLVED
            thread["resolved_at"] = _now()
            thread["resolved_by"] = author or grogu_plans.actor()
            self._write_unlocked(state)
            return copy.deepcopy(thread)

    def reopen_thread(self, plan_id, thread_id) -> dict:
        service = self._document(plan_id)
        if service is not None:
            role = self._document_role()
            value = service.set_thread_status(
                thread_id, "open", role=role
            )
            state = self._document_state(plan_id, role=role)
            return next(
                item
                for item in state["threads"]
                if item.get("graph_id") == value["id"]
                or item.get("id") == thread_id
            )
        with self._mutex, self.plans.locked():
            state = self._load_unlocked(plan_id)
            if state.get("approval_pending"):
                raise ReviewError("plan approval is in progress; retry reopening")
            thread = self._thread(state, thread_id)
            self._require_readable_thread(thread)
            thread["status"] = OPEN
            thread["resolved_at"] = ""
            thread["resolved_by"] = ""
            review_round = self._open_round(state)
            thread["round"] = review_round["number"]
            if thread_id not in review_round["thread_ids"]:
                review_round["thread_ids"].append(thread_id)
            self._write_unlocked(state)
            return copy.deepcopy(thread)

    def threads(self, plan_id, *, stage="", status="") -> list:
        if self._document(plan_id) is not None:
            values = self._document_state(plan_id)["threads"]
            if stage:
                values = [
                    thread
                    for thread in values
                    if thread.get("stage") == stage
                ]
            if status:
                values = [
                    thread
                    for thread in values
                    if thread.get("status") == status
                ]
            return copy.deepcopy(values)
        state = self.load(plan_id)
        values = state["threads"]
        if stage:
            values = [thread for thread in values if thread.get("stage") == stage]
        if status:
            values = [thread for thread in values if thread.get("status") == status]
        return copy.deepcopy(values)

    @staticmethod
    def _anchor_description(thread: dict) -> tuple:
        anchor = thread.get("anchor") or {}
        if anchor.get("kind") == "text":
            quote = str(anchor.get("exact", "")).replace("\n", " ")
            if len(quote) > 160:
                quote = quote[:159] + "…"
            return "", quote
        target = anchor.get("target", "diagram")
        if target == "edge":
            edge = anchor.get("edge") or {}
            location = f"{edge.get('from', '')}→{edge.get('to', '')}"
        else:
            location = str(
                anchor.get("node_id")
                or anchor.get("subgraph_id")
                or anchor.get("label", "")
            )
        return f" {target}:{location}", str(anchor.get("label", ""))

    def request_changes(self, plan_id, *, note="", role="") -> dict:
        effective_role = role or grogu_plans.current_role() or grogu_plans.REVIEWER
        if self._document(plan_id) is not None:
            state = self._document_state(plan_id, role=effective_role)
            review_round = (
                state["rounds"][-1]
                if state["rounds"]
                and state["rounds"][-1].get("state") == ROUND_OPEN
                else None
            )
            if review_round is None:
                raise ReviewError("there is no open review round")
            open_threads = [
                thread
                for thread in state["threads"]
                if thread.get("status") == OPEN
                and thread.get("id") in review_round.get("thread_ids", [])
            ]
            if not open_threads:
                raise ReviewError(
                    "there are no open comments to request changes for"
                )
            lines = []
            if note.strip():
                lines.extend([note.strip(), ""])
            lines.extend(
                [
                    f"Review round {review_round['number']} — "
                    f"{len(open_threads)} open comment(s). Read them with",
                    f"`grogu review list {plan_id} --json`.",
                    "",
                ]
            )
            for thread in open_threads:
                comment = thread.get("comments", [{}])[-1].get("body", "")
                lines.extend(
                    [
                        f"[{thread['id']} {thread['stage']}]",
                        "  " + str(comment).replace("\n", "\n  "),
                        "",
                    ]
                )
            steering = self.plans.steer(
                "\n".join(lines).rstrip(),
                plan_id=plan_id,
                role=grogu_plans.ARCHITECT,
                requires_replan=True,
            )
            with self.plans.locked():
                manifest = self.plans.load(plan_id)
                rounds = manifest.setdefault("review_rounds", [])
                current = next(
                    (
                        item
                        for item in rounds
                        if item.get("number") == review_round["number"]
                    ),
                    None,
                )
                if current is None:
                    raise ReviewError("review round changed while requesting changes")
                current["state"] = ROUND_REQUESTED
                current["requested_at"] = _now()
                current["note"] = note
                current["requested_thread_ids"] = [
                    thread.get("graph_id", thread["id"])
                    for thread in open_threads
                ]
                current["steering_seq"] = int(steering.get("seq", 0))
                self.plans._write_json(
                    self.plans.manifest_path(plan_id), manifest
                )
                return copy.deepcopy(current)
        with self._mutex:
            for _ in range(4):
                preliminary = self.sync(plan_id, role=effective_role)
                preliminary_round = (
                    preliminary["rounds"][-1]
                    if preliminary["rounds"]
                    and preliminary["rounds"][-1].get("state") == ROUND_OPEN
                    else None
                )
                if preliminary_round is None:
                    raise ReviewError("there is no open review round")
                preliminary_threads = [
                    thread
                    for thread in preliminary["threads"]
                    if thread.get("status") == OPEN
                    and thread.get("id")
                    in preliminary_round.get("thread_ids", [])
                ]
                if not preliminary_threads:
                    raise ReviewError(
                        "there are no open comments to request changes for"
                    )
                stages = {thread.get("stage") for thread in preliminary_threads}
                snapshots = {
                    stage: self._read_stage_snapshot(
                        plan_id, stage, effective_role
                    )
                    for stage in stages
                }
                with self.plans.locked():
                    state = self._load_unlocked(plan_id)
                    review_round = (
                        state["rounds"][-1]
                        if state["rounds"]
                        and state["rounds"][-1].get("state") == ROUND_OPEN
                        else None
                    )
                    if review_round is None:
                        raise ReviewError("there is no open review round")
                    open_threads = [
                        thread
                        for thread in state["threads"]
                        if thread.get("status") == OPEN
                        and self._readable(
                            effective_role, str(thread.get("stage", ""))
                        )
                        and thread.get("id")
                        in review_round.get("thread_ids", [])
                    ]
                    current_stages = {
                        thread.get("stage") for thread in open_threads
                    }
                    if current_stages != stages:
                        continue
                    current_manifest = self.plans.load(plan_id)
                    if any(
                        self._stage_token(current_manifest, stage)
                        != self._stage_token(snapshot, stage)
                        for stage, (snapshot, _) in snapshots.items()
                    ):
                        continue
                    stage_digests = {
                        stage: digest(body)
                        for stage, (_, body) in snapshots.items()
                    }
                    if any(
                        state["stage_digests"].get(stage) != stage_digest
                        for stage, stage_digest in stage_digests.items()
                    ):
                        continue
                    review_round["state"] = ROUND_REQUESTED
                    review_round["requested_at"] = _now()
                    review_round["note"] = note
                    review_round["requested_thread_ids"] = [
                        thread["id"] for thread in open_threads
                    ]
                    review_round["stage_digests"] = stage_digests
                    self._write_unlocked(state)
                    break
            else:
                raise ReviewError(
                    "plan stages or review threads changed repeatedly "
                    "while requesting changes"
                )

            lines = []
            if note.strip():
                lines.extend([note.strip(), ""])
            lines.extend(
                [
                    f"Review round {review_round['number']} — "
                    f"{len(open_threads)} open comment(s). Read them with",
                    f"`grogu review list {plan_id} --json`.",
                    "",
                ]
            )
            for thread in open_threads:
                comment = thread.get("comments", [{}])[-1].get("body", "")
                location, quote = self._anchor_description(thread)
                lines.extend(
                    [
                        f"[{thread['id']} {thread['stage']}{location}] "
                        f'"{quote}"',
                        "  " + str(comment).replace("\n", "\n  "),
                        "",
                    ]
                )
            try:
                steering = self.plans.steer(
                    "\n".join(lines).rstrip(),
                    plan_id=plan_id,
                    role=grogu_plans.ARCHITECT,
                    requires_replan=True,
                )
            except Exception:
                with self.plans.locked():
                    latest = self._load_unlocked(plan_id)
                    current = next(
                        (
                            item
                            for item in latest["rounds"]
                            if item.get("number") == review_round["number"]
                        ),
                        None,
                    )
                    if current is not None and not current.get("steering_seq"):
                        current["state"] = ROUND_OPEN
                        current["requested_at"] = ""
                        current["requested_thread_ids"] = []
                        current["stage_digests"] = {}
                        self._write_unlocked(latest)
                raise
            with self.plans.locked():
                latest = self._load_unlocked(plan_id)
                current = next(
                    (
                        item
                        for item in latest["rounds"]
                        if item.get("number") == review_round["number"]
                    ),
                    None,
                )
                if current is None or current.get("state") != ROUND_REQUESTED:
                    raise ReviewError("review round changed while requesting changes")
                current["steering_seq"] = int(steering.get("seq", 0))
                self._write_unlocked(latest)
                return copy.deepcopy(current)

    def approve(self, plan_id, *, confirm_open=False, note="") -> dict:
        self.plans.load(plan_id)
        if self._document(plan_id) is not None:
            open_ids = [
                thread["id"]
                for thread in self._document_state(plan_id)["threads"]
                if thread.get("status") == OPEN
            ]
            if open_ids and not confirm_open:
                raise ReviewError(
                    "open review threads must be confirmed before approval: "
                    + ", ".join(open_ids)
                )
            return self.plans.approve(plan_id, note=note)
        marker = _now()
        with self._mutex, self.plans.locked():
            state = self._load_unlocked(plan_id)
            open_ids = [
                thread["id"]
                for thread in state["threads"]
                if thread.get("status") == OPEN
            ]
            if open_ids and not confirm_open:
                raise ReviewError(
                    "open review threads must be confirmed before approval: "
                    + ", ".join(open_ids)
                )
            if state.get("approval_pending"):
                raise ReviewError("plan approval is already in progress")
            state["approval_pending"] = marker
            self._write_unlocked(state)
        try:
            return self.plans.approve(plan_id, note=note)
        finally:
            with self._mutex, self.plans.locked():
                state = self._load_unlocked(plan_id)
                if state.get("approval_pending") == marker:
                    state.pop("approval_pending", None)
                    self._write_unlocked(state)

    def summary(self, plan_id: str) -> dict:
        if self._document(plan_id) is not None:
            state = self._document_state(plan_id)
            open_count = sum(
                thread.get("status") == OPEN for thread in state["threads"]
            )
            resolved_count = sum(
                thread.get("status") == RESOLVED for thread in state["threads"]
            )
            orphaned_count = sum(
                thread.get("anchor_state") == ORPHANED
                for thread in state["threads"]
            )
            current_round = state["rounds"][-1] if state["rounds"] else {}
            return {
                "round": int(current_round.get("number", 0)),
                "round_state": current_round.get("state", ""),
                "open": open_count,
                "resolved": resolved_count,
                "orphaned": orphaned_count,
                "threads": len(state["threads"]),
            }
        role = grogu_plans.current_role() or grogu_plans.REVIEWER
        state = (
            self.sync(plan_id, role=role)
            if self.path(plan_id).exists()
            else self.load(plan_id)
        )
        open_count = sum(
            thread.get("status") == OPEN for thread in state["threads"]
        )
        resolved_count = sum(
            thread.get("status") == RESOLVED for thread in state["threads"]
        )
        orphaned_count = sum(
            thread.get("anchor_state") == ORPHANED for thread in state["threads"]
        )
        current_round = state["rounds"][-1] if state["rounds"] else {}
        return {
            "round": int(current_round.get("number", 0)),
            "round_state": current_round.get("state", ""),
            "open": open_count,
            "resolved": resolved_count,
            "orphaned": orphaned_count,
            "threads": len(state["threads"]),
        }
