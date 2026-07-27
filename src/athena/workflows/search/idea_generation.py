"""Search papers and models, generate falsifiable hypotheses for the SEARCH loop."""

from pydantic import BaseModel, Field
from athena.core.schemas import ArtifactRef, Hypothesis


class PaperRef(BaseModel):
    title: str
    source: str  # "arxiv" | "semantic_scholar" | "web"
    url: str = ""
    key_findings: str = ""
    relevance: str = ""
    markdown_ref: str = ""


class HFModelRef(BaseModel):
    repo: str
    revision: str = "main"
    license: str = ""
    param_count: int | None = None
    task_match: str = ""


class PaperSearch:
    """Search papers and models relevant to a task. Degrades gracefully."""

    async def search(
        self, query: str, source: str = "arxiv", n: int = 5
    ) -> list[PaperRef]:
        """Search for papers. Falls back through sources on failure."""
        # MVP: returns empty list, to be wired to real APIs
        return []

    async def search_models(self, query: str, n: int = 5) -> list[HFModelRef]:
        """Search HuggingFace for relevant pretrained models."""
        return []


async def generate_hypotheses(
    data_profile: "DataProfile",
    papers: list[PaperRef],
    models: list[HFModelRef],
    tree: "ResearchTree",
    llm=None,
) -> list[Hypothesis]:
    """Generate 3-5 falsifiable hypotheses from search results.

    In MVP, generates simple hypotheses from data profile alone.
    Full implementation uses LLM with paper/model context.
    """
    from uuid import uuid4

    hypotheses = []
    best = tree.best_experiment()
    parent_id = best.id if best else None

    templates = [
        (
            "Standardize numerical features",
            "Apply StandardScaler to numeric columns",
            "Improve convergence and metric stability",
        ),
        (
            "Add feature interactions",
            "Create pairwise interaction features for top-K correlated columns",
            "Capture non-linear relationships",
        ),
        (
            "Try gradient boosting",
            "Replace linear model with LightGBM",
            "Better handle non-linear patterns in tabular data",
        ),
    ]

    for statement, intervention, effect in templates[:3]:
        h = Hypothesis(
            id=f"hyp_{uuid4().hex[:12]}",
            parent_id=parent_id,
            statement=statement,
            intervention=intervention,
            expected_effect=effect,
            sources=[p.url for p in papers[:2]],
            status="PROPOSED",
        )
        hypotheses.append(h)
        tree.add_hypothesis(h)

    return hypotheses
