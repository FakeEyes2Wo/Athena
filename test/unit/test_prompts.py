"""Unit tests for the shared English prompt templates."""

import unittest

from athena.workflows.prompts import (
    FALSIFIABILITY_CHECK_SYSTEM_PROMPT,
    FALSIFIABILITY_CHECK_USER_PROMPT_TEMPLATE,
    IDEA_GENERATOR_SYSTEM_PROMPT,
    IDEA_GENERATOR_USER_PROMPT_TEMPLATE,
)


class PromptTemplatesTest(unittest.TestCase):
    def test_all_templates_are_english_only(self) -> None:
        for text in (
            IDEA_GENERATOR_SYSTEM_PROMPT,
            IDEA_GENERATOR_USER_PROMPT_TEMPLATE,
            FALSIFIABILITY_CHECK_SYSTEM_PROMPT,
            FALSIFIABILITY_CHECK_USER_PROMPT_TEMPLATE,
        ):
            self.assertTrue(text.isascii(), f"prompt must be English-only: {text!r}")

    def test_idea_generator_user_prompt_formats(self) -> None:
        rendered = IDEA_GENERATOR_USER_PROMPT_TEMPLATE.format(
            question="Does X affect Y?",
            domain="biology",
            objective="find a testable mechanism",
            constraints="- none",
            evidence="ev-0: prior study found ...",
        )
        self.assertIn("Does X affect Y?", rendered)
        self.assertIn("ev-0", rendered)

    def test_falsifiability_user_prompt_formats(self) -> None:
        rendered = FALSIFIABILITY_CHECK_USER_PROMPT_TEMPLATE.format(
            novel_hypothesis="X causes Y",
            predicted_observations="- Y increases",
            disconfirming_observations="- Y stays flat",
        )
        self.assertIn("X causes Y", rendered)

    def test_idea_generator_prompt_forbids_non_premise_roles_in_supported_premises(self) -> None:
        # 真实调用发现 Qwen 会把 prediction 角色的声明塞进 supported_premises，导致
        # structural_check 判定零合格前提；这条测试锁定 prompt 里明确禁止这种做法的措辞，
        # 防止后续改写时又把这条约束改没了
        self.assertIn("supported_premise", IDEA_GENERATOR_SYSTEM_PROMPT)
        self.assertIn("predicted_observations", IDEA_GENERATOR_SYSTEM_PROMPT)
