"""Runtime schemas and graph invariants for Grogu plan documents.

The validator is intentionally standard-library only.  The matching published
JSON Schema 2020-12 documents live under ``schemas/`` for external tooling.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import grogu_plandoc_canon as canon

SCHEMA_VERSION = 1
DESIGN, IMPLEMENTATION, TESTING, EVALUATION = (
    "design",
    "implementation",
    "testing",
    "evaluation",
)
STAGES = (DESIGN, IMPLEMENTATION, TESTING, EVALUATION)
SEALED_STAGES = frozenset({TESTING, EVALUATION})
ROLES = ("architect", "designer", "engineer", "tester", "reviewer", "supervisor")
ROLE_READABLE_STAGES = {
    "architect": frozenset(STAGES),
    "designer": frozenset({DESIGN, IMPLEMENTATION}),
    "engineer": frozenset({DESIGN, IMPLEMENTATION}),
    "tester": frozenset({DESIGN, TESTING, EVALUATION}),
    "reviewer": frozenset(STAGES),
    "supervisor": frozenset(),
}

NODE_KINDS = {
    "goal": {"prefix": "goal", "normative": True},
    "directive": {"prefix": "dir", "normative": True},
    "constraint": {"prefix": "con", "normative": True},
    "invariant": {"prefix": "inv", "normative": True},
    "decision": {"prefix": "dec", "normative": True},
    "criterion": {"prefix": "crit", "normative": True},
    "task": {"prefix": "task", "normative": True},
    "risk": {"prefix": "risk", "normative": False},
    "question": {"prefix": "qn", "normative": False},
    "note": {"prefix": "note", "normative": False},
    "evidence": {"prefix": "ev", "normative": False},
    "reference": {"prefix": "ref", "normative": False},
    "diagram": {"prefix": "dia", "normative": False},
    "region": {"prefix": "reg", "normative": False},
    "thread": {"prefix": "thr", "normative": False},
}
EDGE_KINDS = (
    "depends_on",
    "blocks",
    "refines",
    "contains",
    "validates",
    "supersedes",
    "derives_from",
    "references",
    "answers",
    "anchors",
    "diagram_edge",
)
ACYCLIC_EDGE_KINDS = frozenset({"depends_on", "blocks", "refines", "contains"})
DIRECTIVE_BINDINGS = ("must", "should")
DIRECTIVE_STATUSES = ("active", "satisfied", "withdrawn", "superseded")
THREAD_STATUSES = ("open", "resolved")
ANCHOR_STATES = ("resolved", "shifted", "orphaned", "anchored")
SELECTOR_TYPES = ("node", "text", "object", "edge", "region", "composite")
MAX_SELECTOR_DEPTH = 4
MAX_SAFE_INTEGER = 2**53 - 1

_STABLE_ID = re.compile(r"^[a-z][a-z0-9_]*-[1-9][0-9]*$")
_REVISION_ID = re.compile(r"^r[0-9]{4,}$")
_PLAN_ID = re.compile(r"^p-[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_POINTER = re.compile(r"^(?:/(?:[^~/]|~[01])*)*$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}$")
_PLAIN_TEXT = re.compile(r"^[^\x00-\x1f\x7f]*$")
_REVERSE_DNS = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
)
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)


class SchemaError(ValueError):
    """A document does not satisfy the runtime schema."""

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def _error(path: str, message: str) -> None:
    raise SchemaError(path, message)


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _error(path, "expected an object")
    return dict(value)


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        _error(path, "expected an array")
    return value


def _keys(
    value: Mapping[str, Any],
    path: str,
    *,
    required: Iterable[str] = (),
    optional: Iterable[str] = (),
    additional: bool = False,
) -> None:
    required_set = set(required)
    missing = sorted(required_set - set(value))
    if missing:
        _error(path, f"missing required field(s): {', '.join(missing)}")
    if not additional:
        allowed = required_set | set(optional)
        unknown = sorted(set(value) - allowed)
        if unknown:
            _error(path, f"unknown field(s): {', '.join(unknown)}")


def _string(
    value: Any,
    path: str,
    *,
    nonempty: bool = False,
    maximum: int | None = None,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str):
        _error(path, "expected a string")
    normalized = canon.normalize_string(value, path=path)
    if nonempty and not normalized:
        _error(path, "must not be empty")
    if maximum is not None and len(normalized) > maximum:
        _error(path, f"must be at most {maximum} characters")
    if pattern is not None and pattern.fullmatch(normalized) is None:
        _error(path, "has an invalid format")
    return normalized


def _enum(value: Any, choices: Sequence[str], path: str) -> str:
    result = _string(value, path)
    if result not in choices:
        _error(path, f"expected one of {', '.join(choices)}")
    return result


def _line_text(value: Any, path: str, *, nonempty: bool = True) -> str:
    result = _string(value, path, nonempty=nonempty)
    if "\r" in result or "\n" in result:
        _error(path, "must fit on one line")
    return result


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int = canon.MIN_INTEGER,
    maximum: int = canon.MAX_INTEGER,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _error(path, "expected an integer")
    if value < minimum or value > maximum:
        _error(path, f"must be between {minimum} and {maximum}")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _error(path, "expected a boolean")
    return value


def _nullable(value: Any, validator, path: str):
    return None if value is None else validator(value, path)


def _revision(value: Any, path: str, *, empty: bool = False) -> str:
    if empty and value == "":
        return ""
    return _string(value, path, pattern=_REVISION_ID)


def _schema_version(value: Any, path: str) -> int:
    version = _integer(value, path, minimum=1, maximum=MAX_SAFE_INTEGER)
    if version > SCHEMA_VERSION:
        _error(
            path,
            f"schema version {version} requires a reader supporting version "
            f"{version}; current version is {SCHEMA_VERSION}",
        )
    if version != SCHEMA_VERSION:
        _error(path, f"must equal {SCHEMA_VERSION}")
    return version


def _timestamp(value: Any, path: str) -> str:
    result = _string(value, path, maximum=40, pattern=_UTC_TIMESTAMP)
    try:
        dt.datetime.fromisoformat(result.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise SchemaError(path, "is not a valid UTC timestamp") from error
    return result


def _digest(value: Any, path: str) -> str:
    return _string(value, path, pattern=_SHA256)


def _stable_id(value: Any, path: str, *, prefix: str | None = None) -> str:
    result = _string(value, path, maximum=128, pattern=_STABLE_ID)
    if prefix is not None and not result.startswith(f"{prefix}-"):
        _error(path, f"must use the {prefix}- stable-id prefix")
    return result


def _json_value(value: Any, path: str) -> Any:
    try:
        return canon.normalize(value, path=path)
    except canon.CanonicalError as error:
        raise SchemaError(path, error.args[0].split(": ", 1)[-1]) from error


def _geometry(value: Any, path: str) -> dict[str, int]:
    geometry = _object(value, path)
    _keys(geometry, path, required=("x", "y", "w", "h", "z"))
    result = {
        key: _integer(geometry[key], f"{path}.{key}")
        for key in ("x", "y", "w", "h", "z")
    }
    if result["w"] <= 0 or result["h"] <= 0:
        _error(path, "w and h must be positive")
    return result


def validate_selector(value: Any, *, path: str = "$", depth: int = 0) -> dict:
    """Validate the selector tagged union and return an isolated copy."""
    if depth > MAX_SELECTOR_DEPTH:
        _error(path, f"composite nesting exceeds {MAX_SELECTOR_DEPTH}")
    selector = _object(value, path)
    selector_type = _enum(selector.get("type"), SELECTOR_TYPES, f"{path}.type")

    if selector_type == "node":
        _keys(selector, path, required=("type", "id"))
        return {"type": "node", "id": _stable_id(selector["id"], f"{path}.id")}

    if selector_type == "text":
        _keys(
            selector,
            path,
            required=("type", "node", "quote", "position", "body_digest"),
        )
        quote = _object(selector["quote"], f"{path}.quote")
        _keys(quote, f"{path}.quote", required=("exact", "prefix", "suffix"))
        position = _object(selector["position"], f"{path}.position")
        _keys(position, f"{path}.position", required=("start", "end"))
        start = _integer(position["start"], f"{path}.position.start", minimum=0)
        end = _integer(position["end"], f"{path}.position.end", minimum=0)
        if end <= start:
            _error(f"{path}.position", "end must be greater than start")
        return {
            "type": "text",
            "node": _stable_id(selector["node"], f"{path}.node"),
            "quote": {
                "exact": _string(quote["exact"], f"{path}.quote.exact", nonempty=True),
                "prefix": _string(quote["prefix"], f"{path}.quote.prefix"),
                "suffix": _string(quote["suffix"], f"{path}.quote.suffix"),
            },
            "position": {"start": start, "end": end},
            "body_digest": _digest(selector["body_digest"], f"{path}.body_digest"),
        }

    if selector_type == "object":
        _keys(selector, path, required=("type", "id", "part"))
        part = _string(selector["part"], f"{path}.part", nonempty=True, maximum=256)
        if part not in {"title", "body"} and not part.startswith("attrs."):
            _error(f"{path}.part", "must be title, body, or an attrs.<key> path")
        return {
            "type": "object",
            "id": _stable_id(selector["id"], f"{path}.id"),
            "part": part,
        }

    if selector_type == "edge":
        by_id = "id" in selector
        by_pair = "from" in selector or "to" in selector or "ordinal" in selector
        if by_id == by_pair:
            _error(path, "edge selector requires either id or from/to/ordinal")
        if by_id:
            _keys(selector, path, required=("type", "id"))
            return {
                "type": "edge",
                "id": _stable_id(selector["id"], f"{path}.id", prefix="edge"),
            }
        _keys(selector, path, required=("type", "from", "to", "ordinal"))
        return {
            "type": "edge",
            "from": _stable_id(selector["from"], f"{path}.from"),
            "to": _stable_id(selector["to"], f"{path}.to"),
            "ordinal": _integer(
                selector["ordinal"], f"{path}.ordinal", minimum=0, maximum=MAX_SAFE_INTEGER
            ),
        }

    if selector_type == "region":
        _keys(selector, path, required=("type", "id"))
        return {
            "type": "region",
            "id": _stable_id(selector["id"], f"{path}.id", prefix="reg"),
        }

    _keys(selector, path, required=("type", "op", "members"))
    members = _array(selector["members"], f"{path}.members")
    if not members:
        _error(f"{path}.members", "must contain at least one selector")
    return {
        "type": "composite",
        "op": _enum(selector["op"], ("all", "any"), f"{path}.op"),
        "members": [
            validate_selector(item, path=f"{path}.members[{index}]", depth=depth + 1)
            for index, item in enumerate(members)
        ],
    }


def _comment(value: Any, path: str) -> dict:
    comment = _object(value, path)
    _keys(
        comment,
        path,
        required=("id", "at", "author", "body"),
        optional=("role", "revision"),
    )
    result = {
        "id": _string(comment["id"], f"{path}.id", nonempty=True, maximum=128),
        "at": _timestamp(comment["at"], f"{path}.at"),
        "author": _string(comment["author"], f"{path}.author", nonempty=True, maximum=256),
        "body": _string(comment["body"], f"{path}.body", nonempty=True, maximum=8000),
    }
    if "role" in comment:
        result["role"] = _enum(comment["role"], ROLES, f"{path}.role")
    if "revision" in comment:
        result["revision"] = _revision(comment["revision"], f"{path}.revision")
    return result


def _validate_attrs(kind: str, value: Any, path: str) -> dict:
    attrs = _object(value, path)
    normalized = _json_value(attrs, path)
    if kind == "directive":
        for key in ("binding", "audience", "status"):
            if key not in attrs:
                _error(path, f"directive attrs require {key}")
        _enum(attrs["binding"], DIRECTIVE_BINDINGS, f"{path}.binding")
        audience = _array(attrs["audience"], f"{path}.audience")
        if not audience:
            _error(f"{path}.audience", "must not be empty")
        for index, role in enumerate(audience):
            if role != "everyone":
                _enum(role, ROLES, f"{path}.audience[{index}]")
        _enum(attrs["status"], DIRECTIVE_STATUSES, f"{path}.status")
    elif kind == "thread":
        for key in ("selector", "comments", "status"):
            if key not in attrs:
                _error(path, f"thread attrs require {key}")
        validate_selector(attrs["selector"], path=f"{path}.selector")
        comments = _array(attrs["comments"], f"{path}.comments")
        for index, comment in enumerate(comments):
            _comment(comment, f"{path}.comments[{index}]")
        _enum(attrs["status"], THREAD_STATUSES, f"{path}.status")
        if "anchor_state" in attrs:
            _enum(attrs["anchor_state"], ANCHOR_STATES, f"{path}.anchor_state")
    elif kind == "diagram":
        if "source" in attrs:
            _string(attrs["source"], f"{path}.source")
        if "parsed" in attrs:
            _object(attrs["parsed"], f"{path}.parsed")
    elif kind == "reference" and "url" in attrs:
        _string(attrs["url"], f"{path}.url", nonempty=True, maximum=4096)
    return normalized


def _validate_ext(value: Any, path: str) -> dict:
    extension = _object(value, path)
    normalized = {}
    for key in sorted(extension, key=canon.utf16_key):
        _string(key, f"{path}<key>", maximum=253, pattern=_REVERSE_DNS)
        normalized[key] = _json_value(extension[key], f"{path}.{key}")
    return normalized


def validate_node(value: Any, *, path: str = "$") -> dict:
    """Validate one typed plan-document node."""
    node = _object(value, path)
    _keys(
        node,
        path,
        required=(
            "id",
            "kind",
            "stage",
            "title",
            "body",
            "attrs",
            "order",
            "created_rev",
            "updated_rev",
        ),
        optional=("geometry", "ext"),
    )
    kind = _enum(node["kind"], tuple(NODE_KINDS), f"{path}.kind")
    prefix = NODE_KINDS[kind]["prefix"]
    stage = _string(node["stage"], f"{path}.stage")
    if stage not in (*STAGES, ""):
        _error(f"{path}.stage", "must be a plan stage or empty for plan-level")
    result = {
        "id": _stable_id(node["id"], f"{path}.id", prefix=prefix),
        "kind": kind,
        "stage": stage,
        "title": _line_text(node["title"], f"{path}.title"),
        "body": _string(node["body"], f"{path}.body"),
        "attrs": _validate_attrs(kind, node["attrs"], f"{path}.attrs"),
        "order": _integer(node["order"], f"{path}.order"),
        "created_rev": _revision(node["created_rev"], f"{path}.created_rev"),
        "updated_rev": _revision(node["updated_rev"], f"{path}.updated_rev"),
    }
    if "geometry" in node:
        result["geometry"] = _geometry(node["geometry"], f"{path}.geometry")
    if "ext" in node:
        result["ext"] = _validate_ext(node["ext"], f"{path}.ext")
    return result


def validate_edge(value: Any, *, path: str = "$") -> dict:
    """Validate one typed graph edge."""
    edge = _object(value, path)
    _keys(
        edge,
        path,
        required=("id", "kind", "from", "to", "attrs", "created_rev"),
        optional=("ext",),
    )
    result = {
        "id": _stable_id(edge["id"], f"{path}.id", prefix="edge"),
        "kind": _enum(edge["kind"], EDGE_KINDS, f"{path}.kind"),
        "from": _stable_id(edge["from"], f"{path}.from"),
        "to": _stable_id(edge["to"], f"{path}.to"),
        "attrs": _json_value(_object(edge["attrs"], f"{path}.attrs"), f"{path}.attrs"),
        "created_rev": _revision(edge["created_rev"], f"{path}.created_rev"),
    }
    if result["kind"] == "diagram_edge":
        attrs = result["attrs"]
        for key in ("operator", "label"):
            if key not in attrs:
                _error(f"{path}.attrs", f"diagram_edge attrs require {key}")
            _string(attrs[key], f"{path}.attrs.{key}")
        if "pair_ordinal" in attrs:
            _integer(
                attrs["pair_ordinal"],
                f"{path}.attrs.pair_ordinal",
                minimum=0,
                maximum=MAX_SAFE_INTEGER,
            )
        if "edge_index" in attrs:
            _integer(
                attrs["edge_index"],
                f"{path}.attrs.edge_index",
                minimum=0,
                maximum=MAX_SAFE_INTEGER,
            )
    if "ext" in edge:
        result["ext"] = _validate_ext(edge["ext"], f"{path}.ext")
    return result


def _cycle(nodes: Mapping[str, dict], edges: Iterable[dict], kind: str) -> list[str]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if edge["kind"] == kind:
            adjacency[edge["from"]].append(edge["to"])
    for values in adjacency.values():
        values.sort(key=canon.id_sort_key)
    visiting: list[str] = []
    active: set[str] = set()
    visited: set[str] = set()

    def walk(node_id: str) -> list[str]:
        if node_id in active:
            start = visiting.index(node_id)
            return visiting[start:] + [node_id]
        if node_id in visited:
            return []
        active.add(node_id)
        visiting.append(node_id)
        for target in adjacency.get(node_id, []):
            found = walk(target)
            if found:
                return found
        visiting.pop()
        active.remove(node_id)
        visited.add(node_id)
        return []

    for node_id in sorted(nodes, key=canon.id_sort_key):
        found = walk(node_id)
        if found:
            return found
    return []


def validate_document(value: Any, *, path: str = "$") -> dict:
    """Validate a complete graph, including all edge invariants."""
    document = _object(value, path)
    _keys(
        document,
        path,
        required=(
            "schema_version",
            "kind",
            "plan_id",
            "title",
            "revision",
            "nodes",
            "edges",
        ),
        optional=("partition", "canvas", "provenance"),
    )
    _schema_version(document["schema_version"], f"{path}.schema_version")
    if document["kind"] != "grogu.plan_document":
        _error(f"{path}.kind", "must equal grogu.plan_document")
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": "grogu.plan_document",
        "plan_id": _string(document["plan_id"], f"{path}.plan_id", pattern=_PLAN_ID),
        "title": _line_text(document["title"], f"{path}.title"),
        "revision": _revision(document["revision"], f"{path}.revision"),
        "nodes": {},
        "edges": {},
    }
    if "partition" in document:
        result["partition"] = _enum(
            document["partition"], ("open", TESTING, EVALUATION), f"{path}.partition"
        )
    raw_nodes = _object(document["nodes"], f"{path}.nodes")
    for node_id in sorted(raw_nodes, key=canon.id_sort_key):
        node = validate_node(raw_nodes[node_id], path=f"{path}.nodes.{node_id}")
        if node_id != node["id"]:
            _error(f"{path}.nodes.{node_id}", "map key must equal node id")
        result["nodes"][node_id] = node
    raw_edges = _object(document["edges"], f"{path}.edges")
    for edge_id in sorted(raw_edges, key=canon.id_sort_key):
        edge = validate_edge(raw_edges[edge_id], path=f"{path}.edges.{edge_id}")
        if edge_id != edge["id"]:
            _error(f"{path}.edges.{edge_id}", "map key must equal edge id")
        result["edges"][edge_id] = edge

    for edge_id, edge in result["edges"].items():
        edge_path = f"{path}.edges.{edge_id}"
        if edge["from"] not in result["nodes"] or edge["to"] not in result["nodes"]:
            _error(edge_path, "both endpoints must exist")
        if edge["from"] == edge["to"] and edge["kind"] not in {
            "references",
            "diagram_edge",
        }:
            _error(
                edge_path,
                "self-edges are allowed only for references and diagram_edge",
            )
        source_stage = result["nodes"][edge["from"]]["stage"]
        target_stage = result["nodes"][edge["to"]]["stage"]
        crosses_seal = (
            source_stage != target_stage
            and ((source_stage in SEALED_STAGES) != (target_stage in SEALED_STAGES))
        )
        if crosses_seal and edge["kind"] != "references":
            _error(edge_path, "only references may cross an open/sealed boundary")

    parents: dict[str, str] = {}
    for edge in result["edges"].values():
        if edge["kind"] == "contains":
            if edge["to"] in parents:
                _error(
                    f"{path}.edges.{edge['id']}",
                    f"contains target already has parent {parents[edge['to']]}",
                )
            parents[edge["to"]] = edge["from"]

    for kind in sorted(ACYCLIC_EDGE_KINDS):
        found = _cycle(result["nodes"], result["edges"].values(), kind)
        if found:
            _error(f"{path}.edges", f"{kind} cycle: {' -> '.join(found)}")

    for node_id, node in result["nodes"].items():
        if node["kind"] != "thread":
            continue
        selector = validate_selector(
            node["attrs"]["selector"], path=f"{path}.nodes.{node_id}.attrs.selector"
        )
        thread_partition = (
            node["stage"] if node["stage"] in SEALED_STAGES else "open"
        )
        for target_id in selector_node_ids(selector):
            target = result["nodes"].get(target_id)
            if target is None:
                continue
            target_partition = (
                target["stage"] if target["stage"] in SEALED_STAGES else "open"
            )
            if target_partition != thread_partition:
                _error(
                    f"{path}.nodes.{node_id}.attrs.selector",
                    "thread selector may not cross a partition boundary",
                )
        for edge_id in selector_edge_ids(selector):
            edge = result["edges"].get(edge_id)
            if edge is None:
                continue
            endpoint_partitions = {
                (
                    result["nodes"][endpoint]["stage"]
                    if result["nodes"][endpoint]["stage"] in SEALED_STAGES
                    else "open"
                )
                for endpoint in (edge["from"], edge["to"])
            }
            if endpoint_partitions != {thread_partition}:
                _error(
                    f"{path}.nodes.{node_id}.attrs.selector",
                    "thread selector may not cross a partition boundary",
                )
        anchor_state = node["attrs"].get("anchor_state", "resolved")
        missing_nodes = set(selector_node_ids(selector)) - set(result["nodes"])
        missing_edges = set(selector_edge_ids(selector)) - set(result["edges"])
        if (missing_nodes or missing_edges) and anchor_state not in {"orphaned"}:
            details = []
            if missing_nodes:
                details.append(
                    f"node(s): {', '.join(sorted(missing_nodes, key=canon.id_sort_key))}"
                )
            if missing_edges:
                details.append(
                    f"edge(s): {', '.join(sorted(missing_edges, key=canon.id_sort_key))}"
                )
            _error(
                f"{path}.nodes.{node_id}.attrs.selector",
                "references missing " + "; ".join(details),
            )

    if "canvas" in document:
        canvas = _object(document["canvas"], f"{path}.canvas")
        _keys(canvas, f"{path}.canvas", required=("snap",), optional=("viewport",))
        result["canvas"] = {
            "snap": _integer(canvas["snap"], f"{path}.canvas.snap", minimum=1)
        }
        if "viewport" in canvas:
            viewport = _object(canvas["viewport"], f"{path}.canvas.viewport")
            _keys(viewport, f"{path}.canvas.viewport", required=("x", "y", "zoom_milli"))
            result["canvas"]["viewport"] = {
                "x": _integer(viewport["x"], f"{path}.canvas.viewport.x"),
                "y": _integer(viewport["y"], f"{path}.canvas.viewport.y"),
                "zoom_milli": _integer(
                    viewport["zoom_milli"],
                    f"{path}.canvas.viewport.zoom_milli",
                    minimum=1,
                ),
            }
    if "provenance" in document:
        result["provenance"] = _json_value(
            _object(document["provenance"], f"{path}.provenance"),
            f"{path}.provenance",
        )
    return result


def selector_node_ids(selector: Mapping[str, Any]) -> list[str]:
    """Return directly named node ids without resolving the selector."""
    selector_type = selector.get("type")
    if selector_type in {"node", "object", "region"}:
        return [str(selector.get("id", ""))]
    if selector_type == "text":
        return [str(selector.get("node", ""))]
    if selector_type == "edge" and "from" in selector:
        return [str(selector.get("from", "")), str(selector.get("to", ""))]
    if selector_type == "composite":
        values = {
            node_id
            for member in selector.get("members", [])
            for node_id in selector_node_ids(member)
            if node_id
        }
        return sorted(values, key=canon.id_sort_key)
    return []


def selector_edge_ids(selector: Mapping[str, Any]) -> list[str]:
    """Return directly named edge ids without resolving endpoint selectors."""
    selector_type = selector.get("type")
    if selector_type == "edge" and "id" in selector:
        return [str(selector.get("id", ""))]
    if selector_type == "composite":
        values = {
            edge_id
            for member in selector.get("members", [])
            for edge_id in selector_edge_ids(member)
            if edge_id
        }
        return sorted(values, key=canon.id_sort_key)
    return []


def validate_patch_ops(value: Any, *, path: str = "$") -> list[dict]:
    """Validate the immutable JSON-Patch subset used in revision envelopes."""
    operations = _array(value, path)
    result = []
    for index, raw in enumerate(operations):
        op_path = f"{path}[{index}]"
        operation = _object(raw, op_path)
        op = _enum(
            operation.get("op"),
            ("add", "remove", "replace", "test", "copy", "move"),
            f"{op_path}.op",
        )
        required = {"op", "path"}
        optional: set[str] = set()
        if op in {"add", "replace", "test"}:
            required.add("value")
        if op in {"copy", "move"}:
            required.add("from")
        _keys(operation, op_path, required=required, optional=optional)
        pointer = _string(operation["path"], f"{op_path}.path", pattern=_POINTER)
        item = {"op": op, "path": pointer}
        if "from" in operation:
            item["from"] = _string(
                operation["from"], f"{op_path}.from", pattern=_POINTER
            )
        if "value" in operation:
            item["value"] = _json_value(operation["value"], f"{op_path}.value")
        result.append(item)
    return result


def validate_revision(value: Any, *, path: str = "$") -> dict:
    """Validate one immutable revision envelope."""
    revision = _object(value, path)
    _keys(
        revision,
        path,
        required=(
            "schema_version",
            "revision",
            "seq",
            "parent",
            "at",
            "actor",
            "role",
            "agent",
            "intent",
            "origin",
            "ops",
            "before_digest",
            "after_digest",
        ),
    )
    _schema_version(revision["schema_version"], f"{path}.schema_version")
    seq = _integer(revision["seq"], f"{path}.seq", minimum=1, maximum=MAX_SAFE_INTEGER)
    revision_id = _revision(revision["revision"], f"{path}.revision")
    if revision_id != f"r{seq:04d}":
        _error(f"{path}.revision", "must be r plus the zero-padded sequence")
    parent = _revision(revision["parent"], f"{path}.parent", empty=True)
    expected_parent = "" if seq == 1 else f"r{seq - 1:04d}"
    if parent != expected_parent:
        _error(f"{path}.parent", f"must equal {expected_parent!r}")
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": revision_id,
        "seq": seq,
        "parent": parent,
        "at": _timestamp(revision["at"], f"{path}.at"),
        "actor": _string(revision["actor"], f"{path}.actor", nonempty=True, maximum=256),
        "role": _enum(revision["role"], ROLES, f"{path}.role"),
        "agent": _string(revision["agent"], f"{path}.agent", nonempty=True, maximum=256),
        "intent": _string(revision["intent"], f"{path}.intent", nonempty=True, maximum=8000),
        "origin": _string(revision["origin"], f"{path}.origin", nonempty=True, maximum=256),
        "ops": validate_patch_ops(revision["ops"], path=f"{path}.ops"),
        "before_digest": _digest(revision["before_digest"], f"{path}.before_digest"),
        "after_digest": _digest(revision["after_digest"], f"{path}.after_digest"),
    }


def validate_projection_spec(value: Any, *, path: str = "$") -> dict:
    spec = _object(value, path)
    _keys(
        spec,
        path,
        required=("role", "stages", "include", "budget_chars", "since", "compiler"),
        optional=("depth",),
    )
    role = _enum(spec["role"], ROLES, f"{path}.role")
    stages = _array(spec["stages"], f"{path}.stages")
    normalized_stages = []
    for index, stage in enumerate(stages):
        normalized_stage = _enum(stage, STAGES, f"{path}.stages[{index}]")
        if normalized_stage in normalized_stages:
            _error(f"{path}.stages", f"duplicate stage {normalized_stage}")
        normalized_stages.append(normalized_stage)
    unreadable = set(normalized_stages) - set(ROLE_READABLE_STAGES.get(role, ()))
    if unreadable:
        _error(f"{path}.stages", "contains a stage the role cannot read")
    budget = spec["budget_chars"]
    if budget is not None:
        budget = _integer(
            budget, f"{path}.budget_chars", minimum=1, maximum=MAX_SAFE_INTEGER
        )
    result = {
        "role": role,
        "stages": [stage for stage in STAGES if stage in normalized_stages],
        "include": _enum(spec["include"], ("normative", "all"), f"{path}.include"),
        "budget_chars": budget,
        "since": _revision(spec["since"], f"{path}.since", empty=True),
        "compiler": _integer(spec["compiler"], f"{path}.compiler", minimum=1),
    }
    if "depth" in spec:
        result["depth"] = _integer(
            spec["depth"], f"{path}.depth", minimum=0, maximum=MAX_SAFE_INTEGER
        )
    return result


# Dashboard DTO validation -------------------------------------------------

SOURCE_HEALTH_SOURCES = ("watch", "trace", "session", "plan")
SOURCE_HEALTH_STATES = ("live", "partial", "stale", "unavailable")
COVERAGE_STATES = ("complete", "partial", "unavailable")
LIFECYCLE_STATES = (
    "registered",
    "running",
    "finished",
    "failed",
    "cancelled",
    "unknown",
)
ACTIVITY_STATES = ("active", "quiet", "possibly_stuck", "blocked", "unknown")
CONNECTION_STATES = ("live", "stale", "disconnected", "unknown")
EVIDENCE_BASES = ("observed", "inferred", "mixed")
EVENT_BASES = ("observed", "model_summary", "inferred")
ACTION_PHASES = ("started", "completed", "failed", "unknown")
OBJECT_RELATIONS = ("affected", "evidence", "context")
CONTROL_SOURCE_STATUSES = (
    "available",
    "empty",
    "unavailable",
    "stale",
    "disconnected",
    "permission_limited",
)
CONTROL_SAFE_REASONS = (
    "not_registered",
    "no_run_events",
    "unsupported_source",
    "read_error",
    "permission_denied",
    "malformed_source",
    "source_changed",
    "source_limit",
    "plan_unavailable",
    "not_observed",
    "none",
)
CONTROL_SOURCES = (
    "registrations",
    "commands",
    "session_events",
    "runtime",
    "plan_state",
)
CONTROL_EVENT_TYPES = (
    "command",
    "tool",
    "run_started",
    "run_finished",
    "permission_requested",
    "permission_completed",
)


def _strict_object(
    value: Any,
    path: str,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> dict[str, Any]:
    result = _object(value, path)
    _keys(result, path, required=required, optional=optional)
    return result


def _bounded_id(value: Any, path: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    return _string(
        value,
        path,
        nonempty=True,
        maximum=256,
        pattern=_PLAIN_TEXT,
    )


def _safe_code(value: Any, path: str) -> str:
    return _string(value, path, nonempty=True, maximum=256, pattern=_SAFE_IDENTIFIER)


def _plain_text(
    value: Any,
    path: str,
    *,
    nonempty: bool = False,
    maximum: int = 512,
) -> str:
    return _string(
        value,
        path,
        nonempty=nonempty,
        maximum=maximum,
        pattern=_PLAIN_TEXT,
    )


def _control_source_coverage(value: Any, path: str) -> dict:
    item = _strict_object(
        value,
        path,
        ("source", "status", "observed_through", "reason"),
    )
    return {
        "source": _enum(item["source"], CONTROL_SOURCES, f"{path}.source"),
        "status": _enum(
            item["status"], CONTROL_SOURCE_STATUSES, f"{path}.status"
        ),
        "observed_through": (
            None
            if item["observed_through"] is None
            else _integer(
                item["observed_through"],
                f"{path}.observed_through",
                minimum=0,
                maximum=MAX_SAFE_INTEGER,
            )
        ),
        "reason": _enum(
            item["reason"], CONTROL_SAFE_REASONS, f"{path}.reason"
        ),
    }


def _control_operational_event(value: Any, path: str) -> dict:
    item = _strict_object(
        value,
        path,
        (
            "id",
            "agent_key",
            "plan",
            "type",
            "at",
            "name",
            "phase",
            "success",
            "duration_ms",
            "error_code",
            "basis",
        ),
    )
    return {
        "id": _bounded_id(item["id"], f"{path}.id"),
        "agent_key": _bounded_id(item["agent_key"], f"{path}.agent_key"),
        "plan": _string(item["plan"], f"{path}.plan", pattern=_PLAN_ID),
        "type": _enum(item["type"], CONTROL_EVENT_TYPES, f"{path}.type"),
        "at": _integer(
            item["at"], f"{path}.at", minimum=0, maximum=MAX_SAFE_INTEGER
        ),
        "name": (
            None
            if item["name"] is None
            else _safe_code(item["name"], f"{path}.name")
        ),
        "phase": (
            None
            if item["phase"] is None
            else _enum(
                item["phase"], ("started", "completed"), f"{path}.phase"
            )
        ),
        "success": (
            None
            if item["success"] is None
            else _boolean(item["success"], f"{path}.success")
        ),
        "duration_ms": (
            None
            if item["duration_ms"] is None
            else _integer(
                item["duration_ms"],
                f"{path}.duration_ms",
                minimum=0,
                maximum=MAX_SAFE_INTEGER,
            )
        ),
        "error_code": (
            None
            if item["error_code"] is None
            else _safe_code(item["error_code"], f"{path}.error_code")
        ),
        "basis": _enum(item["basis"], ("observed",), f"{path}.basis"),
    }


def _control_consequence(value: Any, path: str) -> dict:
    item = _strict_object(
        value,
        path,
        ("binding", "gates", "requires_replan", "release"),
    )
    return {
        "binding": _boolean(item["binding"], f"{path}.binding"),
        "gates": [
            _enum(gate, ("implement", "test", "evaluate"), f"{path}.gates[{index}]")
            for index, gate in enumerate(_array(item["gates"], f"{path}.gates"))
        ],
        "requires_replan": _boolean(
            item["requires_replan"], f"{path}.requires_replan"
        ),
        "release": _enum(
            item["release"],
            ("acknowledgement_or_withdrawal", "replan", "none"),
            f"{path}.release",
        ),
    }


def validate_object_ref(value: Any, *, path: str = "$") -> dict:
    ref = _strict_object(
        value,
        path,
        ("plan_id", "revision", "object_id", "edge_id", "frame_id", "artifact_id", "relation"),
    )
    return {
        "plan_id": _string(ref["plan_id"], f"{path}.plan_id", pattern=_PLAN_ID),
        "revision": _nullable(ref["revision"], _revision, f"{path}.revision"),
        "object_id": (
            None
            if ref["object_id"] is None
            else _stable_id(ref["object_id"], f"{path}.object_id")
        ),
        "edge_id": (
            None
            if ref["edge_id"] is None
            else _stable_id(ref["edge_id"], f"{path}.edge_id", prefix="edge")
        ),
        "frame_id": (
            None
            if ref["frame_id"] is None
            else _stable_id(ref["frame_id"], f"{path}.frame_id", prefix="reg")
        ),
        "artifact_id": (
            None
            if ref["artifact_id"] is None
            else _string(
                ref["artifact_id"],
                f"{path}.artifact_id",
                nonempty=True,
                maximum=256,
                pattern=_ARTIFACT_ID,
            )
        ),
        "relation": _enum(ref["relation"], OBJECT_RELATIONS, f"{path}.relation"),
    }


def _object_refs(value: Any, path: str) -> list[dict]:
    refs = _array(value, path)
    return [
        validate_object_ref(ref, path=f"{path}[{index}]")
        for index, ref in enumerate(refs)
    ]


def validate_action(value: Any, *, path: str = "$") -> dict:
    action = _strict_object(
        value,
        path,
        (
            "event_id",
            "tool_call_id",
            "kind",
            "tool_name",
            "phase",
            "at",
            "duration_ms",
            "refs",
            "basis",
        ),
    )
    return {
        "event_id": _bounded_id(action["event_id"], f"{path}.event_id"),
        "tool_call_id": _bounded_id(
            action["tool_call_id"], f"{path}.tool_call_id", nullable=True
        ),
        "kind": _safe_code(action["kind"], f"{path}.kind"),
        "tool_name": (
            None
            if action["tool_name"] is None
            else _safe_code(action["tool_name"], f"{path}.tool_name")
        ),
        "phase": _enum(action["phase"], ACTION_PHASES, f"{path}.phase"),
        "at": _timestamp(action["at"], f"{path}.at"),
        "duration_ms": (
            None
            if action["duration_ms"] is None
            else _integer(
                action["duration_ms"],
                f"{path}.duration_ms",
                minimum=0,
                maximum=MAX_SAFE_INTEGER,
            )
        ),
        "refs": _object_refs(action["refs"], f"{path}.refs"),
        "basis": _enum(action["basis"], ("observed",), f"{path}.basis"),
    }


def validate_model_summary(value: Any, *, path: str = "$") -> dict:
    summary = _strict_object(
        value,
        path,
        ("text", "author_role", "classification", "source_event_id", "at", "basis"),
    )
    return {
        "text": _plain_text(
            summary["text"], f"{path}.text", nonempty=True, maximum=240
        ),
        "author_role": _enum(summary["author_role"], ROLES, f"{path}.author_role"),
        "classification": _safe_code(
            summary["classification"], f"{path}.classification"
        ),
        "source_event_id": _bounded_id(
            summary["source_event_id"], f"{path}.source_event_id"
        ),
        "at": _timestamp(summary["at"], f"{path}.at"),
        "basis": _enum(summary["basis"], ("model_summary",), f"{path}.basis"),
    }


def _source_health(value: Any, path: str) -> dict:
    health = _strict_object(
        value,
        path,
        ("source", "state", "last_success_at", "watermark", "gap", "reason_code"),
    )
    return {
        "source": _enum(health["source"], SOURCE_HEALTH_SOURCES, f"{path}.source"),
        "state": _enum(health["state"], SOURCE_HEALTH_STATES, f"{path}.state"),
        "last_success_at": (
            None
            if health["last_success_at"] is None
            else _timestamp(health["last_success_at"], f"{path}.last_success_at")
        ),
        "watermark": (
            None
            if health["watermark"] is None
            else _plain_text(
                health["watermark"], f"{path}.watermark", maximum=2048
            )
        ),
        "gap": _boolean(health["gap"], f"{path}.gap"),
        "reason_code": (
            None
            if health["reason_code"] is None
            else _safe_code(health["reason_code"], f"{path}.reason_code")
        ),
    }


def _coverage(value: Any, path: str) -> dict:
    coverage = _strict_object(
        value, path, ("lifecycle", "tools", "outcomes", "truncated")
    )
    return {
        "lifecycle": _enum(coverage["lifecycle"], COVERAGE_STATES, f"{path}.lifecycle"),
        "tools": _enum(coverage["tools"], COVERAGE_STATES, f"{path}.tools"),
        "outcomes": _enum(coverage["outcomes"], COVERAGE_STATES, f"{path}.outcomes"),
        "truncated": _boolean(coverage["truncated"], f"{path}.truncated"),
    }


def _agent_row(value: Any, path: str) -> dict:
    required = (
        "agent_key",
        "agent",
        "run_id",
        "parent_agent_key",
        "role",
        "workstream",
        "plan_id",
        "revision",
        "lifecycle",
        "activity",
        "connection",
        "started_at",
        "last_observed_at",
        "elapsed_ms",
        "elapsed_basis",
        "current_action",
        "last_action",
        "tools",
        "failures",
        "blockers",
        "steering",
        "evidence",
        "links",
        "summary",
    )
    optional = (
        "lifecycle_source",
        "lifecycle_observed_at",
        "lifecycle_reason",
        "owned_stages",
        "stage_ownership",
        "source_coverage",
        "recent_events",
        "events_status",
        "event_source_registered",
        "blocker_details",
    )
    row = _strict_object(value, path, required, optional)
    revision = _strict_object(
        row["revision"], f"{path}.revision", ("last_read", "current", "relation")
    )
    tools = _strict_object(
        row["tools"],
        f"{path}.tools",
        ("started", "completed", "failed", "inflight", "grogu_commands", "complete"),
    )
    steering = _strict_object(
        row["steering"],
        f"{path}.steering",
        ("unread", "queued", "delivered", "acknowledged", "audit_refs"),
    )
    evidence = _strict_object(
        row["evidence"], f"{path}.evidence", ("basis", "sources", "reason_codes", "coverage")
    )
    result = {
        "agent_key": _bounded_id(row["agent_key"], f"{path}.agent_key"),
        "agent": _plain_text(
            row["agent"], f"{path}.agent", nonempty=True, maximum=256
        ),
        "run_id": _bounded_id(row["run_id"], f"{path}.run_id"),
        "parent_agent_key": _bounded_id(
            row["parent_agent_key"], f"{path}.parent_agent_key", nullable=True
        ),
        "role": _enum(row["role"], (*ROLES, "unknown"), f"{path}.role"),
        "workstream": _bounded_id(
            row["workstream"], f"{path}.workstream", nullable=True
        ),
        "plan_id": _string(row["plan_id"], f"{path}.plan_id", pattern=_PLAN_ID),
        "revision": {
            "last_read": (
                None
                if revision["last_read"] is None
                else _revision(revision["last_read"], f"{path}.revision.last_read")
            ),
            "current": (
                None
                if revision["current"] is None
                else _revision(revision["current"], f"{path}.revision.current")
            ),
            "relation": _enum(
                revision["relation"], ("current", "behind", "unknown"), f"{path}.revision.relation"
            ),
        },
        "lifecycle": _enum(row["lifecycle"], LIFECYCLE_STATES, f"{path}.lifecycle"),
        "activity": _enum(row["activity"], ACTIVITY_STATES, f"{path}.activity"),
        "connection": _enum(row["connection"], CONNECTION_STATES, f"{path}.connection"),
        "started_at": (
            None if row["started_at"] is None else _timestamp(row["started_at"], f"{path}.started_at")
        ),
        "last_observed_at": (
            None
            if row["last_observed_at"] is None
            else _timestamp(row["last_observed_at"], f"{path}.last_observed_at")
        ),
        "elapsed_ms": (
            None
            if row["elapsed_ms"] is None
            else _integer(
                row["elapsed_ms"], f"{path}.elapsed_ms", minimum=0, maximum=MAX_SAFE_INTEGER
            )
        ),
        "elapsed_basis": _enum(
            row["elapsed_basis"],
            ("lifecycle_start", "first_observed", "unknown"),
            f"{path}.elapsed_basis",
        ),
        "current_action": (
            None
            if row["current_action"] is None
            else validate_action(row["current_action"], path=f"{path}.current_action")
        ),
        "last_action": (
            None
            if row["last_action"] is None
            else validate_action(row["last_action"], path=f"{path}.last_action")
        ),
        "tools": {},
        "failures": [],
        "blockers": [],
        "steering": {},
        "evidence": {},
        "links": _object_refs(row["links"], f"{path}.links"),
        "summary": (
            None
            if row["summary"] is None
            else validate_model_summary(row["summary"], path=f"{path}.summary")
        ),
    }
    for key in ("started", "completed", "failed", "inflight"):
        result["tools"][key] = (
            None
            if tools[key] is None
            else _integer(
                tools[key], f"{path}.tools.{key}", minimum=0, maximum=MAX_SAFE_INTEGER
            )
        )
    result["tools"]["grogu_commands"] = _integer(
        tools["grogu_commands"],
        f"{path}.tools.grogu_commands",
        minimum=0,
        maximum=MAX_SAFE_INTEGER,
    )
    result["tools"]["complete"] = _boolean(tools["complete"], f"{path}.tools.complete")

    for index, raw in enumerate(_array(row["failures"], f"{path}.failures")):
        item_path = f"{path}.failures[{index}]"
        failure = _strict_object(
            raw, item_path, ("event_id", "category", "safe_code", "at", "ref")
        )
        result["failures"].append(
            {
                "event_id": _bounded_id(failure["event_id"], f"{item_path}.event_id"),
                "category": _enum(
                    failure["category"],
                    ("tool_transport", "command_exit", "agent_lifecycle"),
                    f"{item_path}.category",
                ),
                "safe_code": (
                    None
                    if failure["safe_code"] is None
                    else _safe_code(failure["safe_code"], f"{item_path}.safe_code")
                ),
                "at": _timestamp(failure["at"], f"{item_path}.at"),
                "ref": (
                    None
                    if failure["ref"] is None
                    else validate_object_ref(failure["ref"], path=f"{item_path}.ref")
                ),
            }
        )
    for index, raw in enumerate(_array(row["blockers"], f"{path}.blockers")):
        item_path = f"{path}.blockers[{index}]"
        blocker = _strict_object(raw, item_path, ("kind", "visible_ref", "observed_at"))
        result["blockers"].append(
            {
                "kind": _enum(
                    blocker["kind"],
                    ("permission", "gate", "amendment", "defect", "steering"),
                    f"{item_path}.kind",
                ),
                "visible_ref": (
                    None
                    if blocker["visible_ref"] is None
                    else _bounded_id(blocker["visible_ref"], f"{item_path}.visible_ref")
                ),
                "observed_at": _timestamp(
                    blocker["observed_at"], f"{item_path}.observed_at"
                ),
            }
        )
    for key in ("unread", "queued", "delivered", "acknowledged"):
        result["steering"][key] = _integer(
            steering[key],
            f"{path}.steering.{key}",
            minimum=0,
            maximum=MAX_SAFE_INTEGER,
        )
    result["steering"]["audit_refs"] = [
        _bounded_id(item, f"{path}.steering.audit_refs[{index}]")
        for index, item in enumerate(
            _array(steering["audit_refs"], f"{path}.steering.audit_refs")
        )
    ]
    result["evidence"] = {
        "basis": _enum(evidence["basis"], EVIDENCE_BASES, f"{path}.evidence.basis"),
        "sources": [
            _enum(
                source,
                SOURCE_HEALTH_SOURCES,
                f"{path}.evidence.sources[{index}]",
            )
            for index, source in enumerate(
                _array(evidence["sources"], f"{path}.evidence.sources")
            )
        ],
        "reason_codes": [
            _safe_code(code, f"{path}.evidence.reason_codes[{index}]")
            for index, code in enumerate(
                _array(evidence["reason_codes"], f"{path}.evidence.reason_codes")
            )
        ],
        "coverage": _enum(
            evidence["coverage"], COVERAGE_STATES, f"{path}.evidence.coverage"
        ),
    }
    if "lifecycle_source" in row:
        result["lifecycle_source"] = _enum(
            row["lifecycle_source"],
            ("session_events", "runtime", "unavailable"),
            f"{path}.lifecycle_source",
        )
    if "lifecycle_observed_at" in row:
        result["lifecycle_observed_at"] = (
            None
            if row["lifecycle_observed_at"] is None
            else _integer(
                row["lifecycle_observed_at"],
                f"{path}.lifecycle_observed_at",
                minimum=0,
                maximum=MAX_SAFE_INTEGER,
            )
        )
    if "lifecycle_reason" in row:
        result["lifecycle_reason"] = _enum(
            row["lifecycle_reason"],
            CONTROL_SAFE_REASONS,
            f"{path}.lifecycle_reason",
        )
    if "owned_stages" in row:
        result["owned_stages"] = [
            _enum(stage, STAGES, f"{path}.owned_stages[{index}]")
            for index, stage in enumerate(
                _array(row["owned_stages"], f"{path}.owned_stages")
            )
        ]
    if "stage_ownership" in row:
        result["stage_ownership"] = _enum(
            row["stage_ownership"],
            ("recorded", "unavailable"),
            f"{path}.stage_ownership",
        )
    if "source_coverage" in row:
        result["source_coverage"] = [
            _control_source_coverage(item, f"{path}.source_coverage[{index}]")
            for index, item in enumerate(
                _array(row["source_coverage"], f"{path}.source_coverage")
            )
        ]
    if "recent_events" in row:
        events = _array(row["recent_events"], f"{path}.recent_events")
        if len(events) > 3:
            _error(f"{path}.recent_events", "must contain at most 3 events")
        result["recent_events"] = [
            _control_operational_event(item, f"{path}.recent_events[{index}]")
            for index, item in enumerate(events)
        ]
    if "events_status" in row:
        result["events_status"] = _enum(
            row["events_status"],
            CONTROL_SOURCE_STATUSES,
            f"{path}.events_status",
        )
    if "event_source_registered" in row:
        result["event_source_registered"] = _boolean(
            row["event_source_registered"],
            f"{path}.event_source_registered",
        )
    if "blocker_details" in row:
        result["blocker_details"] = []
        for index, raw in enumerate(
            _array(row["blocker_details"], f"{path}.blocker_details")
        ):
            item_path = f"{path}.blocker_details[{index}]"
            item = _strict_object(
                raw, item_path, ("kind", "waiting_on_user")
            )
            result["blocker_details"].append(
                {
                    "kind": _enum(
                        item["kind"],
                        (
                            "permission_pending",
                            "gate_closed",
                            "defect_open",
                            "amendment_open",
                            "requires_replan",
                            "binding_feedback",
                        ),
                        f"{item_path}.kind",
                    ),
                    "waiting_on_user": _boolean(
                        item["waiting_on_user"],
                        f"{item_path}.waiting_on_user",
                    ),
                }
            )
    return result


def validate_dashboard_snapshot(value: Any, *, path: str = "$") -> dict:
    snapshot = _strict_object(
        value,
        path,
        (
            "schema_version",
            "snapshot_id",
            "plan_id",
            "sampled_at",
            "observed_through",
            "poll_after_ms",
            "cursor",
            "source_health",
            "coverage",
            "agents",
            "next_cursor",
        ),
        ("source_coverage", "recent_events", "events_status"),
    )
    _schema_version(snapshot["schema_version"], f"{path}.schema_version")
    agents = _array(snapshot["agents"], f"{path}.agents")
    if len(agents) > 200:
        _error(f"{path}.agents", "must contain at most 200 agents")
    result = {
        "schema_version": 1,
        "snapshot_id": _bounded_id(snapshot["snapshot_id"], f"{path}.snapshot_id"),
        "plan_id": _string(snapshot["plan_id"], f"{path}.plan_id", pattern=_PLAN_ID),
        "sampled_at": _timestamp(snapshot["sampled_at"], f"{path}.sampled_at"),
        "observed_through": (
            None
            if snapshot["observed_through"] is None
            else _timestamp(snapshot["observed_through"], f"{path}.observed_through")
        ),
        "poll_after_ms": _integer(
            snapshot["poll_after_ms"],
            f"{path}.poll_after_ms",
            minimum=1,
            maximum=MAX_SAFE_INTEGER,
        ),
        "cursor": _plain_text(snapshot["cursor"], f"{path}.cursor", maximum=2048),
        "source_health": [
            _source_health(item, f"{path}.source_health[{index}]")
            for index, item in enumerate(
                _array(snapshot["source_health"], f"{path}.source_health")
            )
        ],
        "coverage": _coverage(snapshot["coverage"], f"{path}.coverage"),
        "agents": [
            _agent_row(item, f"{path}.agents[{index}]")
            for index, item in enumerate(agents)
        ],
        "next_cursor": (
            None
            if snapshot["next_cursor"] is None
            else _plain_text(
                snapshot["next_cursor"], f"{path}.next_cursor", maximum=2048
            )
        ),
    }
    if "source_coverage" in snapshot:
        result["source_coverage"] = [
            _control_source_coverage(item, f"{path}.source_coverage[{index}]")
            for index, item in enumerate(
                _array(snapshot["source_coverage"], f"{path}.source_coverage")
            )
        ]
    if "recent_events" in snapshot:
        events = _array(snapshot["recent_events"], f"{path}.recent_events")
        if len(events) > 50:
            _error(f"{path}.recent_events", "must contain at most 50 events")
        result["recent_events"] = [
            _control_operational_event(item, f"{path}.recent_events[{index}]")
            for index, item in enumerate(events)
        ]
    if "events_status" in snapshot:
        result["events_status"] = _enum(
            snapshot["events_status"],
            CONTROL_SOURCE_STATUSES,
            f"{path}.events_status",
        )
    return result


def validate_event_page(value: Any, *, path: str = "$") -> dict:
    page = _strict_object(
        value,
        path,
        (
            "schema_version",
            "agent_key",
            "events",
            "next_cursor",
            "gap",
            "truncated",
        ),
    )
    _schema_version(page["schema_version"], f"{path}.schema_version")
    events = _array(page["events"], f"{path}.events")
    if len(events) > 100:
        _error(f"{path}.events", "must contain at most 100 events")
    normalized_events = []
    for index, raw in enumerate(events):
        item_path = f"{path}.events[{index}]"
        event = _strict_object(
            raw,
            item_path,
            (
                "id",
                "sequence",
                "occurred_at",
                "collected_at",
                "source",
                "basis",
                "kind",
                "action",
                "refs",
                "safe_code",
                "summary",
            ),
        )
        normalized_events.append(
            {
                "id": _bounded_id(event["id"], f"{item_path}.id"),
                "sequence": _integer(
                    event["sequence"],
                    f"{item_path}.sequence",
                    minimum=0,
                    maximum=MAX_SAFE_INTEGER,
                ),
                "occurred_at": _timestamp(
                    event["occurred_at"], f"{item_path}.occurred_at"
                ),
                "collected_at": _timestamp(
                    event["collected_at"], f"{item_path}.collected_at"
                ),
                "source": _enum(
                    event["source"], SOURCE_HEALTH_SOURCES, f"{item_path}.source"
                ),
                "basis": _enum(event["basis"], EVENT_BASES, f"{item_path}.basis"),
                "kind": _safe_code(event["kind"], f"{item_path}.kind"),
                "action": (
                    None
                    if event["action"] is None
                    else validate_action(event["action"], path=f"{item_path}.action")
                ),
                "refs": _object_refs(event["refs"], f"{item_path}.refs"),
                "safe_code": (
                    None
                    if event["safe_code"] is None
                    else _safe_code(event["safe_code"], f"{item_path}.safe_code")
                ),
                "summary": (
                    None
                    if event["summary"] is None
                    else validate_model_summary(
                        event["summary"], path=f"{item_path}.summary"
                    )
                ),
            }
        )
    return {
        "schema_version": 1,
        "agent_key": _bounded_id(page["agent_key"], f"{path}.agent_key"),
        "events": normalized_events,
        "next_cursor": (
            None
            if page["next_cursor"] is None
            else _plain_text(
                page["next_cursor"], f"{path}.next_cursor", maximum=2048
            )
        ),
        "gap": _boolean(page["gap"], f"{path}.gap"),
        "truncated": _boolean(page["truncated"], f"{path}.truncated"),
    }


def validate_feedback_request(value: Any, *, path: str = "$") -> dict:
    request = _strict_object(
        value,
        path,
        (
            "schema_version",
            "idempotency_key",
            "scope",
            "target_agent_key",
            "target_role",
            "expected_plan_revision",
            "refs",
            "body",
            "requires_replan",
        ),
    )
    _schema_version(request["schema_version"], f"{path}.schema_version")
    scope = _enum(request["scope"], ("agent", "role", "plan"), f"{path}.scope")
    target_agent = _bounded_id(
        request["target_agent_key"], f"{path}.target_agent_key", nullable=True
    )
    target_role = (
        None
        if request["target_role"] is None
        else _enum(request["target_role"], ROLES, f"{path}.target_role")
    )
    if scope == "agent" and target_agent is None:
        _error(f"{path}.target_agent_key", "is required for agent scope")
    if scope == "agent" and target_role is not None:
        _error(f"{path}.target_role", "must be null for agent scope")
    if scope == "role" and target_role is None:
        _error(f"{path}.target_role", "is required for role scope")
    if scope == "role" and target_agent is not None:
        _error(f"{path}.target_agent_key", "must be null for role scope")
    if scope == "plan" and (target_agent is not None or target_role is not None):
        _error(path, "plan scope cannot name an agent or role target")
    return {
        "schema_version": 1,
        "idempotency_key": _bounded_id(
            request["idempotency_key"], f"{path}.idempotency_key"
        ),
        "scope": scope,
        "target_agent_key": target_agent,
        "target_role": target_role,
        "expected_plan_revision": (
            None
            if request["expected_plan_revision"] is None
            else _revision(
                request["expected_plan_revision"], f"{path}.expected_plan_revision"
            )
        ),
        "refs": _object_refs(request["refs"], f"{path}.refs"),
        "body": _string(request["body"], f"{path}.body", nonempty=True, maximum=8000),
        "requires_replan": _boolean(
            request["requires_replan"], f"{path}.requires_replan"
        ),
    }


def validate_feedback_receipt(value: Any, *, path: str = "$") -> dict:
    receipt = _strict_object(
        value,
        path,
        (
            "feedback_id",
            "plan_id",
            "note_seq",
            "scope",
            "authorized_targets",
            "created_at",
            "status",
            "per_target",
            "requires_replan",
            "gate_effect",
            "retryable",
            "reason_code",
        ),
        ("receipt_key", "delivery_state", "consequence"),
    )
    targets = [
        _bounded_id(item, f"{path}.authorized_targets[{index}]")
        for index, item in enumerate(
            _array(receipt["authorized_targets"], f"{path}.authorized_targets")
        )
    ]
    per_target = []
    for index, raw in enumerate(_array(receipt["per_target"], f"{path}.per_target")):
        item_path = f"{path}.per_target[{index}]"
        item = _strict_object(
            raw,
            item_path,
            ("agent_key", "delivered_at", "acked_at", "delivery_source", "ack_source"),
        )
        per_target.append(
            {
                "agent_key": _bounded_id(item["agent_key"], f"{item_path}.agent_key"),
                "delivered_at": (
                    None
                    if item["delivered_at"] is None
                    else _timestamp(item["delivered_at"], f"{item_path}.delivered_at")
                ),
                "acked_at": (
                    None
                    if item["acked_at"] is None
                    else _timestamp(item["acked_at"], f"{item_path}.acked_at")
                ),
                "delivery_source": (
                    None
                    if item["delivery_source"] is None
                    else _enum(
                        item["delivery_source"],
                        ("cli_banner", "supervisor_relay"),
                        f"{item_path}.delivery_source",
                    )
                ),
                "ack_source": (
                    None
                    if item["ack_source"] is None
                    else _enum(
                        item["ack_source"],
                        ("harness_receipt", "explicit_agent"),
                        f"{item_path}.ack_source",
                    )
                ),
            }
        )
    result = {
        "feedback_id": _bounded_id(receipt["feedback_id"], f"{path}.feedback_id"),
        "plan_id": _string(receipt["plan_id"], f"{path}.plan_id", pattern=_PLAN_ID),
        "note_seq": _integer(
            receipt["note_seq"], f"{path}.note_seq", minimum=1, maximum=MAX_SAFE_INTEGER
        ),
        "scope": _enum(receipt["scope"], ("agent", "role", "plan"), f"{path}.scope"),
        "authorized_targets": targets,
        "created_at": _timestamp(receipt["created_at"], f"{path}.created_at"),
        "status": _enum(
            receipt["status"],
            ("queued", "delivered", "acknowledged", "folded", "retracted"),
            f"{path}.status",
        ),
        "per_target": per_target,
        "requires_replan": _boolean(
            receipt["requires_replan"], f"{path}.requires_replan"
        ),
        "gate_effect": _enum(
            receipt["gate_effect"],
            ("none", "plan_needs_review"),
            f"{path}.gate_effect",
        ),
        "retryable": _boolean(receipt["retryable"], f"{path}.retryable"),
        "reason_code": (
            None
            if receipt["reason_code"] is None
            else _safe_code(receipt["reason_code"], f"{path}.reason_code")
        ),
    }
    if "receipt_key" in receipt:
        result["receipt_key"] = _bounded_id(
            receipt["receipt_key"], f"{path}.receipt_key"
        )
    if "delivery_state" in receipt:
        result["delivery_state"] = _enum(
            receipt["delivery_state"],
            (
                "sent",
                "routed",
                "delivered",
                "acknowledged",
                "undeliverable",
                "withdrawn",
            ),
            f"{path}.delivery_state",
        )
    if "consequence" in receipt:
        result["consequence"] = _control_consequence(
            receipt["consequence"], f"{path}.consequence"
        )
    return result


def validate_dashboard_payload(value: Any, kind: str) -> dict:
    """Validate a named dashboard wire object."""
    validators = {
        "DashboardSnapshot": validate_dashboard_snapshot,
        "EventPage": validate_event_page,
        "FeedbackRequest": validate_feedback_request,
        "FeedbackReceipt": validate_feedback_receipt,
        "ObjectRef": validate_object_ref,
        "Action": validate_action,
        "ModelSummary": validate_model_summary,
    }
    try:
        validator = validators[kind]
    except KeyError as error:
        raise ValueError(f"unknown dashboard payload kind {kind!r}") from error
    return validator(copy.deepcopy(value))


def load_document(source: str | bytes | bytearray) -> dict:
    """Parse canonical-profile JSON with duplicate-key rejection, then validate."""
    return validate_document(canon.loads(source))


def load_revision(source: str | bytes | bytearray) -> dict:
    """Parse and validate an immutable revision envelope."""
    return validate_revision(canon.loads(source))


def load_dashboard_payload(
    source: str | bytes | bytearray,
    kind: str,
) -> dict:
    """Parse a dashboard DTO without ever accepting duplicate keys."""
    return validate_dashboard_payload(canon.loads(source), kind)


validate = validate_document
