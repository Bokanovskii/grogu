"""Deterministic role-bounded compiler and Markdown importer.

The compiler has one executable identity: ``parse(render(ir)) == ir``.
Markdown remains readable, while each item carries a short canonical metadata
line and an explicit body character count so arbitrary verbatim Markdown
bodies cannot be mistaken for compiler structure.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import re
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
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
_H1 = re.compile(r"(?m)^# (.+?)\r?$")
_H2 = re.compile(r"(?m)^## (.+?)\r?$")


class CompileError(ValueError):
    """Compilation or semantic-equivalence verification failed."""

    def __init__(self, message: str, *, field: str = ""):
        self.field = field
        super().__init__(f"{field}: {message}" if field else message)


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
    body_state: str = "full"

    @property
    def attrs(self) -> dict:
        return canon.loads(self.attrs_json)

    @property
    def geometry(self) -> dict | None:
        return canon.loads(self.geometry_json) if self.geometry_json else None


@dataclass(frozen=True)
class ProjectionEdge:
    id: str
    kind: str
    source: str
    target: str
    attrs_json: str
    created_rev: str

    @property
    def attrs(self) -> dict:
        return canon.loads(self.attrs_json)


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


@dataclass(frozen=True)
class ProjectionIR:
    title: str
    nodes: tuple[ProjectionNode, ...]
    edges: tuple[ProjectionEdge, ...]
    elided: tuple[Elision, ...]
    provenance: Provenance
    projection_digest: str


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
    return value


def _edge_dict(edge: ProjectionEdge) -> dict:
    return {
        "id": edge.id,
        "kind": edge.kind,
        "from": edge.source,
        "to": edge.target,
        "attrs": edge.attrs,
        "created_rev": edge.created_rev,
    }


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


def _initial_selection(graph: dict, spec: dict) -> tuple[set[str], dict[str, str]]:
    stage_ids = {
        node_id
        for node_id, node in graph["nodes"].items()
        if node["stage"] in spec["stages"] or node["stage"] == ""
    }
    selected: set[str] = set()
    body_states: dict[str, str] = {}
    since = _revision_number(spec["since"])
    role = spec["role"]
    for node_id in sorted(stage_ids, key=canon.id_sort_key):
        node = graph["nodes"][node_id]
        kind = node["kind"]
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
                body_states[node_id] = "full"
            continue
        if spec["include"] == "all":
            selected.add(node_id)
            body_states[node_id] = "full"
        elif schema.NODE_KINDS[kind]["normative"] or kind in {"risk", "question"}:
            selected.add(node_id)
            body_states[node_id] = "full"
        elif kind in {"diagram", "region"}:
            selected.add(node_id)
            body_states[node_id] = "full"
        elif kind in {"note", "evidence", "reference"}:
            selected.add(node_id)
            body_states[node_id] = "projection"

    if spec["include"] == "normative":
        for node_id in sorted(stage_ids, key=canon.id_sort_key):
            node = graph["nodes"][node_id]
            if (
                node["kind"] == "thread"
                and node["attrs"].get("status") != "resolved"
                and _thread_attached(node, selected)
            ):
                selected.add(node_id)
                body_states[node_id] = "full"

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
                    selected.add(edge["to"])
                    body_states.setdefault(edge["to"], "projection")
                if (
                    edge["to"] in selected
                    and edge["from"] in stage_ids
                    and can_add(edge["from"])
                ):
                    if edge["from"] not in selected:
                        changed = True
                    selected.add(edge["from"])
                    body_states.setdefault(edge["from"], "projection")
    return selected, body_states


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


def project(graph: Mapping[str, Any], spec: Mapping[str, Any]) -> ProjectionIR:
    """Produce the ordered, role-filtered projection intermediate."""
    document = schema.validate_document(graph)
    projection_spec = schema.validate_projection_spec(spec)
    if projection_spec["compiler"] != COMPILER_VERSION:
        raise CompileError(
            f"unsupported compiler {projection_spec['compiler']}",
            field="provenance.compiler",
        )
    selected, body_states = _initial_selection(document, projection_spec)
    nodes = []
    elided = []
    for node_id in sorted(selected, key=canon.id_sort_key):
        node = document["nodes"][node_id]
        state = body_states.get(node_id, "full")
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
        if edge["from"] in selected and edge["to"] in selected
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
        graph_digest=canon.digest(document),
        projection_id=canon.digest(projection_spec),
        source_at=str(graph_provenance.get("at", "")),
        source_actor=str(graph_provenance.get("actor", "")),
        source_role=str(graph_provenance.get("role", "")),
        source_agent=str(graph_provenance.get("agent", "")),
        sealed_relationships=int(graph_provenance.get("sealed_relationships", 0)),
        budget_chars=projection_spec["budget_chars"],
        since=projection_spec["since"],
        depth=projection_spec.get("depth"),
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
    ir = _stamp(ir)
    if strict_enabled():
        round_tripped = parse(render(ir))
        assert_equivalent(ir, round_tripped)
    return ir


def _stage_label(stages: tuple[str, ...]) -> str:
    return ",".join(stages) if stages else "plan"


def _metadata(node: ProjectionNode) -> dict:
    value = {
        "attrs": node.attrs,
        "body_chars": len(node.body),
        "body_state": node.body_state,
        "created_rev": node.created_rev,
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
        if section in {"Elided", "Provenance"}:
            continue
        nodes = by_section.get(section, [])
        if not nodes:
            continue
        lines.extend([f"## {section}", ""])
        for node in nodes:
            lines.append(_render_item(node, edges_by_source.get(node.id, [])))
            lines.append("")

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
    )


def _parse_node_metadata(value: Any, node_id: str) -> dict:
    if not isinstance(value, Mapping):
        raise CompileError("node metadata must be an object", field=f"nodes.{node_id}")
    required = {
        "attrs",
        "body_chars",
        "body_state",
        "created_rev",
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
