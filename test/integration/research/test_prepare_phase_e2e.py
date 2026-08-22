"""E2E：前端 prompt 进入完整 PREPARE 阶段的组合根演练。

LLM 用 agent-aware 假客户端，编排 / 文件 IO / git / 评估脚本执行全真。
Kaggle 关闭，用本地合成 MAGFiLO 形状数据；dataclean 演示一个修复轮。
"""

import asyncio
import json
from pathlib import Path

import pytest

from test.unit._support import _finish_chunk, _text_chunk, _tool_chunk

from athena.core.agent import settings
from athena.research.runtime import ResearchRuntime

_SUBMIT = json.dumps(
    {"decision": "submit", "reason": "ok", "suggestions": []}, ensure_ascii=False
)


def _handoff_result(output_file: str) -> str:
    return json.dumps(
        {"summary": "done", "handoff_file": output_file}, ensure_ascii=False
    )


_EVAL_PY = """\
import json
import sys

def main():
    out = open(sys.argv[sys.argv.index('--output') + 1], 'w')
    preds = {}
    with open("predictions/predictions.csv", encoding="utf-8") as f:
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
    json.dump(
        {
            "primary": sum(1.0 for a, b in rows if a == b) / len(rows),
            "metric": "accuracy",
        },
        out,
    )

main()
"""

_LABELS_CSV = (
    "__athena_row_id,label\n"
    "20140609195854Bh.jpeg,1\n"
    "20140610195854Bh.jpeg,0\n"
)

_MODEL_PY = """\
from pathlib import Path
Path("predictions").mkdir(exist_ok=True)
Path("predictions/predictions.csv").write_text(
    "__athena_row_id,prediction\\n"
    "20140609195854Bh.jpeg,1\\n"
    "20140610195854Bh.jpeg,0\\n",
    encoding="utf-8",
)
Path("report.md").write_text("# Baseline\\n", encoding="utf-8")
"""

_CLEAN_PY = """\
from pathlib import Path
for p in Path(".").rglob("*.jpeg"):
    assert p.read_bytes()[:2] == b"\\xff\\xd8"
print("clean")
"""

_DATACLEAN_HANDOFF = """\
# DataClean Handoff

Inspected the local filament-segmentation-2026 directory (train_images + one
COCO annotations file + test_images). All images parse as JPEG and annotations
reference existing images. No rows dropped, no repairs needed; the data is
consumed as-is. Reproduce with clean.py in this workspace.
"""

_EDA_TODO = (
    "## Stage 0: Overview (parallel: true)\n"
    "- [ ] Read the cleaned data and write a report -> EDA_REPORT_00_OVERVIEW.md\n"
)


class _AgentScript:
    """一个 agent 的逐 turn 假剧本；每 turn 一个 tool call 或最终 JSON 文本。"""

    def __init__(self, turns: list[tuple[str, ...]]) -> None:
        self.turns = turns
        self.calls = 0
        self.submits = 0

    def next(self) -> list:
        self.calls += 1
        turn = self.turns[self.calls - 1]
        if turn[0] == "text":
            if '"decision": "submit"' in turn[1]:
                self.submits += 1
            return [_text_chunk(turn[1]), _finish_chunk("stop")]
        # call_id 跨轮次唯一，避免工具结果按 id 匹配时串扰
        return [_tool_chunk(self.calls, turn[1], turn[2]), _finish_chunk("tool_calls")]


def _system_prompt(messages) -> str:
    """拼接全部 system 消息的内容。

    ``ResponsesProvider.stream`` 先经 ``_to_api`` 把 ModelMessage 转成 OpenAI dict
    （{"role": "system", "content": ...}），假客户端的 ``create`` 拿到的就是 dict；
    同时保留对 ModelMessage 的兼容，避免将来 provider 直传原始消息时失效。
    """
    chunks: list[str] = []
    for m in messages:
        if isinstance(m, dict):
            if m.get("role") == "system" and isinstance(m.get("content"), str):
                chunks.append(m["content"])
            continue
        for p in getattr(m, "parts", []):
            if getattr(p, "part_kind", "") == "system-prompt":
                content = getattr(p, "content", "")
                if isinstance(content, str):
                    chunks.append(content)
    return "\n".join(chunks)


def _user_content(messages) -> str:
    """返回最近一条 user 消息的内容；EDA worker 无 user 提示时为 ''。"""
    for m in messages:
        if isinstance(m, dict):
            if m.get("role") == "user" and m.get("content"):
                return str(m["content"])
            continue
        for p in getattr(m, "parts", []):
            if getattr(p, "part_kind", "") == "user-prompt":
                content = getattr(p, "content", "")
                if isinstance(content, str) and content:
                    return content
    return ""


