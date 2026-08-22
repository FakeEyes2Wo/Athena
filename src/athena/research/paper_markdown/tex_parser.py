"""把 TeX Source 转成保留公式、引用、图表和源码定位的结构化 Markdown。"""

import bisect
import hashlib
import re
from pathlib import PurePosixPath
from collections.abc import Iterable

from dataclasses import dataclass, field

import pylatexenc
from pylatexenc.latex2text import LatexNodes2Text
from pylatexenc.latexwalker import (
    LatexCharsNode,
    LatexCommentNode,
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMacroNode,
    LatexMathNode,
    LatexNode,
    LatexSpecialsNode,
    LatexWalker,
    get_default_latex_context_db,
)
from pylatexenc.macrospec import MacroSpec

from athena.research.paper_markdown.document import (
    VISUAL_TOKEN,
    ParsedElement,
    ParsedPaper,
    ParsedVisual,
)
from athena.research.paper_markdown.schemas import ProcessingDiagnostic, SourceLocator
from athena.research.paper_markdown.tex_source import (
    ExpandedTex,
    decode_tex,
    strip_comment,
)

SECTION_LEVELS = {
    "part": 1,
    "chapter": 1,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
    "subparagraph": 5,
}
MATH_ENVIRONMENTS = {
    "equation",
    "equation*",
    "align",
    "align*",
    "alignat",
    "alignat*",
    "gather",
    "gather*",
    "multline",
    "multline*",
    "displaymath",
    "eqnarray",
    "eqnarray*",
}
LIST_ENVIRONMENTS = {"itemize", "enumerate", "description"}
TABLE_ENVIRONMENTS = {"table", "table*"}
TABULAR_ENVIRONMENTS = {"tabular", "tabular*", "tabularx", "longtable", "array"}
CODE_ENVIRONMENTS = {"verbatim", "verbatim*", "lstlisting", "minted"}
REFERENCE_ENVIRONMENTS = {"thebibliography"}
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
FIGURE_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".svg", ".eps")
CITATION_PATTERN = re.compile(
    r"\\(?:cite|citep|citet|citealp|citeauthor|parencite|textcite)\w*\s*(?:\[[^]]*\]\s*)*\{([^}]+)\}"
)
IMAGE_PATH = re.compile(r"\S*\.(?:png|jpe?g|pdf|eps|svg|gif|tif{1,2})\b", re.IGNORECASE)
INLINE_MARKUP = re.compile(r"[`*_]{1,3}")
# 只剥标题最前面的"会场缩写 + 四位年份"，避免误伤 "BERT 2018 revisited" 这类正文标题
VENUE_PREFIX = re.compile(r"^(?:[A-Z][A-Za-z&.\-]{1,14}\s*)?[A-Z]{2,12}\s*'?\d{2,4}\b")
LABEL_PATTERN = re.compile(r"\\label\s*\{([^}]+)\}")
REFERENCE_PATTERN = re.compile(r"\\(?:ref|eqref|autoref|cref|Cref)\s*\{([^}]+)\}")
GRAPHICSPATH_PATTERN = re.compile(r"\\graphicspath\s*\{((?:\s*\{[^{}]*\}\s*)+)\}")
DEF_MACRO_PATTERN = re.compile(r"\\def\s*\\([A-Za-z@]+)\s*")
COMMAND_MACRO_PATTERN = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand)\*?\s*(?:\{\s*\\([A-Za-z@]+)\s*\}|\\([A-Za-z@]+))\s*"
)


def latex_context():
    """返回补全论文常见宏参数规格的 pylatexenc 上下文。"""
    context = get_default_latex_context_db()
    macros = [
        *(MacroSpec(name, "*[{") for name in SECTION_LEVELS),
        MacroSpec("title", "{"),
        MacroSpec("author", "{"),
        MacroSpec("affiliation", "{"),
        MacroSpec("institute", "{"),
        MacroSpec("keywords", "{"),
        MacroSpec("caption", "[{"),
        MacroSpec("includegraphics", "[{"),
        MacroSpec("graphicspath", "{"),
        MacroSpec("label", "{"),
        MacroSpec("ref", "{"),
        MacroSpec("eqref", "{"),
        MacroSpec("autoref", "{"),
        MacroSpec("cref", "{"),
        MacroSpec("Cref", "{"),
        MacroSpec("href", "{{"),
        MacroSpec("url", "{"),
        MacroSpec("footnote", "[{"),
        MacroSpec("footnotetext", "[{"),
        MacroSpec("textbf", "{"),
        MacroSpec("textit", "{"),
        MacroSpec("emph", "{"),
        MacroSpec("texttt", "{"),
        MacroSpec("underline", "{"),
        MacroSpec("textsc", "{"),
        MacroSpec("textsuperscript", "{"),
        MacroSpec("texorpdfstring", "{{"),
        MacroSpec("multicolumn", "{{{"),
        MacroSpec("multirow", "{{{"),
        MacroSpec("makecell", "[{"),
        MacroSpec("bibitem", "[{"),
        MacroSpec("bibliography", "{"),
        MacroSpec("addbibresource", "[{"),
        MacroSpec("SI", "{{"),
        MacroSpec("qty", "{{"),
        MacroSpec("SIrange", "{{{"),
    ]
    context.add_context_category("athena-paper", macros=macros, prepend=True)
    return context


def argument_nodes(node: LatexMacroNode) -> list[LatexNode]:
    """返回宏中非空的参数节点，保持原顺序。"""
    nodeargd = getattr(node, "nodeargd", None)
    if nodeargd is None:
        return []
    return [argument for argument in nodeargd.argnlist if argument is not None]


def last_argument(node: LatexMacroNode) -> LatexNode | None:
    """返回宏最后一个非空参数，适用于 section/caption/cite 等宏。"""
    arguments = argument_nodes(node)
    return arguments[-1] if arguments else None


