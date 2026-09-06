"""Deterministic role-bounded compiler and Markdown importer.

The compiler has one executable identity: ``parse(render(ir)) == ir``.
Markdown remains readable, while each item carries a short canonical metadata
line and an explicit body character count so arbitrary verbatim Markdown
bodies cannot be mistaken for compiler structure.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import os
import re
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import grogu_markdown
import grogu_mermaid
import grogu_plandoc as plandoc
import grogu_plandoc_canon as canon
import grogu_plandoc_schema as schema

COMPILER_VERSION = 1
COMPILER_ID = f"grogu-plan-compile/{COMPILER_VERSION}"
STRICT_ENV = "GROGU_PLANDOC_STRICT"

SECTION_ORDER = (
    "Directives",
    "Goals",
    "Decisions",
    "Constraints and invariants",
    "Tasks",
    "Diagrams",
    "Validation criteria",
    "Risks",
    "Open questions",
    "Discussion",
    "Comment threads",
    "Extensions",
    "Elided",
    "Provenance",
)
KIND_SECTION = {
    "directive": "Directives",
    "goal": "Goals",
    "decision": "Decisions",
    "constraint": "Constraints and invariants",
    "invariant": "Constraints and invariants",
    "task": "Tasks",
    "diagram": "Diagrams",
    "criterion": "Validation criteria",
    "risk": "Risks",
    "question": "Open questions",
    "note": "Discussion",
    "evidence": "Discussion",
    "reference": "Discussion",
    "region": "Discussion",
    "thread": "Comment threads",
}
RELATIONSHIP_LABELS = {
    "depends_on": "depends on",
    "blocks": "blocks",
    "refines": "refines",
    "contains": "contains",
    "validates": "validates",
    "supersedes": "supersedes",
    "derives_from": "derives from",
    "references": "references",
    "answers": "answers",
    "anchors": "anchors",
    "diagram_edge": "diagram edge",
}
LABEL_RELATIONSHIPS = {value: key for key, value in RELATIONSHIP_LABELS.items()}

_HEADER = re.compile(
    r"^> plan (?P<plan>[^ ·]+) · revision (?P<revision>r[0-9]{4,}) · "
    r"projection (?P<role>[^/ ·]+)/(?P<stages>[^ ·]+) · compiler (?P<compiler>[0-9]+) · "
    r"graph (?P<graph>sha256:[0-9a-f]{64}) · projection "
    r"(?P<projection>sha256:[0-9a-f]{64})$"
)
_NODE_HEADING = re.compile(
    r"^### (?P<id>[a-z][a-z0-9_]*-[0-9]+) · (?P<title>.*)$"
)
_DIRECTIVE = re.compile(
    r"^- \*\*(?P<id>[a-z][a-z0-9_]*-[0-9]+)\*\* "
    r"\((?P<binding>must|should) · (?P<audience>[^)]*)\) (?P<title>.*)$"
)
_RELATIONSHIP = re.compile(
    r"^(?P<kind>[a-z ]+): "
    r"(?P<ids>[a-z][a-z0-9_]*-[0-9]+(?:, [a-z][a-z0-9_]*-[0-9]+)*)$"
)
class CompileError(ValueError):
    """Compilation or semantic-equivalence verification failed."""

    def __init__(self, message: str, *, field: str = ""):
        self.field = field
        super().__init__(f"{field}: {message}" if field else message)


@dataclass(frozen=True)
class GraphFragment:
    """Canonical, role-bounded source sub-graph selected by ``scope``."""

    document_json: str
    spec_json: str
    source_digest: str
    relationship_stub_ids: tuple[str, ...] = field(default=(), compare=False)

    @property
    def document(self) -> dict:
        return canon.loads(self.document_json)

    @property
    def spec(self) -> dict:
        return canon.loads(self.spec_json)

    @property
    def nodes(self) -> dict:
        return self.document["nodes"]

    @property
    def edges(self) -> dict:
        return self.document["edges"]


@dataclass(frozen=True)
class ProjectionNode:
    id: str
    kind: str
    stage: str
    title: str
    body: str
    attrs_json: str
    order: int
    created_rev: str
    updated_rev: str
    geometry_json: str = ""
    ext_json: str = ""
    body_state: str = "full"

    @property
    def attrs(self) -> dict:
        return canon.loads(self.attrs_json)

    @property
    def geometry(self) -> dict | None:
        return canon.loads(self.geometry_json) if self.geometry_json else None

    @property
    def ext(self) -> dict | None:
        return canon.loads(self.ext_json) if self.ext_json else None


@dataclass(frozen=True)
class ProjectionEdge:
    id: str
    kind: str
    source: str
    target: str
    attrs_json: str
    created_rev: str
    ext_json: str = ""

    @property
    def attrs(self) -> dict:
        return canon.loads(self.attrs_json)

    @property
    def ext(self) -> dict | None:
        return canon.loads(self.ext_json) if self.ext_json else None


@dataclass(frozen=True)
class Elision:
    id: str
    kind: str
    detail: str


@dataclass(frozen=True)
class Provenance:
    plan_id: str
    revision: str
    role: str
    stages: tuple[str, ...]
    include: str
    compiler: int
    graph_digest: str
    projection_id: str
    source_at: str = ""
    source_actor: str = ""
    source_role: str = ""
    source_agent: str = ""
    sealed_relationships: int = 0
    budget_chars: int | None = None
    budget_exceeded: bool = False
    since: str = ""
    depth: int | None = None
    source_provenance_json: str = "{}"
    canvas_json: str = ""


@dataclass(frozen=True)
class ProjectionIR:
    title: str
    nodes: tuple[ProjectionNode, ...]
    edges: tuple[ProjectionEdge, ...]
    elided: tuple[Elision, ...]
    provenance: Provenance
    projection_digest: str


@dataclass(frozen=True)
class CachedProjection:
    format: str
    etag: str
    payload: bytes


def _node_from_dict(node: Mapping[str, Any], *, body_state: str = "full") -> ProjectionNode:
    return ProjectionNode(
        id=node["id"],
        kind=node["kind"],
        stage=node["stage"],
        title=node["title"],
        body=node["body"],
        attrs_json=canon.dumps(node["attrs"]),
        order=node["order"],
        created_rev=node["created_rev"],
        updated_rev=node["updated_rev"],
        geometry_json=canon.dumps(node["geometry"]) if "geometry" in node else "",
        ext_json=canon.dumps(node["ext"]) if "ext" in node else "",
        body_state=body_state,
    )


def _edge_from_dict(edge: Mapping[str, Any]) -> ProjectionEdge:
    return ProjectionEdge(
        id=edge["id"],
        kind=edge["kind"],
        source=edge["from"],
        target=edge["to"],
        attrs_json=canon.dumps(edge["attrs"]),
        created_rev=edge["created_rev"],
        ext_json=canon.dumps(edge["ext"]) if "ext" in edge else "",
    )


def _node_dict(node: ProjectionNode) -> dict:
    value = {
        "id": node.id,
        "kind": node.kind,
        "stage": node.stage,
        "title": node.title,
        "body": node.body,
        "attrs": node.attrs,
        "order": node.order,
        "created_rev": node.created_rev,
        "updated_rev": node.updated_rev,
        "body_state": node.body_state,
    }
    if node.geometry_json:
        value["geometry"] = node.geometry
    if node.ext_json:
        value["ext"] = node.ext
    return value


def _edge_dict(edge: ProjectionEdge) -> dict:
    value = {
        "id": edge.id,
        "kind": edge.kind,
        "from": edge.source,
        "to": edge.target,
        "attrs": edge.attrs,
        "created_rev": edge.created_rev,
    }
    if edge.ext_json:
        value["ext"] = edge.ext
    return value


def _provenance_dict(provenance: Provenance) -> dict:
    return {
        "plan_id": provenance.plan_id,
        "revision": provenance.revision,
        "role": provenance.role,
        "stages": list(provenance.stages),
        "include": provenance.include,
        "compiler": provenance.compiler,
        "graph_digest": provenance.graph_digest,
        "projection_id": provenance.projection_id,
        "source_at": provenance.source_at,
        "source_actor": provenance.source_actor,
        "source_role": provenance.source_role,
        "source_agent": provenance.source_agent,
        "sealed_relationships": provenance.sealed_relationships,
        "budget_chars": provenance.budget_chars,
        "budget_exceeded": provenance.budget_exceeded,
        "since": provenance.since,
        "depth": provenance.depth,
        "source_provenance": canon.loads(provenance.source_provenance_json),
        "canvas": (
            canon.loads(provenance.canvas_json)
            if provenance.canvas_json
            else None
        ),
    }


def _payload(ir: ProjectionIR, *, include_digest: bool) -> dict:
    value = {
        "schema_version": 1,
        "kind": "grogu.plan_projection",
        "title": ir.title,
        "nodes": [_node_dict(node) for node in ir.nodes],
        "edges": [_edge_dict(edge) for edge in ir.edges],
        "elided": [dataclasses.asdict(item) for item in ir.elided],
        "provenance": _provenance_dict(ir.provenance),
    }
    if include_digest:
        value["projection_digest"] = ir.projection_digest
    return value


def projection_digest(ir: ProjectionIR) -> str:
    """Digest the semantic IR without recursively including its digest."""
    return canon.digest(_payload(ir, include_digest=False))


def _stamp(ir: ProjectionIR) -> ProjectionIR:
    return replace(ir, projection_digest=projection_digest(ir))


def projection_id(spec: Mapping[str, Any]) -> str:
    return canon.digest(schema.validate_projection_spec(spec))


def cache_key(revision: str, spec: Mapping[str, Any]) -> tuple[str, str, int]:
    normalized = schema.validate_projection_spec(spec)
    return revision, canon.digest(normalized), normalized["compiler"]


def cache_path(
    package: Path,
    revision: str,
    spec: Mapping[str, Any],
    *,
    format: str = "md",
) -> Path:
    """Return the content-addressed role/spec/compiler cache path."""
    if re.fullmatch(r"r[0-9]{4,}", revision) is None:
        raise CompileError("invalid cache revision", field="revision")
    if format not in {"md", "json"}:
        raise CompileError("cache format must be md or json", field="format")
    _revision, identifier, compiler_version = cache_key(revision, spec)
    return (
        Path(package)
        / "projections"
        / revision
        / identifier.removeprefix("sha256:")
        / f"compiler-{compiler_version}.{format}.cache"
    )


def write_cache(
    package: Path,
    ir: ProjectionIR,
    spec: Mapping[str, Any],
    *,
    format: str = "md",
) -> Path:
    """Atomically write one cache entry containing its strong ETag."""
    normalized_spec = schema.validate_projection_spec(spec)
    if normalized_spec["compiler"] != COMPILER_VERSION:
        raise CompileError("cannot write a stale compiler cache", field="compiler")
    if canon.dumps(normalized_spec) != canon.dumps(
        _spec_from_provenance(ir.provenance)
    ):
        raise CompileError("cache spec differs from projection", field="spec")
    payload = (
        render(ir).encode("utf8")
        if format == "md"
        else canon.dumpb(render_json(ir))
    )
    payload_digest = hashlib.sha256(payload).hexdigest()
    header = (
        f"GROGU-PROJECTION-CACHE/1 {format} {ir.projection_digest} "
        f"{payload_digest}\n"
    ).encode("ascii")
    path = cache_path(
        package, ir.provenance.revision, spec, format=format
    )
    import grogu_plandoc_revision

    grogu_plandoc_revision.atomic_write(path, header + payload)
    return path


def read_cache(
    package: Path,
    revision: str,
    spec: Mapping[str, Any],
    *,
    format: str = "md",
) -> CachedProjection | None:
    """Serve a warm hit with one file read and no graph or Markdown parse."""
    normalized_spec = schema.validate_projection_spec(spec)
    if normalized_spec["compiler"] != COMPILER_VERSION:
        return None
    path = cache_path(package, revision, spec, format=format)
    import grogu_plandoc_revision

    try:
        raw = grogu_plandoc_revision.safe_read(path)
    except FileNotFoundError:
        return None
    header, separator, payload = raw.partition(b"\n")
    expected_prefix = f"GROGU-PROJECTION-CACHE/1 {format} ".encode("ascii")
    if not separator or not header.startswith(expected_prefix):
        raise CompileError("invalid projection cache header", field="cache")
    try:
        fields = header[len(expected_prefix) :].decode("ascii").split(" ")
    except UnicodeDecodeError as error:
        raise CompileError("invalid projection cache ETag", field="cache") from error
    if (
        len(fields) != 2
        or re.fullmatch(r"sha256:[0-9a-f]{64}", fields[0]) is None
        or re.fullmatch(r"[0-9a-f]{64}", fields[1]) is None
    ):
        raise CompileError("invalid projection cache ETag", field="cache")
    if hashlib.sha256(payload).hexdigest() != fields[1]:
        raise CompileError("projection cache payload digest differs", field="cache")
    return CachedProjection(format=format, etag=fields[0], payload=payload)


def _revision_number(value: str) -> int:
    return int(value[1:]) if value else -1


def _thread_attached(
    node: Mapping[str, Any], selected_ids: set[str]
) -> bool:
    try:
        ids = set(schema.selector_node_ids(node["attrs"]["selector"]))
    except (KeyError, TypeError):
        return False
    return bool(ids & selected_ids)


def _scope_selection(graph: dict, spec: dict) -> tuple[set[str], set[str]]:
    stage_ids = {
        node_id
        for node_id, node in graph["nodes"].items()
        if node["stage"] in spec["stages"] or node["stage"] == ""
    }
    selected: set[str] = set()
    relationship_stubs: set[str] = set()
    since = _revision_number(spec["since"])
    role = spec["role"]
    for node_id in sorted(stage_ids, key=canon.id_sort_key):
        node = graph["nodes"][node_id]
        kind = node["kind"]
        if kind not in KIND_SECTION:
            raise CompileError(f"unknown node kind {kind!r}", field=f"nodes.{node_id}.kind")
        if spec["since"] and max(
            _revision_number(node["created_rev"]),
            _revision_number(node["updated_rev"]),
        ) <= since:
            continue
        if kind == "directive":
            attrs = node["attrs"]
            addressed = role in attrs["audience"] or "everyone" in attrs["audience"]
            changed_directive = bool(spec["since"])
            if addressed and (attrs["status"] == "active" or changed_directive):
                selected.add(node_id)
            continue
        if spec["include"] == "all":
            selected.add(node_id)
        elif schema.NODE_KINDS[kind]["normative"] or kind in {"risk", "question"}:
            selected.add(node_id)
        elif kind in {"diagram", "region"}:
            selected.add(node_id)
        elif kind in {"note", "evidence", "reference"}:
            selected.add(node_id)

    if spec["include"] == "normative":
        for node_id in sorted(stage_ids, key=canon.id_sort_key):
            node = graph["nodes"][node_id]
            if (
                node["kind"] == "thread"
                and node["attrs"].get("status") != "resolved"
                and _thread_attached(node, selected)
            ):
                selected.add(node_id)

        # Preserve relationships from selected facts.  Any remaining endpoint
        # is a title/attribute stub unless --include all was requested.
        changed = True
        ordered_edges = sorted(
            graph["edges"].values(),
            key=lambda edge: canon.id_sort_key(edge["id"]),
        )

        def can_add(node_id: str) -> bool:
            node = graph["nodes"][node_id]
            if node["kind"] == "directive":
                attrs = node["attrs"]
                addressed = role in attrs["audience"] or "everyone" in attrs["audience"]
                return addressed and (
                    attrs["status"] == "active" or bool(spec["since"])
                )
            if node["kind"] == "thread":
                return node["attrs"].get("status") != "resolved"
            return True

        while changed:
            changed = False
            for edge in ordered_edges:
                if (
                    edge["from"] in selected
                    and edge["to"] in stage_ids
                    and can_add(edge["to"])
                ):
                    if edge["to"] not in selected:
                        changed = True
                        relationship_stubs.add(edge["to"])
                    selected.add(edge["to"])
                if (
                    edge["to"] in selected
                    and edge["from"] in stage_ids
                    and can_add(edge["from"])
                ):
                    if edge["from"] not in selected:
                        changed = True
                        relationship_stubs.add(edge["from"])
                    selected.add(edge["from"])
    return selected, relationship_stubs


def _goal_depths(nodes: tuple[ProjectionNode, ...], edges: tuple[ProjectionEdge, ...]) -> dict[str, int]:
    node_ids = {node.id for node in nodes}
    roots = sorted(
        (node.id for node in nodes if node.kind == "goal"), key=canon.id_sort_key
    )
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if edge.kind in plandoc.IMPACT_EDGE_KINDS:
            adjacency[edge.source].append(edge.target)
            adjacency[edge.target].append(edge.source)
    for values in adjacency.values():
        values.sort(key=canon.id_sort_key)
    distance: dict[str, int] = {}
    queue = deque((root, 0) for root in roots)
    while queue:
        node_id, value = queue.popleft()
        if node_id in distance and distance[node_id] <= value:
            continue
        distance[node_id] = value
        for target in adjacency.get(node_id, []):
            if target in node_ids:
                queue.append((target, value + 1))
    return distance


def _with_node(
    ir: ProjectionIR,
    node_id: str,
    *,
    body: str | None = None,
    body_state: str,
    detail: str,
) -> ProjectionIR:
    nodes = []
    kind = ""
    changed = False
    for node in ir.nodes:
        if node.id != node_id:
            nodes.append(node)
            continue
        kind = node.kind
        replacement = replace(
            node,
            body=node.body if body is None else body,
            body_state=body_state,
        )
        changed = replacement != node
        nodes.append(replacement)
    if not changed:
        return ir
    elided = (*ir.elided, Elision(node_id, kind, detail))
    return replace(ir, nodes=tuple(nodes), elided=elided, projection_digest="")


def _without_node(ir: ProjectionIR, node_id: str, detail: str) -> ProjectionIR:
    connected = any(edge.source == node_id or edge.target == node_id for edge in ir.edges)
    if connected:
        return _with_node(
            ir, node_id, body="", body_state="projection", detail=f"{detail} body"
        )
    found = next((node for node in ir.nodes if node.id == node_id), None)
    if found is None:
        return ir
    return replace(
        ir,
        nodes=tuple(node for node in ir.nodes if node.id != node_id),
        elided=(*ir.elided, Elision(node_id, found.kind, detail)),
        projection_digest="",
    )


def _render_size(ir: ProjectionIR) -> int:
    return len(render(_stamp(ir)))


def _apply_budget(ir: ProjectionIR, *, depth: int = 0) -> ProjectionIR:
    budget = ir.provenance.budget_chars
    if budget is None or _render_size(ir) <= budget:
        return ir

    for node in ir.nodes:
        if node.kind in {"reference", "evidence"} and node.body:
            ir = _with_node(
                ir, node.id, body="", body_state="elided", detail="body"
            )
            if _render_size(ir) <= budget:
                return ir

    for node in ir.nodes:
        if node.kind == "note" and node.body:
            ir = _with_node(
                ir, node.id, body="", body_state="elided", detail="body"
            )
            if _render_size(ir) <= budget:
                return ir
    for node in tuple(ir.nodes):
        if node.kind == "note":
            ir = _without_node(ir, node.id, "node")
            if _render_size(ir) <= budget:
                return ir

    for node in tuple(ir.nodes):
        if node.kind == "thread" and node.attrs.get("status") == "resolved":
            ir = _without_node(ir, node.id, "resolved thread")
            if _render_size(ir) <= budget:
                return ir

    for node in ir.nodes:
        if node.kind == "decision" and node.body:
            ir = _with_node(
                ir, node.id, body="", body_state="elided", detail="rationale"
            )
            if _render_size(ir) <= budget:
                return ir

    depths = _goal_depths(ir.nodes, ir.edges)
    depth_limit = depth
    tasks = sorted(
        (
            node
            for node in ir.nodes
            if node.kind == "task"
            and node.body
            and depths.get(node.id, schema.MAX_SAFE_INTEGER) > depth_limit
        ),
        key=lambda node: (
            -depths.get(node.id, schema.MAX_SAFE_INTEGER),
            canon.id_sort_key(node.id),
        ),
    )
    for node in tasks:
        ir = _with_node(
            ir, node.id, body="", body_state="elided", detail="distant task body"
        )
        if _render_size(ir) <= budget:
            return ir

    return replace(
        ir,
        provenance=replace(ir.provenance, budget_exceeded=True),
        projection_digest="",
    )


def _check_closed_kinds(document: Mapping[str, Any], *, operation: str) -> None:
    for node_id, node in document["nodes"].items():
        if node["kind"] not in KIND_SECTION:
            raise CompileError(
                f"{operation} has no dispatch for node kind {node['kind']!r}",
                field=f"nodes.{node_id}.kind",
            )
    for edge_id, edge in document["edges"].items():
        if edge["kind"] not in RELATIONSHIP_LABELS:
            raise CompileError(
                f"{operation} has no dispatch for edge kind {edge['kind']!r}",
                field=f"edges.{edge_id}.kind",
            )


def _safe_provenance(value: Any) -> dict:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    at = value.get("at")
    if at is not None:
        if not isinstance(at, str) or (
            at
            and re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
                r"[0-9]{2}(?:\.[0-9]{1,6})?Z",
                at,
            )
            is None
        ):
            raise CompileError(
                "source timestamp must be empty or UTC",
                field="provenance.at",
            )
        result["at"] = at
    role = value.get("role")
    if role is not None:
        if role not in schema.ROLES:
            raise CompileError(
                "source role is not permitted", field="provenance.role"
            )
        result["role"] = role
    for key in ("actor", "agent"):
        text = value.get(key)
        if text is None:
            continue
        if (
            not isinstance(text, str)
            or len(text) > 256
            or (text and text.splitlines() != [text])
            or any(ord(character) < 32 or ord(character) == 127 for character in text)
        ):
            raise CompileError(
                f"source {key} must be bounded single-line text",
                field=f"provenance.{key}",
            )
        result[key] = text
    count = value.get("sealed_relationships")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        result["sealed_relationships"] = count
    return result


def scope(graph: Mapping[str, Any], spec: Mapping[str, Any]) -> GraphFragment:
    """Select the complete role/stage/include sub-graph without eliding it."""
    document = schema.validate_document(graph)
    projection_spec = schema.validate_projection_spec(spec)
    if projection_spec["compiler"] != COMPILER_VERSION:
        raise CompileError(
            f"unsupported compiler {projection_spec['compiler']}",
            field="provenance.compiler",
        )
    _check_closed_kinds(document, operation="scope")
    selected, relationship_stubs = _scope_selection(document, projection_spec)
    fragment_document = {
        "schema_version": 1,
        "kind": "grogu.plan_document",
        "plan_id": document["plan_id"],
        "title": document["title"],
        "revision": document["revision"],
        "nodes": {
            node_id: copy.deepcopy(document["nodes"][node_id])
            for node_id in sorted(selected, key=canon.id_sort_key)
        },
        "edges": {
            edge_id: copy.deepcopy(edge)
            for edge_id, edge in sorted(
                document["edges"].items(),
                key=lambda item: canon.id_sort_key(item[0]),
            )
            if edge["from"] in selected and edge["to"] in selected
        },
    }
    if "canvas" in document:
        fragment_document["canvas"] = copy.deepcopy(document["canvas"])
    safe_provenance = _safe_provenance(document.get("provenance"))
    if safe_provenance:
        fragment_document["provenance"] = safe_provenance
    fragment_document = schema.validate_document(fragment_document)
    return GraphFragment(
        document_json=canon.dumps(fragment_document),
        spec_json=canon.dumps(projection_spec),
        source_digest=canon.digest(fragment_document),
        relationship_stub_ids=tuple(
            sorted(relationship_stubs, key=canon.id_sort_key)
        ),
    )


def _project_unchecked(
    source_fragment: GraphFragment,
    projection_spec: Mapping[str, Any],
) -> ProjectionIR:
    document = schema.validate_document(source_fragment.document)
    _check_closed_kinds(document, operation="project")
    nodes = []
    elided = []
    for node_id in sorted(document["nodes"], key=canon.id_sort_key):
        node = document["nodes"][node_id]
        state = (
            "projection"
            if node_id in source_fragment.relationship_stub_ids
            or (
                projection_spec["include"] == "normative"
                and node["kind"] in {"note", "evidence", "reference"}
            )
            else "full"
        )
        projected = copy.deepcopy(node)
        if state != "full" and projected["body"]:
            projected["body"] = ""
            elided.append(Elision(node_id, node["kind"], "projection body"))
        nodes.append(_node_from_dict(projected, body_state=state))
    nodes.sort(
        key=lambda node: (
            SECTION_ORDER.index(KIND_SECTION[node.kind]),
            schema.STAGES.index(node.stage) if node.stage in schema.STAGES else -1,
            node.order,
            canon.id_sort_key(node.id),
        )
    )
    edges = [
        _edge_from_dict(edge)
        for edge in document["edges"].values()
    ]
    edges.sort(
        key=lambda edge: (
            canon.id_sort_key(edge.source),
            edge.kind,
            canon.id_sort_key(edge.target),
            canon.id_sort_key(edge.id),
        )
    )
    graph_provenance = document.get("provenance") or {}
    provenance = Provenance(
        plan_id=document["plan_id"],
        revision=document["revision"],
        role=projection_spec["role"],
        stages=tuple(projection_spec["stages"]),
        include=projection_spec["include"],
        compiler=projection_spec["compiler"],
        graph_digest=source_fragment.source_digest,
        projection_id=canon.digest(projection_spec),
        source_at=str(graph_provenance.get("at", "")),
        source_actor=str(graph_provenance.get("actor", "")),
        source_role=str(graph_provenance.get("role", "")),
        source_agent=str(graph_provenance.get("agent", "")),
        sealed_relationships=int(graph_provenance.get("sealed_relationships", 0)),
        budget_chars=projection_spec["budget_chars"],
        since=projection_spec["since"],
        depth=projection_spec.get("depth"),
        source_provenance_json=canon.dumps(graph_provenance),
        canvas_json=(
            canon.dumps(document["canvas"]) if "canvas" in document else ""
        ),
    )
    ir = ProjectionIR(
        title=document["title"],
        nodes=tuple(nodes),
        edges=tuple(edges),
        elided=tuple(elided),
        provenance=provenance,
        projection_digest="",
    )
    ir = _apply_budget(ir, depth=projection_spec.get("depth", 0))
    return _stamp(ir)


def project(
    fragment: GraphFragment | Mapping[str, Any],
    spec: Mapping[str, Any],
) -> ProjectionIR:
    """Order and deliberately elide a previously scoped graph fragment."""
    projection_spec = schema.validate_projection_spec(spec)
    if projection_spec["compiler"] != COMPILER_VERSION:
        raise CompileError(
            f"unsupported compiler {projection_spec['compiler']}",
            field="provenance.compiler",
        )
    if isinstance(fragment, GraphFragment):
        expected_spec = canon.dumps(projection_spec)
        if fragment.spec_json != expected_spec:
            raise CompileError(
                "projection spec differs from the scoped fragment",
                field="spec",
            )
        source_fragment = fragment
    else:
        source_fragment = scope(fragment, projection_spec)
    ir = _project_unchecked(source_fragment, projection_spec)
    round_tripped = parse(render(ir))
    assert_equivalent(ir, round_tripped)
    if strict_enabled():
        verify_completeness(source_fragment, ir)
        verify_graph_equivalence(source_fragment, round_tripped)
    return ir


def _spec_from_provenance(provenance: Provenance) -> dict:
    spec = {
        "role": provenance.role,
        "stages": list(provenance.stages),
        "include": provenance.include,
        "budget_chars": provenance.budget_chars,
        "since": provenance.since,
        "compiler": provenance.compiler,
    }
    if provenance.depth is not None:
        spec["depth"] = provenance.depth
    return schema.validate_projection_spec(spec)


def lift(ir: ProjectionIR) -> GraphFragment:
    """Invert ``project`` for every element not deliberately elided."""
    if not isinstance(ir, ProjectionIR):
        raise TypeError("ir must be a ProjectionIR")
    _check_closed_kinds(
        {
            "nodes": {node.id: _node_dict(node) for node in ir.nodes},
            "edges": {edge.id: _edge_dict(edge) for edge in ir.edges},
        },
        operation="lift",
    )
    document = {
        "schema_version": 1,
        "kind": "grogu.plan_document",
        "plan_id": ir.provenance.plan_id,
        "title": ir.title,
        "revision": ir.provenance.revision,
        "nodes": {},
        "edges": {},
    }
    for node in ir.nodes:
        value = _node_dict(node)
        value.pop("body_state", None)
        document["nodes"][node.id] = value
    for edge in ir.edges:
        document["edges"][edge.id] = _edge_dict(edge)
    source_provenance = canon.loads(ir.provenance.source_provenance_json)
    if source_provenance:
        document["provenance"] = source_provenance
    if ir.provenance.canvas_json:
        document["canvas"] = canon.loads(ir.provenance.canvas_json)
    document = schema.validate_document(document)
    return GraphFragment(
        document_json=canon.dumps(document),
        spec_json=canon.dumps(_spec_from_provenance(ir.provenance)),
        source_digest=ir.provenance.graph_digest,
    )


def subtract_elided(
    fragment: GraphFragment,
    elisions: tuple[Elision, ...],
) -> GraphFragment:
    """Apply the declared elision set to a fragment for Identity 3."""
    document = copy.deepcopy(fragment.document)
    for item in elisions:
        if item.detail in {
            "body",
            "projection body",
            "rationale",
            "distant task body",
            "node body",
            "resolved thread body",
        }:
            if item.id in document["nodes"]:
                document["nodes"][item.id]["body"] = ""
            continue
        if item.detail in {"node", "resolved thread"}:
            document["nodes"].pop(item.id, None)
            document["edges"] = {
                edge_id: edge
                for edge_id, edge in document["edges"].items()
                if item.id not in {edge["from"], edge["to"]}
            }
            continue
        raise CompileError(
            f"unknown elision detail {item.detail!r}",
            field=f"elided.{item.id}",
        )
    document = schema.validate_document(document)
    return GraphFragment(
        document_json=canon.dumps(document),
        spec_json=fragment.spec_json,
        source_digest=fragment.source_digest,
    )


def _leaf_elements(value: Any, path: str, output: set[str]) -> None:
    if isinstance(value, Mapping):
        if not value:
            output.add(path)
        for key in sorted(value, key=canon.utf16_key):
            _leaf_elements(value[key], f"{path}.{key}", output)
        return
    if isinstance(value, list):
        if not value:
            output.add(path)
        for index, item in enumerate(value):
            _leaf_elements(item, f"{path}[{index}]", output)
        return
    output.add(path)


def elements(value: GraphFragment | ProjectionIR) -> frozenset[str]:
    """Return stable leaf element paths for completeness checks."""
    fragment = value if isinstance(value, GraphFragment) else lift(value)
    result: set[str] = set()
    _leaf_elements(fragment.document, "$", result)
    return frozenset(result)


def elided(fragment: GraphFragment, ir: ProjectionIR) -> frozenset[str]:
    """Return the exact element paths deliberately removed by projection."""
    return elements(fragment) - elements(subtract_elided(fragment, ir.elided))


def _json_difference(left: Any, right: Any, path: str = "$") -> str:
    if type(left) is not type(right):
        return path
    if isinstance(left, Mapping):
        if set(left) != set(right):
            return path
        for key in sorted(left, key=canon.utf16_key):
            found = _json_difference(left[key], right[key], f"{path}.{key}")
            if found:
                return found
        return ""
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path}.length"
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            found = _json_difference(
                left_item, right_item, f"{path}[{index}]"
            )
            if found:
                return found
        return ""
    return "" if left == right else path


def _verify_elision_policy(fragment: GraphFragment, ir: ProjectionIR) -> None:
    trusted = _project_unchecked(fragment, fragment.spec)
    if trusted.elided != ir.elided:
        field = first_difference(trusted.elided, ir.elided, "$.elided")
        raise CompileError(
            "candidate elisions do not match the deterministic projection policy",
            field=field or "$.elided",
        )


def verify_completeness(fragment: GraphFragment, ir: ProjectionIR) -> bool:
    """Evaluate Identity 1 and name the first missing or invented element."""
    _verify_elision_policy(fragment, ir)
    expected = elements(fragment)
    named_elisions = elided(fragment, ir)
    actual = elements(ir)
    combined = actual | named_elisions
    if combined != expected:
        differing = sorted(combined ^ expected)[0]
        raise CompileError(
            "identity 1 completeness failed",
            field=differing,
        )
    return True


def verify_graph_equivalence(fragment: GraphFragment, ir: ProjectionIR) -> bool:
    """Evaluate Identity 3 using structural canonical JSON equality."""
    _verify_elision_policy(fragment, ir)
    expected = subtract_elided(fragment, ir.elided)
    actual = lift(ir)
    if expected.document_json != actual.document_json:
        field = _json_difference(expected.document, actual.document)
        raise CompileError(
            "identity 3 graph equivalence failed",
            field=field or "$",
        )
    if expected.spec_json != actual.spec_json:
        raise CompileError(
            "identity 3 graph equivalence failed",
            field="$.spec",
        )
    if expected.source_digest != actual.source_digest:
        raise CompileError(
            "identity 3 graph equivalence failed",
            field="$.source_digest",
        )
    return True


def verify_identities(fragment: GraphFragment, ir: ProjectionIR) -> dict:
    """Run all three compiler identities and return durable evidence."""
    verify_completeness(fragment, ir)
    parsed = parse(render(ir))
    assert_equivalent(ir, parsed)
    verify_graph_equivalence(fragment, parsed)
    return {
        "identity_1": True,
        "identity_2": True,
        "identity_3": True,
        "projection_digest": ir.projection_digest,
    }


def compile_checked(
    graph: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict:
    """Compile one projection and return artifacts only after all identities."""
    fragment = scope(graph, spec)
    ir = project(fragment, spec)
    identities = verify_identities(fragment, ir)
    return {
        "fragment": fragment,
        "ir": ir,
        "markdown": render(ir),
        "json": render_json(ir),
        "identities": identities,
    }


def _stage_label(stages: tuple[str, ...]) -> str:
    return ",".join(stages) if stages else "plan"


def _metadata(node: ProjectionNode) -> dict:
    value = {
        "attrs": node.attrs,
        "body_chars": len(node.body),
        "body_state": node.body_state,
        "created_rev": node.created_rev,
        "ext": node.ext,
        "geometry": node.geometry,
        "kind": node.kind,
        "order": node.order,
        "stage": node.stage,
        "updated_rev": node.updated_rev,
    }
    return value


def _edges_by_source(ir: ProjectionIR) -> dict[str, list[ProjectionEdge]]:
    result: dict[str, list[ProjectionEdge]] = defaultdict(list)
    for edge in ir.edges:
        result[edge.source].append(edge)
    for edges in result.values():
        edges.sort(
            key=lambda edge: (
                edge.kind,
                canon.id_sort_key(edge.target),
                canon.id_sort_key(edge.id),
            )
        )
    return result


def _fence(body: str) -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", body)), default=0)
    return "`" * max(3, longest + 1)


def _render_item(
    node: ProjectionNode,
    edges: list[ProjectionEdge],
) -> str:
    attrs = node.attrs
    if node.kind == "directive":
        audience = ", ".join(attrs.get("audience", []))
        first = (
            f"- **{node.id}** ({attrs.get('binding', 'must')} · {audience}) "
            f"{node.title}"
        )
    else:
        first = f"### {node.id} · {node.title}"
    lines = [first, f"meta: {canon.dumps(_metadata(node))}"]
    grouped: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        grouped[edge.kind].append(edge.target)
    for kind in sorted(grouped):
        targets = sorted(grouped[kind], key=canon.id_sort_key)
        lines.append(f"{RELATIONSHIP_LABELS[kind]}: {', '.join(targets)}")
    lines.append(
        "relationships-json: "
        + canon.dumps([_edge_dict(edge) for edge in edges])
    )
    lines.append("")
    if node.kind == "diagram":
        fence = _fence(node.body)
        lines.extend([f"{fence}mermaid", node.body, fence])
    else:
        lines.append(node.body)
    return "\n".join(lines)


def _elision_summary(elided: tuple[Elision, ...]) -> list[str]:
    counts: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item in elided:
        counts[(item.kind, item.detail)].append(item.id)
    lines = []
    for (kind, detail), ids in sorted(counts.items()):
        ordered = sorted(ids, key=canon.id_sort_key)
        lines.append(
            f"- {len(ordered)} {kind} item(s), {detail}: {', '.join(ordered)}"
        )
    return lines


def render(ir: ProjectionIR) -> str:
    """Render a projection to deterministic LF Markdown."""
    if not isinstance(ir, ProjectionIR):
        raise TypeError("ir must be a ProjectionIR")
    _check_closed_kinds(
        {
            "nodes": {node.id: _node_dict(node) for node in ir.nodes},
            "edges": {edge.id: _edge_dict(edge) for edge in ir.edges},
        },
        operation="render",
    )
    expected = projection_digest(ir)
    if ir.projection_digest != expected:
        raise CompileError(
            f"stored {ir.projection_digest!r}, expected {expected!r}",
            field="projection_digest",
        )
    provenance = ir.provenance
    stage_label = _stage_label(provenance.stages)
    title_stage = provenance.stages[0] if len(provenance.stages) == 1 else "projected"
    lines = [
        f"# {ir.title} — {title_stage} plan",
        "",
        (
            f"> plan {provenance.plan_id} · revision {provenance.revision} · "
            f"projection {provenance.role}/{stage_label} · compiler "
            f"{provenance.compiler} · graph {provenance.graph_digest} · "
            f"projection {ir.projection_digest}"
        ),
        "",
    ]
    edges_by_source = _edges_by_source(ir)
    by_section: dict[str, list[ProjectionNode]] = defaultdict(list)
    for node in ir.nodes:
        by_section[KIND_SECTION[node.kind]].append(node)
    for section in SECTION_ORDER:
        if section in {"Extensions", "Elided", "Provenance"}:
            continue
        nodes = by_section.get(section, [])
        if not nodes:
            continue
        lines.extend([f"## {section}", ""])
        for node in nodes:
            lines.append(_render_item(node, edges_by_source.get(node.id, [])))
            lines.append("")

    extensions = [
        {
            "element": "node",
            "id": node.id,
            "ext": node.ext,
        }
        for node in ir.nodes
        if node.ext_json
    ] + [
        {
            "element": "edge",
            "id": edge.id,
            "ext": edge.ext,
        }
        for edge in ir.edges
        if edge.ext_json
    ]
    if extensions:
        extensions.sort(key=lambda item: canon.id_sort_key(item["id"]))
        lines.extend(["## Extensions", ""])
        for extension in extensions:
            lines.extend(
                [
                    f"### {extension['id']} · Extensions",
                    "```json",
                    canon.dumps(extension),
                    "```",
                    "",
                ]
            )

    if ir.elided:
        lines.extend(["## Elided", "", *_elision_summary(ir.elided)])
        lines.append(f"elided-json: {canon.dumps([dataclasses.asdict(item) for item in ir.elided])}")
        lines.append("")

    source = (
        f"{provenance.source_role}@{provenance.source_agent}"
        if provenance.source_role or provenance.source_agent
        else "unknown"
    )
    lines.extend(
        [
            "## Provenance",
            "",
            (
                f"- source revision {provenance.revision}, written "
                f"{provenance.source_at or 'unknown'} by {source}"
            ),
            f"- graph digest {provenance.graph_digest}",
            f"- projection digest {ir.projection_digest}",
            f"- compiler {COMPILER_ID}",
        ]
    )
    if provenance.sealed_relationships:
        lines.append(
            f"- {provenance.sealed_relationships} relationship(s) to sealed stages"
        )
    if provenance.budget_chars is not None:
        status = "exceeded" if provenance.budget_exceeded else "met"
        lines.append(f"- budget {provenance.budget_chars} characters ({status})")
    if provenance.since:
        lines.append(f"- changes since {provenance.since}")
    if provenance.depth is not None:
        lines.append(f"- task body depth {provenance.depth}")
    lines.append(f"provenance-json: {canon.dumps(_provenance_dict(provenance))}")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_json(ir: ProjectionIR) -> dict:
    """Return the byte-exact JSON twin produced from the same IR."""
    expected = projection_digest(ir)
    if ir.projection_digest != expected:
        raise CompileError(
            f"stored {ir.projection_digest!r}, expected {expected!r}",
            field="projection_digest",
        )
    return _payload(ir, include_digest=True)


class _Cursor:
    def __init__(self, source: str):
        self.source = source
        self.position = 0

    def line(self) -> str:
        end = self.source.find("\n", self.position)
        if end < 0:
            raise CompileError("expected a newline", field=f"markdown[{self.position}]")
        value = self.source[self.position:end]
        self.position = end + 1
        return value

    def expect_line(self, expected: str) -> None:
        actual = self.line()
        if actual != expected:
            raise CompileError(
                f"expected {expected!r}, got {actual!r}",
                field=f"markdown[{self.position - len(actual) - 1}]",
            )

    def take(self, count: int) -> str:
        end = self.position + count
        if end > len(self.source):
            raise CompileError("body character count exceeds input", field="body")
        value = self.source[self.position:end]
        self.position = end
        return value

    def remaining(self) -> str:
        return self.source[self.position :]


def _parse_provenance(value: Mapping[str, Any]) -> Provenance:
    required = {
        "plan_id",
        "revision",
        "role",
        "stages",
        "include",
        "compiler",
        "graph_digest",
        "projection_id",
        "source_at",
        "source_actor",
        "source_role",
        "source_agent",
        "sealed_relationships",
        "budget_chars",
        "budget_exceeded",
        "since",
        "depth",
        "source_provenance",
        "canvas",
    }
    if set(value) != required:
        raise CompileError("invalid provenance metadata fields", field="provenance")
    try:
        raw_spec = {
            "role": value["role"],
            "stages": value["stages"],
            "include": value["include"],
            "budget_chars": value["budget_chars"],
            "since": value["since"],
            "compiler": value["compiler"],
        }
        if value["depth"] is not None:
            raw_spec["depth"] = value["depth"]
        normalized_spec = schema.validate_projection_spec(raw_spec)
    except schema.SchemaError as error:
        raise CompileError(error.message, field=f"provenance{error.path[1:]}") from error
    if (
        not isinstance(value["graph_digest"], str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value["graph_digest"]) is None
        or not isinstance(value["projection_id"], str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value["projection_id"]) is None
    ):
        raise CompileError("invalid provenance digest", field="provenance")
    if value["projection_id"] != canon.digest(normalized_spec):
        raise CompileError(
            "projection id does not match the projection spec",
            field="provenance.projection_id",
        )
    if isinstance(value["sealed_relationships"], bool) or not isinstance(
        value["sealed_relationships"], int
    ):
        raise CompileError(
            "sealed relationship count must be an integer",
            field="provenance.sealed_relationships",
        )
    if value["sealed_relationships"] < 0:
        raise CompileError(
            "sealed relationship count must be nonnegative",
            field="provenance.sealed_relationships",
        )
    if not isinstance(value["budget_exceeded"], bool):
        raise CompileError(
            "budget_exceeded must be boolean", field="provenance.budget_exceeded"
        )
    if not isinstance(value["source_provenance"], Mapping):
        raise CompileError(
            "source provenance must be an object",
            field="provenance.source_provenance",
        )
    if value["canvas"] is not None and not isinstance(value["canvas"], Mapping):
        raise CompileError(
            "canvas must be an object or null", field="provenance.canvas"
        )
    return Provenance(
        plan_id=str(value["plan_id"]),
        revision=str(value["revision"]),
        role=normalized_spec["role"],
        stages=tuple(normalized_spec["stages"]),
        include=normalized_spec["include"],
        compiler=normalized_spec["compiler"],
        graph_digest=str(value["graph_digest"]),
        projection_id=str(value["projection_id"]),
        source_at=str(value["source_at"]),
        source_actor=str(value["source_actor"]),
        source_role=str(value["source_role"]),
        source_agent=str(value["source_agent"]),
        sealed_relationships=int(value["sealed_relationships"]),
        budget_chars=(
            normalized_spec["budget_chars"]
        ),
        budget_exceeded=value["budget_exceeded"],
        since=normalized_spec["since"],
        depth=normalized_spec.get("depth"),
        source_provenance_json=canon.dumps(value["source_provenance"]),
        canvas_json=(
            "" if value["canvas"] is None else canon.dumps(value["canvas"])
        ),
    )


def _parse_node_metadata(value: Any, node_id: str) -> dict:
    if not isinstance(value, Mapping):
        raise CompileError("node metadata must be an object", field=f"nodes.{node_id}")
    required = {
        "attrs",
        "body_chars",
        "body_state",
        "created_rev",
        "ext",
        "geometry",
        "kind",
        "order",
        "stage",
        "updated_rev",
    }
    if set(value) != required:
        raise CompileError(
            "invalid node metadata fields", field=f"nodes.{node_id}"
        )
    body_chars = value["body_chars"]
    if isinstance(body_chars, bool) or not isinstance(body_chars, int) or body_chars < 0:
        raise CompileError(
            "body_chars must be a nonnegative integer",
            field=f"nodes.{node_id}.body_chars",
        )
    if value["body_state"] not in {"full", "projection", "elided"}:
        raise CompileError(
            "invalid body state", field=f"nodes.{node_id}.body_state"
        )
    if not isinstance(value["attrs"], Mapping):
        raise CompileError("attrs must be an object", field=f"nodes.{node_id}.attrs")
    if value["geometry"] is not None and not isinstance(value["geometry"], Mapping):
        raise CompileError(
            "geometry must be an object or null", field=f"nodes.{node_id}.geometry"
        )
    if value["ext"] is not None and not isinstance(value["ext"], Mapping):
        raise CompileError(
            "ext must be an object or null", field=f"nodes.{node_id}.ext"
        )
    return dict(value)


def _parse_compiled(markdown: str) -> ProjectionIR:
    if "\r" in markdown:
        raise CompileError("compiled Markdown must use LF line endings", field="markdown")
    cursor = _Cursor(markdown)
    title_line = cursor.line()
    if not title_line.startswith("# ") or " — " not in title_line:
        raise CompileError("invalid compiled title", field="title")
    title = title_line[2:].rsplit(" — ", 1)[0]
    cursor.expect_line("")
    header_line = cursor.line()
    header = _HEADER.fullmatch(header_line)
    if header is None:
        raise CompileError("invalid compiler digest header", field="header")
    cursor.expect_line("")
    nodes: list[ProjectionNode] = []
    edges_by_id: dict[str, ProjectionEdge] = {}
    elided: tuple[Elision, ...] = ()
    provenance: Provenance | None = None
    extension_blocks: dict[tuple[str, str], dict] = {}

    while cursor.position < len(markdown):
        heading = cursor.line()
        if not heading:
            continue
        if not heading.startswith("## "):
            raise CompileError(f"expected section heading, got {heading!r}", field="section")
        section = heading[3:]
        if section not in SECTION_ORDER:
            raise CompileError(f"unknown section {section!r}", field="section")
        cursor.expect_line("")
        if section == "Elided":
            while cursor.remaining() and not cursor.remaining().startswith("elided-json: "):
                cursor.line()
            line = cursor.line()
            try:
                raw = canon.loads(line.removeprefix("elided-json: "))
                if not isinstance(raw, list):
                    raise TypeError("not an array")
                parsed_elisions = []
                for item in raw:
                    if not isinstance(item, Mapping) or set(item) != {
                        "id",
                        "kind",
                        "detail",
                    }:
                        raise TypeError("invalid elision item")
                    parsed_elisions.append(
                        Elision(
                            str(item["id"]),
                            str(item["kind"]),
                            str(item["detail"]),
                        )
                    )
                elided = tuple(parsed_elisions)
            except (canon.CanonicalError, KeyError, TypeError) as error:
                raise CompileError(
                    "invalid elision metadata", field="elided"
                ) from error
            if cursor.remaining().startswith("\n"):
                cursor.expect_line("")
            continue
        if section == "Extensions":
            while cursor.position < len(markdown):
                if cursor.remaining().startswith("## "):
                    break
                first = cursor.line()
                if not first:
                    continue
                heading_match = _NODE_HEADING.fullmatch(first)
                if (
                    heading_match is None
                    or heading_match.group("title") != "Extensions"
                ):
                    raise CompileError(
                        "invalid extension heading", field="extensions"
                    )
                cursor.expect_line("```json")
                try:
                    extension = canon.loads(cursor.line())
                except canon.CanonicalError as error:
                    raise CompileError(
                        str(error), field="extensions"
                    ) from error
                cursor.expect_line("```")
                if cursor.remaining().startswith("\n"):
                    cursor.expect_line("")
                if not isinstance(extension, Mapping) or set(extension) != {
                    "element",
                    "id",
                    "ext",
                }:
                    raise CompileError(
                        "invalid extension block", field="extensions"
                    )
                element = str(extension["element"])
                identifier = str(extension["id"])
                if element not in {"node", "edge"}:
                    raise CompileError(
                        "invalid extension element type", field="extensions"
                    )
                if identifier != heading_match.group("id"):
                    raise CompileError(
                        "extension heading and payload ids differ",
                        field=f"extensions.{identifier}",
                    )
                key = (element, identifier)
                if key in extension_blocks:
                    raise CompileError(
                        "duplicate extension block",
                        field=f"extensions.{identifier}",
                    )
                extension_blocks[key] = canon.normalize(extension["ext"])
            continue
        if section == "Provenance":
            provenance_line = ""
            while cursor.position < len(markdown):
                line = cursor.line()
                if line.startswith("provenance-json: "):
                    provenance_line = line
                    break
            if not provenance_line:
                raise CompileError("missing provenance metadata", field="provenance")
            provenance = _parse_provenance(
                canon.loads(provenance_line.removeprefix("provenance-json: "))
            )
            break

        while cursor.position < len(markdown):
            if cursor.remaining().startswith("## "):
                break
            first = cursor.line()
            if not first:
                continue
            directive = _DIRECTIVE.fullmatch(first)
            node_heading = _NODE_HEADING.fullmatch(first)
            if directive is None and node_heading is None:
                raise CompileError(f"invalid node heading {first!r}", field="node")
            match = directive or node_heading
            node_id = match.group("id")
            node_title = match.group("title")
            meta_line = cursor.line()
            if not meta_line.startswith("meta: "):
                raise CompileError("missing node metadata", field=f"nodes.{node_id}")
            try:
                meta = _parse_node_metadata(
                    canon.loads(meta_line.removeprefix("meta: ")), node_id
                )
            except canon.CanonicalError as error:
                raise CompileError(
                    str(error), field=f"nodes.{node_id}.meta"
                ) from error
            readable_relationships: dict[str, list[str]] = defaultdict(list)
            while True:
                line = cursor.line()
                if line.startswith("relationships-json: "):
                    try:
                        raw_edges = canon.loads(
                            line.removeprefix("relationships-json: ")
                        )
                    except canon.CanonicalError as error:
                        raise CompileError(
                            str(error),
                            field=f"nodes.{node_id}.relationships",
                        ) from error
                    if not isinstance(raw_edges, list):
                        raise CompileError(
                            "relationship metadata must be an array",
                            field=f"nodes.{node_id}.relationships",
                        )
                    break
                relationship = _RELATIONSHIP.fullmatch(line)
                if relationship is None:
                    raise CompileError(
                        f"invalid relationship line {line!r}",
                        field=f"nodes.{node_id}.relationships",
                    )
                readable_relationships[relationship.group("kind")].extend(
                    relationship.group("ids").split(", ")
                )
            cursor.expect_line("")
            body_chars = meta["body_chars"]
            kind = str(meta["kind"])
            if kind == "diagram":
                opening = cursor.line()
                if not opening.endswith("mermaid") or set(opening[:-7]) != {"`"}:
                    raise CompileError("invalid Mermaid fence", field=f"nodes.{node_id}.body")
                fence = opening[:-7]
                body = cursor.take(body_chars)
                cursor.expect_line("")
                cursor.expect_line(fence)
            else:
                body = cursor.take(body_chars)
                cursor.expect_line("")
            if cursor.remaining().startswith("\n"):
                cursor.expect_line("")
            raw_node = {
                "id": node_id,
                "kind": kind,
                "stage": meta["stage"],
                "title": node_title,
                "body": body,
                "attrs": meta["attrs"],
                "order": meta["order"],
                "created_rev": meta["created_rev"],
                "updated_rev": meta["updated_rev"],
            }
            if meta["geometry"] is not None:
                raw_node["geometry"] = meta["geometry"]
            if meta["ext"] is not None:
                raw_node["ext"] = meta["ext"]
            try:
                node = _node_from_dict(
                    schema.validate_node(raw_node, path=f"$.nodes.{node_id}"),
                    body_state=meta["body_state"],
                )
            except schema.SchemaError as error:
                raise CompileError(
                    error.message, field=error.path.removeprefix("$.")
                ) from error
            if directive is not None:
                if node.kind != "directive":
                    raise CompileError("directive line has non-directive metadata", field=node_id)
                if directive.group("binding") != node.attrs.get("binding"):
                    raise CompileError("directive binding differs from metadata", field=node_id)
                if directive.group("audience") != ", ".join(node.attrs.get("audience", [])):
                    raise CompileError("directive audience differs from metadata", field=node_id)
            if any(existing.id == node.id for existing in nodes):
                raise CompileError("duplicate node id", field=f"nodes.{node.id}")
            nodes.append(node)
            expected_readable: dict[str, list[str]] = defaultdict(list)
            for edge_value in raw_edges:
                try:
                    edge = _edge_from_dict(schema.validate_edge(edge_value))
                except schema.SchemaError as error:
                    raise CompileError(
                        error.message, field=error.path.removeprefix("$.")
                    ) from error
                if edge.source != node.id:
                    raise CompileError(
                        "relationship metadata is under the wrong source node",
                        field=edge.id,
                    )
                if edge.id in edges_by_id and edges_by_id[edge.id] != edge:
                    raise CompileError("edge metadata is inconsistent", field=edge.id)
                edges_by_id[edge.id] = edge
                label = RELATIONSHIP_LABELS[edge.kind]
                expected_readable[label].append(edge.target)
            for values in expected_readable.values():
                values.sort(key=canon.id_sort_key)
            for values in readable_relationships.values():
                values.sort(key=canon.id_sort_key)
            if dict(expected_readable) != dict(readable_relationships):
                raise CompileError(
                    "readable relationships differ from metadata",
                    field=f"nodes.{node_id}.relationships",
                )

    if provenance is None:
        raise CompileError("missing Provenance section", field="provenance")
    node_ids = {node.id for node in nodes}
    for edge in edges_by_id.values():
        if edge.source not in node_ids or edge.target not in node_ids:
            raise CompileError(
                "relationship endpoint is absent from the projection",
                field=edge.id,
            )
    expected_extensions = {
        ("node", node.id): node.ext
        for node in nodes
        if node.ext_json
    }
    expected_extensions.update(
        {
            ("edge", edge.id): edge.ext
            for edge in edges_by_id.values()
            if edge.ext_json
        }
    )
    if extension_blocks != expected_extensions:
        raise CompileError(
            "extension blocks differ from element metadata",
            field="extensions",
        )
    header_stages = tuple(header.group("stages").split(","))
    if (
        header.group("plan") != provenance.plan_id
        or header.group("revision") != provenance.revision
        or header.group("role") != provenance.role
        or header_stages != provenance.stages
        or int(header.group("compiler")) != provenance.compiler
        or header.group("graph") != provenance.graph_digest
    ):
        raise CompileError("header and provenance metadata differ", field="header")
    ir = ProjectionIR(
        title=title,
        nodes=tuple(nodes),
        edges=tuple(
            sorted(
                edges_by_id.values(),
                key=lambda edge: (
                    canon.id_sort_key(edge.source),
                    edge.kind,
                    canon.id_sort_key(edge.target),
                    canon.id_sort_key(edge.id),
                ),
            )
        ),
        elided=elided,
        provenance=provenance,
        projection_digest=header.group("projection"),
    )
    actual = projection_digest(ir)
    if actual != ir.projection_digest:
        raise CompileError(
            f"header has {ir.projection_digest}, semantic IR has {actual}",
            field="projection_digest",
        )
    if render(ir) != markdown:
        raise CompileError("compiled Markdown is not in canonical form", field="markdown")
    return ir


def _section_spans(markdown: str) -> tuple[str, list[tuple[str, int, int]]]:
    rendered = grogu_markdown.render_document(markdown)
    headings = []
    for block in rendered["blocks"]:
        if block["kind"] != "heading" or block["level"] not in {1, 2}:
            continue
        raw = markdown[block["start"] : block["end"]].rstrip("\r\n")
        match = re.match(r"^ {0,3}(#{1,2})(?:[ \t]+|$)(.*)$", raw)
        if match is not None:
            headings.append(
                {
                    "level": len(match.group(1)),
                    "title": match.group(2).strip(),
                    "start": block["start"],
                    "end": block["end"],
                }
            )
    title_heading = next(
        (heading for heading in headings if heading["level"] == 1), None
    )
    title = title_heading["title"] if title_heading else "Imported plan"
    sections = [heading for heading in headings if heading["level"] == 2]
    spans = []
    if (
        title_heading is not None
        and title_heading["start"] > 0
        and markdown[: title_heading["start"]].strip()
    ):
        spans.append(("Imported preface", 0, title_heading["start"]))
    preamble_start = title_heading["end"] if title_heading is not None else 0
    preamble_end = sections[0]["start"] if sections else len(markdown)
    if markdown[preamble_start:preamble_end].strip():
        spans.append(("Overview", preamble_start, preamble_end))
    for index, heading in enumerate(sections):
        start = heading["end"]
        end = sections[index + 1]["start"] if index + 1 < len(sections) else len(markdown)
        spans.append((heading["title"], start, end))
    if not spans and markdown:
        spans.append(("Imported content", 0, len(markdown)))
    return title, spans


def import_markdown(
    markdown: str,
    *,
    plan_id: str = "p-imported",
    stage: str = schema.IMPLEMENTATION,
    revision: str = "r0001",
) -> dict:
    """Import arbitrary legacy Markdown without dropping unrecognized text."""
    if not isinstance(markdown, str):
        raise TypeError("markdown must be a string")
    if stage not in schema.STAGES:
        raise CompileError(f"unknown import stage {stage!r}", field="stage")
    title, spans = _section_spans(markdown)
    document = plandoc.new_document(plan_id, title, revision=revision)
    manifest: dict[str, Any] = {"plandoc": {"counters": {}}}
    order = 1000
    section_nodes = []
    for heading, start, end in spans:
        node_id = plandoc.allocate_id(
            manifest, "note", existing_ids=document["nodes"]
        )
        node = plandoc.make_node(
            node_id,
            "note",
            heading,
            stage=stage,
            body=markdown[start:end],
            attrs={"source": "markdown-import", "source_start": start, "source_end": end},
            order=order,
            revision=revision,
        )
        document["nodes"][node_id] = node
        section_nodes.append((node_id, start, end))
        order += 1000

    parsed_document = grogu_markdown.render_document(markdown)
    mermaid_blocks = [
        block
        for block in parsed_document["code_blocks"]
        if str(block.get("lang", "")).lower() == "mermaid"
    ]
    for block_index, block in enumerate(mermaid_blocks):
        source = block["body"]
        parsed = grogu_mermaid.parse(source)
        diagram_id = plandoc.allocate_id(
            manifest, "diagram", existing_ids=document["nodes"]
        )
        diagram = plandoc.make_node(
            diagram_id,
            "diagram",
            f"Mermaid diagram {block_index + 1}",
            stage=stage,
            body=source,
            attrs={
                "source": source,
                "parsed": parsed,
                "source_start": block["start"],
                "source_end": block["end"],
            },
            order=order,
            revision=revision,
        )
        document["nodes"][diagram_id] = diagram
        order += 1000
        owner = next(
            (
                node_id
                for node_id, start, end in section_nodes
                if start <= block["start"] < end
            ),
            None,
        )
        if owner is not None:
            edge_id = plandoc.allocate_id(
                manifest, "edge", existing_ids=document["edges"]
            )
            document["edges"][edge_id] = plandoc.make_edge(
                edge_id,
                "contains",
                owner,
                diagram_id,
                revision=revision,
            )

        imported_nodes: dict[str, str] = {}
        for parsed_node in parsed.get("nodes", []):
            child_id = plandoc.allocate_id(
                manifest, "note", existing_ids=document["nodes"]
            )
            imported_nodes[str(parsed_node["id"])] = child_id
            document["nodes"][child_id] = plandoc.make_node(
                child_id,
                "note",
                str(parsed_node.get("label") or parsed_node["id"]),
                stage=stage,
                attrs={
                    "source": "mermaid",
                    "diagram": diagram_id,
                    "mermaid_id": str(parsed_node["id"]),
                    "shape": str(parsed_node.get("shape", "bare")),
                },
                order=order,
                revision=revision,
            )
            order += 1000
            edge_id = plandoc.allocate_id(
                manifest, "edge", existing_ids=document["edges"]
            )
            document["edges"][edge_id] = plandoc.make_edge(
                edge_id,
                "contains",
                diagram_id,
                child_id,
                revision=revision,
            )
        for parsed_edge in parsed.get("edges", []):
            source_id = imported_nodes.get(str(parsed_edge["from"]))
            target_id = imported_nodes.get(str(parsed_edge["to"]))
            if source_id is None or target_id is None:
                continue
            edge_id = plandoc.allocate_id(
                manifest, "edge", existing_ids=document["edges"]
            )
            document["edges"][edge_id] = plandoc.make_edge(
                edge_id,
                "diagram_edge",
                source_id,
                target_id,
                attrs={
                    "source": "mermaid",
                    "operator": str(parsed_edge.get("operator", "")),
                    "edge_kind": str(parsed_edge.get("kind", "")),
                    "label": str(parsed_edge.get("label", "")),
                    "pair_ordinal": int(parsed_edge.get("pair_ordinal", 0)),
                    "edge_index": int(parsed_edge.get("edge_index", 0)),
                },
                revision=revision,
            )
    document["provenance"] = {
        "at": "",
        "actor": "",
        "role": "reviewer",
        "agent": "migration",
        "origin": "migration",
    }
    return schema.validate_document(document)


def parse(markdown: str) -> ProjectionIR:
    """Parse compiled Markdown, or import legacy Markdown into a reviewer IR."""
    if not isinstance(markdown, str):
        raise TypeError("markdown must be a string")
    lines = markdown.splitlines()
    if len(lines) >= 3 and _HEADER.fullmatch(lines[2]):
        return _parse_compiled(markdown)
    document = import_markdown(markdown)
    return project(
        document,
        {
            "role": "reviewer",
            "stages": [schema.IMPLEMENTATION],
            "include": "all",
            "budget_chars": None,
            "since": "",
            "compiler": COMPILER_VERSION,
        },
    )


def first_difference(left: Any, right: Any, path: str = "$") -> str:
    """Return the first deterministic semantic field path that differs."""
    if type(left) is not type(right):
        return path
    if dataclasses.is_dataclass(left):
        for field in dataclasses.fields(left):
            found = first_difference(
                getattr(left, field.name),
                getattr(right, field.name),
                f"{path}.{field.name}",
            )
            if found:
                return found
        return ""
    if isinstance(left, tuple):
        if len(left) != len(right):
            return f"{path}.length"
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            found = first_difference(left_item, right_item, f"{path}[{index}]")
            if found:
                return found
        return ""
    return "" if left == right else path


def assert_equivalent(left: ProjectionIR, right: ProjectionIR) -> None:
    """Raise with the first differing semantic field."""
    field = first_difference(left, right)
    if field:
        raise CompileError("projection IR differs after round-trip", field=field)


def strict_enabled() -> bool:
    """Tests and explicit checks default to strict semantic equivalence."""
    value = os.environ.get(STRICT_ENV)
    if value is None:
        return True
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def verify_equivalence(ir: ProjectionIR) -> bool:
    parsed = parse(render(ir))
    assert_equivalent(ir, parsed)
    json_twin = render_json(ir)
    if [node["id"] for node in json_twin["nodes"]] != [
        node.id for node in ir.nodes
    ]:
        raise CompileError("JSON twin node ids differ", field="nodes")
    return True