def _build_scripts(data_dir: Path) -> dict[str, _AgentScript]:
    supervisor = _AgentScript(
        [
            ("tool", "configure_kaggle", {"enabled": False, "download": False}),
            (
                "tool",
                "record_task_understanding",
                {
                    "title": "solar filament segmentation 2026",
                    "dataset": str(data_dir),
                    "target": "filament instance segmentation",
                    "task_type": "instance_segmentation",
                    "primary_metric": "map",
                    "direction": "maximize",
                    "evaluation_plan": (
                        "eval.py reads predictions/predictions.csv and labels.csv "
                        "aligned by __athena_row_id and computes mean average precision."
                    ),
                },
            ),
            ("text", json.dumps({"answer": "task understood"}, ensure_ascii=False)),
            # PREPARE 完成后 SEARCH 预算为 0 → supervisor 的 _budget_gate 回合
            # （读假设并向人类提问）：与任务理解同属 SupervisorAgent 脚本。
            ("tool", "read_hypotheses", {}),
            (
                "text",
                json.dumps(
                    {"answer": "budget exhausted; please choose the next step."},
                    ensure_ascii=False,
                ),
            ),
        ]
    )
    evaluator = _AgentScript(
        [
            (
                "tool",
                "write_file",
                {"path": "metric.json", "content": '{"eval_script": "evaluate.py"}\n'},
            ),
            (
                "tool",
                "write_file",
                {
                    "path": "pyproject.toml",
                    "content": "[project]\nname = 'eval'\nversion = '0.1.0'\n"
                    "requires-python = '>=3.11'\ndependencies = []\n",
                },
            ),
            ("tool", "write_file", {"path": "evaluate.py", "content": _EVAL_PY}),
            ("tool", "write_file", {"path": "labels.csv", "content": _LABELS_CSV}),
            (
                "tool",
                "write_file",
                {
                    "path": "HANDOFF.md",
                    "content": "# Eval contract\npredictions/predictions.csv "
                    "(__athena_row_id,prediction); accuracy over aligned ids\n",
                },
            ),
            ("text", _SUBMIT),
        ]
    )
    dataclean = _AgentScript(
        [
            ("tool", "write_file", {"path": "clean.py", "content": _CLEAN_PY}),
            ("text", _SUBMIT),  # 无 handoff → gate 打回 → 修复轮
            (
                "tool",
                "write_file",
                {"path": "DATACLEAN_HANDOFF.md", "content": _DATACLEAN_HANDOFF},
            ),
            ("text", _SUBMIT),
        ]
    )
    eda_orchestrator = _AgentScript(
        [
            ("tool", "write_file", {"path": "EDA_TODO.md", "content": _EDA_TODO}),
            ("text", _handoff_result("EDA_TODO.md")),
            ("tool", "write_file", {"path": "EDA_INDEX.md", "content": "# EDA Index\n"}),
            (
                "tool",
                "write_file",
                {"path": "EDA_HANDOFF.md", "content": "# EDA Handoff\n"},
            ),
            ("text", _handoff_result("EDA_INDEX.md")),
        ]
    )
    baseline_ideator = _AgentScript(
        [
            (
                "tool",
                "write_file",
                {"path": "BASELINE_DESIGN.md", "content": "# Baseline Design\n"},
            ),
            ("text", _handoff_result("BASELINE_DESIGN.md")),
        ]
    )
    prepare = _AgentScript(
        [
            (
                "tool",
                "write_file",
                {
                    "path": "solution/features.py",
                    "content": "def feature(value):\n    return int(value)\n",
                },
            ),
            ("tool", "write_file", {"path": "solution/model.py", "content": _MODEL_PY}),
            (
                "tool",
                "write_file",
                {
                    "path": "experiment.json",
                    "content": json.dumps(
                        {
                            "version": 1,
                            "commands": [["python", "solution/model.py"]],
                            "outputs": {
                                "predictions": "predictions",
                                "report": "report.md",
                            },
                        }
                    ),
                },
            ),
            ("text", _SUBMIT),
        ]
    )
    return {
        "# SupervisorAgent": supervisor,
        "# Evaluator Agent": evaluator,
        "# DataClean Agent": dataclean,
        "# PREPARE EDA Agent": eda_orchestrator,
        "# Baseline Ideator Agent": baseline_ideator,
        "# PREPARE Agent": prepare,
    }


class _FakeChat:
    def __init__(self, client: "FakePrepareClient") -> None:
        self.completions = _FakeCompletions(client)


class _FakeCompletions:
    def __init__(self, client: "FakePrepareClient") -> None:
        self._client = client

    async def create(self, **kwargs):
        self._client.calls += 1
        messages = kwargs.get("messages", [])
        script = self._client.dispatch(messages)

        async def stream():
            for chunk in script.next():
                yield chunk

        return stream()


