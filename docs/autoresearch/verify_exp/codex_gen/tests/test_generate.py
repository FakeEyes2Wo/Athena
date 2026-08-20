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


if __name__ == "__main__":
    unittest.main()
