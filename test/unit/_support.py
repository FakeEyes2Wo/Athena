"""项目 Composition Root 测试支持：带真实 model + fake 内层 LLM 的 make_project。"""

import json
from collections.abc import AsyncGenerator
from pathlib import Path

from athena.agents.prompt_agent import load_prompt
from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.agent import settings
from athena.core.agent.provider import StreamEvent
from athena.core.agent.runtime import Agent
from athena.research.config import ProviderConfig, RuntimeDependencies
from athena.research.runtime import ResearchRuntime

# 固定格式 task-understanding 报告（init_agent.md prompt §格式）。
FAKE_TASK_UNDERSTANDING = """\
# Task Understanding

## Dataset
- path: dataset.csv, rows: 20
- columns: age, income, label

## Target
- column: label, dtype: int64, cardinality: 2, distribution: [0, 1]

## Task Type
classification (binary target)

## Primary Metric
f1_macro (maximize)

## Evaluation Plan
eval.py reads predictions.csv + labels.csv, aligns by __athena_row_id,
computes the primary metric, prints one JSON line.
"""

# 自包含 eval.py（eval.py 契约：stdlib-only，读 predictions.csv + labels.csv 算
# accuracy，把结果写入 --output 文件——DataScriptRunner 要求 bundle 写 result.json，
# 不是打印 stdout）。确定性测试用 stdlib-only，避免 uv sync 安装 numpy。
FAKE_EVAL_PY = """\
import json
import sys

def main():
    out = open(sys.argv[sys.argv.index('--output') + 1], 'w')
    preds = {}
    with open("predictions.csv", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) >= 2 and parts[0] != "__athena_row_id":
                preds[parts[0]] = float(parts[1])

    labels = {}
    with open("labels.csv", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) >= 2 and parts[0] != "__athena_row_id":
                labels[parts[0]] = float(parts[1])

    rows = [(preds[k], labels[k]) for k in preds if k in labels]
    if not rows:
        raise RuntimeError("no aligned predictions")
    primary = sum(1.0 for a, b in rows if a == b) / len(rows)
    json.dump({"primary": primary, "metric": "accuracy"}, out)

main()
"""

FAKE_DATA_ANALYSIS_PY = """\
import json
from pathlib import Path

Path("figures").mkdir(exist_ok=True)
Path("figures/plot.png").write_bytes(b"fake-png")
Path("report.md").write_text("分析报告", encoding="utf-8")
Path("dataset_role_proposal.json").write_text(
    json.dumps({
        "role_proposal": "train",
        "data_files": ["dataset.csv"],
        "target_column": "label",
        "reasoning": "fake role proposal",
    }),
    encoding="utf-8",
)
"""


class FakeProvider:
    """stub stream：直接 emit text_delta + response_completed，让内层 Agent 收尾。

    duck-typed ``ResponsesProvider``：不真正调 LLM（Task 6 起 data/init 内层
    LLM agent 的测试接缝）。
    """

    def __init__(self) -> None:
        self.model_name = "fake"

    async def stream(self, config, tools, messages, cancel, *, output_type=None):
        """stub 流：直接 emit text_delta + response_completed，让内层 Agent 收尾。"""
        yield StreamEvent(
            kind="text_delta", data={"delta": "done", "accumulated": "done"}
        )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


def fake_inner_builder(agent_type, *, model, client, workspace, runtime=None):
    """fake 内层 agent 构建器：按 agent_type 预写 prompt 要求的固定名产物再收尾。

    模拟真实 LLM 的产物：data（data_agent prompt 要求 workspace 根 report.md +
    至少一张 ``figures/*`` 图）、init（init_agent.md prompt 要求
    ``task_understanding.md`` + ``eval.py``）。让外层编排（DataAgent 收集-提交 /
    InitAgent 收集-打包）不依赖真实 API。
    """
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    if agent_type == "init":
        (ws / "task_understanding.md").write_text(
            FAKE_TASK_UNDERSTANDING, encoding="utf-8"
        )
        (ws / "eval.py").write_text(FAKE_EVAL_PY, encoding="utf-8")
        (ws / "labels.csv").write_text(
            "__athena_row_id,target\nr1,0.0\nr2,1.0\n", encoding="utf-8"
        )
    else:
        (ws / "analysis.py").write_text(FAKE_DATA_ANALYSIS_PY, encoding="utf-8")
    return Agent(FakeProvider(), generic_tool_registry(ws), load_prompt(agent_type))


def make_project(tmp_path: Path) -> ResearchRuntime:
    """带 DeepSeek model + fake 流式 client 的组合根（LLM 相关测试的入口）。

    ``register_defaults`` 已随 agent 注册改为按 phase 懒注册而移除；现在只需
    ``model`` + ``client``，supervisor 在 ``__init__`` 内注册，其余 agent 懒注册。
    """
    return ResearchRuntime(
        project_root=tmp_path,
        dependencies=RuntimeDependencies(
            provider=ProviderConfig(
                model=settings.model_name(), client=FakeStreamingClient()
            )
        ),
    )


