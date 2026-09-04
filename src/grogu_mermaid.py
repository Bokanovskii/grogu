"""Conservative Mermaid flowchart parser for semantic review anchors."""

from __future__ import annotations

import re

FLOWCHART_TYPES = ("flowchart", "graph")

_FLOWCHART_HEADER = re.compile(r"^\s*(flowchart|graph)\s+(TB|TD|BT|RL|LR)\b")
_DIAGRAM_HEADER = re.compile(
    r"^\s*(sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|erDiagram|"
    r"journey|gantt|pie|gitGraph|mindmap|timeline|quadrantChart|"
    r"requirementDiagram|C4Context|C4Container|C4Component|C4Dynamic|"
    r"C4Deployment|xychart-beta|block-beta|packet-beta|kanban|architecture-beta)\b"
)
_SKIP = re.compile(
    r"^\s*(?:classDef|class|style|linkStyle|click|direction|accTitle|accDescr)\b"
)
_SUBGRAPH = re.compile(
    r'^\s*subgraph\s+([A-Za-z_][\w.-]*)(?:\s+\[(.*?)\]|\s+(.+?))?\s*;?\s*$'
)
_END = re.compile(r"^\s*end\s*;?\s*$")
_IDENTIFIER = re.compile(r"[A-Za-z_][\w.-]*")
_EDGE = re.compile(
    r"""
    (?:
      --\s+(?P<middle>[^|\n]+?)\s+(?P<middle_op>-->|---|--x|--o)
      |
      (?P<op>-\.\->|-\.-|-->|---|==>|===|--x|--o)
      (?:\|(?P<pipe>[^|\n]*)\|)?
    )
    """,
    re.VERBOSE,
)

_SHAPES = (
    ("subroutine", "[[", "]]"),
    ("cylinder", "[(", ")]"),
    ("stadium", "([", "])"),
    ("circle", "((", "))"),
    ("round", "(", ")"),
    ("diamond", "{", "}"),
    ("asymmetric", ">", "]"),
    ("rectangle", "[", "]"),
)


def _strip_label(label: str) -> str:
    value = label.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _node(expression: str) -> tuple | None:
    text = expression.strip().rstrip(";").strip()
    match = _IDENTIFIER.match(text)
    if not match:
        return None
    node_id = match.group(0)
    remainder = text[match.end() :].strip()
    if not remainder:
        return node_id, node_id, "bare"
    for shape, opening, closing in _SHAPES:
        if remainder.startswith(opening) and remainder.endswith(closing):
            label = remainder[len(opening) : len(remainder) - len(closing)]
            return node_id, _strip_label(label), shape
    return None


def _edge_kind(operator: str) -> str:
    if operator in {"---", "-.-", "==="}:
        return "line"
    if operator == "--x":
        return "cross"
    if operator == "--o":
        return "circle"
    return "arrow"


def _clean_lines(source: str) -> list:
    cleaned = []
    in_directive = False
    for number, original in enumerate(source.splitlines(), 1):
        text = original
        if in_directive:
            if "}%%" in text:
                text = text.split("}%%", 1)[1]
                in_directive = False
            else:
                cleaned.append((number, "", True))
                continue
        if "%%{" in text:
            before, after = text.split("%%{", 1)
            if "}%%" in after:
                text = before + after.split("}%%", 1)[1]
            else:
                text = before
                in_directive = True
        if "%%" in text:
            text = text.split("%%", 1)[0]
        cleaned.append((number, text, False))
    return cleaned