def walk_nodes(nodes: Iterable[LatexNode]) -> Iterable[LatexNode]:
    """深度优先遍历节点及其子节点。"""
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
    """返回平衡分组结束位置（exclusive）；不把转义括号计入层级。"""
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


def mask_macro_scan_source(source: str) -> str:
    """屏蔽 comment 环境与行注释，同时保持字符位置不变。"""
    masked = list(source)
    for environment in IGNORED_ENVIRONMENTS:
        pattern = re.compile(
            r"\\begin\{"
            + re.escape(environment)
            + r"\}.*?\\end\{"
            + re.escape(environment)
            + r"\}",
            re.DOTALL,
        )
        for match in pattern.finditer(source):
            for index in range(match.start(), match.end()):
                if masked[index] != "\n":
                    masked[index] = " "
    offset = 0
    masked_text = "".join(masked)
    for line in masked_text.splitlines(keepends=True):
        searchable = line.rstrip("\r\n")
        visible = strip_comment(searchable)
        if len(visible) < len(searchable):
            for index in range(offset + len(visible), offset + len(searchable)):
                masked[index] = " "
        offset += len(line)
    return "".join(masked)


def extract_zero_argument_macros(source: str) -> dict[str, str]:
    """提取安全的零参数 ``def/newcommand``，供正文渲染时展开。"""
    searchable = mask_macro_scan_source(source)
    definitions: list[tuple[int, str, re.Match[str]]] = []
    definitions.extend(
        (match.start(), "def", match)
        for match in DEF_MACRO_PATTERN.finditer(searchable)
    )
    definitions.extend(
        (match.start(), "command", match)
        for match in COMMAND_MACRO_PATTERN.finditer(searchable)
    )
    result: dict[str, str] = {}
    for _, kind, match in sorted(definitions, key=lambda item: item[0]):
        name = match.group(1) if kind == "def" else (match.group(1) or match.group(2))
        cursor = match.end()
        if kind == "def" and cursor < len(searchable) and searchable[cursor] == "#":
            continue
        if kind == "command" and cursor < len(searchable) and searchable[cursor] == "[":
            end = searchable.find("]", cursor + 1)
            if end < 0 or searchable[cursor + 1 : end].strip() not in {"", "0"}:
                continue
            cursor = end + 1
        while cursor < len(searchable) and searchable[cursor].isspace():
            cursor += 1
        end = balanced_group_end(searchable, cursor)
        if end is None:
            continue
        result[name] = source[cursor + 1 : end - 1]
    return result


class TexRenderer:
    """将 pylatexenc 节点转换成内容保真的 Markdown 片段。"""

    def __init__(
        self,
        source: str,
        context,
        custom_macros: dict[str, str] | None = None,
        macro_stack: tuple[str, ...] = (),
    ) -> None:
        self.source = source
        self.context = context
        self.custom_macros = custom_macros or {}
        self.macro_stack = macro_stack
        self.fallback = LatexNodes2Text(latex_context=context)

    def nodes(self, nodes: Iterable[LatexNode]) -> str:
        """按节点顺序渲染，数学节点保留原始 TeX。"""
        return "".join(self.node(node) for node in nodes)

    def argument(self, node: LatexNode | None) -> str:
        """渲染宏参数的内部节点。"""
        if node is None:
            return ""
        children = getattr(node, "nodelist", None)
        return self.nodes(children) if children is not None else self.node(node)

    def node(self, node: LatexNode) -> str:
        """渲染单个 TeX AST 节点。"""
        if isinstance(node, LatexCharsNode):
            return node.chars
        if isinstance(node, LatexCommentNode):
            return "\n" if "\n" in node.comment_post_space else ""
        if isinstance(node, LatexMathNode):
            return self.source[node.pos : node.pos + node.len]
        if isinstance(node, LatexGroupNode):
            return self.nodes(node.nodelist)
        if isinstance(node, LatexSpecialsNode):
            return " " if node.specials_chars == "~" else node.specials_chars
        if isinstance(node, LatexMacroNode):
            return self.macro(node)
        if isinstance(node, LatexEnvironmentNode):
            return self.environment(node)
        return self.source[node.pos : node.pos + node.len]

    def macro(self, node: LatexMacroNode) -> str:
        """把结构、格式、引用和常见文本宏映射为 Markdown。"""
        name = node.macroname
        arguments = argument_nodes(node)
        final = self.argument(arguments[-1]) if arguments else ""
        if name in self.custom_macros and not arguments:
            if name in self.macro_stack:
                return f"\\{name}"
            expansion = self.custom_macros[name]
            try:
                nodes, _, _ = LatexWalker(
                    expansion, latex_context=self.context
                ).get_latex_nodes()
                value = TexRenderer(
                    expansion,
                    self.context,
                    self.custom_macros,
                    (*self.macro_stack, name),
                ).nodes(nodes)
            except Exception:
                value = expansion
            if (
                getattr(node, "macro_post_space", "")
                and value
                and not value.endswith((" ", "\n"))
            ):
                value += " "
            return value
        if name in SECTION_LEVELS or name in {
            "title",
            "author",
            "caption",
            "label",
            "graphicspath",
            "includegraphics",
        }:
            return ""
        if name in {
            "maketitle",
            "tableofcontents",
            "bibliographystyle",
            "bibitem",
            "def",
            "gdef",
            "edef",
            "xdef",
            "let",
            "newcommand",
            "renewcommand",
            "providecommand",
            "setlength",
            "addtolength",
            "setcounter",
            "appendix",
        }:
            return ""
        if name in {"textbf", "mathbf"}:
            return f"**{final}**"
        if name in {"textit", "emph"}:
            return f"*{final}*"
        if name in {"texttt", "verb"}:
            return f"`{final}`"
        if name in {
            "underline",
            "textsc",
            "textsuperscript",
            "makecell",
            "multicolumn",
            "multirow",
        }:
            return final
        if name in {
            "cite",
            "citep",
            "citet",
            "citealp",
            "citeauthor",
            "parencite",
            "textcite",
            "nocite",
        } or name.startswith("cite"):
            keys = [key.strip() for key in final.split(",") if key.strip()]
            return "[" + "; ".join(f"@{key}" for key in keys) + "]"
        if name in {"ref", "autoref", "cref", "Cref"}:
            return f"[{final}]"
        if name == "eqref":
            return f"[{final}]"
        if name == "href" and len(arguments) >= 2:
            return f"[{self.argument(arguments[-1])}]({self.argument(arguments[-2])})"
        if name == "url":
            return f"<{final}>"
        if name in {"footnote", "footnotetext", "thanks"}:
            return f"^[{final}]"
        if name == "texorpdfstring" and arguments:
            return self.argument(arguments[0])
        if name in {"LaTeX", "TeX", "BibTeX"}:
            return name + (" " if getattr(node, "macro_post_space", "") else "")
        if name in {"xspace", "centering", "raggedright", "newblock"}:
            return " " if name == "newblock" else ""
        if name in {"quad", "qquad", "enspace", "hspace", "vspace"}:
            return " "
        if name in {"newline", "linebreak", "par", "\\"}:
            return "\n"
        if name in {"%", "&", "#", "_", "{", "}"}:
            return name
        if name == "item":
            return "\n- "
        if arguments:
            return " ".join(
                value
                for value in (self.argument(argument).strip() for argument in arguments)
                if value
            )
        try:
            return self.fallback.node_to_text(node)
        except Exception:
            return f"\\{name}"

    def environment(self, node: LatexEnvironmentNode) -> str:
        """渲染可内联环境；结构环境由主解析器单独处理。"""
        name = node.environmentname
        if name in IGNORED_ENVIRONMENTS:
            return ""
        if name in MATH_ENVIRONMENTS:
            return (
                "\n"
                + math_to_markdown(self.source[node.pos : node.pos + node.len], name)
                + "\n"
            )
        if name in CODE_ENVIRONMENTS:
            body = environment_body(self.source[node.pos : node.pos + node.len], name)
            return f"\n```text\n{body.strip()}\n```\n"
        if name in LIST_ENVIRONMENTS:
            return render_list(node, self)
        return self.nodes(node.nodelist)


