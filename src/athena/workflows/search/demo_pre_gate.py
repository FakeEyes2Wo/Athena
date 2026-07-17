"""CLI demo：手动跑一遍 [1]→[4] 的 pre_gate 闭环，接真实 LLM（需要配置好 provider 的 API key）。

用法：
    python -m athena.workflows.search.demo_pre_gate \
        --question "Does X affect Y?" --domain biology \
        --objective "find a testable mechanism" \
        --evidence "Prior study found X correlates with Y in mice." \
        --evidence "Another paper reports a dose-dependent effect." \
        --verbose
"""

import argparse
import asyncio

from athena.workflows.search.idea_generation_service import run_pre_gate
from athena.workflows.search.idea_schemas import ResearchProblemInput


def parse_args(argv: list[str] | None = None) -> tuple[ResearchProblemInput, bool]:
    """把命令行参数解析成 (ResearchProblemInput, verbose)，不涉及任何 LLM 调用（方便单测）。

    Example:
        >>> parse_args(["--question", "q", "--domain", "d", "--objective", "o"])[0].question
        'q'
        >>> parse_args(["--question", "q", "--domain", "d", "--objective", "o"])[1]
        False
    """
    parser = argparse.ArgumentParser(description="Run the Idea Generation pre_gate demo.")
    parser.add_argument("--question", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--constraint", action="append", default=[], dest="constraints")
    parser.add_argument("--evidence", action="append", default=[], dest="evidence_texts")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Also print the full HypothesisPackage (premises, inference chain, predictions).",
    )
    args = parser.parse_args(argv)
    problem = ResearchProblemInput(
        question=args.question,
        domain=args.domain,
        objective=args.objective,
        constraints=args.constraints,
        evidence_texts=args.evidence_texts,
    )
    return problem, args.verbose


async def main(argv: list[str] | None = None) -> None:
    """demo 入口：解析参数、跑一遍闭环、把 HypothesisPackage 与 GateDecision 打印成人类可读格式。

    --verbose 时额外打印 HypothesisPackage 的中间细节（premises/inference_chain/预测/反证），
    方便排查 pre_gate 为什么判 REVISE。
    """
    problem, verbose = parse_args(argv)
    node, package, decision = await run_pre_gate(problem)

    print(f"[HYPOTHESIS] {node.node_id}: {package.novel_hypothesis}")
    print(f"  status={node.status}")

    if verbose:
        print(f"[PACKAGE] generation_strategy={package.generation_strategy} lineage_op={package.lineage_op}")
        print("  supported_premises:")
        for premise in package.supported_premises:
            refs = ", ".join(premise.supporting_refs) or "-"
            print(f"    - [{premise.role.value}] {premise.claim} (refs: {refs})")
        print("  inference_chain:")
        for step in package.inference_chain:
            print(f"    - {step.step_id}: {step.operator} {step.from_premises} -> {step.to_claim} "
                  f"(uncertainty={step.uncertainty})")
        print("  predicted_observations:")
        for observation in package.predicted_observations:
            print(f"    - {observation}")
        print("  disconfirming_observations:")
        for observation in package.disconfirming_observations:
            print(f"    - {observation}")

    print(f"[GATE] phase={decision.gate_phase} verdict={decision.verdict.value} "
          f"rubric={decision.rubric_version}")
    for item in decision.item_scores:
        print(f"  - {item.item}: {item.score} ({item.evidence})")
    if decision.blocking_factor:
        print(f"  blocking_factor={decision.blocking_factor}")


if __name__ == "__main__":
    asyncio.run(main())
