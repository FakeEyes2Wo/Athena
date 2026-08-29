"""Run a small synthetic DeepSeek contract probe without research data.

The probe deliberately avoids project files and datasets.  It verifies the
same provider/schema boundaries used by Ideator semantic correction, the
Layer 2 batch rubric, and VALIDATE while reporting only pass/fail metadata.
"""

import asyncio
import json
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.agents.validate_agent import ValidationRepair
from athena.core.agent import settings
from athena.core.agent.models import AgentConfig
from athena.core.agent.provider import DeepSeekProvider
from athena.core.agent.runtime import _semantic_structured_retry
from athena.core.tool import ToolRegistry
from athena.research.idea_generation.idea_schemas import IdeatorHypothesisBatch
from athena.research.rubrics.models import HypothesisPriorityBatch

ContractT = TypeVar("ContractT", bound=BaseModel)


async def _structured_call(
    provider: DeepSeekProvider,
    contract: type[ContractT],
    prompt: str,
) -> tuple[ContractT, bool]:
    """Make one tool-free structured call and validate its exact contract."""

    text = ""
    reasoning_received = False
    messages = [ModelRequest(parts=[UserPromptPart(content=prompt)])]
    async for event in provider.stream(
        AgentConfig(max_turns=1, max_tokens=1400, temperature=0.0),
        ToolRegistry(),
        messages,
        asyncio.Event(),
        output_type=contract,
    ):
        if event.kind == "reasoning_delta":
            reasoning_received = True
        elif event.kind == "error":
            raise RuntimeError(str(event.data.get("message") or "provider error"))
        elif event.kind == "response_completed":
            text = str(event.data.get("accumulated_text") or "")
    if not text.strip():
        raise RuntimeError(f"{contract.__name__} probe returned no text")
    return contract.model_validate_json(text), reasoning_received


async def main() -> None:
    """Execute three bounded synthetic calls and print safe metadata only."""

    if settings.provider_kind() != "deepseek":
        raise RuntimeError("this probe requires LLM_PROVIDER=deepseek")

    client = settings.get_client()
    normal = DeepSeekProvider(settings.model_name(), client=client)
    reasoning = DeepSeekProvider(
        settings.pro_model_name(), client=client, thinking=True
    )
    report: dict[str, object] = {
        "provider": "deepseek",
        "normal_model": normal.model_name,
        "reasoning_model": reasoning.model_name,
        "synthetic_only": True,
    }
    try:
        try:
            IdeatorHypothesisBatch.model_validate({"hypotheses": []})
        except ValidationError as validation_error:
            repaired = await _semantic_structured_retry(
                provider=reasoning,
                config=AgentConfig(max_turns=1, max_tokens=1400, temperature=0.0),
                output_type=IdeatorHypothesisBatch,
                history=[
                    ModelRequest(
                        parts=[
                            UserPromptPart(
                                content=(
                                    "Synthetic task only: a table has numeric feature x "
                                    "and binary target y. Propose exactly one falsifiable "
                                    "modeling hypothesis. No files or external evidence "
                                    "are available; leave sources and supported premises "
                                    "empty. Include a prediction and a disconfirmer."
                                )
                            )
                        ]
                    )
                ],
                validation_error=validation_error,
                cancel=asyncio.Event(),
            )
        report["ideator_semantic_correction"] = {
            "status": "PASSED",
            "hypothesis_count": len(repaired.hypotheses),
        }

        ranking, ranking_reasoning = await _structured_call(
            reasoning,
            HypothesisPriorityBatch,
            (
                "Synthetic Layer 2 review. Return exactly one review for each ID h1 "
                "and h2, with no duplicates or omissions. h1 tests median imputation; "
                "h2 tests a missingness indicator. Score evidence/testability, "
                "scientific value, resource penalties, validity/risk control, and "
                "confidence in [0,1]. Use concise explanations and no evidence refs."
            ),
        )
        ranking_ids = [review.hypothesis_id for review in ranking.reviews]
        if sorted(ranking_ids) != ["h1", "h2"]:
            raise RuntimeError(f"Layer 2 returned unexpected IDs: {ranking_ids}")
        report["layer2_batch"] = {
            "status": "PASSED",
            "review_count": len(ranking.reviews),
            "thinking_received": ranking_reasoning,
        }

        validation, validation_reasoning = await _structured_call(
            normal,
            ValidationRepair,
            (
                "Synthetic VALIDATE contract check only. State that a proposed repair "
                "would re-run the already defined held-out evaluator without changing "
                "its frozen metric or labels. Return no other fields."
            ),
        )
        report["validate_output"] = {
            "status": "PASSED",
            "explanation_present": bool(validation.explanation.strip()),
            "thinking_received": validation_reasoning,
        }
        report["status"] = "PASSED"
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
