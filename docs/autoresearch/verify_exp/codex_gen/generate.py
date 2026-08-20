"""Self-contained draw.io MCP diagram generator."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NodeSpec:
    label: str
    icon: str


@dataclass(frozen=True)
class EdgeSpec:
    source: str
    target: str


@dataclass(frozen=True)
class GroupSpec:
    label: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class DiagramSpec:
    stem: str
    title: str
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...]
    groups: tuple[GroupSpec, ...]
    annotations: tuple[str, ...]
    body: str


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_prompt(text: str, stem: str) -> DiagramSpec:
    """Parse the fixed frontmatter and reject malformed references."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing frontmatter")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError("unterminated frontmatter") from exc

    scalar: dict[str, str] = {}
    lists: dict[str, list[str]] = {
        "nodes": [],
        "edges": [],
        "groups": [],
        "annotations": [],
    }
    current: str | None = None
    for raw in lines[1:end]:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            if current is None:
                raise ValueError("list item without section")
            lists[current].append(_unquote(stripped[2:]))
            continue
        if ":" not in stripped:
            raise ValueError(f"malformed frontmatter line: {stripped}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        if key in lists:
            current = key
        else:
            current = None
            scalar[key] = _unquote(value)

    title = scalar.get("title", "").strip()
    if not title:
        raise ValueError("empty title")

    nodes: list[NodeSpec] = []
    for item in lists["nodes"]:
        if "|" not in item:
            raise ValueError(f"malformed node: {item}")
        label, icon = (part.strip() for part in item.split("|", 1))
        if not label or not icon:
            raise ValueError(f"malformed node: {item}")
        nodes.append(NodeSpec(label, icon))
    names = [node.label for node in nodes]
    if len(set(names)) != len(names):
        raise ValueError("duplicate node label")
    known = set(names)

    edges: list[EdgeSpec] = []
    for item in lists["edges"]:
        if "->" not in item:
            raise ValueError(f"malformed edge: {item}")
        source, target = (part.strip() for part in item.split("->", 1))
        if source not in known or target not in known:
            raise ValueError(f"edge references unknown node: {item}")
        edges.append(EdgeSpec(source, target))

    groups: list[GroupSpec] = []
    for item in lists["groups"]:
        if ":" not in item:
            raise ValueError(f"malformed group: {item}")
        label, raw_members = (part.strip() for part in item.split(":", 1))
        members = tuple(part.strip() for part in raw_members.split(",") if part.strip())
        unknown = set(members) - known
        if not label or not members:
            raise ValueError(f"malformed group: {item}")
        if unknown:
            raise ValueError(f"group references unknown node: {sorted(unknown)}")
        groups.append(GroupSpec(label, members))

    body = "\n".join(lines[end + 1 :]).strip()
    return DiagramSpec(
        stem=stem,
        title=title,
        nodes=tuple(nodes),
        edges=tuple(edges),
        groups=tuple(groups),
        annotations=tuple(lists["annotations"]),
        body=body,
    )
