"""Unit tests for the demo_pre_gate CLI argument parsing (no real LLM calls)."""

import unittest

from athena.workflows.search.demo_pre_gate import parse_args


class ParseArgsTest(unittest.TestCase):
    def test_parses_required_and_repeated_flags(self) -> None:
        problem, verbose = parse_args([
            "--question", "Does X affect Y?",
            "--domain", "biology",
            "--objective", "find a mechanism",
            "--evidence", "fact one",
            "--evidence", "fact two",
            "--constraint", "no animal testing",
        ])
        self.assertEqual("Does X affect Y?", problem.question)
        self.assertEqual(["fact one", "fact two"], problem.evidence_texts)
        self.assertEqual(["no animal testing"], problem.constraints)
        self.assertFalse(verbose)

    def test_defaults_empty_lists(self) -> None:
        problem, verbose = parse_args(["--question", "q", "--domain", "d", "--objective", "o"])
        self.assertEqual([], problem.evidence_texts)
        self.assertEqual([], problem.constraints)
        self.assertFalse(verbose)

    def test_verbose_flag_sets_true(self) -> None:
        _, verbose = parse_args([
            "--question", "q", "--domain", "d", "--objective", "o", "--verbose",
        ])
        self.assertTrue(verbose)
