"""Pure TeX AST traversal and inline-render normalization helpers."""

import re
from collections.abc import Iterable
from typing import Any

from pylatexenc.latexwalker import (
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMacroNode,
    LatexNode,
)

IGNORED_ENVIRONMENTS = {"comment"}
ENVIRONMENT_REQUIRED_ARGUMENTS = {
    "alignat": 1,
    "alignat*": 1,
    "tabular": 1,
    "tabular*": 2,
    "tabularx": 2,
    "longtable": 1,
    "array": 1,
    "thebibliography": 1,
    "minted": 1,
}
IMAGE_PATH = re.compile(r"\S*\.(?:png|jpe?g|pdf|eps|svg|gif|tif{1,2})\b", re.IGNORECASE)
INLINE_MARKUP = re.compile(r"[`*_]{1,3}")
VENUE_PREFIX = re.compile(r"^(?:[A-Z][A-Za-z&.\-]{1,14}\s*)?[A-Z]{2,12}\s*'?\d{2,4}\b")
CITATION_PATTERN = re.compile(
    r"\\(?:cite|citep|citet|citealp|citeauthor|parencite|textcite)\w*\s*(?:\[[^]]*\]\s*)*\{([^}]+)\}"
)
LABEL_PATTERN = re.compile(r"\\label\s*\{([^}]+)\}")
REFERENCE_PATTERN = re.compile(r"\\(?:ref|eqref|autoref|cref|Cref)\s*\{([^}]+)\}")


def argument_nodes(node: LatexMacroNode) -> list[LatexNode]:
    """Return non-empty macro arguments in source order."""
    nodeargd = getattr(node, "nodeargd", None)
    if nodeargd is None:
        return []
    return [argument for argument in nodeargd.argnlist if argument is not None]


def last_argument(node: LatexMacroNode) -> LatexNode | None:
    """Return the final non-empty argument of a macro."""
    arguments = argument_nodes(node)
    return arguments[-1] if arguments else None


def walk_nodes(nodes: Iterable[LatexNode]) -> Iterable[LatexNode]:
    """Traverse TeX nodes and nested argument nodes depth first."""
    for node in nodes:
        yield node
        if (
            isinstance(node, LatexEnvironmentNode)
            and node.environmentname in IGNORED_ENVIRONMENTS
        ):
            continue
        children = getattr(node, "nodelist", None)
        if children:
            yield from walk_nodes(children)
        for argument in (
            argument_nodes(node) if isinstance(node, LatexMacroNode) else []
        ):
            argument_children = getattr(argument, "nodelist", None)
            if argument_children:
                yield from walk_nodes(argument_children)


def balanced_group_end(
    source: str, start: int, opening: str = "{", closing: str = "}"
) -> int | None:
    """Return the exclusive end of a balanced, escape-aware group."""
    if start >= len(source) or source[start] != opening:
        return None
    depth = 0
    for index in range(start, len(source)):
        char = source[index]
        if char not in {opening, closing}:
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and source[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2:
            continue
        depth += 1 if char == opening else -1
        if depth == 0:
            return index + 1
    return None


def environment_body(raw: str, name: str) -> str:
    """Remove an environment wrapper while preserving its original body."""
    start = re.match(r"^\\begin\{" + re.escape(name) + r"\}", raw)
    if start is None:
        return raw
    cursor = start.end()
    while cursor < len(raw) and raw[cursor].isspace():
        cursor += 1
    while cursor < len(raw) and raw[cursor] == "[":
        optional_end = balanced_group_end(raw, cursor, "[", "]")
        if optional_end is None:
            break
        cursor = optional_end
        while cursor < len(raw) and raw[cursor].isspace():
            cursor += 1
    for _ in range(ENVIRONMENT_REQUIRED_ARGUMENTS.get(name, 0)):
        required_end = balanced_group_end(raw, cursor)
        if required_end is None:
            break
        cursor = required_end
        while cursor < len(raw) and raw[cursor].isspace():
            cursor += 1
    end = re.search(r"\\end\{" + re.escape(name) + r"\}\s*$", raw)
    return raw[cursor : end.start() if end else len(raw)]


def content_nodes(nodes: list[LatexNode]) -> list[LatexNode]:
    """Remove TeX definition commands and their definition bodies."""
    result: list[LatexNode] = []
    index = 0
    while index < len(nodes):
        node = nodes[index]
        if not isinstance(node, LatexMacroNode) or node.macroname not in {
            "def",
            "gdef",
            "edef",
            "xdef",
        }:
            result.append(node)
            index += 1
            continue
        index += 1
        if index < len(nodes) and isinstance(nodes[index], LatexMacroNode):
            index += 1
        while index < len(nodes) and not isinstance(nodes[index], LatexGroupNode):
            index += 1
        if index < len(nodes):
            index += 1
    return result


def clean_inline(text: str) -> str:
    """Normalize inline whitespace while preserving explicit line breaks."""
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def normalize_title(value: str) -> str:
    """Normalize rendered title text and remove layout-only decorations."""
    text = IMAGE_PATH.sub(" ", value)
    text = INLINE_MARKUP.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return VENUE_PREFIX.sub("", text).strip(" :-—")


def clean_author_name(value: str) -> str:
    """Remove trailing contribution markers from an author name."""
    cleaned = clean_inline(value).strip(" ,;")
    cleaned = re.sub(r"\s*\$\s*\^(?:\{[^}]*\}|\\[A-Za-z]+|.)\s*\$\s*$", "", cleaned)
    return cleaned.strip(" ,;")


def bibliography_to_markdown(
    node: LatexEnvironmentNode,
    renderer: Any,
) -> tuple[str, list[str]]:
    """Render a ``thebibliography`` environment as keyed Markdown entries."""
    entries: list[tuple[str, str]] = []
    current_key = ""
    current_nodes: list[LatexNode] = []

    def flush() -> None:
        nonlocal current_key, current_nodes
        if current_key:
            text = re.sub(r"\s+", " ", renderer.nodes(current_nodes)).strip()
            if text:
                entries.append((current_key, text))
        current_key = ""
        current_nodes = []

    for child in node.nodelist:
        if isinstance(child, LatexMacroNode) and child.macroname == "bibitem":
            flush()
            current_key = clean_inline(renderer.argument(last_argument(child)))
        else:
            current_nodes.append(child)
    flush()
    if not entries:
        body = re.sub(r"\s+", " ", renderer.nodes(node.nodelist)).strip()
        return (f"## References\n\n{body}" if body else "", [])
    return "## References\n\n" + "\n\n".join(
        f"- [@{key}] {text}" for key, text in entries
    ), [key for key, _ in entries]


def citations(raw: str) -> list[str]:
    """Extract unique citation keys in first-seen order."""
    return list(
        dict.fromkeys(
            key.strip()
            for match in CITATION_PATTERN.finditer(raw)
            for key in match.group(1).split(",")
            if key.strip()
        )
    )


def references(raw: str) -> list[str]:
    """Extract unique local-reference keys in first-seen order."""
    return list(
        dict.fromkeys(
            key.strip()
            for match in REFERENCE_PATTERN.finditer(raw)
            for key in match.group(1).split(",")
            if key.strip()
        )
    )


def labels(raw: str) -> list[str]:
    """Extract unique section, equation, figure, and table labels."""
    return list(
        dict.fromkeys(match.group(1).strip() for match in LABEL_PATTERN.finditer(raw))
    )
