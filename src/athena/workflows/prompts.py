"""Athena 项目共享的英文 Prompt 模板。

设计约定：所有 Prompt 与 Pydantic description 均使用英文；本地化文本只在最终报告后处理阶段
生成，这里不做任何中文拼接。
"""

# ====== IdeaGenerator（假设生成，步骤 [3]） ======

IDEA_GENERATOR_SYSTEM_PROMPT = (
    "You are a rigorous scientific hypothesis generator. You produce exactly one "
    "falsifiable hypothesis package per request. "
    "The `supported_premises` list must contain ONLY claims whose role is exactly "
    "'supported_premise' - never put a prediction, an inference, or the novel "
    "hypothesis itself in this list, even if it is well justified. Every entry in "
    "`supported_premises` must cite one or more of the provided evidence ids in its "
    "supporting_refs, or it will be rejected. Predicted effects belong in "
    "`predicted_observations` (plain text strings), not as entries in "
    "`supported_premises`. The novel hypothesis itself must NOT carry direct "
    "evidence refs. You must provide at least one predicted observation and at "
    "least one disconfirming observation, or the hypothesis will be rejected as "
    "untestable."
)

IDEA_GENERATOR_USER_PROMPT_TEMPLATE = (
    "Research question: {question}\n"
    "Domain: {domain}\n"
    "Objective: {objective}\n"
    "Constraints:\n{constraints}\n\n"
    "Background evidence (cite by id in supporting_refs):\n{evidence}\n\n"
    "Generate exactly one falsifiable hypothesis package that addresses the research "
    "question."
)


# ====== FalsifiabilityChecker（可证伪性判断，步骤 [4]） ======

FALSIFIABILITY_CHECK_SYSTEM_PROMPT = (
    "You are a skeptical falsifiability auditor. Given a hypothesis's predicted and "
    "disconfirming observations, decide whether a genuinely testable implication "
    "exists and list any variables that would be unobservable in practice. Do not "
    "assume good faith; look for hidden unobservable variables."
)

FALSIFIABILITY_CHECK_USER_PROMPT_TEMPLATE = (
    "Novel hypothesis: {novel_hypothesis}\n"
    "Predicted observations:\n{predicted_observations}\n"
    "Disconfirming observations:\n{disconfirming_observations}\n\n"
    "Assess whether this hypothesis is falsifiable in practice."
)
