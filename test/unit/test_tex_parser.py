"""TeX-to-RAG-Markdown parser tests."""

import io
import tarfile
import unittest

import fitz

from athena.research.paper_markdown.chunking import build_chunks
from athena.research.paper_markdown.schemas import ChunkingConfig
from athena.research.paper_markdown.tex_parser import normalize_title, parse_tex_paper
from athena.research.paper_markdown.tex_source import load_tex_source


def png_bytes() -> bytes:
    """Create a valid source figure PNG."""
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 16, 16), False)
    pixmap.clear_with(180)
    return pixmap.tobytes("png")


def source_tar(files: dict[str, bytes]) -> bytes:
    """Build an in-memory TeX source package."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class TexParserTest(unittest.TestCase):
    def setUp(self) -> None:
        main = r"""\documentclass{article}
\title{Retrieval Paper}
\author{Alice \and Bob}
\graphicspath{{figures/}}
\begin{document}
\maketitle
\begin{abstract}We preserve $x^2$ and \cite{smith2024,doe2025}.\end{abstract}
\input{sections/body}
\begin{figure}
\includegraphics[width=.5\textwidth]{architecture}
\caption{System architecture.}\label{fig:arch}
\end{figure}
\begin{table}
\caption{Scores.}\label{tab:scores}
\begin{tabular}{lr}
Method & Score \\
Athena & $0.91$ \\
\end{tabular}
\end{table}
\begin{thebibliography}{9}\bibitem{smith2024} Smith. Paper.\end{thebibliography}
\end{document}
"""
        body = r"""\section{Method}\label{sec:method}
Text with \textbf{bold evidence}, Eq.~\eqref{eq:loss}, a \SI{1}{meter} span, and a footnote\footnote{detail}.
\begin{equation}\label{eq:loss}\mathcal{L}=\sum_i x_i^2\end{equation}
\begin{itemize}\item First claim.\item Second claim.\end{itemize}
"""
        package = source_tar(
            {
                "main.tex": main.encode(),
                "sections/body.tex": body.encode(),
                "figures/architecture.png": png_bytes(),
            }
        )
        self.paper = parse_tex_paper(load_tex_source(package))

    def test_recovers_front_matter_abstract_and_source_locations(self) -> None:
        self.assertEqual("Retrieval Paper", self.paper.title)
        self.assertEqual(["Alice", "Bob"], self.paper.authors)
        self.assertIn("$x^2$", self.paper.abstract)
        method = next(
            element for element in self.paper.elements if element.kind == "heading"
        )
        self.assertEqual("sections/body.tex", method.locators[0].file)
        self.assertEqual(["Method"], method.heading_path)

    def test_preserves_math_citations_labels_lists_and_formatting(self) -> None:
        markdown = "\n\n".join(element.markdown for element in self.paper.elements)
        abstract = next(
            element for element in self.paper.elements if element.kind == "abstract"
        )
        equation = next(
            element for element in self.paper.elements if element.kind == "equation"
        )

        self.assertIn("[@smith2024; @doe2025]", markdown)
        self.assertEqual(["smith2024", "doe2025"], abstract.citation_keys)
        self.assertIn("\\mathcal{L}=\\sum_i x_i^2", equation.markdown)
        self.assertIn("eq:loss", equation.labels)
        self.assertIn("**bold evidence**", markdown)
        self.assertIn("1 meter", markdown)
        self.assertIn("^[detail]", markdown)
        self.assertIn("- First claim.", markdown)

    def test_element_ids_are_unique_and_chunk_offsets_round_trip(self) -> None:
        tex = r"""\title{Stable Paper}\author{Alice}
\begin{document}\maketitle
\section{Method}
Repeated paragraph.

