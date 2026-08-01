"""PaperScout 的策略提示与相关性打分提示。

策略提示与 ``SELECT_PROMPT`` 逐字取自 PaperScout 开源实现
(https://github.com/pty12345/PaperScout, Apache-2.0)，保持与论文一致的口径：观测里
只给 pool 摘要和历史动作，模型在 ``<analysis>`` 中说明判断，然后并行发出工具调用。

``GRADED_SELECT_PROMPT`` 是本仓库补充的。论文的相关性分数是 selector 模型输出
"True" token 的概率，需要 logprobs；当推理端点不返回 logprobs 时，二值判定会让
排序退化，因此改用论文 LLM-score 一节的 0–3 分级评分再归一到 [0,1]。
"""

PAPERSCOUT_SYSTEM_PROMPT = (
    "You are a research agent. Your goal is to find papers relevant to the User Query."
)

PAPERSCOUT_USER_PROMPT = """### User Query
{user_query}

### History Actions
{history_actions}

### Paper List
{paper_list}

### Instructions
Analyze the **Paper List** and **History Actions** to determine the next set of \
actions. Enclose your analysis of the state and decision logic within \
`<analysis>...</analysis>` tags.
**You support parallel tool calling.** You should output multiple tool calls in a \
single step if several independent actions are valuable at the current state.
**Attend to the history actions and avoid expanding the same papers.**
"""

SELECT_PROMPT = """You are an elite researcher in the field of AI, conducting \
research on {user_query}. Evaluate whether the following paper fully satisfies the \
detailed requirements of the user query and provide your reasoning. Ensure that your \
decision and reasoning are consistent.

Searched Paper:
Title: {title}
Abstract: {abstract}

User Query: {user_query}

Output format: Decision: True/False
Reason:...
Decision:"""

GRADED_SELECT_PROMPT = """You are an elite researcher assessing whether papers \
satisfy a research query. Judge only semantic relevance to the query; ignore writing \
quality, length, style and popularity.

Rate each paper on this four-level scale:
3 - directly answers the query; it is exactly the kind of paper being asked for
2 - clearly on topic and useful, but does not satisfy every stated condition
1 - same broad area, yet misses the specific subject of the query
0 - unrelated, or violates an explicit exclusion in the query

User Query: {user_query}

Papers:
{papers}

Return a JSON object mapping each paper index to its integer score, and nothing else.
Example for two papers: {{"1": 3, "2": 0}}"""

SEARCH_TOOL_DESCRIPTION = "Search for relevant papers in the arXiv repository."
SEARCH_QUERY_DESCRIPTION = (
    "A single search query (natural language or keywords). No field scopes (ti:/abs:) "
    "or boolean ops. Must differ from all history queries."
)
EXPAND_TOOL_DESCRIPTION = (
    "Expand from an existing paper by following its references to surface additional "
    "relevant works. Use this when search is saturated and you want to broaden "
    "coverage around a known paper."
)
EXPAND_ID_DESCRIPTION = (
    "The arXiv identifier (e.g., '1706.03762') of a paper already in the current "
    "paper list."
)


def format_history(actions: list[tuple[str, str]]) -> str:
    """把动作历史渲染成提示里的 ``[Search] q`` / ``[Expand] id`` 行；空历史为 ``None``。"""
    if not actions:
        return "None"
    labels = {"search": "[Search]", "expand": "[Expand]"}
    return "\n".join(f"{labels[kind]} {argument}" for kind, argument in actions)


def format_papers_for_scoring(papers: list[tuple[str, str]]) -> str:
    """把 ``(title, abstract)`` 列表渲染成带 1-based 序号的评分输入。"""
    blocks = []
    for index, (title, abstract) in enumerate(papers, start=1):
        blocks.append(f"[{index}] Title: {title}\nAbstract: {abstract}")
    return "\n\n".join(blocks)