class FakePrepareClient:
    """按 system prompt 的 agent 标记分发脚本的假客户端。

    分发规则：
    - system 含 "# PREPARE EDA Agent" 且有 user 内容 → EDA orchestrator；
    - system 含 "# EDA Worker" 或同 EDA system 但无 user 内容 → eda-worker
      （spawn 的 task dict 没有 content 键，因此 worker 的 LLM 上下文只有 system
      prompt；其 output_file 与假 orchestrator 写入的 EDA_TODO.md 约定一致）；
    - 其余按 marker 表分发，未命中即抛错。
    """

    def __init__(self, data_dir: Path) -> None:
        self.chat = _FakeChat(self)
        self.calls = 0
        self._scripts = _build_scripts(data_dir)
        self._worker = _AgentScript(
            [
                (
                    "tool",
                    "write_file",
                    {"path": "EDA_REPORT_00_OVERVIEW.md", "content": "# Report\n"},
                ),
                ("text", _handoff_result("EDA_REPORT_00_OVERVIEW.md")),
            ]
        )

    def dispatch(self, messages) -> _AgentScript:
        system = _system_prompt(messages)
        user = _user_content(messages)
        if "# EDA Worker" in system:
            return self._worker
        if "# PREPARE EDA Agent" in system:
            if user:
                return self._scripts["# PREPARE EDA Agent"]
            return self._worker
        for marker, script in self._scripts.items():
            if marker in system:
                return script
        raise AssertionError(f"E2E 假客户端遇到未知 agent：{system[:120]!r}")

    @property
    def dataclean_submits(self) -> int:
        return self._scripts["# DataClean Agent"].submits


def _seed_magfilo_like(tmp_path: Path) -> Path:
    """种一份形状贴合 MAGFiLO 的合成数据集（train_images + COCO + test_images）。"""
    data = tmp_path / "filament-segmentation-2026"
    train_imgs = data / "train" / "train_images"
    test_imgs = data / "test" / "test_images"
    train_imgs.mkdir(parents=True)
    test_imgs.mkdir(parents=True)
    for name in ("20140609195854Bh.jpeg", "20140610195854Bh.jpeg"):
        (train_imgs / name).write_bytes(b"\xff\xd8\xff\xd9")
    (test_imgs / "20150609195854Bh.jpeg").write_bytes(b"\xff\xd8\xff\xd9")
    coco = {
        "images": [
            {"id": "img-a", "file_name": "20140609195854Bh.jpeg",
             "width": 2048, "height": 2048},
            {"id": "img-b", "file_name": "20140610195854Bh.jpeg",
             "width": 2048, "height": 2048},
        ],
        "annotations": [
            {
                "id": "ann-1",
                "image_id": "img-a",
                "category_id": 1,
                "bbox": [1.0, 1.0, 10.0, 10.0],
                "area": 100.0,
                "iscrowd": 0,
                "segmentation": [
                    [1.0, 1.0, 1.0, 11.0, 11.0, 11.0, 11.0, 1.0]
                ],
            }
        ],
        "categories": [
            {"id": 1, "name": "Left", "supercategory": "filament"}
        ],
    }
    (data / "train" / "MAGFiLO_1.0_Annotations_kaggle2026_train.json").write_text(
        json.dumps(coco), encoding="utf-8"
    )
    return data


async def _eventually(predicate, timeout: float = 120) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_full_prepare_phase_e2e(tmp_path: Path) -> None:
    data_dir = _seed_magfilo_like(tmp_path)
    runtime = ResearchRuntime(
        project_root=tmp_path,
        model=settings.model_name(),
        client=FakePrepareClient(data_dir),
        search_limit=0,
        auto_validate=False,
    )
    try:
        task = f"我要参加 solar filament segmentation 2026 比赛，本地数据在 {data_dir}"
        await runtime.start_task(task)
        # PREPARE 跑完 → SEARCH（search_limit=0 且 auto_validate=False，停在 WAITING）
        await _eventually(lambda: runtime.state.phase == "SEARCH")

        root = tmp_path
        assert (root / "workspaces/evaluator/metric.json").is_file()
        assert (root / "workspaces/evaluator/evaluate.py").is_file()
        assert (root / "workspaces/evaluator/labels.csv").is_file()
        handoff = root / "workspaces/dataclean/DATACLEAN_HANDOFF.md"
        assert handoff.is_file() and handoff.read_text(encoding="utf-8").strip()
        assert (root / "workspaces/dataclean/clean.py").is_file()
        assert (root / "workspaces/eda/EDA_INDEX.md").is_file()
        assert (root / "workspaces/eda/EDA_HANDOFF.md").is_file()
        assert (root / "workspaces/eda/BASELINE_DESIGN.md").is_file()
        assert (root / "workspaces/eda/EDA_REPORT_00_OVERVIEW.md").is_file()

        best = runtime.tree.best_experiment_id()
        assert best is not None
        experiment = runtime.tree.get_experiment(best)
        assert experiment.eval is not None
        assert experiment.eval.primary == 1.0

        # dataclean 修复轮发生过：提交了两次才通过 gate
        assert runtime._client.dataclean_submits == 2
    finally:
        await runtime.aclose()
