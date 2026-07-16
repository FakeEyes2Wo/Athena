"""Unit tests for the AcademicSurvey data contracts and RRF fusion."""

import unittest

from pydantic import ValidationError

from athena.research.academic_survey import (
    CriterionJudgment,
    PaperRecord,
    RelevanceJudgment,
    SurveyCorpus,
    SurveyRequest,
    rrf_fuse,
)


def make_judgment(verdict: str = "relevant") -> RelevanceJudgment:
    return RelevanceJudgment(
        rubric_version="relevance-v1",
        criteria=[
            CriterionJudgment(
                criterion="Studies agentic paper retrieval",
                verdict="met",
                evidence="Title mentions multi-agent scholar paper retrieval.",
            )
        ],
        verdict=verdict,
    )


class SchemaTest(unittest.TestCase):
    def test_corpus_roundtrip(self) -> None:
        paper = PaperRecord(
            paper_id="arXiv:2507.15245",
            title="SPAR",
            abstract="Multi-agent scholar paper retrieval.",
            judgment=make_judgment(),
            rank_score=0.42,
        )
        corpus = SurveyCorpus(
            request_ref="artifact://survey/request/1",
            prompt_bundle_version="bundle-v1",
            papers=[paper],
            stats_ref="artifact://survey/stats/1",
        )

        self.assertIsNone(corpus.papers[0].content)
        self.assertEqual(corpus, SurveyCorpus.model_validate(corpus.model_dump()))

    def test_judgment_requires_known_verdict(self) -> None:
        with self.assertRaises(ValidationError):
            make_judgment(verdict="maybe")

    def test_request_requires_known_mode(self) -> None:
        with self.assertRaises(ValidationError):
            SurveyRequest(topic="agentic retrieval", mode="exhaustive")


class RrfFuseTest(unittest.TestCase):
    def test_consensus_paper_wins(self) -> None:
        scores = rrf_fuse([["a", "b", "c"], ["b", "a"], ["b"]])

        self.assertEqual("b", max(scores, key=scores.get))
        self.assertAlmostEqual(1 / 62 + 1 / 61 + 1 / 61, scores["b"])
        self.assertAlmostEqual(1 / 61 + 1 / 62, scores["a"])
        self.assertAlmostEqual(1 / 63, scores["c"])