# FakeStreamingClient：ResponsesProvider 兼容的 fake 模型（real-search-worktree Task 3）

# code agent 首轮写出的三个必产物（workspace 相对路径）。
_CODE_FILES = {
    "model.py": "print('model')\n",
    "predictions.csv": "__athena_row_id,prediction\nr1,0.0\nr2,1.0\n",
    "REPORT.md": "# Report\n",
}

HYPOTHESIS_JSON = json.dumps(
    {
        "hypotheses": [
            {
                "statement": "feature scaling should help",
                "intervention": "standardize numeric inputs",
                "expected_effect": "raise primary metric",
            },
            {
                "statement": "ensemble should help",
                "intervention": "average top models",
                "expected_effect": "raise primary metric",
            },
            {
                "statement": "feature selection should help",
                "intervention": "drop noisy columns",
                "expected_effect": "raise primary metric",
            },
        ]
    }
)


class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, index: int, call_id: str, name: str, arguments: str) -> None:
        self.index = index
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeDelta:
    def __init__(
        self, content: str | None = None, tool_calls: list | None = None
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, delta: _FakeDelta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, choices: list) -> None:
        self.choices = choices


def _text_chunk(content: str) -> _FakeChunk:
    return _FakeChunk([_FakeChoice(_FakeDelta(content=content))])


def _finish_chunk(reason: str) -> _FakeChunk:
    return _FakeChunk([_FakeChoice(_FakeDelta(), finish_reason=reason)])


def _tool_chunk(index: int, name: str, args: dict) -> _FakeChunk:
    return _FakeChunk(
        [
            _FakeChoice(
                _FakeDelta(
                    tool_calls=[
                        _FakeToolCall(
                            index,
                            f"call_{index}",
                            name,
                            json.dumps(args, ensure_ascii=False),
                        )
                    ]
                )
            )
        ]
    )


def _has_tool_return(messages: list) -> bool:
    return any(
        getattr(part, "part_kind", None) == "tool-return"
        for m in messages
        for part in getattr(m, "parts", [])
    )


def _structured_title(response_format: dict, messages: list) -> str:
    """解析结构化输出请求对应的 schema title。

    - openai ``json_schema`` 模式：name 字段。
    - deepseek ``json_object`` 模式：schema 由 provider 注入为 system 消息，解析其 title。
    """
    name = response_format.get("json_schema", {}).get("name", "")
    if name:
        return name
    marker = "Return a JSON object matching this schema:\n"
    for msg in messages:
        content = msg.get("content", "") if isinstance(msg, dict) else ""
        if isinstance(content, str) and content.startswith(marker):
            try:
                return json.loads(content[len(marker) :]).get("title", "")
            except (json.JSONDecodeError, AttributeError):
                return ""
    return ""


class FakeStreamingClient:
    """ResponsesProvider 兼容 fake：chat.completions.create 按请求类型返回流。

    - 结构化输出（ideator 的 response_format）→ emit HypothesisBatch JSON。
    - code 首轮（无 tool-return）→ emit 三个 write_file 写 model.py/predictions.csv/REPORT.md。
    - code 后续轮（有 tool-return）→ emit 文本完成。
    """

    def __init__(self) -> None:
        self.chat = _FakeChat(self)
        self.calls = 0


class _FakeChat:
    def __init__(self, client: FakeStreamingClient) -> None:
        self.completions = _FakeCompletions(client)


class _FakeCompletions:
    def __init__(self, client: FakeStreamingClient) -> None:
        self._client = client

    async def create(self, **kwargs) -> AsyncGenerator[_FakeChunk, None]:
        """返回按请求类型构造的 chunk 流（真实 client.create 也是 async 返回 stream）。"""
        self._client.calls += 1
        messages = kwargs.get("messages", [])
        response_format = kwargs.get("response_format")
        if response_format is not None:
            schema_name = _structured_title(response_format, messages)
            if schema_name == "HypothesisBatch":
                chunks = [_text_chunk(HYPOTHESIS_JSON), _finish_chunk("stop")]
            else:
                # reflection 评审：结构化 ACCEPT（默认放行，让 PREPARE 推进）
                review = json.dumps(
                    {
                        "decision": "ACCEPT",
                        "issues": [],
                        "required_changes": [],
                        "evidence_refs": [],
                    }
                )
                chunks = [_text_chunk(review), _finish_chunk("stop")]
        elif _has_tool_return(messages):
            chunks = [_text_chunk("done"), _finish_chunk("stop")]
        else:
            chunks = [
                _tool_chunk(i, "write_file", {"path": name, "content": content})
                for i, (name, content) in enumerate(_CODE_FILES.items())
            ]
            chunks.append(_finish_chunk("tool_calls"))

        async def stream() -> AsyncGenerator[_FakeChunk, None]:
            """逐块产出预设的 fake chunk 流。"""
            for chunk in chunks:
                yield chunk

        return stream()
