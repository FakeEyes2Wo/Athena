"""Unit tests for VerifierRegistry and ValidationPlanner."""

import tempfile
import unittest

from athena.storage import LocalArtifactStore
from athena.workflows.search.idea_schemas import HypothesisPackage
from athena.workflows.search.validation import BUILTIN_VERIFIERS, match_verifier, plan_validation


def _package(sampling_probability: float = 0.9) -> HypothesisPackage:
    return HypothesisPackage(
        idea_id="idea-1", generation_strategy="s", novel_hypothesis="X causes Y",
        sampling_probability=sampling_probability, supported_premises=[], inference_chain=[],
        predicted_observations=["Y increases"], disconfirming_observations=["Y stays flat"],
        lineage_op="generate",
    )


class VerifierRegistryTest(unittest.TestCase):
    def test_matches_known_domain(self) -> None:
        verifier = match_verifier(_package(), "biology")
        self.assertIsNotNone(verifier)
        self.assertEqual("controlled_experiment_ttest", verifier.verifier_type)

    def test_returns_none_for_unknown_domain(self) -> None:
        self.assertIsNone(match_verifier(_package(), "underwater_basket_weaving"))

    def test_domain_matching_is_case_and_space_insensitive(self) -> None:
        verifier = match_verifier(_package(), "Machine Learning")
        self.assertIsNotNone(verifier)
        self.assertEqual("ablation_replication", verifier.verifier_type)

    def test_builtin_registry_is_non_empty(self) -> None:
        self.assertGreater(len(BUILTIN_VERIFIERS), 0)


class PlanValidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_none_verifier_produces_exploratory_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            plan = await plan_validation(_package(), None, artifacts=artifacts)
            self.assertIsNone(plan.verifier)
            self.assertIn("EXPLORATORY", plan.decision_rule)
            self.assertTrue(plan.estimated_cost_ref.startswith("sha256:"))

    async def test_matched_verifier_produces_bound_plan_with_real_cost_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            verifier = match_verifier(_package(), "biology")
            plan = await plan_validation(_package(), verifier, artifacts=artifacts)
            self.assertIsNotNone(plan.verifier)
            self.assertTrue(plan.verifier.cost_ref.startswith("sha256:"))
            self.assertTrue(plan.estimated_cost_ref.startswith("sha256:"))
