"""Typed plan-document graph helpers.

This module is storage- and transport-neutral.  ``grogu_plans`` owns plan
permissions and sealing; the helpers here provide the graph, partition,
selector, impact, stable-id and authorized-object semantics that integration
uses without adding a second stage read path.
"""

from __future__ import annotations

import copy
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from typing import Any

import grogu_plandoc_anchor as anchors
import grogu_plandoc_canon as canon
import grogu_plandoc_schema as schema

OPEN_PARTITION = "open"
PARTITIONS = (OPEN_PARTITION, schema.TESTING, schema.EVALUATION)
IMPACT_EDGE_KINDS = frozenset(
    {
        "depends_on",
        "blocks",
        "refines",
        "contains",
        "validates",
        "diagram_edge",
    }
)
RESOLVED, SHIFTED, ORPHANED = "resolved", "shifted", "orphaned"


class PlanDocumentError(ValueError):
    """A graph operation cannot preserve the plan-document contract."""


def new_document(
    plan_id: str,
    title: str,
    *,
    revision: str = "r0000",
    canvas_snap: int = 8,
) -> dict:
    """Create an empty, valid plan-document graph."""
    return schema.validate_document(
        {
            "schema_version": 1,
            "kind": "grogu.plan_document",
            "plan_id": plan_id,
            "title": title,
            "revision": revision,
            "nodes": {},
            "edges": {},
            "canvas": {"snap": canvas_snap},
        }
    )


def document_digest(document_or_partitions: Any) -> str:
    """Return the canonical source digest used by revisions and projections."""
    return canon.digest(document_or_partitions)


def node_sort_key(node: Mapping[str, Any]) -> tuple:
    stage = str(node.get("stage", ""))
    stage_index = (
        schema.STAGES.index(stage) if stage in schema.STAGES else -1
    )
    return (
        stage_index,
        int(node.get("order", 0)),
        canon.id_sort_key(str(node.get("id", ""))),
    )


def edge_sort_key(edge: Mapping[str, Any]) -> tuple:
    return (
        str(edge.get("kind", "")),
        canon.id_sort_key(str(edge.get("to", ""))),
        canon.id_sort_key(str(edge.get("id", ""))),
    )


