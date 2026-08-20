from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

CODEX_GEN = Path(__file__).resolve().parents[1]
INPUTS = CODEX_GEN / "inputs"
TEST_OUTPUTS = CODEX_GEN / "tests" / "_outputs"
sys.path.insert(0, str(CODEX_GEN))

import generate  # noqa: E402


class PromptParserTests(unittest.TestCase):
    def test_parse_first_prompt(self) -> None:
        parser = getattr(generate, "parse_prompt", None)
        self.assertIsNotNone(parser, "parse_prompt must be implemented")
        text = (INPUTS / "01-candidate-to-paper-pipeline.md").read_text(
            encoding="utf-8"
        )
        spec = parser(text, "01-candidate-to-paper-pipeline")
        self.assertEqual(spec.title, "Candidate-to-Paper Autonomous Research Pipeline")
        self.assertEqual(len(spec.nodes), 10)
        self.assertEqual(len(spec.edges), 9)
        self.assertEqual(len(spec.groups), 2)

    def test_all_inputs_reference_known_nodes(self) -> None:
        parser = getattr(generate, "parse_prompt", None)
        self.assertIsNotNone(parser, "parse_prompt must be implemented")
        self.assertEqual(len(list(INPUTS.glob("*.md"))), 10)
        for path in sorted(INPUTS.glob("*.md")):
            spec = parser(path.read_text(encoding="utf-8"), path.stem)
            names = {node.label for node in spec.nodes}
            self.assertTrue(
                all(
                    edge.source in names and edge.target in names for edge in spec.edges
                ),
                path.name,
            )
            self.assertTrue(
                all(set(group.members) <= names for group in spec.groups),
                path.name,
            )

    def test_rejects_dangling_edge(self) -> None:
        parser = getattr(generate, "parse_prompt", None)
        self.assertIsNotNone(parser, "parse_prompt must be implemented")
        text = """---
title: "Broken"
nodes:
  - "Known|box"
edges:
  - "Known -> Missing"
groups:
annotations:
---
Broken body.
"""
        with self.assertRaisesRegex(ValueError, "unknown node"):
            parser(text, "broken")


class DrawioXmlTests(unittest.TestCase):
    def test_all_specs_build_valid_editable_xml(self) -> None:
        builder = getattr(generate, "build_drawio_xml", None)
        validator = getattr(generate, "validate_drawio_xml", None)
        self.assertIsNotNone(builder, "build_drawio_xml must be implemented")
        self.assertIsNotNone(validator, "validate_drawio_xml must be implemented")
        for path in sorted(INPUTS.glob("*.md")):
            spec = generate.parse_prompt(path.read_text(encoding="utf-8"), path.stem)
            xml = builder(spec, {})
            check = validator(xml)
            self.assertTrue(check["xml_ok"], (path.name, check))
            self.assertEqual(check["external_urls"], [], path.name)
            self.assertGreaterEqual(check["vertices"], len(spec.nodes), path.name)
            self.assertEqual(check["edges"], len(spec.edges), path.name)
            self.assertGreaterEqual(
                check["editable_labels"], len(spec.nodes) + 1, path.name
            )

    def test_layout_has_no_node_overlap(self) -> None:
        builder = getattr(generate, "build_drawio_xml", None)
        validator = getattr(generate, "validate_drawio_xml", None)
        self.assertIsNotNone(builder, "build_drawio_xml must be implemented")
        self.assertIsNotNone(validator, "validate_drawio_xml must be implemented")
        for path in sorted(INPUTS.glob("*.md")):
            spec = generate.parse_prompt(path.read_text(encoding="utf-8"), path.stem)
            check = validator(builder(spec, {}))
            self.assertEqual(check["node_overlaps"], [], path.name)

    def test_layout_hints_cover_each_node_once(self) -> None:
        layouts = getattr(generate, "LAYOUT_ROWS", None)
        self.assertIsNotNone(layouts, "LAYOUT_ROWS must be implemented")
        for path in sorted(INPUTS.glob("*.md")):
            spec = generate.parse_prompt(path.read_text(encoding="utf-8"), path.stem)
            laid_out = [label for row in layouts[path.stem] for label in row]
            self.assertCountEqual(laid_out, [node.label for node in spec.nodes])
            self.assertEqual(len(laid_out), len(set(laid_out)), path.name)


class ExportValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_OUTPUTS.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TEST_OUTPUTS, ignore_errors=True)

    def test_validates_png_svg_and_pdf_signatures(self) -> None:
        validator = getattr(generate, "validate_export_file", None)
        self.assertIsNotNone(validator, "validate_export_file must be implemented")
        png = TEST_OUTPUTS / "sample.png"
        svg = TEST_OUTPUTS / "sample.svg"
        pdf = TEST_OUTPUTS / "sample.pdf"
        png.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (640).to_bytes(4, "big")
            + (360).to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00"
            + b"\x00\x00\x00\x00"
        )
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
        pdf.write_bytes(b"%PDF-1.7\n")
        self.assertEqual(validator(png, "png")["dimensions"], [640, 360])
        self.assertTrue(validator(svg, "svg")["ok"])
        self.assertTrue(validator(pdf, "pdf")["ok"])


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        shutil.rmtree(TEST_OUTPUTS, ignore_errors=True)
        TEST_OUTPUTS.mkdir(parents=True)

    async def asyncTearDown(self) -> None:
        shutil.rmtree(TEST_OUTPUTS, ignore_errors=True)

    @staticmethod
    def _fake_export(path: Path, drawio_cli: Path) -> dict[str, object]:
        del drawio_cli
        files: dict[str, str] = {}
        for fmt in ("png", "svg", "pdf"):
            target = path.with_suffix(f".{fmt}")
            target.write_bytes(b"test-export")
            files[fmt] = target.name
        return {"ok": True, "files": files, "formats": {}}

    async def test_pipeline_writes_required_evidence(self) -> None:
        runner = getattr(generate, "run_pipeline", None)
        self.assertIsNotNone(runner, "run_pipeline must be implemented")
        with (
            mock.patch.object(generate, "search_shapes", AsyncMock(return_value={})),
            mock.patch.object(
                generate,
                "create_diagram",
                AsyncMock(
                    return_value={
                        "ok": True,
                        "xml": None,
                        "build_id": "test-build",
                        "error": None,
                    }
                ),
            ),
            mock.patch.object(generate, "export_drawio", side_effect=self._fake_export),
        ):
            report = await runner(INPUTS, TEST_OUTPUTS, Path("DrawIO.exe"))
        self.assertEqual(report["total"], 10)
        self.assertEqual(report["mcp_passed"], 10)
        self.assertEqual(report["passed"], 10)
        stem = "01-candidate-to-paper-pipeline"
        self.assertTrue((TEST_OUTPUTS / f"{stem}.drawio").exists())
        self.assertTrue((TEST_OUTPUTS / f"{stem}.mcp.json").exists())
        self.assertTrue((TEST_OUTPUTS / f"{stem}.check.json").exists())
        evidence = json.loads(
            (TEST_OUTPUTS / f"{stem}.mcp.json").read_text(encoding="utf-8")
        )
        self.assertEqual(evidence["build_id"], "test-build")

    async def test_pipeline_records_mcp_failure_without_stopping(self) -> None:
        runner = getattr(generate, "run_pipeline", None)
        self.assertIsNotNone(runner, "run_pipeline must be implemented")
        with (
            mock.patch.object(generate, "search_shapes", AsyncMock(return_value={})),
            mock.patch.object(
                generate,
                "create_diagram",
                AsyncMock(
                    return_value={
                        "ok": False,
                        "xml": None,
                        "build_id": None,
                        "error": "offline",
                    }
                ),
            ),
            mock.patch.object(generate, "export_drawio", side_effect=self._fake_export),
        ):
            report = await runner(INPUTS, TEST_OUTPUTS, Path("DrawIO.exe"))
        self.assertEqual(report["total"], 10)
        self.assertEqual(report["failed"], 10)
        self.assertTrue(
            all(item["mcp"]["error"] == "offline" for item in report["files"])
        )


if __name__ == "__main__":
    unittest.main()