def environment_body(raw: str, name: str) -> str:
    """移除环境首尾标记并保留内部原始 TeX。"""
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


def math_to_markdown(raw: str, name: str) -> str:
    """把展示数学环境转换为渲染器可接受的 Markdown TeX。"""
    body = environment_body(raw, name)
    body = LABEL_PATTERN.sub("", body)
    body = re.sub(r"\\(?:nonumber|notag)\b", "", body).strip()
    base_name = name.removesuffix("*")
    if base_name == "eqnarray":
        body = re.sub(r"&\s*([^&\s]+)\s*&", r"&\1", body)
        body = re.sub(r"(?m)^(\s*)&&", r"\1&", body)
    if base_name in {"align", "alignat", "eqnarray"}:
        body = f"\\begin{{aligned}}\n{body}\n\\end{{aligned}}"
    elif base_name in {"gather", "multline"}:
        body = f"\\begin{{gathered}}\n{body}\n\\end{{gathered}}"
    return f"$$\n{body}\n$$"


def render_list(node: LatexEnvironmentNode, renderer: TexRenderer) -> str:
    """把 itemize/enumerate/description 环境转换成 Markdown 列表。"""
    items: list[list[LatexNode]] = []
    current: list[LatexNode] = []
    for child in node.nodelist:
        if isinstance(child, LatexMacroNode) and child.macroname == "item":
            if current:
                items.append(current)
            current = []
        else:
            current.append(child)
    if current:
        items.append(current)
    ordered = node.environmentname == "enumerate"
    lines: list[str] = []
    item_number = 0
    for item in items:
        text = clean_inline(renderer.nodes(item))
        if not text:
            continue
        item_number += 1
        prefix = f"{item_number}." if ordered else "-"
        lines.append(f"{prefix} {text}")
    return "\n".join(lines)


def content_nodes(nodes: list[LatexNode]) -> list[LatexNode]:
    """移除正文中的 TeX 定义命令及其目标/定义体，避免配置泄漏为内容。"""
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


def strip_nested_tabular_markers(raw: str) -> str:
    """删除外层表格正文中的嵌套 tabular 声明，保留其单元格文本。"""
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


def clean_inline(text: str) -> str:
    """规整段落内部空白，同时保留显式换行。"""
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def normalize_title(value: str) -> str:
    """把渲染后的 ``\\title`` 压成一行纯文本标题，去掉排版夹带的内容。

    ``\\title{}`` 的参数经常不只有标题：作者会把会场名、占位图标、换行排版一起塞进去，
    而渲染器忠实地把它们都渲染出来。标题还会流进 ``PaperContent.title`` 和交给
    paper_source 的 ``upstream_metadata``，后者用它做身份校验，脏标题会引发误报的
    ``title_mismatch``，所以这里按元数据口径再规整一次，正文标题也用同一个值。

    三类夹带各有来源：图片路径来自用户自定义宏（``\\icon`` → ``\\img{...}``）里未知宏
    的花括号组直落为字面文本；会场名是作者真的写在 ``\\title{}`` 里的；换行则是标题本
    身的 ``\\\\``。``normalize_title("`ReAct`: Synergizing")`` 返回
    ``"ReAct: Synergizing"``。
    """
    text = IMAGE_PATH.sub(" ", value)
    text = INLINE_MARKUP.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return VENUE_PREFIX.sub("", text).strip(" :-—")


def clean_author_name(value: str) -> str:
    """移除作者名后的贡献标记，不保留单位、邮箱等元数据。"""
    cleaned = clean_inline(value).strip(" ,;")
    cleaned = re.sub(r"\s*\$\s*\^(?:\{[^}]*\}|\\[A-Za-z]+|.)\s*\$\s*$", "", cleaned)
    return cleaned.strip(" ,;")