Repeated paragraph.
\end{document}"""
        paper = parse_tex_paper(load_tex_source(tex.encode(), "plain"))
        element_ids = [element.element_id for element in paper.elements]
        markdown, chunks = build_chunks(paper.elements, ChunkingConfig())

        self.assertEqual(len(element_ids), len(set(element_ids)))
        for chunk in chunks:
            text = (
                chunk.content_text.split("\n\n", 1)[1]
                if chunk.content_text.startswith("> Section: ")
                else chunk.content_text
            )
            self.assertEqual(text, markdown[chunk.char_start : chunk.char_end])

    def test_heading_labels_ignore_comments_and_survive_without_body_text(self) -> None:
        tex = r"""\begin{document}
\section{Selector Details}\label{selector_detail}
See Appendix~\ref{selector_detail}.
% \section{Disabled}\label{ablation_study}\cite{commented2024}
\begin{comment}\label{comment_environment}\end{comment}
\section{Additional Results}
\label{exp_rst_appx}
\end{document}"""

        paper = parse_tex_paper(load_tex_source(tex.encode(), "plain"))
        headings = {
            element.markdown: element
            for element in paper.elements
            if element.kind == "heading"
        }
        persisted_labels = {
            label for element in paper.elements for label in element.labels
        }
        persisted_citations = {
            key for element in paper.elements for key in element.citation_keys
        }
        persisted_references = {
            key for element in paper.elements for key in element.reference_keys
        }

        self.assertEqual(["selector_detail"], headings["## Selector Details"].labels)
        self.assertEqual(["exp_rst_appx"], headings["## Additional Results"].labels)
        self.assertEqual({"selector_detail", "exp_rst_appx"}, persisted_labels)
        self.assertNotIn("commented2024", persisted_citations)
        self.assertEqual({"selector_detail"}, persisted_references)
        self.assertEqual(["selector_detail", "exp_rst_appx"], paper.source_labels)
        self.assertEqual(["selector_detail"], paper.source_reference_keys)

    def test_float_semantic_heading_uses_nearest_same_root_reference(self) -> None:
        tex = r"""\begin{document}
\section{Experiments}
\subsection{Setup}
\begin{table}\caption{Main scores.}\label{main_scores}
\begin{tabular}{lr}Method & Score \\ PaSa & 1.0 \\ \end{tabular}\end{table}
\subsection{Results}
Table~\ref{main_scores} reports the main result.
\end{document}"""

        paper = parse_tex_paper(load_tex_source(tex.encode(), "plain"))
        table = next(element for element in paper.elements if element.kind == "table")

        self.assertEqual(["Experiments", "Setup"], table.heading_path)
        self.assertEqual(["Experiments", "Results"], table.semantic_heading_path)
        self.assertIn(
            "main_scores",
            {key for element in paper.elements for key in element.reference_keys},
        )
        self.assertIn(
            "tex_float_semantic_heading_inferred",
            {item.code for item in paper.diagnostics},
        )

    def test_float_semantic_heading_does_not_cross_top_level_sections(self) -> None:
        tex = r"""\begin{document}
\section{Dataset}
\begin{figure}\caption{Dataset overview.}\label{dataset_figure}\end{figure}
\section{Experiments}
Figure~\ref{dataset_figure} is reused here.
\end{document}"""

        paper = parse_tex_paper(load_tex_source(tex.encode(), "plain"))
        figure = next(element for element in paper.elements if element.kind == "figure")

        self.assertEqual(["Dataset"], figure.heading_path)
        self.assertEqual(["Dataset"], figure.semantic_heading_path)

    def test_eqnarray_is_normalized_to_valid_aligned_math(self) -> None:
        tex = r"""\begin{document}
