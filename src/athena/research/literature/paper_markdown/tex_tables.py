"""TeX table normalization for the paper Markdown parser."""

import re
from typing import Any

from pylatexenc.latexwalker import LatexEnvironmentNode, LatexMacroNode, LatexWalker

from athena.research.literature.paper_markdown.tex_render import (
    ENVIRONMENT_REQUIRED_ARGUMENTS,
    argument_nodes,
    balanced_group_end,
    clean_inline,
    environment_body,
    walk_nodes,
)

TABULAR_ENVIRONMENTS = {"tabular", "tabular*", "tabularx", "longtable", "array"}


def strip_nested_tabular_markers(raw: str) -> str:
    """Remove nested tabular wrappers while retaining their cell contents."""
    pattern = re.compile(
        r"\\begin\{("
        + "|".join(re.escape(name) for name in TABULAR_ENVIRONMENTS)
        + r")\}"
    )
    output: list[str] = []
    cursor = 0
    for match in pattern.finditer(raw):
        output.append(raw[cursor : match.start()])
        end = match.end()
        while end < len(raw) and raw[end].isspace():
            end += 1
        while end < len(raw) and raw[end] == "[":
            optional_end = balanced_group_end(raw, end, "[", "]")
            if optional_end is None:
                break
            end = optional_end
            while end < len(raw) and raw[end].isspace():
                end += 1
        for _ in range(ENVIRONMENT_REQUIRED_ARGUMENTS.get(match.group(1), 0)):
            required_end = balanced_group_end(raw, end)
            if required_end is None:
                break
            end = required_end
            while end < len(raw) and raw[end].isspace():
                end += 1
        cursor = end
    output.append(raw[cursor:])
    cleaned = "".join(output)
    return re.sub(
        r"\\end\{(?:"
        + "|".join(re.escape(name) for name in TABULAR_ENVIRONMENTS)
        + r")\}",
        "",
        cleaned,
    )


def table_to_markdown(
    node: LatexEnvironmentNode | None,
    renderer: Any,
    context: Any,
    custom_macros: dict[str, str] | None = None,
) -> str:
    """Convert a TeX tabular node to Markdown, or return empty on unsafe input."""
    if node is None:
        return ""
    renderer_type = renderer.__class__
    raw = environment_body(
        renderer.source[node.pos : node.pos + node.len], node.environmentname
    )
    raw = strip_nested_tabular_markers(raw)
    raw = re.sub(
        r"\\(?:hline|toprule|midrule|bottomrule)(?:\[[^]]*\])?|\\cline\{[^}]+\}",
        "",
        raw,
    )
    raw = re.sub(r"\\(?:caption|label)\s*\{[^{}]*\}", "", raw)
    rows = re.split(r"(?<!\\)\\\\(?:\[[^]]*\])?", raw)
    parsed: list[list[str]] = []
    continuations: list[list[bool]] = []
    for row in rows:
        if not row.strip():
            continue
        cells = re.split(r"(?<!\\)&", row)
        rendered_cells: list[str] = []
        continuation_cells: list[bool] = []
        for cell in cells:
            try:
                cell_source = cell.strip()
                nodes, _, _ = LatexWalker(
                    cell_source, latex_context=context
                ).get_latex_nodes()
                value = (
                    clean_inline(
                        renderer_type(cell_source, context, custom_macros).nodes(nodes)
                    )
                    .replace("|", "\\|")
                    .replace("\n", "<br>")
                )
            except Exception:
                return ""
            rendered_cells.append(value)
            continuation_cells.append(False)
            span = 1
            multicolumn = next(
                (
                    candidate
                    for candidate in walk_nodes(nodes)
                    if isinstance(candidate, LatexMacroNode)
                    and candidate.macroname == "multicolumn"
                ),
                None,
            )
            if multicolumn is not None:
                arguments = argument_nodes(multicolumn)
                try:
                    span = max(
                        1,
                        int(
                            clean_inline(
                                renderer_type(cell_source, context).argument(
                                    arguments[0]
                                )
                            )
                        ),
                    )
                except (IndexError, ValueError):
                    span = 1
            rendered_cells.extend(value for _ in range(span - 1))
            continuation_cells.extend(True for _ in range(span - 1))
        if rendered_cells and any(rendered_cells):
            parsed.append(rendered_cells)
            continuations.append(continuation_cells)
    if not parsed:
        return ""
    width = max(len(row) for row in parsed)
    normalized = [row + [""] * (width - len(row)) for row in parsed]
    normalized_continuations = [
        row + [False] * (width - len(row)) for row in continuations
    ]
    header = normalized[0]
    data = normalized[1:]
    header_continuations = normalized_continuations[0]
    data_continuations = normalized_continuations[1:]
    if data:
        possible_subheader = data[0]
        nonempty_subheaders = [value for value in possible_subheader if value]
        has_grouped_header = len(set(value for value in header if value)) < len(
            [value for value in header if value]
        )
        short_text_subheader = bool(nonempty_subheaders) and all(
            len(value) <= 80
            and not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value.strip("*$ `"))
            for value in nonempty_subheaders
        )
        if not possible_subheader[0] and has_grouped_header and short_text_subheader:
            header = [
                (
                    primary
                    if not secondary or primary == secondary
                    else secondary if not primary else f"{primary} / {secondary}"
                )
                for primary, secondary in zip(header, possible_subheader)
            ]
            data = data[1:]
            data_continuations = data_continuations[1:]
    header = [
        (
            f"{value} (continued)"
            if continuation and value and index > 0 and value == header[index - 1]
            else value
        )
        for index, (value, continuation) in enumerate(zip(header, header_continuations))
    ]
    data = [
        [
            "" if continuation else value
            for value, continuation in zip(row, row_continuations)
        ]
        for row, row_continuations in zip(data, data_continuations)
    ]
    header = [
        value or ("Field" if index == 0 else f"Column {index + 1}")
        for index, value in enumerate(header)
    ]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in data)
    return "\n".join(lines)


__all__ = ["TABULAR_ENVIRONMENTS", "strip_nested_tabular_markers", "table_to_markdown"]
