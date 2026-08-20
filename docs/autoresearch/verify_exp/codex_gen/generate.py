"""Self-contained draw.io MCP diagram generator."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET


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


LAYOUT_ROWS: dict[str, tuple[tuple[str, ...], ...]] = {
    "01-candidate-to-paper-pipeline": (
        (
            "Candidate Intake",
            "Brainstorm",
            "Ideation",
            "Experiment Plan",
            "Experiment",
            "Evidence",
            "Claims & Outline",
            "Writing",
            "Review",
            "Packaging",
        ),
    ),
    "02-rag-system": (
        ("Corpus", "Encoder", "Vector Index"),
        ("Query", "Retriever", "Reranker", "Generator"),
    ),
    "03-rlhf-pipeline": (
        ("SFT Model", "Sampling", "Reward Model", "Policy Update", "Evaluation"),
        ("Preference Data",),
    ),
    "04-diffusion-llm-training": (
        (
            "Text Tokens",
            "Noising",
            "Denoiser",
            "Left-to-Right Decoder",
            "Reasoning Trace",
            "Answer",
        ),
    ),
    "05-multiturn-eval-harness": (
        (
            "Single-Turn Benchmarks",
            "Sharding",
            "Conversation Simulator",
            "LLM Under Test",
        ),
        ("User Model", "Aptitude Metric", "Reliability Metric"),
    ),
    "06-model-editing-framework": (
        ("Base LLM", "Edited LLM", "Preservation Check"),
        ("Edit Request", "Key-Value Mapping", "Null-Space Projection"),
    ),
    "07-evidential-multiview-learning": (
        ("View 1", "View-Specific Evidence", "Opinion Construction"),
        ("View 2", "Conflictive Aggregation", "Decision + Reliability"),
        ("View 3",),
    ),
    "08-transformer-succinctness": (
        ("Language Family", "Transformer", "Verification Problem"),
        ("LTL Formula",),
        ("RNN",),
        ("Finite Automaton",),
    ),
    "09-finetuning-influence-flow": (
        ("Training Example A", "Parameter Update", "Good Outputs"),
        ("Training Example B", "Probability Mass Shift", "Bad Outputs"),
    ),
    "10-agent-reflection-loop": (
        ("Planner", "Executor", "Observation", "Reflection", "Memory"),
        ("Task", "Next Action"),
    ),
}


POSITION_HINTS: dict[str, dict[str, tuple[float, float]]] = {
    "01-candidate-to-paper-pipeline": {
        label: (float(index), 0.0)
        for index, label in enumerate(LAYOUT_ROWS["01-candidate-to-paper-pipeline"][0])
    },
    "02-rag-system": {
        "Corpus": (0, 0),
        "Encoder": (1, 0),
        "Vector Index": (2, 0),
        "Query": (0, 1.25),
        "Retriever": (1, 1.25),
        "Reranker": (2, 1.25),
        "Generator": (3, 1.25),
    },
    "03-rlhf-pipeline": {
        "SFT Model": (0, 0),
        "Sampling": (1, 0),
        "Reward Model": (2, 0),
        "Policy Update": (3, 0),
        "Evaluation": (4, 0),
        "Preference Data": (1.5, 1.25),
    },
    "04-diffusion-llm-training": {
        label: (float(index), 0.0)
        for index, label in enumerate(LAYOUT_ROWS["04-diffusion-llm-training"][0])
    },
    "05-multiturn-eval-harness": {
        "Single-Turn Benchmarks": (0, 0),
        "Sharding": (1, 0),
        "Conversation Simulator": (2, 0),
        "LLM Under Test": (3, 0),
        "User Model": (1, 1.25),
        "Aptitude Metric": (3, 1.25),
        "Reliability Metric": (4, 1.25),
    },
    "06-model-editing-framework": {
        "Base LLM": (0, 0),
        "Edited LLM": (3, 0),
        "Preservation Check": (4, 0),
        "Edit Request": (0, 1.25),
        "Key-Value Mapping": (1, 1.25),
        "Null-Space Projection": (2, 1.25),
    },
    "07-evidential-multiview-learning": {
        "View 1": (0, 0),
        "View 2": (0, 1.25),
        "View 3": (0, 2.5),
        "View-Specific Evidence": (1, 1.25),
        "Opinion Construction": (2, 1.25),
        "Conflictive Aggregation": (3, 1.25),
        "Decision + Reliability": (4, 1.25),
    },
    "08-transformer-succinctness": {
        "Language Family": (0, 1.9),
        "Transformer": (1, 0),
        "LTL Formula": (1, 1.25),
        "RNN": (1, 2.5),
        "Finite Automaton": (1, 3.75),
        "Verification Problem": (2, 1.9),
    },
    "09-finetuning-influence-flow": {
        "Training Example A": (0, 0),
        "Training Example B": (0, 1.25),
        "Parameter Update": (1, 0.625),
        "Probability Mass Shift": (2, 0.625),
        "Good Outputs": (3, 0),
        "Bad Outputs": (3, 1.25),
    },
    "10-agent-reflection-loop": {
        "Planner": (1, 0),
        "Executor": (2, 0),
        "Observation": (3, 0),
        "Reflection": (4, 0),
        "Memory": (5, 0),
        "Task": (0, 1.25),
        "Next Action": (2, 1.25),
    },
}

NODE_W = 170.0
NODE_H = 70.0
X_STEP = 245.0
Y_STEP = 142.0
MARGIN_X = 78.0
MARGIN_Y = 112.0
GROUP_COLORS = ("#DBEAFE", "#CCFBF1", "#FEF3C7", "#EDE9FE")
GROUP_STROKES = ("#60A5FA", "#2DD4BF", "#F59E0B", "#A78BFA")
ICON_BADGES = {
    "person": "USR",
    "lightbulb": "IDEA",
    "idea": "IDEA",
    "clipboard": "PLAN",
    "flask": "EXP",
    "database": "DB",
    "list": "LIST",
    "document": "DOC",
    "documents": "DOC",
    "magnifier": "FIND",
    "box": "PKG",
    "text": "TXT",
    "embedding": "VEC",
    "scale": "SCORE",
    "llm": "AI",
    "dice": "SAMPLE",
    "loop": "LOOP",
    "chart": "METRIC",
    "noise": "NOISE",
    "arrow": "DECODE",
    "path": "TRACE",
    "check": "OK",
    "scissors": "SPLIT",
    "chat": "CHAT",
    "edit": "EDIT",
    "key": "KEY",
    "matrix": "MATRIX",
    "image": "IMG",
    "audio": "AUDIO",
    "merge": "FUSE",
    "set": "SET",
    "formula": "LTL",
    "network": "RNN",
    "machine": "FSM",
    "gavel": "VERIFY",
    "flow": "SHIFT",
    "cross": "RISK",
    "inbox": "TASK",
    "plan": "PLAN",
    "gear": "ACT",
    "eye": "OBS",
    "mirror": "REFLECT",
}


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


def _geometry(
    parent: ET.Element,
    *,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    relative: bool = False,
) -> ET.Element:
    attrs: dict[str, str] = {"as": "geometry"}
    if relative:
        attrs["relative"] = "1"
    for key, value in (("x", x), ("y", y), ("width", width), ("height", height)):
        if value is not None:
            attrs[key] = f"{value:g}"
    return ET.SubElement(parent, "mxGeometry", attrs)


def _positions(spec: DiagramSpec) -> dict[str, tuple[float, float]]:
    try:
        hints = POSITION_HINTS[spec.stem]
    except KeyError as exc:
        raise ValueError(f"missing layout hints for {spec.stem}") from exc
    names = {node.label for node in spec.nodes}
    if set(hints) != names:
        raise ValueError(f"layout hints do not cover {spec.stem}")
    return {
        label: (MARGIN_X + gx * X_STEP, MARGIN_Y + gy * Y_STEP)
        for label, (gx, gy) in hints.items()
    }


def _group_bounds(
    members: tuple[str, ...], positions: dict[str, tuple[float, float]]
) -> tuple[float, float, float, float]:
    xs = [positions[label][0] for label in members]
    ys = [positions[label][1] for label in members]
    left = min(xs) - 24
    top = min(ys) - 42
    right = max(xs) + NODE_W + 24
    bottom = max(ys) + NODE_H + 26
    return left, top, right - left, bottom - top


def _node_color(spec: DiagramSpec, label: str) -> str:
    for index, group in enumerate(spec.groups):
        if label in group.members:
            return GROUP_STROKES[index % len(GROUP_STROKES)]
    return "#64748B"


def build_drawio_xml(
    spec: DiagramSpec, shape_results: dict[str, list[dict[str, str]]]
) -> str:
    """Build editable, uncompressed draw.io XML without external resources."""
    del shape_results  # Search evidence is reported, not embedded as remote assets.
    positions = _positions(spec)
    max_x = max(x for x, _ in positions.values()) + NODE_W + MARGIN_X
    max_y = max(y for _, y in positions.values()) + NODE_H + 104

    mxfile = ET.Element(
        "mxfile",
        {
            "host": "app.diagrams.net",
            "agent": "codex-gen-from-scratch",
            "version": "26.0.16",
        },
    )
    diagram = ET.SubElement(mxfile, "diagram", {"id": spec.stem, "name": "Page-1"})
    model = ET.SubElement(
        diagram,
        "mxGraphModel",
        {
            "dx": "1200",
            "dy": "800",
            "grid": "1",
            "gridSize": "10",
            "guides": "1",
            "tooltips": "1",
            "connect": "1",
            "arrows": "1",
            "fold": "1",
            "page": "1",
            "pageScale": "1",
            "pageWidth": f"{max(1169, int(max_x)):d}",
            "pageHeight": f"{max(827, int(max_y)):d}",
            "math": "0",
            "shadow": "0",
        },
    )
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})

    for index, group in enumerate(spec.groups):
        x, y, width, height = _group_bounds(group.members, positions)
        color = GROUP_COLORS[index % len(GROUP_COLORS)]
        stroke = GROUP_STROKES[index % len(GROUP_STROKES)]
        cell = ET.SubElement(
            root,
            "mxCell",
            {
                "id": f"group-{index + 1}",
                "value": f"<b>{html.escape(group.label)}</b>",
                "style": (
                    "rounded=1;whiteSpace=wrap;html=1;dashed=1;dashPattern=6 5;"
                    f"fillColor={color};fillOpacity=28;strokeColor={stroke};"
                    "strokeWidth=1.4;fontColor=#334155;fontSize=14;fontStyle=1;"
                    "verticalAlign=top;spacingTop=10;align=left;spacingLeft=12;"
                ),
                "vertex": "1",
                "parent": "1",
                "athenaRole": "group",
            },
        )
        _geometry(cell, x=x, y=y, width=width, height=height)

    title = ET.SubElement(
        root,
        "mxCell",
        {
            "id": "title",
            "value": f"<b>{html.escape(spec.title)}</b>",
            "style": (
                "text;html=1;strokeColor=none;fillColor=none;align=left;"
                "verticalAlign=middle;fontColor=#0F172A;fontSize=20;fontStyle=1;"
            ),
            "vertex": "1",
            "parent": "1",
            "athenaRole": "title",
        },
    )
    _geometry(title, x=MARGIN_X - 24, y=30, width=max_x - 2 * MARGIN_X + 48, height=38)

    node_ids = {
        node.label: f"node-{index + 1}" for index, node in enumerate(spec.nodes)
    }
    for index, edge in enumerate(spec.edges):
        source_x, source_y = positions[edge.source]
        target_x, target_y = positions[edge.target]
        backward = target_x <= source_x
        style = (
            "edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;"
            "jettySize=auto;html=1;strokeColor=#2563EB;strokeWidth=2;"
            "endArrow=block;endFill=1;"
        )
        if backward:
            style += "exitY=1;exitX=0.5;entryY=1;entryX=0.5;"
        elif abs(target_y - source_y) > NODE_H:
            style += "exitX=1;exitY=0.5;entryX=0;entryY=0.5;"
        edge_cell = ET.SubElement(
            root,
            "mxCell",
            {
                "id": f"edge-{index + 1}",
                "style": style,
                "edge": "1",
                "parent": "1",
                "source": node_ids[edge.source],
                "target": node_ids[edge.target],
                "athenaRole": "edge",
            },
        )
        _geometry(edge_cell, relative=True)

    for node in spec.nodes:
        x, y = positions[node.label]
        badge = ICON_BADGES.get(node.icon, node.icon.upper()[:8])
        color = _node_color(spec, node.label)
        value = (
            f"<b>{html.escape(node.label)}</b><br>"
            f'<font color="#64748B" size="2">{html.escape(badge)}</font>'
        )
        cell = ET.SubElement(
            root,
            "mxCell",
            {
                "id": node_ids[node.label],
                "value": value,
                "style": (
                    "rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;"
                    f"strokeColor={color};strokeWidth=1.8;fontColor=#1E293B;"
                    "fontSize=13;align=center;verticalAlign=middle;spacing=8;"
                    "shadow=0;arcSize=14;"
                ),
                "vertex": "1",
                "parent": "1",
                "athenaRole": "node",
                "athenaLabel": node.label,
            },
        )
        _geometry(cell, x=x, y=y, width=NODE_W, height=NODE_H)

    for index, annotation in enumerate(spec.annotations):
        target, separator, detail = annotation.partition(":")
        if not separator or target.strip() not in positions:
            continue
        target = target.strip()
        x, y = positions[target]
        note = ET.SubElement(
            root,
            "mxCell",
            {
                "id": f"note-{index + 1}",
                "value": html.escape(detail.strip()),
                "style": (
                    "rounded=1;whiteSpace=wrap;html=1;fillColor=#F8FAFC;"
                    "strokeColor=#CBD5E1;dashed=1;fontColor=#475569;fontSize=10;"
                    "align=center;verticalAlign=middle;"
                ),
                "vertex": "1",
                "parent": "1",
                "athenaRole": "annotation",
            },
        )
        _geometry(note, x=x, y=y + NODE_H + 12, width=NODE_W, height=32)

    ET.indent(mxfile, space="  ")
    return ET.tostring(mxfile, encoding="unicode", xml_declaration=False)


def _rect(cell: ET.Element) -> tuple[float, float, float, float] | None:
    geometry = cell.find("mxGeometry")
    if geometry is None:
        return None
    try:
        x = float(geometry.get("x", "0"))
        y = float(geometry.get("y", "0"))
        width = float(geometry.get("width", "0"))
        height = float(geometry.get("height", "0"))
    except ValueError:
        return None
    return x, y, width, height


def validate_drawio_xml(xml: str) -> dict[str, object]:
    """Validate editability, structure, external resources, and node overlap."""
    result: dict[str, object] = {
        "xml_ok": False,
        "reason": None,
        "vertices": 0,
        "edges": 0,
        "editable_labels": 0,
        "external_urls": re.findall(r"https?://[^\s\"'<>]+", xml),
        "node_overlaps": [],
    }
    try:
        mxfile = ET.fromstring(xml)
    except ET.ParseError as exc:
        result["reason"] = str(exc)
        return result

    cells = list(mxfile.iter("mxCell"))
    vertices = [cell for cell in cells if cell.get("vertex") == "1"]
    edges = [cell for cell in cells if cell.get("edge") == "1"]
    result["vertices"] = len(vertices)
    result["edges"] = len(edges)
    result["editable_labels"] = sum(
        bool(cell.get("value", "").strip()) for cell in vertices
    )

    nodes = [cell for cell in vertices if cell.get("athenaRole") == "node"]
    overlaps: list[list[str]] = []
    for index, first in enumerate(nodes):
        first_rect = _rect(first)
        if first_rect is None:
            continue
        ax, ay, aw, ah = first_rect
        for second in nodes[index + 1 :]:
            second_rect = _rect(second)
            if second_rect is None:
                continue
            bx, by, bw, bh = second_rect
            if ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by:
                overlaps.append(
                    [first.get("athenaLabel", ""), second.get("athenaLabel", "")]
                )
    result["node_overlaps"] = overlaps

    ids = {cell.get("id") for cell in cells}
    external_urls = result["external_urls"]
    valid = (
        mxfile.tag == "mxfile"
        and mxfile.find("./diagram/mxGraphModel/root") is not None
        and {"0", "1"} <= ids
        and len(nodes) >= 2
        and len(edges) >= 1
        and not external_urls
        and not overlaps
    )
    result["xml_ok"] = valid
    if not valid:
        result["reason"] = "draw.io structural requirements failed"
    return result