def parse(source: str) -> dict:
    """Return a bounded semantic description; arbitrary input never raises."""
    result = {
        "type": "",
        "supported": False,
        "partial": False,
        "nodes": [],
        "edges": [],
        "subgraphs": [],
        "lines": len(source.splitlines()) if isinstance(source, str) else 0,
        "unparsed": 0,
    }
    if not isinstance(source, str):
        return result

    try:
        lines = _clean_lines(source)
        first = next((text for _, text, _ in lines if text.strip()), "")
        flowchart = _FLOWCHART_HEADER.match(first)
        if flowchart:
            result["type"] = flowchart.group(1)
            result["supported"] = True
        else:
            diagram = _DIAGRAM_HEADER.match(first)
            if diagram:
                result["type"] = diagram.group(1)
            return result

        nodes_by_id = {}
        pair_counts = {}
        subgraph_stack = []
        considered = 0

        def add_node(expression: str, line_number: int) -> dict | None:
            parsed = _node(expression)
            if parsed is None:
                return None
            node_id, label, shape = parsed
            existing = nodes_by_id.get(node_id)
            if existing is None:
                existing = {
                    "id": node_id,
                    "label": label,
                    "shape": shape,
                    "subgraph": subgraph_stack[-1] if subgraph_stack else "",
                    "line": line_number,
                }
                nodes_by_id[node_id] = existing
                result["nodes"].append(existing)
            elif shape != "bare" and existing["shape"] == "bare":
                existing["label"] = label
                existing["shape"] = shape
                if subgraph_stack and not existing["subgraph"]:
                    existing["subgraph"] = subgraph_stack[-1]
            return existing

        for line_number, raw, directive_only in lines:
            text = raw.strip()
            if not text or directive_only:
                continue
            considered += 1
            if _FLOWCHART_HEADER.match(text):
                continue
            if _SKIP.match(text):
                continue
            subgraph = _SUBGRAPH.match(text)
            if subgraph:
                subgraph_id = subgraph.group(1)
                title = _strip_label(subgraph.group(2) or subgraph.group(3) or subgraph_id)
                result["subgraphs"].append(
                    {"id": subgraph_id, "title": title, "line": line_number}
                )
                subgraph_stack.append(subgraph_id)
                continue
            if _END.match(text):
                if subgraph_stack:
                    subgraph_stack.pop()
                continue

            matches = list(_EDGE.finditer(text))
            if matches:
                expressions = []
                cursor = 0
                valid = True
                for edge_match in matches:
                    expressions.append(text[cursor : edge_match.start()])
                    cursor = edge_match.end()
                expressions.append(text[cursor:])
                parsed_nodes = [add_node(value, line_number) for value in expressions]
                if any(node is None for node in parsed_nodes):
                    valid = False
                if valid:
                    for offset, edge_match in enumerate(matches):
                        source_node = parsed_nodes[offset]
                        target_node = parsed_nodes[offset + 1]
                        operator = edge_match.group("middle_op") or edge_match.group("op")
                        label = _strip_label(
                            edge_match.group("middle") or edge_match.group("pipe") or ""
                        )
                        pair = (source_node["id"], target_node["id"])
                        ordinal = pair_counts.get(pair, 0)
                        pair_counts[pair] = ordinal + 1
                        result["edges"].append(
                            {
                                "from": source_node["id"],
                                "to": target_node["id"],
                                "label": label,
                                "kind": _edge_kind(operator),
                                "edge_index": len(result["edges"]),
                                "pair_ordinal": ordinal,
                                "line": line_number,
                            }
                        )
                    continue

            if add_node(text, line_number) is None:
                result["unparsed"] += 1

        result["partial"] = considered > 0 and result["unparsed"] / considered > 0.20
    except Exception:
        # A diagram is user-authored plan text. A malformed construct degrades
        # semantic anchoring; it must not prevent the rest of the plan loading.
        result["partial"] = True
        result["unparsed"] += 1
    return result


def find_node(parsed: dict, node_id: str) -> dict | None:
    """Find a parsed node by its semantic id."""
    for node in parsed.get("nodes", []):
        if node.get("id") == node_id:
            return node
    return None


def find_edge(
    parsed: dict,
    source_id: str,
    target_id: str,
    pair_ordinal: int = 0,
) -> dict | None:
    """Find an edge by endpoints and its ordinal among duplicate pairs."""
    for edge in parsed.get("edges", []):
        if (
            edge.get("from") == source_id
            and edge.get("to") == target_id
            and int(edge.get("pair_ordinal", 0)) == int(pair_ordinal)
        ):
            return edge
    return None