\begin{eqnarray}\label{eq:return}
R_t &=& r_t + \gamma V(s_{t+1}) \\
&& {} - \beta K_t \nonumber
\end{eqnarray}
\end{document}"""
        paper = parse_tex_paper(load_tex_source(tex.encode(), "plain"))
        equation = next(
            element for element in paper.elements if element.kind == "equation"
        )

        self.assertIn("\\begin{aligned}", equation.markdown)
        self.assertIn("R_t &= r_t", equation.markdown)
        self.assertNotIn("&=&", equation.markdown)
        self.assertNotIn("&&", equation.markdown)
        self.assertNotIn("\\label", equation.markdown)
        self.assertNotIn("\\nonumber", equation.markdown)
        self.assertEqual(["eq:return"], equation.labels)

    def test_recovers_figure_asset_and_markdown_table(self) -> None:
        figure = next(
            visual for visual in self.paper.visuals if visual.kind == "figure"
        )
        table = next(visual for visual in self.paper.visuals if visual.kind == "table")

        self.assertEqual(png_bytes(), figure.asset_bytes)
        self.assertEqual("image/png", figure.asset_media_type)
        self.assertEqual("fig:arch", figure.label)
        self.assertIn("| Method | Score |", table.structured_text or "")
        self.assertIn("| Athena | $0.91$ |", table.structured_text or "")

    def test_bibliography_is_preserved(self) -> None:
        self.assertIn("Smith. Paper.", self.paper.bibliography)

    def test_figure_without_asset_and_complex_table_leave_diagnostics(self) -> None:
        tex = r"""\begin{document}
\begin{figure}\caption{Generated diagram}\label{fig:x}\end{figure}
\begin{table}\caption{Complex}\label{tab:x}Raw custom content\end{table}
\end{document}"""
        paper = parse_tex_paper(load_tex_source(tex.encode(), "plain"))

        codes = {item.code for item in paper.diagnostics}
        self.assertIn("tex_figure_without_asset", codes)
        self.assertIn("tex_table_complex_fallback", codes)
        self.assertEqual(2, len(paper.visuals))

    def test_empty_tex_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "no recoverable paper content"):
            parse_tex_paper(load_tex_source(b"", "plain"))

    def test_expands_zero_argument_macros_and_ignores_commented_author(self) -> None:
        main = r"""\documentclass{article}
\usepackage{comment}
\def\pasa{PaSa\xspace}
\newcommand{\autoS}{AutoScholarQuery\xspace}
\providecommand{\realS}{RealScholarQuery\xspace}
\title{\pasa: Academic Search}
\begin{comment}
\author{First Author \\ Placeholder University}
\end{comment}
\author{
  \textbf{Alice$^*$}\textsuperscript{1}\quad
  \textbf{Bob}\textsuperscript{2}\\
  \textsuperscript{1}Research Lab \quad \textsuperscript{2}Example University\\
  \texttt{alice@example.org}}
\begin{document}
\maketitle
\begin{abstract}\pasa is trained on \autoS and evaluated on \realS.\end{abstract}
\section{Results on \autoS}
\begin{table}
\caption{\pasa results on \realS.}\label{tab:results}
\begin{tabular}{lcc}
\toprule[1pt]
\multirow{2}{*}{\textbf{Method}} & \multicolumn{2}{c}{\textbf{Scores}} \\
& Precision & Recall \\
\midrule[0.5pt]
\pasa-7b & 0.9 & 0.8 \\
\bottomrule[1pt]
\end{tabular}
\end{table}
\bibliography{references}
\end{document}
"""
        bibliography = r"""\begin{thebibliography}{2}