def allocate_id(
    manifest: MutableMapping[str, Any],
    kind: str,
    *,
    existing_ids: Iterable[str] = (),
) -> str:
    """Allocate and persist the next stable kind id in manifest counters.

    Allocation is intentionally mutating: callers hold the plan lock and write
    the manifest in the same generation as the revision.  Removed ids passed in
    ``existing_ids`` are still skipped and counters only ever increase.
    """
    if kind == "edge":
        prefix = "edge"
    else:
        try:
            prefix = str(schema.NODE_KINDS[kind]["prefix"])
        except KeyError as error:
            raise PlanDocumentError(f"unknown id kind {kind!r}") from error
    plandoc = manifest.setdefault("plandoc", {})
    if not isinstance(plandoc, MutableMapping):
        raise PlanDocumentError("manifest.plandoc must be an object")
    counters = plandoc.setdefault("counters", {})
    if not isinstance(counters, MutableMapping):
        raise PlanDocumentError("manifest.plandoc.counters must be an object")
    raw = counters.get(prefix, 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise PlanDocumentError(f"counter {prefix!r} must be a nonnegative integer")
    occupied = set(existing_ids)
    number = raw + 1
    candidate = f"{prefix}-{number}"
    while candidate in occupied:
        number += 1
        candidate = f"{prefix}-{number}"
    counters[prefix] = number
    return candidate


def make_node(
    node_id: str,
    kind: str,
    title: str,
    *,
    stage: str = "",
    body: str = "",
    attrs: Mapping[str, Any] | None = None,
    order: int = 1000,
    revision: str = "r0001",
    geometry: Mapping[str, int] | None = None,
) -> dict:
    """Build and validate one graph node."""
    value = {
        "id": node_id,
        "kind": kind,
        "stage": stage,
        "title": title,
        "body": body,
        "attrs": dict(attrs or {}),
        "order": order,
        "created_rev": revision,
        "updated_rev": revision,
    }
    if geometry is not None:
        value["geometry"] = dict(geometry)
    return schema.validate_node(value)


def make_edge(
    edge_id: str,
    kind: str,
    source: str,
    target: str,
    *,
    attrs: Mapping[str, Any] | None = None,
    revision: str = "r0001",
) -> dict:
    """Build and validate one graph edge."""
    return schema.validate_edge(
        {
            "id": edge_id,
            "kind": kind,
            "from": source,
            "to": target,
            "attrs": dict(attrs or {}),
            "created_rev": revision,
        }
    )


def partition_for_stage(stage: str) -> str:
    """Return the package partition name for a graph stage."""
    if stage in {"", schema.DESIGN, schema.IMPLEMENTATION}:
        return OPEN_PARTITION
    if stage in {schema.TESTING, schema.EVALUATION}:
        return stage
    raise PlanDocumentError(f"unknown stage {stage!r}")


def split_partitions(document: Mapping[str, Any]) -> dict[str, dict]:
    """Split a validated graph without exposing cross-seal reference ids.

    Cross-partition ``references`` edges are stored in a sealed side whenever
    one exists.  Callers validate the merged partition set rather than treating
    an individual partition as a complete graph.
    """
    graph = schema.validate_document(document)
    partitions = {
        name: {
            "schema_version": 1,
            "kind": "grogu.plan_document",
            "plan_id": graph["plan_id"],
            "title": graph["title"],
            "revision": graph["revision"],
            "partition": name,
            "nodes": {},
            "edges": {},
        }
        for name in PARTITIONS
    }
    if "canvas" in graph:
        partitions[OPEN_PARTITION]["canvas"] = copy.deepcopy(graph["canvas"])
    if "provenance" in graph:
        for partition in partitions.values():
            partition["provenance"] = copy.deepcopy(graph["provenance"])
    for node_id, node in graph["nodes"].items():
        partitions[partition_for_stage(node["stage"])]["nodes"][node_id] = copy.deepcopy(
            node
        )
    for edge_id, edge in graph["edges"].items():
        source_partition = partition_for_stage(graph["nodes"][edge["from"]]["stage"])
        target_partition = partition_for_stage(graph["nodes"][edge["to"]]["stage"])
        if source_partition == target_partition:
            owner = source_partition
        elif source_partition != OPEN_PARTITION:
            owner = source_partition
        elif target_partition != OPEN_PARTITION:
            owner = target_partition
        else:
            owner = source_partition
        partitions[owner]["edges"][edge_id] = copy.deepcopy(edge)
    return partitions


def merge_partitions(partitions: Mapping[str, Mapping[str, Any]]) -> dict:
    """Merge a full decoded partition set and validate the complete graph."""
    available = [partitions[name] for name in PARTITIONS if name in partitions]
    if not available:
        raise PlanDocumentError("no plan-document partitions were supplied")
    first = available[0]
    merged = {
        "schema_version": 1,
        "kind": "grogu.plan_document",
        "plan_id": first["plan_id"],
        "title": first["title"],
        "revision": first["revision"],
        "nodes": {},
        "edges": {},
    }
    for partition in available:
        for field in ("plan_id", "title", "revision"):
            if partition.get(field) != merged[field]:
                raise PlanDocumentError(f"partition {field} values do not agree")
        for node_id, node in (partition.get("nodes") or {}).items():
            if node_id in merged["nodes"]:
                raise PlanDocumentError(f"duplicate node {node_id!r} across partitions")
            merged["nodes"][node_id] = copy.deepcopy(node)
        for edge_id, edge in (partition.get("edges") or {}).items():
            if edge_id in merged["edges"]:
                raise PlanDocumentError(f"duplicate edge {edge_id!r} across partitions")
            merged["edges"][edge_id] = copy.deepcopy(edge)
        if partition.get("partition") == OPEN_PARTITION and "canvas" in partition:
            merged["canvas"] = copy.deepcopy(partition["canvas"])
        if "provenance" in partition:
            merged["provenance"] = copy.deepcopy(partition["provenance"])
    return schema.validate_document(merged)


def readable_partitions(role: str) -> tuple[str, ...]:
    """Return only partition names a role may load."""
    stages = schema.ROLE_READABLE_STAGES.get(role)
    if stages is None:
        raise PlanDocumentError(f"unknown role {role!r}")
    values = []
    if stages & {schema.DESIGN, schema.IMPLEMENTATION}:
        values.append(OPEN_PARTITION)
    if schema.TESTING in stages:
        values.append(schema.TESTING)
    if schema.EVALUATION in stages:
        values.append(schema.EVALUATION)
    return tuple(values)


def load_visible(
    load_partition: Callable[[str], Mapping[str, Any]],
    role: str,
    *,
    sealed_relationship_count: int = 0,
) -> dict:
    """Load only authorized partitions and return a role-bounded graph.

    The callback is never invoked for a forbidden partition.  Edges whose other
    endpoint is not visible are omitted; only the caller-supplied aggregate
    count may be reported.
    """
    loaded = {
        name: copy.deepcopy(load_partition(name)) for name in readable_partitions(role)
    }
    if not loaded:
        raise PlanDocumentError(f"role {role!r} may not load plan stages")
    first = next(iter(loaded.values()))
    visible = {
        "schema_version": 1,
        "kind": "grogu.plan_document",
        "plan_id": first["plan_id"],
        "title": first["title"],
        "revision": first["revision"],
        "nodes": {},
        "edges": {},
        "provenance": {
            "sealed_relationships": int(max(0, sealed_relationship_count))
        },
    }
    allowed_stages = schema.ROLE_READABLE_STAGES[role] | {""}
    for partition in loaded.values():
        for node_id, node in (partition.get("nodes") or {}).items():
            if node.get("stage", "") in allowed_stages:
                visible["nodes"][node_id] = copy.deepcopy(node)
    for partition in loaded.values():
        for edge_id, edge in (partition.get("edges") or {}).items():
            if edge.get("from") in visible["nodes"] and edge.get("to") in visible["nodes"]:
                visible["edges"][edge_id] = copy.deepcopy(edge)
    if OPEN_PARTITION in loaded and "canvas" in loaded[OPEN_PARTITION]:
        visible["canvas"] = copy.deepcopy(loaded[OPEN_PARTITION]["canvas"])
    return schema.validate_document(visible)


def _edge_by_pair(document: Mapping[str, Any], selector: Mapping[str, Any]) -> dict | None:
    matches = sorted(
        (
            edge
            for edge in document["edges"].values()
            if edge["from"] == selector["from"] and edge["to"] == selector["to"]
        ),
        key=lambda edge: canon.id_sort_key(edge["id"]),
    )
    ordinal = int(selector["ordinal"])
    return matches[ordinal] if ordinal < len(matches) else None


def _attr_part(node: Mapping[str, Any], part: str) -> bool:
    if part in {"title", "body"}:
        return True
    current: Any = node
    for token in part.split("."):
        if not isinstance(current, Mapping) or token not in current:
            return False
        current = current[token]
    return True


def resolve_selector(document: Mapping[str, Any], value: Mapping[str, Any]) -> dict:
    """Resolve any selector to stable node/edge ids without fuzzy relocation."""
    graph = schema.validate_document(document)
    selector = schema.validate_selector(value)
    selector_type = selector["type"]

    if selector_type in {"node", "object", "region"}:
        node_id = selector["id"]
        node = graph["nodes"].get(node_id)
        resolved = node is not None
        if selector_type == "region":
            resolved = resolved and node["kind"] == "region"
        if selector_type == "object":
            resolved = resolved and _attr_part(node, selector["part"])
        return {
            "state": RESOLVED if resolved else ORPHANED,
            "nodes": [node_id] if resolved else [],
            "edges": [],
            "selector": selector,
        }

    if selector_type == "text":
        node = graph["nodes"].get(selector["node"])
        if node is None:
            return {
                "state": ORPHANED,
                "nodes": [],
                "edges": [],
                "selector": selector,
            }
        legacy = anchors.text_anchor_from_selector(
            selector,
            stage=node["stage"],
            revision=int(graph["revision"][1:]),
        )
        outcome = anchors.reanchor_text(legacy, node["body"])
        if outcome["state"] == anchors.ORPHANED:
            state = ORPHANED
            updated = selector
        else:
            updated = anchors.selector_from_text_anchor(
                node["id"], outcome["anchor"]
            )
            changed_position = updated["position"] != selector["position"]
            state = (
                SHIFTED
                if outcome["state"] == anchors.SHIFTED or changed_position
                else RESOLVED
            )
        return {
            "state": state,
            "nodes": [node["id"]] if state != ORPHANED else [],
            "edges": [],
            "selector": updated,
            "confidence": outcome["confidence"],
        }

    if selector_type == "edge":
        edge = (
            graph["edges"].get(selector["id"])
            if "id" in selector
            else _edge_by_pair(graph, selector)
        )
        return {
            "state": RESOLVED if edge is not None else ORPHANED,
            "nodes": (
                sorted({edge["from"], edge["to"]}, key=canon.id_sort_key)
                if edge is not None
                else []
            ),
            "edges": [edge["id"]] if edge is not None else [],
            "selector": selector,
        }

    members = [resolve_selector(graph, member) for member in selector["members"]]
    node_sets = [set(member["nodes"]) for member in members]
    edge_sets = [set(member["edges"]) for member in members]
    if selector["op"] == "any":
        nodes = set().union(*node_sets)
        edges = set().union(*edge_sets)
    else:
        nodes = set.intersection(*node_sets)
        edges = set.intersection(*edge_sets)
    if not nodes and not edges:
        state = ORPHANED
    elif any(member["state"] != RESOLVED for member in members):
        state = SHIFTED
    else:
        state = RESOLVED
    return {
        "state": state,
        "nodes": sorted(nodes, key=canon.id_sort_key),
        "edges": sorted(edges, key=canon.id_sort_key),
        "selector": selector,
    }


def _impact_adjacency(document: Mapping[str, Any]) -> dict[str, list[tuple[str, str]]]:
    adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for edge in document["edges"].values():
        if edge["kind"] not in IMPACT_EDGE_KINDS:
            continue
        adjacency[edge["from"]].append((edge["to"], edge["id"]))
        adjacency[edge["to"]].append((edge["from"], edge["id"]))
    for entries in adjacency.values():
        entries.sort(key=lambda item: (canon.id_sort_key(item[0]), canon.id_sort_key(item[1])))
    return adjacency


def _cycles_from(
    document: Mapping[str, Any], selected: set[str]
) -> list[list[str]]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in document["edges"].values():
        if edge["kind"] in IMPACT_EDGE_KINDS:
            adjacency[edge["from"]].append(edge["to"])
    for values in adjacency.values():
        values.sort(key=canon.id_sort_key)
    found: set[tuple[str, ...]] = set()

    def canonical_cycle(nodes: list[str]) -> tuple[str, ...]:
        body = nodes[:-1]
        rotations = [tuple(body[index:] + body[:index]) for index in range(len(body))]
        best = min(rotations, key=lambda cycle: tuple(canon.id_sort_key(item) for item in cycle))
        return (*best, best[0])

    def shortest_path(start: str, target: str) -> list[str] | None:
        queue = deque([(start, [start])])
        visited = {start}
        while queue:
            current, path = queue.popleft()
            for candidate in adjacency.get(current, []):
                if candidate == target:
                    return path + [target]
                if candidate not in visited:
                    visited.add(candidate)
                    queue.append((candidate, path + [candidate]))
        return None

    for origin in sorted(selected, key=canon.id_sort_key):
        for target in adjacency.get(origin, []):
            if target == origin:
                found.add((origin, origin))
                continue
            returning = shortest_path(target, origin)
            if returning is not None:
                found.add(canonical_cycle([origin, *returning]))
    return sorted(
        (list(cycle) for cycle in found),
        key=lambda cycle: tuple(canon.id_sort_key(item) for item in cycle),
    )


def compiled_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Return stable ids whose rendered facts differ between two graphs."""
    left = schema.validate_document(before)
    right = schema.validate_document(after)
    changed = set()
    for node_id in set(left["nodes"]) | set(right["nodes"]):
        if canon.digest(left["nodes"].get(node_id)) != canon.digest(
            right["nodes"].get(node_id)
        ):
            changed.add(node_id)
    for edge_id in set(left["edges"]) | set(right["edges"]):
        left_edge = left["edges"].get(edge_id)
        right_edge = right["edges"].get(edge_id)
        if canon.digest(left_edge) != canon.digest(right_edge):
            changed.add(edge_id)
            for edge in (left_edge, right_edge):
                if edge:
                    changed.update((edge["from"], edge["to"]))
    return sorted(changed, key=canon.id_sort_key)


def impact(
    document: Mapping[str, Any],
    selector: Mapping[str, Any],
    *,
    depth: int | None = None,
    proposed: Mapping[str, Any] | None = None,
) -> dict:
    """Return deterministic direct/transitive dependency impact and paths."""
    graph = schema.validate_document(document)
    if depth is not None and (isinstance(depth, bool) or not isinstance(depth, int) or depth < 0):
        raise PlanDocumentError("depth must be a nonnegative integer or None")
    resolved = resolve_selector(graph, selector)
    selected = set(resolved["nodes"])
    for edge_id in resolved["edges"]:
        edge = graph["edges"][edge_id]
        selected.update((edge["from"], edge["to"]))
    adjacency = _impact_adjacency(graph)
    queue = deque(
        (node_id, 0, [node_id])
        for node_id in sorted(selected, key=canon.id_sort_key)
    )
    shortest: dict[str, tuple[int, list[str]]] = {
        node_id: (0, [node_id]) for node_id in selected
    }
    while queue:
        current, distance, path = queue.popleft()
        if depth is not None and distance >= depth:
            continue
        for target, _edge_id in adjacency.get(current, []):
            candidate = (distance + 1, path + [target])
            previous = shortest.get(target)
            if previous is None or candidate[0] < previous[0] or (
                candidate[0] == previous[0]
                and tuple(canon.id_sort_key(item) for item in candidate[1])
                < tuple(canon.id_sort_key(item) for item in previous[1])
            ):
                shortest[target] = candidate
                queue.append((target, candidate[0], candidate[1]))
    impacted = {
        node_id: value
        for node_id, value in shortest.items()
        if node_id not in selected
    }
    direct = sorted(
        (node_id for node_id, (distance, _path) in impacted.items() if distance == 1),
        key=canon.id_sort_key,
    )
    transitive = [
        {"id": node_id, "distance": distance, "path": path}
        for node_id, (distance, path) in sorted(
            impacted.items(),
            key=lambda item: (item[1][0], canon.id_sort_key(item[0])),
        )
    ]
    return {
        "selection": resolved,
        "direct": direct,
        "transitive": transitive,
        "cycles": _cycles_from(graph, selected),
        "compiled_delta": (
            compiled_delta(graph, proposed) if proposed is not None else []
        ),
    }


def promote_diagram_edge(
    document: Mapping[str, Any],
    edge_id: str,
) -> dict:
    """Immutably promote a diagram edge to a validated dependency assertion."""
    graph = schema.validate_document(document)
    edge = graph["edges"].get(edge_id)
    if edge is None:
        raise PlanDocumentError(f"no edge {edge_id!r}")
    if edge["kind"] != "diagram_edge":
        raise PlanDocumentError(f"edge {edge_id!r} is not a diagram_edge")
    promoted = copy.deepcopy(graph)
    promoted["edges"][edge_id]["kind"] = "depends_on"
    promoted["edges"][edge_id]["attrs"] = {
        **promoted["edges"][edge_id]["attrs"],
        "promoted_from": "diagram_edge",
    }
    try:
        return schema.validate_document(promoted)
    except schema.SchemaError as error:
        raise PlanDocumentError(
            f"cannot promote {edge_id}: {error.message}"
        ) from error


def unavailable_object_ref(plan_id: str, relation: str, *, revision: str | None = None) -> dict:
    """Return a generic non-navigable reference without leaking hidden ids."""
    return {
        "plan_id": plan_id,
        "revision": revision,
        "object_id": None,
        "edge_id": None,
        "frame_id": None,
        "artifact_id": None,
        "relation": relation,
    }


def _role_visible_graph(document: Mapping[str, Any], role: str) -> dict:
    if role not in schema.ROLE_READABLE_STAGES:
        raise PlanDocumentError(f"unknown role {role!r}")
    graph = schema.validate_document(document)
    allowed_stages = schema.ROLE_READABLE_STAGES[role] | {""}
    visible_node_ids = {
        node_id
        for node_id, node in graph["nodes"].items()
        if node["stage"] in allowed_stages
    }
    filtered = {
        **graph,
        "nodes": {
            node_id: copy.deepcopy(graph["nodes"][node_id])
            for node_id in visible_node_ids
        },
        "edges": {
            edge_id: copy.deepcopy(edge)
            for edge_id, edge in graph["edges"].items()
            if edge["from"] in visible_node_ids and edge["to"] in visible_node_ids
        },
    }
    return schema.validate_document(filtered)


def authorize_object_ref(
    visible_document: Mapping[str, Any],
    value: Mapping[str, Any],
    *,
    visible_revisions: Iterable[str] = (),
    artifact_ids: Iterable[str] = (),
    revision_documents: Mapping[str, Mapping[str, Any]] | None = None,
    role: str | None = None,
) -> dict:
    """Return an authorized reference or a generic unavailable reference.

    No fuzzy relocation is attempted.  Absolute paths, URL-like artifact ids
    and targets absent from the role-visible graph are denied.
    """
    graph = (
        _role_visible_graph(visible_document, role)
        if role is not None
        else schema.validate_document(visible_document)
    )
    ref = schema.validate_object_ref(value)
    generic = unavailable_object_ref(
        graph["plan_id"], ref["relation"], revision=ref["revision"]
    )
    if ref["plan_id"] != graph["plan_id"]:
        return generic
    allowed_revisions = {graph["revision"], *visible_revisions}
    if ref["revision"] is not None and ref["revision"] not in allowed_revisions:
        return generic
    target_graph = graph
    if ref["revision"] is not None and ref["revision"] != graph["revision"]:
        if role not in schema.ROLE_READABLE_STAGES:
            return generic
        historical = (revision_documents or {}).get(ref["revision"])
        if historical is None:
            return generic
        target_graph = _role_visible_graph(historical, role)
        if target_graph["plan_id"] != graph["plan_id"]:
            return generic
    if (
        ref["object_id"] is not None
        and ref["object_id"] not in target_graph["nodes"]
    ):
        return generic
    if ref["edge_id"] is not None and ref["edge_id"] not in target_graph["edges"]:
        return generic
    if ref["frame_id"] is not None:
        frame = target_graph["nodes"].get(ref["frame_id"])
        if frame is None or frame["kind"] != "region":
            return generic
    if ref["artifact_id"] is not None:
        artifact = ref["artifact_id"]
        if (
            artifact not in set(artifact_ids)
            or artifact.startswith(("/", "\\"))
            or "://" in artifact
            or "/" in artifact
            or "\\" in artifact
        ):
            return generic
    return ref


resolve = resolve_selector
dependency_impact = impact
