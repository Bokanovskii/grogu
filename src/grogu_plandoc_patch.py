"""Immutable JSON Patch operations for typed plan documents."""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import grogu_plandoc_canon as canon
import grogu_plandoc_schema as schema


class PatchError(ValueError):
    """A patch cannot be applied atomically."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_patch",
        status: int = 422,
        path: str = "",
        current_revision: str = "",
    ):
        self.code = code
        self.status = status
        self.path = path
        self.current_revision = current_revision
        detail = f"{path}: {message}" if path else message
        super().__init__(detail)


class StaleRevision(PatchError):
    """The caller based its write on an older HEAD."""

    def __init__(self, base: str, current: str):
        super().__init__(
            f"base {base!r} does not match current revision {current!r}",
            code="stale_revision",
            status=409,
            current_revision=current,
        )
        self.base = base
        self.current = current


class ForbiddenPatch(PatchError):
    """A patch touches a stage the caller may not read."""

    def __init__(self, path: str):
        super().__init__(
            "patch targets a stage outside the caller's readable partition",
            code="forbidden_stage",
            status=403,
            path=path,
        )


def _pointer_parts(pointer: str) -> list[str]:
    if not isinstance(pointer, str):
        raise PatchError("JSON Pointer must be a string", status=400)
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise PatchError("JSON Pointer must be empty or start with '/'", status=400)
    values = []
    for raw in pointer[1:].split("/"):
        index = 0
        decoded = []
        while index < len(raw):
            if raw[index] != "~":
                decoded.append(raw[index])
                index += 1
                continue
            if index + 1 >= len(raw) or raw[index + 1] not in "01":
                raise PatchError(
                    f"invalid JSON Pointer escape in {pointer!r}", status=400
                )
            decoded.append("~" if raw[index + 1] == "0" else "/")
            index += 2
        values.append("".join(decoded))
    return values


def _array_index(segment: str, length: int, *, add: bool = False) -> int:
    if segment == "-" and add:
        return length
    if (
        not segment
        or (len(segment) > 1 and segment.startswith("0"))
        or not segment.isascii()
    ):
        raise PatchError(f"invalid array index {segment!r}")
    if not segment.isdigit():
        raise PatchError(f"invalid array index {segment!r}")
    index = int(segment)
    maximum = length if add else length - 1
    if index < 0 or index > maximum:
        raise PatchError(f"array index {index} is outside 0..{maximum}")
    return index


def _resolve(document: Any, parts: list[str], *, pointer: str) -> Any:
    current = document
    for segment in parts:
        if isinstance(current, Mapping):
            if segment not in current:
                raise PatchError("path does not exist", path=pointer)
            current = current[segment]
        elif isinstance(current, list):
            current = current[_array_index(segment, len(current))]
        else:
            raise PatchError("path traverses a scalar value", path=pointer)
    return current


def _parent(document: Any, parts: list[str], *, pointer: str) -> tuple[Any, str]:
    if not parts:
        return None, ""
    return _resolve(document, parts[:-1], pointer=pointer), parts[-1]


def _canonical_equal(left: Any, right: Any) -> bool:
    try:
        return canon.dumpb(left) == canon.dumpb(right)
    except canon.CanonicalError:
        return False


def _add(document: Any, pointer: str, value: Any) -> Any:
    parts = _pointer_parts(pointer)
    isolated = copy.deepcopy(value)
    if not parts:
        return isolated
    parent, segment = _parent(document, parts, pointer=pointer)
    if isinstance(parent, dict):
        parent[segment] = isolated
    elif isinstance(parent, list):
        parent.insert(_array_index(segment, len(parent), add=True), isolated)
    else:
        raise PatchError("add target parent is a scalar", path=pointer)
    return document


def _remove(document: Any, pointer: str) -> tuple[Any, Any]:
    parts = _pointer_parts(pointer)
    if not parts:
        raise PatchError("removing the entire document is not allowed", path=pointer)
    parent, segment = _parent(document, parts, pointer=pointer)
    if isinstance(parent, dict):
        if segment not in parent:
            raise PatchError("remove target does not exist", path=pointer)
        return document, parent.pop(segment)
    if isinstance(parent, list):
        return document, parent.pop(_array_index(segment, len(parent)))
    raise PatchError("remove target parent is a scalar", path=pointer)


def _replace(document: Any, pointer: str, value: Any) -> Any:
    parts = _pointer_parts(pointer)
    if not parts:
        return copy.deepcopy(value)
    parent, segment = _parent(document, parts, pointer=pointer)
    isolated = copy.deepcopy(value)
    if isinstance(parent, dict):
        if segment not in parent:
            raise PatchError("replace target does not exist", path=pointer)
        parent[segment] = isolated
    elif isinstance(parent, list):
        parent[_array_index(segment, len(parent))] = isolated
    else:
        raise PatchError("replace target parent is a scalar", path=pointer)
    return document


def _node_stage(document: Mapping[str, Any], node_id: str) -> str | None:
    node = (document.get("nodes") or {}).get(node_id)
    return str(node.get("stage", "")) if isinstance(node, Mapping) else None


def _edge_stages(document: Mapping[str, Any], edge: Mapping[str, Any]) -> set[str]:
    stages = {
        stage
        for endpoint in (edge.get("from"), edge.get("to"))
        if (stage := _node_stage(document, str(endpoint))) is not None
    }
    return stages


def _value_stages(value: Any) -> set[str]:
    if not isinstance(value, Mapping):
        return set()
    if "stage" in value and isinstance(value["stage"], str):
        return {value["stage"]}
    stages = set()
    for item in value.values():
        stages.update(_value_stages(item))
    return stages


def _affected_stages(
    document: Mapping[str, Any],
    pointer: str,
    *,
    value: Any = None,
) -> set[str]:
    parts = _pointer_parts(pointer)
    if not parts:
        return {
            str(node.get("stage", ""))
            for node in (document.get("nodes") or {}).values()
            if isinstance(node, Mapping)
        } | _value_stages(value)
    if parts[0] == "nodes":
        if len(parts) == 1:
            return {
                str(node.get("stage", ""))
                for node in (document.get("nodes") or {}).values()
                if isinstance(node, Mapping)
            } | _value_stages(value)
        existing = _node_stage(document, parts[1])
        stages = {existing} if existing is not None else set()
        if len(parts) >= 3 and parts[2] == "stage" and isinstance(value, str):
            stages.add(value)
        stages.update(_value_stages(value))
        return stages
    if parts[0] == "edges":
        if len(parts) == 1:
            stages = set()
            for edge in (document.get("edges") or {}).values():
                if isinstance(edge, Mapping):
                    stages.update(_edge_stages(document, edge))
            if isinstance(value, Mapping):
                for edge in value.values():
                    if isinstance(edge, Mapping):
                        stages.update(_edge_stages(document, edge))
            return stages
        edge = (document.get("edges") or {}).get(parts[1])
        stages = _edge_stages(document, edge) if isinstance(edge, Mapping) else set()
        if isinstance(value, Mapping):
            stages.update(_edge_stages(document, value))
        return stages
    return {""}


def assert_patch_authorized(
    document: Mapping[str, Any],
    operations: Iterable[Mapping[str, Any]],
    readable_stages: Iterable[str],
    *,
    authorized_new_ids: Iterable[str] = (),
    authorized_remove_ids: Iterable[str] = (),
    after: bool = False,
) -> None:
    """Refuse read or write operations that name an unreadable stage."""
    allowed = set(readable_stages) | {""}
    reserved = set(authorized_new_ids)
    removable = set(authorized_remove_ids)
    visible_nodes = {
        node_id
        for node_id, node in (document.get("nodes") or {}).items()
        if isinstance(node, Mapping) and node.get("stage", "") in allowed
    }
    visible_edges = {
        edge_id
        for edge_id, edge in (document.get("edges") or {}).items()
        if isinstance(edge, Mapping)
        and edge.get("from") in visible_nodes
        and edge.get("to") in visible_nodes
    }

    def check_selector(node: Mapping[str, Any], pointer: str) -> None:
        attrs = node.get("attrs")
        if not isinstance(attrs, Mapping) or not isinstance(
            attrs.get("selector"), Mapping
        ):
            return
        try:
            selector = schema.validate_selector(attrs["selector"])
        except schema.SchemaError:
            return
        if set(schema.selector_node_ids(selector)) - visible_nodes:
            raise ForbiddenPatch(pointer)
        if set(schema.selector_edge_ids(selector)) - visible_edges:
            raise ForbiddenPatch(pointer)

    for operation in operations:
        path_parts = _pointer_parts(str(operation.get("path", "")))
        value = operation.get("value")
        removal_parts = None
        if operation.get("op") == "remove":
            removal_parts = path_parts
        elif operation.get("op") == "move":
            removal_parts = _pointer_parts(str(operation.get("from", "")))
        if (
            removal_parts
            and removal_parts[0] == "nodes"
            and len(removal_parts) <= 2
        ):
            if len(removal_parts) != 2 or removal_parts[1] not in removable:
                raise ForbiddenPatch(
                    str(
                        operation.get("from")
                        if operation.get("op") == "move"
                        else operation.get("path")
                    )
                )
            node_id = removal_parts[1]
            if any(
                edge_id not in visible_edges
                and node_id in {edge.get("from"), edge.get("to")}
                for edge_id, edge in (document.get("edges") or {}).items()
                if isinstance(edge, Mapping)
            ):
                raise ForbiddenPatch(
                    str(
                        operation.get("from")
                        if operation.get("op") == "move"
                        else operation.get("path")
                    )
                )
        for pointer_key in ("path", "from"):
            if pointer_key not in operation:
                continue
            if after and (
                pointer_key == "from"
                or (
                    pointer_key == "path"
                    and operation.get("op") == "remove"
                )
            ):
                continue
            pointer = str(operation[pointer_key])
            pointer_parts = _pointer_parts(pointer)
            if not pointer_parts or (
                len(pointer_parts) == 1
                and pointer_parts[0] in {"nodes", "edges"}
            ):
                raise ForbiddenPatch(pointer)
            if len(pointer_parts) < 2 or pointer_parts[0] not in {
                "nodes",
                "edges",
            }:
                continue
            identifier = pointer_parts[1]
            visible = (
                visible_nodes if pointer_parts[0] == "nodes" else visible_edges
            )
            all_values = (
                document.get("nodes") or {}
                if pointer_parts[0] == "nodes"
                else document.get("edges") or {}
            )
            adding_reserved = (
                pointer_key == "path"
                and operation.get("op") == "add"
                and len(pointer_parts) == 2
                and identifier in reserved
                and identifier not in all_values
            )
            if identifier not in visible and not adding_reserved:
                raise ForbiddenPatch(pointer)
        if (
            len(path_parts) >= 3
            and path_parts[0] == "edges"
            and path_parts[2] in {"from", "to"}
            and isinstance(value, str)
        ):
            target_stage = _node_stage(document, value)
            if target_stage is None or target_stage not in allowed:
                raise ForbiddenPatch(str(operation["path"]))
        if (
            len(path_parts) == 2
            and path_parts[0] == "edges"
            and isinstance(value, Mapping)
        ):
            for endpoint in ("from", "to"):
                target_stage = _node_stage(document, str(value.get(endpoint, "")))
                if target_stage is None or target_stage not in allowed:
                    raise ForbiddenPatch(str(operation["path"]))
        if not path_parts:
            for node in (document.get("nodes") or {}).values():
                if isinstance(node, Mapping):
                    check_selector(node, str(operation["path"]))
        elif path_parts[0] == "nodes":
            if len(path_parts) == 1:
                for node in (document.get("nodes") or {}).values():
                    if isinstance(node, Mapping):
                        check_selector(node, str(operation["path"]))
            elif len(path_parts) >= 2:
                node = (document.get("nodes") or {}).get(path_parts[1])
                if isinstance(node, Mapping):
                    check_selector(node, str(operation["path"]))
        for key in ("path", "from"):
            if key not in operation:
                continue
            pointer = str(operation[key])
            stages = _affected_stages(
                document,
                pointer,
                value=operation.get("value") if key == "path" else None,
            )
            if stages - allowed:
                raise ForbiddenPatch(pointer)


def apply_patch(
    document: Any,
    operations: Iterable[Mapping[str, Any]],
    *,
    base: str | None = None,
    current_revision: str | None = None,
    readable_stages: Iterable[str] | None = None,
    authorized_new_ids: Iterable[str] = (),
    authorized_remove_ids: Iterable[str] = (),
    validator: Callable[[Any], Any] | None = schema.validate_document,
) -> Any:
    """Apply all operations to an isolated copy or apply none of them.

    ``document`` and ``operations`` are never mutated.  When supplied, the
    validator runs once against the whole result after every operation has
    succeeded. Restricted callers must pass server-reserved stable ids through
    ``authorized_new_ids`` before an ``add`` can create a node or edge, and
    server-approved node deletions through ``authorized_remove_ids``.
    """
    if base is not None and current_revision is not None and base != current_revision:
        raise StaleRevision(base, current_revision)
    raw_operations = copy.deepcopy(list(operations))
    try:
        normalized_operations = schema.validate_patch_ops(raw_operations)
    except schema.SchemaError as error:
        raise PatchError(
            error.message, code="invalid_patch", status=400, path=error.path
        ) from error

    allowed_stages = None
    if readable_stages is not None:
        if not isinstance(document, Mapping):
            raise PatchError("stage authorization requires a document object")
        allowed_stages = tuple(readable_stages)
    reserved_ids = tuple(authorized_new_ids)
    removable_ids = tuple(authorized_remove_ids)

    result = copy.deepcopy(document)
    try:
        for operation in normalized_operations:
            if allowed_stages is not None:
                assert_patch_authorized(
                    result,
                    [operation],
                    allowed_stages,
                    authorized_new_ids=reserved_ids,
                    authorized_remove_ids=removable_ids,
                )
            op = operation["op"]
            pointer = operation["path"]
            if allowed_stages is not None and op in {"copy", "move"}:
                source = operation["from"]
                effective = copy.deepcopy(
                    _resolve(result, _pointer_parts(source), pointer=source)
                )
                assert_patch_authorized(
                    result,
                    [{**operation, "value": effective}],
                    allowed_stages,
                    authorized_new_ids=reserved_ids,
                    authorized_remove_ids=removable_ids,
                )
            if op == "add":
                result = _add(result, pointer, operation["value"])
            elif op == "remove":
                result, _removed = _remove(result, pointer)
            elif op == "replace":
                result = _replace(result, pointer, operation["value"])
            elif op == "test":
                actual = _resolve(
                    result, _pointer_parts(pointer), pointer=pointer
                )
                if not _canonical_equal(actual, operation["value"]):
                    raise PatchError(
                        "test operation did not match",
                        code="test_failed",
                        status=409,
                        path=pointer,
                    )
            elif op == "copy":
                source = operation["from"]
                copied = copy.deepcopy(
                    _resolve(result, _pointer_parts(source), pointer=source)
                )
                result = _add(result, pointer, copied)
            elif op == "move":
                source = operation["from"]
                source_parts = _pointer_parts(source)
                target_parts = _pointer_parts(pointer)
                if target_parts[: len(source_parts)] == source_parts:
                    raise PatchError(
                        "cannot move a value into its own descendant", path=pointer
                    )
                moved = copy.deepcopy(
                    _resolve(result, source_parts, pointer=source)
                )
                result, _removed = _remove(result, source)
                result = _add(result, pointer, moved)
            if allowed_stages is not None:
                assert_patch_authorized(
                    result,
                    [operation],
                    allowed_stages,
                    authorized_new_ids=reserved_ids,
                    authorized_remove_ids=removable_ids,
                    after=True,
                )
    except PatchError:
        raise
    except (IndexError, KeyError, TypeError, ValueError) as error:
        raise PatchError(str(error)) from error

    if validator is None:
        return result
    try:
        return validator(result)
    except schema.SchemaError as error:
        raise PatchError(
            error.message,
            code="invalid_document",
            status=422,
            path=error.path,
        ) from error


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def diff(before: Any, after: Any, *, path: str = "") -> list[dict]:
    """Return a deterministic patch that transforms *before* into *after*."""
    if _canonical_equal(before, after):
        return []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        operations = []
        before_keys = set(before)
        after_keys = set(after)
        for key in sorted(before_keys - after_keys, key=canon.utf16_key, reverse=True):
            operations.append({"op": "remove", "path": f"{path}/{_escape(key)}"})
        for key in sorted(after_keys - before_keys, key=canon.utf16_key):
            operations.append(
                {
                    "op": "add",
                    "path": f"{path}/{_escape(key)}",
                    "value": copy.deepcopy(after[key]),
                }
            )
        for key in sorted(before_keys & after_keys, key=canon.utf16_key):
            operations.extend(
                diff(before[key], after[key], path=f"{path}/{_escape(key)}")
            )
        return operations
    if isinstance(before, list) and isinstance(after, list):
        return [{"op": "replace", "path": path, "value": copy.deepcopy(after)}]
    return [{"op": "replace", "path": path, "value": copy.deepcopy(after)}]


apply = apply_patch
