"""Unit tests for Verbalized Sampling candidate generation and dedup."""

import unittest

from athena.workflows.search.candidate_generation import (
    MAX_VERBALIZED_SAMPLES,
    deduplicate_candidates,
    generate_candidates,
)
from athena.workflows.search.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    HypothesisDraft,
    HypothesisPackage,
    ResearchProblemInput,
    VerbalizedSamplingResponse,
)
from unit.fakes import make_scripted_model


def _problem() -> ResearchProblemInput:
    return ResearchProblemInput(
        question="Does X affect Y?", domain="biology", objective="find a mechanism",
        evidence_texts=["X correlates with Y in mice."],
    )


def _draft(statement: str, probability: float) -> HypothesisDraft:
    return HypothesisDraft(
        statement=statement, intervention="knock out X", expected_effect="Y decreases",
        generation_strategy="verbalized_sampling_v1", sampling_probability=probability,
        supported_premises=[
            ClaimEvidence(claim="X correlates with Y", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        inference_chain=[], predicted_observations=["Y decreases"],
        disconfirming_observations=["Y stays flat"],
    )


def _package(novel_hypothesis: str, probability: float = 1.0, idea_id: str = "idea-1") -> HypothesisPackage:
    return HypothesisPackage(
        idea_id=idea_id, generation_strategy="verbalized_sampling_v1", novel_hypothesis=novel_hypothesis,
        sampling_probability=probability, supported_premises=[], inference_chain=[],
        predicted_observations=["p"], disconfirming_observations=["d"], lineage_op="generate",
    )


class GenerateCandidatesTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_one_package_per_draft(self) -> None:
        model = make_scripted_model([
            VerbalizedSamplingResponse(candidates=[_draft("X causes Y", 0.8), _draft("X inhibits Y", 0.3)])
        ])
        packages = await generate_candidates(_problem(), [], model=model)
        self.assertEqual(2, len(packages))
        self.assertEqual({"X causes Y", "X inhibits Y"}, {p.novel_hypothesis for p in packages})

    async def test_carries_over_sampling_probability(self) -> None:
        model = make_scripted_model([VerbalizedSamplingResponse(candidates=[_draft("X causes Y", 0.8)])])
        packages = await generate_candidates(_problem(), [], model=model)
        self.assertEqual(0.8, packages[0].sampling_probability)

    async def test_assigns_unique_idea_ids(self) -> None:
        model = make_scripted_model([
            VerbalizedSamplingResponse(candidates=[_draft("X causes Y", 0.8), _draft("X inhibits Y", 0.3)])
        ])
        packages = await generate_candidates(_problem(), [], model=model)
        self.assertNotEqual(packages[0].idea_id, packages[1].idea_id)

    async def test_rejects_sample_size_above_maximum(self) -> None:
        with self.assertRaises(ValueError):
            await generate_candidates(_problem(), [], sample_size=MAX_VERBALIZED_SAMPLES + 1)

    async def test_rejects_sample_size_below_one(self) -> None:
        with self.assertRaises(ValueError):
            await generate_candidates(_problem(), [], sample_size=0)


class DeduplicateCandidatesTest(unittest.TestCase):
    def test_near_duplicate_statements_collapse_to_one(self) -> None:
        packages = [_package("X causes Y in mice", 0.9), _package("X causes Y in mice indeed", 0.5, "idea-2")]
        kept = deduplicate_candidates(packages)
        self.assertEqual(1, len(kept))

    def test_keeps_the_higher_probability_candidate(self) -> None:
        packages = [_package("X causes Y in mice", 0.4, "idea-1"), _package("X causes Y in mice", 0.9, "idea-2")]
        kept = deduplicate_candidates(packages)
        self.assertEqual(1, len(kept))
        self.assertEqual("idea-2", kept[0].idea_id)

    def test_distinct_statements_are_both_kept(self) -> None:
        packages = [_package("X causes Y", 0.5, "idea-1"), _package("Z inhibits W", 0.5, "idea-2")]
        kept = deduplicate_candidates(packages)
        self.assertEqual(2, len(kept))

    def test_empty_input_returns_empty_list(self) -> None:
        self.assertEqual([], deduplicate_candidates([]))
