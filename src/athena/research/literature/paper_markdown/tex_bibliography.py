"""TeX citation, reference, label, and bibliography transformations."""

import re
from typing import Any

from pylatexenc.latexwalker import LatexEnvironmentNode, LatexMacroNode, LatexNode

from athena.research.literature.paper_markdown.tex_render import (
    clean_inline,
    last_argument,
)

CITATION_PATTERN = re.compile(
    r"\\(?:cite|citep|citet|citealp|citeauthor|parencite|textcite)\w*\s*(?:\[[^]]*\]\s*)*\{([^}]+)\}"
)
LABEL_PATTERN = re.compile(r"\\label\s*\{([^}]+)\}")
REFERENCE_PATTERN = re.compile(r"\\(?:ref|eqref|autoref|cref|Cref)\s*\{([^}]+)\}")


def bibliography_to_markdown(
    node: LatexEnvironmentNode,
    renderer: Any,
) -> tuple[str, list[str]]:
    """Render a ``thebibliography`` environment as keyed Markdown entries."""
    entries: list[tuple[str, str]] = []
    current_key = ""
    current_nodes: list[LatexNode] = []

    def flush() -> None:
        """Commit the current bibliography item, if it has a key."""
        nonlocal current_key, current_nodes
        if not current_key:
            current_nodes = []
            return
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
    keys = [key for key, _ in entries]
    markdown = "## References\n\n" + "\n\n".join(
        f"- [@{key}] {text}" for key, text in entries
    )
    return markdown, keys


def citations(raw: str) -> list[str]:
    """Extract unique citation keys in first-seen order."""
    keys: list[str] = []
    for match in CITATION_PATTERN.finditer(raw):
        for key in match.group(1).split(","):
            normalized = key.strip()
            if normalized and normalized not in keys:
                keys.append(normalized)
    return keys


def references(raw: str) -> list[str]:
    """Extract unique local-reference keys in first-seen order."""
    keys: list[str] = []
    for match in REFERENCE_PATTERN.finditer(raw):
        for key in match.group(1).split(","):
            normalized = key.strip()
            if normalized and normalized not in keys:
                keys.append(normalized)
    return keys


def labels(raw: str) -> list[str]:
    """Extract unique section, equation, figure, and table labels."""
    return list(
        dict.fromkeys(match.group(1).strip() for match in LABEL_PATTERN.finditer(raw))
    )