\bibitem[{Smith(2024)}]{smith2024}
Alice Smith. 2024. \newblock A retrieval paper. \newblock \emph{Journal of Search}.
\bibitem[{Doe(2025)}]{doe2025}
Bob Doe. 2025. \newblock A ranking paper.
\end{thebibliography}
"""
        package = source_tar(
            {"paper.tex": main.encode(), "paper.bbl": bibliography.encode()}
        )

        paper = parse_tex_paper(load_tex_source(package))
        markdown = "\n\n".join(element.markdown for element in paper.elements)
        table = next(visual for visual in paper.visuals if visual.kind == "table")

        self.assertEqual("PaSa: Academic Search", paper.title)
        self.assertEqual(["Alice", "Bob"], paper.authors)
        self.assertIn(
            "PaSa is trained on AutoScholarQuery and evaluated on RealScholarQuery.",
            paper.abstract,
        )
        self.assertIn("## Results on AutoScholarQuery", markdown)
        self.assertIn("PaSa results on RealScholarQuery.", table.caption)
        self.assertIn("| PaSa-7b | 0.9 | 0.8 |", table.structured_text or "")
        self.assertNotIn("[1pt]", table.structured_text or "")
        self.assertNotIn("2 *", table.structured_text or "")
        self.assertNotIn("2 c", table.structured_text or "")
        self.assertIn("[@smith2024]", paper.bibliography)
        self.assertIn("A retrieval paper.", paper.bibliography)
        self.assertIn("[@doe2025]", paper.bibliography)
        bibliography_elements = [
            element for element in paper.elements if element.kind == "bibliography"
        ]
        self.assertEqual(
            [["smith2024"], ["doe2025"]],
            [element.citation_keys for element in bibliography_elements],
        )
        self.assertTrue(
            any(element.markdown == "## References" for element in paper.elements)
        )

    def test_preserves_paragraph_titles_and_appendix_hierarchy(self) -> None:
        tex = r"""\begin{document}
\section{Method}
\paragraph{Reward Design}Reward details.
\appendix
\begin{table}\caption{Appendix data.}\label{tab:appendix}
\begin{tabular}{lc}Name & Value \\ A & 1 \\ \end{tabular}
\end{table}
\section{Implementation Details}Appendix prose.
\end{document}"""

        paper = parse_tex_paper(load_tex_source(tex.encode(), "plain"))
        headings = [element for element in paper.elements if element.kind == "heading"]
        table = next(element for element in paper.elements if element.kind == "table")

        self.assertIn("##### Reward Design", [element.markdown for element in headings])
        self.assertNotIn("#####", [element.markdown for element in headings])
        self.assertIn("## Appendix", [element.markdown for element in headings])
        self.assertIn(
            "### Implementation Details", [element.markdown for element in headings]
        )
        self.assertEqual(["Appendix"], table.heading_path)
        details = next(
            element
            for element in headings
            if "Implementation Details" in element.markdown
        )
        self.assertEqual(["Appendix", "Implementation Details"], details.heading_path)

    def test_flattens_multicolumn_headers_and_encodes_cell_line_breaks(self) -> None:
        tex = r"""\begin{document}
\begin{table}\caption{Structured results.}\label{tab:structured}
\begin{tabular}{lcccccc}
\textbf{Method} & \multicolumn{3}{c}{\textbf{Auto}} & \multicolumn{3}{c}{\textbf{Real}} \\
& \textbf{Crawler Recall} & \textbf{Precision} & \textbf{Recall}
& \textbf{Crawler Recall} & \textbf{Precision} & \textbf{Recall} \\
PaSa & 0.8 & 0.7 & 0.75 & 0.7 & 0.6 & 0.65 \\
\end{tabular}\end{table}
\begin{table}\caption{Prompt text.}\label{tab:prompt}
\begin{tabular}{ll}Name & Prompt \\ Crawler & First line\newline Second line \\ \end{tabular}
\end{table}
\end{document}"""

        paper = parse_tex_paper(load_tex_source(tex.encode(), "plain"))
        tables = {
            visual.label: visual.structured_text or "" for visual in paper.visuals
        }

        self.assertIn("**Auto** / **Crawler Recall**", tables["tab:structured"])
        self.assertIn("**Real** / **Precision**", tables["tab:structured"])
        self.assertNotIn("|  |", tables["tab:structured"].splitlines()[0])
        self.assertIn("First line<br>Second line", tables["tab:prompt"])
        self.assertTrue(
            all(line.startswith("|") for line in tables["tab:prompt"].splitlines())
        )

    def test_multicolumn_continuations_do_not_duplicate_cell_text(self) -> None:
        tex = r"""\begin{document}
