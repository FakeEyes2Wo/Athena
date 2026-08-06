"""Retrieval domain types."""
from dataclasses import dataclass, field

@dataclass
class PaperRef:
    """Reference to an academic paper found via search."""
    title: str
    source: str  # "arxiv" | "semantic_scholar" | "web" | "common_knowledge"
    url: str = ""
    key_findings: str = ""
    relevance: str = ""
    markdown_ref: str = ""

@dataclass
class HFModelRef:
    """Reference to a HuggingFace model."""
    repo: str
    revision: str = "main"
    license: str = ""
    param_count: int | None = None
    task_match: str = ""
    digest: str = ""
    artifact: str = ""

if __name__ == "__main__":
    paper = PaperRef(title="Attention Is All You Need", source="arxiv")
    model = HFModelRef(repo="bert-base-uncased")
    print(f"Paper: {paper}")
    print(f"Model: {model}")
