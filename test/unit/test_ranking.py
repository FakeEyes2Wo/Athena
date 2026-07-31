"""Unit tests for the pure-algorithm HypoPriList Elo ranking module."""

import unittest

from pydantic import ValidationError

from athena.research.ranking import HypoPriList, PairwiseComparison, RANKING_RUBRIC_VERSION


class PairwiseComparisonTest(unittest.TestCase):
    def test_winner_must_be_a_participant(self) -> None:
        with self.assertRaises(ValidationError):
            PairwiseComparison(idea_id_a="a", idea_id_b="b", winner_id="c", rationale="r")

    def test_participants_must_differ(self) -> None:
        with self.assertRaises(ValidationError):
            PairwiseComparison(idea_id_a="a", idea_id_b="a", winner_id="a", rationale="r")

    def test_well_formed_comparison_is_accepted(self) -> None:
        comparison = PairwiseComparison(idea_id_a="a", idea_id_b="b", winner_id="a", rationale="r")
        self.assertEqual("a", comparison.winner_id)


class HypoPriListTest(unittest.TestCase):
    def test_empty_book_ranks_to_empty_list(self) -> None:
        self.assertEqual([], HypoPriList().rank())

    def test_ensure_registered_surfaces_candidate_with_zero_comparisons(self) -> None:
        book = HypoPriList()
        book.ensure_registered("solo")
        ranking = book.rank()
        self.assertEqual(1, len(ranking))
        self.assertEqual("solo", ranking[0].idea_id)
        self.assertEqual(0, ranking[0].comparisons)

    def test_winner_rating_increases_and_loser_decreases(self) -> None:
        book = HypoPriList()
        book.record_comparison(
            PairwiseComparison(idea_id_a="a", idea_id_b="b", winner_id="a", rationale="a is more falsifiable")
        )
        ranking = {entry.idea_id: entry for entry in book.rank()}
        self.assertGreater(ranking["a"].rating, ranking["b"].rating)
        self.assertEqual(1, ranking["a"].comparisons)
        self.assertEqual(1, ranking["b"].comparisons)

    def test_rank_is_sorted_descending_by_rating(self) -> None:
        book = HypoPriList()
        book.record_comparison(
            PairwiseComparison(idea_id_a="a", idea_id_b="b", winner_id="a", rationale="r1")
        )
        book.record_comparison(
            PairwiseComparison(idea_id_a="a", idea_id_b="c", winner_id="a", rationale="r2")
        )
        ranking = book.rank()
        self.assertEqual("a", ranking[0].idea_id)

    def test_evidence_accumulates_rationale_strings(self) -> None:
        book = HypoPriList()
        book.record_comparison(
            PairwiseComparison(idea_id_a="a", idea_id_b="b", winner_id="a", rationale="first reason")
        )
        ranking = {entry.idea_id: entry for entry in book.rank()}
        self.assertIn("first reason", ranking["a"].evidence)

    def test_rank_carries_rubric_version(self) -> None:
        book = HypoPriList()
        book.ensure_registered("solo")
        self.assertEqual(RANKING_RUBRIC_VERSION, book.rank()[0].rubric_version)

    def test_deterministic_tiebreak_by_idea_id(self) -> None:
        book = HypoPriList()
        book.ensure_registered("b")
        book.ensure_registered("a")
        ranking = book.rank()
        self.assertEqual(["a", "b"], [entry.idea_id for entry in ranking])