\begin{table}\caption{Hyperparameters.}\label{tab:params}
\begin{tabular}{lll}
\multicolumn{2}{c}{\textbf{Name}} & \textbf{Value} \\
$\alpha$ & (Equation~\ref{eq:alpha}) & 1.5 \\
\multicolumn{2}{l}{learning rate} & 1e-6 \\
\end{tabular}\end{table}
\end{document}"""

        paper = parse_tex_paper(load_tex_source(tex.encode(), "plain"))
        table = next(visual for visual in paper.visuals if visual.label == "tab:params")
        markdown = table.structured_text or ""

        self.assertIn("| **Name** | **Name** (continued) | **Value** |", markdown)
        self.assertIn("| learning rate |  | 1e-6 |", markdown)
        self.assertNotIn("| learning rate | learning rate |", markdown)

    def test_preserves_footnotetext_after_footnote_marker_definition(self) -> None:
        tex = r"""\begin{document}
\def\thefootnote{$*$}\footnotetext{Equal contribution.}
\def\thefootnote{$\dagger$}\footnotetext{Corresponding author.}
\renewcommand{\thefootnote}{\arabic{footnote}}
Body text.
\end{document}"""

        paper = parse_tex_paper(load_tex_source(tex.encode(), "plain"))
        markdown = "\n\n".join(element.markdown for element in paper.elements)

        self.assertIn("^[Equal contribution.]", markdown)
        self.assertIn("^[Corresponding author.]", markdown)
        self.assertIn("Body text.", markdown)
        self.assertNotIn("thefootnote", markdown)
        self.assertNotIn("arabicfootnote", markdown)

    def test_removes_nested_tabular_layout_declarations(self) -> None:
        tex = r"""\begin{document}
\begin{table}
\caption{Query example.}\label{tab:query}
\begin{tabular}{|p{1.25\textwidth}|}
\hline
\begin{tabular}[c]{@{}p{1.25\textwidth}@{}}
\textbf{Query:} Find retrieval papers.\\
\textbf{Answer:} A relevant paper.
\end{tabular}\\
\hline
\end{tabular}
\end{table}
\end{document}"""

        paper = parse_tex_paper(load_tex_source(tex.encode(), "plain"))
        table = next(visual for visual in paper.visuals if visual.kind == "table")
        structured_text = table.structured_text or ""

        self.assertIn("**Query:** Find retrieval papers.", structured_text)
        self.assertIn("**Answer:** A relevant paper.", structured_text)
        self.assertNotIn("p1.25", structured_text)
        self.assertNotIn("c]@", structured_text)
        self.assertNotIn("begin tabular", structured_text)


class NormalizeTitleTest(unittest.TestCase):
    r"""Authors put typesetting inside ``\title{}``: venue lines, placeholder icons
    and line breaks. The rendered value reaches ``PaperContent.title`` and the
    ``upstream_metadata`` paper_source uses for identity checks, so it must be
    flattened to the title itself."""

    def test_custom_macro_image_path_is_dropped(self):
        self.assertEqual(
            normalize_title("images/icon-no-border.jpg Interleaving Retrieval"),
            "Interleaving Retrieval",
        )

    def test_venue_line_is_dropped(self):
        self.assertEqual(
            normalize_title("ACL 2023\nWhen Not to Trust Language Models"),
            "When Not to Trust Language Models",
        )
        self.assertEqual(
            normalize_title("NeurIPS 2020\nDense Passage Retrieval"),
            "Dense Passage Retrieval",
        )

    def test_wrapped_title_lines_are_joined(self):
        self.assertEqual(
            normalize_title("Few-shot Learning with\nRetrieval Augmented Models"),
            "Few-shot Learning with Retrieval Augmented Models",
        )

    def test_inline_markup_is_stripped(self):
        self.assertEqual(
            normalize_title("`ReAct`: Synergizing Reasoning"),
            "ReAct: Synergizing Reasoning",
        )

    def test_ordinary_titles_are_untouched(self):
        for title in (
            "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
            "BERT: Pre-training of Deep Bidirectional Transformers",
            "Attention Is All You Need",
        ):
            self.assertEqual(normalize_title(title), title)
