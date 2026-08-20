from __future__ import annotations

import sys
import unittest
from pathlib import Path

CODEX_GEN = Path(__file__).resolve().parents[1]
INPUTS = CODEX_GEN / "inputs"
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


if __name__ == "__main__":
    unittest.main()