def bibliography_to_markdown(
    node: LatexEnvironmentNode,
    renderer: TexRenderer,
) -> tuple[str, list[str]]:
    """把 ``thebibliography`` 按 bibitem 切成带 citation key 的 Markdown。"""
    entries: list[tuple[str, str]] = []
    current_key = ""
    current_nodes: list[LatexNode] = []

    def flush() -> None:
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
    """提取片段中的 citation keys，并按首次出现去重。"""
    keys: list[str] = []
    for match in CITATION_PATTERN.finditer(raw):
        for key in match.group(1).split(","):
            normalized = key.strip()
            if normalized and normalized not in keys:
                keys.append(normalized)
    return keys


def references(raw: str) -> list[str]:
    """提取片段引用的本地 label，并按首次出现去重。"""
    keys: list[str] = []
    for match in REFERENCE_PATTERN.finditer(raw):
        for key in match.group(1).split(","):
            normalized = key.strip()
            if normalized and normalized not in keys:
                keys.append(normalized)
    return keys


def labels(raw: str) -> list[str]:
    """提取 section/equation/figure/table 标签。"""
    return list(
        dict.fromkeys(match.group(1).strip() for match in LABEL_PATTERN.finditer(raw))
    )


@dataclass(slots=True)
class ParseSession:
    """Mutable state accumulated while parsing one TeX source package."""

    newlines: list[int]
    elements: list[ParsedElement] = field(default_factory=list)
    visuals: list[ParsedVisual] = field(default_factory=list)
    diagnostics: list[ProcessingDiagnostic] = field(default_factory=list)
    heading_path: list[str] = field(default_factory=list)
    in_appendix: bool = False
    graphic_paths: list[str] = field(default_factory=list)
    bibliography_added: bool = False
    external_bibliography: tuple[str, str, list[str], int] | None = None


