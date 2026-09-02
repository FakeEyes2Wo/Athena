"""Safe TeX source-package loading tests."""

import gzip
import io
import tarfile
import unittest
import zipfile

from athena.research.literature.paper_markdown.tex_source import (
    TexSourceError,
    load_tex_source,
)


def tar_bytes(files: dict[str, bytes]) -> bytes:
    """Create an in-memory tar source package for tests."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def zip_bytes(files: dict[str, bytes]) -> bytes:
    """Create an in-memory zip source package for tests."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class TexSourceTest(unittest.TestCase):
    def test_plain_source_uses_requested_entrypoint(self) -> None:
        source = load_tex_source(b"\\section{Intro}", "plain", "paper.tex")

        self.assertEqual("paper.tex", source.entrypoint)
        self.assertEqual("\\section{Intro}", source.text)

    def test_auto_detects_tar_and_expands_input_with_line_mapping(self) -> None:
        package = tar_bytes(
            {
                "main.tex": b"\\documentclass{article}\n\\begin{document}\n\\input{sections/method}\n\\end{document}",
                "sections/method.tex": b"\\section{Method}\nBody",
            }
        )

        source = load_tex_source(package)

        self.assertIn("\\section{Method}", source.text)
        method_index = source.text.splitlines().index("\\section{Method}")
        self.assertEqual("sections/method.tex", source.line_map[method_index].file)
        self.assertEqual(1, source.line_map[method_index].line)

    def test_auto_detects_zip_and_gzip_plain(self) -> None:
        zipped = load_tex_source(
            zip_bytes({"main.tex": b"\\begin{document}Z\\end{document}"})
        )
        gzipped = load_tex_source(
            gzip.compress(b"\\begin{document}G\\end{document}"), "gzip"
        )

        self.assertIn("Z", zipped.text)
        self.assertIn("G", gzipped.text)

    def test_rejects_path_traversal_and_symlink_members(self) -> None:
        with self.assertRaises(TexSourceError):
            load_tex_source(zip_bytes({"../escape.tex": b"bad"}))
        with self.assertRaises(TexSourceError):
            load_tex_source(zip_bytes({"C:/escape.tex": b"bad"}))

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo("link.tex")
            info.type = tarfile.SYMTYPE
            info.linkname = "main.tex"
            archive.addfile(info)
        with self.assertRaises(TexSourceError):
            load_tex_source(buffer.getvalue())

    def test_reports_missing_include_and_latin1_fallback(self) -> None:
        source = load_tex_source(
            tar_bytes(
                {"main.tex": b"\\documentclass{article}\n\\input{missing}\nCaf\xe9"}
            )
        )

        self.assertIn("\\input{missing}", source.text)
        self.assertEqual(
            {"tex_include_missing", "tex_latin1_fallback"},
            {item.code for item in source.diagnostics},
        )
        self.assertEqual(
            1, sum(item.code == "tex_latin1_fallback" for item in source.diagnostics)
        )

    def test_includegraphics_is_not_treated_as_tex_include(self) -> None:
        source = load_tex_source(
            tar_bytes(
                {
                    "main.tex": b"\\begin{document}\n\\includegraphics[width=1.0\\textwidth]{figures/plot.pdf}\n\\end{document}",
                    "figures/plot.pdf": b"source figure",
                }
            )
        )

        self.assertIn("\\includegraphics", source.text)
        self.assertNotIn(
            "tex_include_missing", {item.code for item in source.diagnostics}
        )

    def test_detects_include_cycle_and_missing_entrypoint(self) -> None:
        package = tar_bytes(
            {"main.tex": b"\\input{part}", "part.tex": b"\\input{main}"}
        )
        with self.assertRaises(TexSourceError):
            load_tex_source(package)
        with self.assertRaises(TexSourceError):
            load_tex_source(tar_bytes({"main.tex": b"x"}), entrypoint="missing.tex")


class IncludeCaseResolutionTest(unittest.TestCase):
    r"""arXiv packages are usually built on case-insensitive filesystems.

    Authors write ``\input{prompts/ALFWorld}`` while the file on disk is
    ``alfworld.tex``; a case-sensitive resolver silently drops that file's content
    and leaves the raw macro in the body. ReAct (arXiv:2210.03629) lost 6789
    characters of appendix prompts this way.
    """

    def test_exact_case_resolves_without_a_diagnostic(self):
        package = tar_bytes(
            {
                "main.tex": rb"\begin{document}\input{parts/body}\end{document}",
                "parts/body.tex": b"Exact case content.",
            }
        )
        source = load_tex_source(package)
        self.assertIn("Exact case content.", source.text)
        self.assertEqual(
            [], [d for d in source.diagnostics if d.code == "tex_include_case_mismatch"]
        )

    def test_case_mismatch_is_recovered_and_reported(self):
        package = tar_bytes(
            {
                "main.tex": rb"\begin{document}\input{prompts/ALFWorld}\end{document}",
                "prompts/alfworld.tex": b"You are in the middle of a room.",
            }
        )
        source = load_tex_source(package)
        codes = [d.code for d in source.diagnostics]
        self.assertIn("You are in the middle of a room.", source.text)
        self.assertIn("tex_include_case_mismatch", codes)
        self.assertNotIn("tex_include_missing", codes)

    def test_a_genuinely_absent_include_is_still_missing(self):
        package = tar_bytes(
            {"main.tex": rb"\begin{document}\input{nowhere/at/all}\end{document}"}
        )
        source = load_tex_source(package)
        self.assertIn("tex_include_missing", [d.code for d in source.diagnostics])

    def test_exact_match_wins_over_a_case_variant(self):
        package = tar_bytes(
            {
                "main.tex": rb"\begin{document}\input{parts/Body}\end{document}",
                "parts/Body.tex": b"Exact wins.",
                "parts/body.tex": b"Variant loses.",
            }
        )
        source = load_tex_source(package)
        self.assertIn("Exact wins.", source.text)
        self.assertNotIn("Variant loses.", source.text)

    def test_ambiguous_case_variants_resolve_deterministically(self):
        package = tar_bytes(
            {
                "main.tex": rb"\begin{document}\input{parts/BODY}\end{document}",
                "parts/Body.tex": b"Capital B.",
                "parts/body.tex": b"Lowercase b.",
            }
        )
        first = load_tex_source(package).text
        second = load_tex_source(package).text
        self.assertEqual(first, second)
        self.assertIn("Capital B.", first)
