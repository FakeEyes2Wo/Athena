"""Model-visible context blocks for research agents."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptBlock:
    """Render one titled, model-visible prompt section."""

    title: str
    body: str
    end_marker: str

    def render(self) -> str:
        """Render a non-empty prompt block with its closing marker."""
        body = self.body.strip()
        if not body:
            return ""
        return f"\n\n--- {self.title} ---\n{body}\n{self.end_marker}"


def handoff_block(handoff: str) -> str:
    """把评估契约拼进 model 实际可见的 prompt 正文。"""
    return PromptBlock(
        title=(
            "Evaluator contract (authoritative; your predictions must match it exactly)"
        ),
        body=handoff,
        end_marker="--- end of evaluator contract ---",
    ).render()


def hypothesis_block(statement: str, intervention: str, expected: str) -> str:
    """把 Plan 要检验的假设拼进 model 实际可见的 prompt 正文。"""
    parts = [
        ("Claim", statement),
        ("Intervention to implement", intervention),
        ("Expected effect", expected),
    ]
    body = "\n".join(
        f"{label}: {value.strip()}" for label, value in parts if value and value.strip()
    )
    return PromptBlock(
        title="Hypothesis under test (implement exactly this, nothing else)",
        body=body,
        end_marker="--- end of hypothesis ---",
    ).render()


def data_contract_block(contract: str) -> str:
    """把训练数据约束拼进 model 实际可见的 prompt 正文。"""
    return PromptBlock(
        title="Data contract (violating this invalidates your score)",
        body=contract,
        end_marker="--- end of data contract ---",
    ).render()


def failure_block(kind: str, error: str) -> str:
    """把上一轮实验失败原因拼进下一轮 prompt 正文。"""
    detail = " ".join(error.split())[:800]
    body = ""
    if detail:
        body = (
            f"Failure kind: {kind}\n"
            f"Detail: {detail}\n"
            "Do not resubmit the same experiment unchanged; diagnose this failure "
            "first, then retry."
        )
    return PromptBlock(
        title="Previous attempt failed (fix this before anything else)",
        body=body,
        end_marker="--- end of failure report ---",
    ).render()