class TexPaperParser:
    """TeX Source 主通道解析器。"""

    def __init__(self, source: ExpandedTex) -> None:
        self.source = source
        self.context = latex_context()
        self.custom_macros = extract_zero_argument_macros(source.text)
        self.renderer = TexRenderer(source.text, self.context, self.custom_macros)
        self.session = ParseSession(
            newlines=[
                index for index, char in enumerate(source.text) if char == "\n"
            ],
            diagnostics=list(source.diagnostics),
            graphic_paths=self.find_graphic_paths(),
        )
        self.session.external_bibliography = self.load_external_bibliography()

    def line_index(self, position: int) -> int:
        """把展开文本字符位置映射为 0-based 行索引。"""
        return bisect.bisect_right(self.session.newlines, position)

    def locator(self, node: LatexNode) -> SourceLocator:
        """把 AST 节点映射回原始 TeX 文件和行号。"""
        start_index = min(self.line_index(node.pos), len(self.source.line_map) - 1)
        end_position = max(node.pos, node.pos + node.len - 1)
        end_index = min(self.line_index(end_position), len(self.source.line_map) - 1)
        start = self.source.line_map[start_index]
        end = self.source.line_map[end_index]
        return SourceLocator(
            source_kind="tex",
            file=start.file,
            line_start=start.line,
            line_end=end.line if end.file == start.file else start.line,
        )

    def unique_locators(self, nodes: Iterable[LatexNode]) -> list[SourceLocator]:
        """按出现顺序去重元素覆盖的源码位置。"""
        result: list[SourceLocator] = []
        seen: set[tuple] = set()
        for node in nodes:
            locator = self.locator(node)
            key = (locator.file, locator.line_start, locator.line_end)
            if key not in seen:
                seen.add(key)
                result.append(locator)
        return result

    def raw(self, node: LatexNode) -> str:
        """取回节点未经重写的 TeX。"""
        return self.source.text[node.pos : node.pos + node.len]

    def find_graphic_paths(self) -> list[str]:
        """收集 ``graphicspath`` 中声明的资源目录。"""
        paths: list[str] = []
        for match in GRAPHICSPATH_PATTERN.finditer(self.source.text):
            paths.extend(
                value.strip()
                for value in re.findall(r"\{([^{}]*)\}", match.group(1))
                if value.strip()
            )
        return paths

    def load_external_bibliography(self) -> tuple[str, str, list[str], int] | None:
        """优先解析与入口同名的 ``.bbl``，其次使用包内唯一 ``.bbl``。"""
        entrypoint = PurePosixPath(self.source.entrypoint)
        exact = str(entrypoint.with_suffix(".bbl"))
        bbl_paths = sorted(
            path for path in self.source.files if path.lower().endswith(".bbl")
        )
        candidates = [exact, *(path for path in bbl_paths if path != exact)]
        for path in candidates:
            if path not in self.source.files:
                continue
            text = decode_tex(path, self.source.files[path], self.session.diagnostics)
            try:
                nodes, _, _ = LatexWalker(
                    text, latex_context=self.context
                ).get_latex_nodes()
                environment = next(
                    (
                        node
                        for node in walk_nodes(nodes)
                        if isinstance(node, LatexEnvironmentNode)
                        and node.environmentname in REFERENCE_ENVIRONMENTS
                    ),
                    None,
                )
                if environment is None:
                    raise ValueError("No thebibliography environment was found.")
                renderer = TexRenderer(text, self.context, self.custom_macros)
                markdown, keys = bibliography_to_markdown(environment, renderer)
                if not markdown:
                    raise ValueError("The bibliography rendered as empty content.")
                return path, markdown, keys, max(1, len(text.splitlines()))
            except Exception as error:
                self.session.diagnostics.append(
                    ProcessingDiagnostic(
                        level="warning",
                        code="tex_bibliography_parse_failed",
                        message=f"Could not parse bibliography file '{path}': {error}",
                    )
                )
        return None

    def add_external_bibliography(self) -> None:
        """在正文 bibliography 声明处插入已解析的外部参考文献。"""
        if self.session.bibliography_added or self.session.external_bibliography is None:
            return
        path, markdown, keys, line_count = self.session.external_bibliography
        self.add_bibliography_markdown(
            markdown,
            keys,
            [
                SourceLocator(
                    source_kind="tex",
                    file=path,
                    line_start=1,
                    line_end=line_count,
                )
            ],
            f"external:{path}",
            len(self.source.text),
        )

    def add_bibliography_markdown(
        self,
        markdown: str,
        keys: list[str],
        locators: list[SourceLocator],
        identity: str,
        position: int,
    ) -> None:
        """将 bibliography 拆为标题和逐条可安全分组的检索元素。"""
        if self.session.bibliography_added:
            return
        self.update_heading(1, "References")
        self.session.elements.append(
            ParsedElement(
                element_id=self.make_id("heading", f"{identity}:heading", position),
                kind="heading",
                markdown="## References",
                heading_path=list(self.session.heading_path),
                locators=locators,
            )
        )
        body = markdown.removeprefix("## References").strip()
        entries = list(
            re.finditer(
                r"(?ms)^- \[@([^]]+)\]\s+(.+?)(?=\n\n- \[@|\Z)",
                body,
            )
        )
        if entries:
            for index, entry in enumerate(entries):
                key = entry.group(1).strip()
                entry_markdown = f"- [@{key}] {entry.group(2).strip()}"
                self.session.elements.append(
                    ParsedElement(
                        element_id=self.make_id(
                            "bibliography",
                            f"{identity}:{entry_markdown}",
                            position + index + 1,
                        ),
                        kind="bibliography",
                        markdown=entry_markdown,
                        heading_path=list(self.session.heading_path),
                        locators=locators,
                        citation_keys=[key],
                    )
                )
        elif body:
            self.session.elements.append(
                ParsedElement(
                    element_id=self.make_id(
                        "bibliography", f"{identity}:{body}", position + 1
                    ),
                    kind="bibliography",
                    markdown=body,
                    heading_path=list(self.session.heading_path),
                    locators=locators,
                    citation_keys=keys,
                )
            )
        self.session.bibliography_added = True

    def make_id(self, prefix: str, raw: str, position: int) -> str:
        """由源码内容和位置生成稳定短标识。"""
        digest = hashlib.sha256(f"{position}:{raw}".encode()).hexdigest()[:12]
        return f"{prefix}-{digest}"

    def add_element(
        self,
        kind,
        markdown: str,
        nodes: list[LatexNode],
        visual_ids: list[str] | None = None,
        repair_issue_codes: list[str] | None = None,
        identity_suffix: str | None = None,
    ) -> ParsedElement | None:
        """规整并添加结构元素；空内容不产生 chunk。"""
        normalized = markdown.strip()
        if not normalized:
            return None
        raw = "".join(self.raw(node) for node in nodes)
        active_raw = mask_macro_scan_source(raw)
        position = nodes[0].pos if nodes else len(self.session.elements)
        identity = raw or normalized
        if identity_suffix is not None:
            identity = f"{identity}\0{identity_suffix}"
        element = ParsedElement(
            element_id=self.make_id(kind, identity, position),
            kind=kind,
            markdown=normalized,
            heading_path=list(self.session.heading_path),
            locators=self.unique_locators(nodes),
            citation_keys=citations(active_raw),
            labels=labels(active_raw),
            visual_ids=visual_ids or [],
            repair_issue_codes=repair_issue_codes or [],
            reference_keys=references(active_raw),
            semantic_heading_path=list(self.session.heading_path),
        )
        self.session.elements.append(element)
        return element

    def macro_value(self, nodes: Iterable[LatexNode], name: str) -> str:
        """查找首个指定宏并渲染其最后参数。"""
        for node in walk_nodes(nodes):
            if isinstance(node, LatexMacroNode) and node.macroname == name:
                return clean_inline(self.renderer.argument(last_argument(node)))
        return ""

    def macro_values(self, nodes: Iterable[LatexNode], names: set[str]) -> list[str]:
        """从活动 AST 收集宏参数，comment 节点和 comment 环境不会进入结果。"""
        values: list[str] = []
        for node in walk_nodes(nodes):
            if not isinstance(node, LatexMacroNode) or node.macroname not in names:
                continue
            value = clean_inline(self.renderer.argument(last_argument(node)))
            if value and value not in values:
                values.append(value)
        return values

    def author_values(self, nodes: Iterable[LatexNode]) -> list[str]:
        """优先读取显式加粗作者名，否则按常见 ``and`` 作者组恢复。"""
        for node in walk_nodes(nodes):
            if not isinstance(node, LatexMacroNode) or node.macroname != "author":
                continue
            argument = last_argument(node)
            if argument is None:
                return []
            bold_names = [
                clean_author_name(self.renderer.argument(last_argument(child)))
                for child in walk_nodes(getattr(argument, "nodelist", []))
                if isinstance(child, LatexMacroNode) and child.macroname == "textbf"
            ]
            bold_names = list(dict.fromkeys(name for name in bold_names if name))
            if bold_names:
                return bold_names
            raw = self.raw(argument)
            if isinstance(argument, LatexGroupNode) and len(raw) >= 2:
                raw = raw[1:-1]
            values: list[str] = []
            groups = re.split(r"\\(?:and|And|AND)\b", raw)
            if len(groups) == 1:
                groups = [re.split(r"\\\\(?:\[[^]]*\])?", raw, maxsplit=1)[0]]
            for group in groups:
                first_line = re.split(r"\\\\(?:\[[^]]*\])?", group, maxsplit=1)[0]
                parts = re.split(r"\\quad\b|;", first_line)
                for part in parts:
                    if not part.strip():
                        continue
                    try:
                        part_nodes, _, _ = LatexWalker(
                            part, latex_context=self.context
                        ).get_latex_nodes()
                        rendered = clean_author_name(
                            TexRenderer(part, self.context, self.custom_macros).nodes(
                                part_nodes
                            )
                        )
                    except Exception:
                        rendered = clean_author_name(part)
                    if rendered and "@" not in rendered:
                        values.append(rendered)
            return list(dict.fromkeys(values))
        return []

    def update_heading(self, level: int, title: str) -> None:
        """更新当前章节祖先路径。"""
        self.session.heading_path = self.session.heading_path[: level - 1]
        self.session.heading_path.append(title)

    def resolve_asset(
        self, requested: str, source_file: str | None
    ) -> tuple[str, bytes] | None:
        """从 TeX 源码包中解析 includegraphics 路径和省略的扩展名。"""
        requested = requested.strip().replace("\\", "/")
        current_dir = (
            PurePosixPath(source_file).parent if source_file else PurePosixPath(".")
        )
        bases = [
            current_dir,
            *(current_dir / path for path in self.session.graphic_paths),
            PurePosixPath("."),
        ]
        choices: list[str] = []
        for base in bases:
            candidate = base / requested
            names = (
                [candidate]
                if candidate.suffix
                else [
                    candidate.with_suffix(extension) for extension in FIGURE_EXTENSIONS
                ]
            )
            for name in names:
                normalized = str(name).removeprefix("./")
                if normalized not in choices:
                    choices.append(normalized)
        for choice in choices:
            if choice in self.source.files:
                return choice, self.source.files[choice]
        basename = PurePosixPath(requested).stem
        matches = [
            path
            for path in self.source.files
            if PurePosixPath(path).stem == basename
            and PurePosixPath(path).suffix.lower() in FIGURE_EXTENSIONS
        ]
        if len(matches) == 1:
            return matches[0], self.source.files[matches[0]]
        return None

    def caption_and_label(self, node: LatexEnvironmentNode) -> tuple[str, str | None]:
        """提取 figure/table 环境的长 caption 与 label。"""
        caption = ""
        label = None
        for child in walk_nodes(node.nodelist):
            if (
                isinstance(child, LatexMacroNode)
                and child.macroname == "caption"
                and not caption
            ):
                caption = clean_inline(self.renderer.argument(last_argument(child)))
            if (
                isinstance(child, LatexMacroNode)
                and child.macroname == "label"
                and label is None
            ):
                label = clean_inline(self.renderer.argument(last_argument(child)))
        return caption, label

    def parse_figure(self, node: LatexEnvironmentNode) -> None:
        """恢复 figure 的原始资源，并留下供 VLM 解释的占位符。"""
        caption, label = self.caption_and_label(node)
        include_nodes = [
            child
            for child in walk_nodes(node.nodelist)
            if isinstance(child, LatexMacroNode)
            and child.macroname == "includegraphics"
        ]
        visual_ids: list[str] = []
        if not include_nodes:
            visual_id = self.make_id("figure", self.raw(node), node.pos)
            visual_ids.append(visual_id)
            self.session.visuals.append(
                ParsedVisual(
                    visual_id=visual_id,
                    kind="figure",
                    locator=self.locator(node),
                    element_id="",
                    label=label,
                    caption=caption,
                    structured_text=self.raw(node),
                    surrounding_text=caption,
                )
            )
            self.session.diagnostics.append(
                ProcessingDiagnostic(
                    level="warning",
                    code="tex_figure_without_asset",
                    message="Figure has no recoverable includegraphics asset; the model receives its TeX source.",
                    locator=self.locator(node),
                )
            )
        for index, include_node in enumerate(include_nodes, start=1):
            requested = clean_inline(
                self.renderer.argument(last_argument(include_node))
            )
            visual_id = self.make_id(
                "figure", f"{label}:{requested}:{index}", include_node.pos
            )
            visual_ids.append(visual_id)
            locator = self.locator(include_node)
            resolved = self.resolve_asset(requested, locator.file)
            asset_bytes = resolved[1] if resolved else None
            media_type = media_type_for_path(resolved[0]) if resolved else None
            if resolved is None:
                self.session.diagnostics.append(
                    ProcessingDiagnostic(
                        level="warning",
                        code="tex_figure_asset_missing",
                        message=f"Could not resolve figure asset '{requested}'.",
                        locator=locator,
                    )
                )
            self.session.visuals.append(
                ParsedVisual(
                    visual_id=visual_id,
                    kind="figure",
                    locator=locator,
                    element_id="",
                    label=(
                        label
                        if len(include_nodes) == 1
                        else f"{label or 'figure'}:{index}"
                    ),
                    caption=caption,
                    asset_bytes=asset_bytes,
                    asset_media_type=media_type,
                    structured_text=None if asset_bytes else caption,
                    surrounding_text=caption,
                )
            )
        markdown = f"**{label or 'Figure'}**: {caption}\n\n" + "\n".join(
            VISUAL_TOKEN.format(visual_id=visual_id) for visual_id in visual_ids
        )
        element = self.add_element("figure", markdown, [node], visual_ids)
        if element:
            for visual in self.session.visuals[-len(visual_ids) :]:
                visual.element_id = element.element_id

    def parse_table(self, node: LatexEnvironmentNode) -> None:
        """把 tabular 尽量还原为 Markdown 表格，并交给模型生成检索解释。"""
        caption, label = self.caption_and_label(node)
        tabular = next(
            (
                child
                for child in walk_nodes(node.nodelist)
                if isinstance(child, LatexEnvironmentNode)
                and child.environmentname in TABULAR_ENVIRONMENTS
            ),
            None,
        )
        table_markdown = (
            table_to_markdown(tabular, self.renderer, self.context, self.custom_macros)
            if tabular
            else ""
        )
        if not table_markdown:
            table_markdown = f"```latex\n{self.raw(node).strip()}\n```"
            self.session.diagnostics.append(
                ProcessingDiagnostic(
                    level="warning",
                    code="tex_table_complex_fallback",
                    message="Complex TeX table was preserved as source for model-assisted interpretation.",
                    locator=self.locator(node),
                )
            )
        visual_id = self.make_id("table", label or self.raw(node), node.pos)
        markdown = f"**{label or 'Table'}**: {caption}\n\n{table_markdown}\n\n{VISUAL_TOKEN.format(visual_id=visual_id)}"
        element = self.add_element("table", markdown, [node], [visual_id])
        if element is None:
            return
        self.session.visuals.append(
            ParsedVisual(
                visual_id=visual_id,
                kind="table",
                locator=self.locator(node),
                element_id=element.element_id,
                label=label,
                caption=caption,
                structured_text=table_markdown,
                surrounding_text=caption,
            )
        )

    def parse_nodes(self, nodes: list[LatexNode]) -> None:
        """按论文结构把 document 节点分割成 RAG 元素。"""
        nodes = content_nodes(nodes)
        buffer: list[LatexNode] = []

        def buffer_is_ignorable() -> bool:
            """标题与后置 label 之间只允许空白或注释。"""
            return all(
                isinstance(node, LatexCommentNode)
                or (isinstance(node, LatexCharsNode) and not node.chars.strip())
                or (
                    isinstance(node, LatexEnvironmentNode)
                    and node.environmentname in IGNORED_ENVIRONMENTS
                )
                for node in buffer
            )

        def flush() -> None:
            if not buffer:
                return
            rendered = self.renderer.nodes(buffer)
            parts = [
                clean_inline(part)
                for part in re.split(r"\n\s*\n", rendered)
                if clean_inline(part)
            ]
            for index, part in enumerate(parts):
                suffix = f"part:{index}" if len(parts) > 1 else None
                self.add_element(
                    "paragraph", part, list(buffer), identity_suffix=suffix
                )
            buffer.clear()

        for node in nodes:
            if isinstance(node, LatexMacroNode) and node.macroname in SECTION_LEVELS:
                flush()
                level = min(5, SECTION_LEVELS[node.macroname] + int(self.session.in_appendix))
                title = clean_inline(self.renderer.argument(last_argument(node)))
                if title:
                    self.update_heading(level, title)
                    self.add_element("heading", f"{'#' * (level + 1)} {title}", [node])
                else:
                    self.session.diagnostics.append(
                        ProcessingDiagnostic(
                            level="warning",
                            code="tex_heading_empty",
                            message=f"Ignored an empty {node.macroname} heading.",
                            locator=self.locator(node),
                        )
                    )
            elif isinstance(node, LatexMacroNode) and node.macroname == "appendix":
                flush()
                self.session.in_appendix = True
                self.update_heading(1, "Appendix")
                self.add_element("heading", "## Appendix", [node])
            elif isinstance(node, LatexMacroNode) and node.macroname == "label":
                previous = self.session.elements[-1] if self.session.elements else None
                label = clean_inline(self.renderer.argument(last_argument(node)))
                if (
                    previous is not None
                    and previous.kind == "heading"
                    and buffer_is_ignorable()
                    and label
                ):
                    if label not in previous.labels:
                        previous.labels.append(label)
                    locator = self.locator(node)
                    if locator not in previous.locators:
                        previous.locators.append(locator)
                    buffer.clear()
                else:
                    buffer.append(node)
            elif (
                isinstance(node, LatexEnvironmentNode)
                and node.environmentname == "abstract"
            ):
                flush()
                abstract = clean_inline(self.renderer.nodes(node.nodelist))
                self.add_element("abstract", f"## Abstract\n\n{abstract}", [node])
            elif isinstance(node, LatexEnvironmentNode) and node.environmentname in {
                "figure",
                "figure*",
            }:
                flush()
                self.parse_figure(node)
            elif (
                isinstance(node, LatexEnvironmentNode)
                and node.environmentname in TABLE_ENVIRONMENTS
            ):
                flush()
                self.parse_table(node)
            elif (
                isinstance(node, LatexEnvironmentNode)
                and node.environmentname in MATH_ENVIRONMENTS
            ):
                flush()
                self.add_element(
                    "equation",
                    math_to_markdown(self.raw(node), node.environmentname),
                    [node],
                )
            elif (
                isinstance(node, LatexEnvironmentNode)
                and node.environmentname in LIST_ENVIRONMENTS
            ):
                flush()
                self.add_element("list", render_list(node, self.renderer), [node])
            elif (
                isinstance(node, LatexEnvironmentNode)
                and node.environmentname in CODE_ENVIRONMENTS
            ):
                flush()
                body = environment_body(self.raw(node), node.environmentname).strip()
                self.add_element("code", f"```text\n{body}\n```", [node])
            elif (
                isinstance(node, LatexEnvironmentNode)
                and node.environmentname in REFERENCE_ENVIRONMENTS
            ):
                flush()
                bibliography, keys = bibliography_to_markdown(node, self.renderer)
                self.add_bibliography_markdown(
                    bibliography,
                    keys,
                    self.unique_locators([node]),
                    f"inline:{self.raw(node)}",
                    node.pos,
                )
            elif isinstance(node, LatexMacroNode) and node.macroname in {
                "bibliography",
                "addbibresource",
            }:
                flush()
                if self.session.external_bibliography is not None:
                    self.add_external_bibliography()
                else:
                    self.add_element("bibliography", self.raw(node), [node])
                    self.session.diagnostics.append(
                        ProcessingDiagnostic(
                            level="warning",
                            code="tex_bibliography_unresolved",
                            message="Bibliography declaration was preserved because no parseable .bbl file was supplied.",
                            locator=self.locator(node),
                        )
                    )
            else:
                buffer.append(node)
        flush()

    def infer_float_semantic_headings(self) -> None:
        """用最近的同顶层引用上下文推断浮动体语义路径，不改变源码顺序。"""
        reference_contexts: dict[str, list[tuple[int, list[str]]]] = {}
        for index, element in enumerate(self.session.elements):
            for key in element.reference_keys:
                reference_contexts.setdefault(key, []).append(
                    (index, list(element.heading_path))
                )
        for index, element in enumerate(self.session.elements):
            if (
                element.kind not in {"figure", "table"}
                or not element.labels
                or not element.heading_path
            ):
                continue
            candidates: list[tuple[int, int, list[str]]] = []
            for label in element.labels:
                for reference_index, path in reference_contexts.get(label, []):
                    if (
                        reference_index == index
                        or not path
                        or path[0] != element.heading_path[0]
                    ):
                        continue
                    candidates.append(
                        (
                            abs(reference_index - index),
                            0 if reference_index > index else 1,
                            path,
                        )
                    )
            if not candidates:
                continue
            best_distance = min(distance for distance, _, _ in candidates)
            best_direction = min(
                direction
                for distance, direction, _ in candidates
                if distance == best_distance
            )
            best_paths = {
                tuple(path)
                for distance, direction, path in candidates
                if distance == best_distance and direction == best_direction
            }
            if len(best_paths) != 1:
                self.session.diagnostics.append(
                    ProcessingDiagnostic(
                        level="warning",
                        code="tex_float_semantic_heading_ambiguous",
                        message=f"Could not infer one semantic heading for {element.element_id}.",
                        locator=element.locators[0] if element.locators else None,
                    )
                )
                continue
            semantic_path = list(next(iter(best_paths)))
            if semantic_path == element.heading_path:
                continue
            element.semantic_heading_path = semantic_path
            self.session.diagnostics.append(
                ProcessingDiagnostic(
                    level="info",
                    code="tex_float_semantic_heading_inferred",
                    message=(
                        f"Mapped {element.element_id} from {' / '.join(element.heading_path)} "
                        f"to {' / '.join(semantic_path)} using its nearest reference context."
                    ),
                    locator=element.locators[0] if element.locators else None,
                )
            )

    def parse(self) -> ParsedPaper:
        """完成 TeX AST 解析并返回统一内存态论文。"""
        try:
            nodes, _, _ = LatexWalker(
                self.source.text, latex_context=self.context
            ).get_latex_nodes()
        except Exception as error:
            raise ValueError(f"Failed to parse TeX source: {error}") from error
        title = normalize_title(self.macro_value(nodes, "title"))
        authors = self.author_values(nodes)
        document = next(
            (
                node
                for node in walk_nodes(nodes)
                if isinstance(node, LatexEnvironmentNode)
                and node.environmentname == "document"
            ),
            None,
        )
        body_nodes = document.nodelist if document else nodes
        if document is None:
            self.session.diagnostics.append(
                ProcessingDiagnostic(
                    level="warning",
                    code="tex_document_environment_missing",
                    message="No document environment was found; parsed the selected entrypoint as a fragment.",
                )
            )
        source_labels = self.macro_values(body_nodes, {"label"})
        source_reference_keys = list(
            dict.fromkeys(
                key.strip()
                for value in self.macro_values(
                    body_nodes, {"ref", "eqref", "autoref", "cref", "Cref"}
                )
                for key in value.split(",")
                if key.strip()
            )
        )
        if title:
            first = body_nodes[0] if body_nodes else nodes[0]
            self.add_element(
                "front_matter", f"# {title}", [first], identity_suffix="title"
            )
        if authors:
            first = body_nodes[0] if body_nodes else nodes[0]
            self.add_element(
                "front_matter",
                "**Authors:** " + "; ".join(authors),
                [first],
                identity_suffix="authors",
            )
        self.parse_nodes(body_nodes)
        self.add_external_bibliography()
        self.infer_float_semantic_headings()
        if not self.session.elements:
            raise ValueError("TeX source contains no recoverable paper content.")
        abstract_element = next(
            (element for element in self.session.elements if element.kind == "abstract"), None
        )
        bibliography_elements = [
            element.markdown
            for element in self.session.elements
            if element.kind == "bibliography"
        ]
        bibliography = ""
        if bibliography_elements:
            bibliography = "## References\n\n" + "\n\n".join(bibliography_elements)
        source_hash = hashlib.sha256()
        for path in sorted(self.source.files):
            source_hash.update(path.encode("utf-8"))
            source_hash.update(b"\0")
            source_hash.update(self.source.files[path])
            source_hash.update(b"\0")
        fingerprint = source_hash.hexdigest()
        return ParsedPaper(
            source_kind="tex",
            source_fingerprint=fingerprint,
            converter=f"pylatexenc-{pylatexenc.__version__}",
            title=title,
            authors=authors,
            abstract=(
                abstract_element.markdown.removeprefix("## Abstract").strip()
                if abstract_element
                else ""
            ),
            elements=self.session.elements,
            visuals=self.session.visuals,
            diagnostics=self.session.diagnostics,
            bibliography=bibliography,
            source_labels=source_labels,
            source_reference_keys=source_reference_keys,
        )


def media_type_for_path(path: str) -> str:
    """按图像扩展名返回媒体类型。"""
    extension = PurePosixPath(path).suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".eps": "application/postscript",
    }.get(extension, "application/octet-stream")


def table_to_markdown(
    node: LatexEnvironmentNode | None,
    renderer: TexRenderer,
    context,
    custom_macros: dict[str, str] | None = None,
) -> str:
    """把规则 tabular 转为 Markdown；无法安全切分时返回空字符串。"""
    if node is None:
        return ""
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
                        TexRenderer(cell_source, context, custom_macros).nodes(nodes)
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
                                TexRenderer(cell_source, context).argument(arguments[0])
                            )
                        ),
                    )
                except (IndexError, ValueError):
                    # multicolumn 跨列参数缺失或非整数 → 按单列保留内容
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


def parse_tex_paper(source: ExpandedTex) -> ParsedPaper:
    """公开入口：把已展开源码包解析为统一 ``ParsedPaper``。"""
    return TexPaperParser(source).parse()
