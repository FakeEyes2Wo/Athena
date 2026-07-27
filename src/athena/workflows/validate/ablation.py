from athena.core.schemas import Hypothesis, EvalResult
from athena.core.research.research_tree import ResearchTree, Experiment


class Validator:
    """Independent validation of SOTA results."""

    async def ablate(
        self, sota_node_id: str, tree: ResearchTree, code_agent
    ) -> list[tuple[Hypothesis, EvalResult]]:
        """Remove each intervention on the SOTA path and measure impact."""
        path = tree.hypotheses_path(sota_node_id)
        results = []
        for h in path:
            # Checkout parent commit and re-run without this intervention
            parent_node = tree.get_node_by_id(h.parent_id) if h.parent_id else None
            if parent_node is None:
                continue
            # Re-run from parent commit
            result = await code_agent.execute(
                h, parent_node.exp.commit, None, parent_node.exp.gitwork
            )
            results.append((h, result.eval))
        return results

    async def final_test(
        self, sota_node_id: str, tree: ResearchTree, code_agent
    ) -> EvalResult:
        """Evaluate on held-out test set. Run once."""
        sota = tree.get_node_by_id(sota_node_id)
        result = await code_agent.execute(
            sota.exp.hypothesis, sota.exp.commit, None, sota.exp.gitwork
        )
        return result.eval
