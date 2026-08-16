"""Composition root for the GUI RPC surface over ``ResearchRuntime``.

Aggregates graph algorithms, settings, traces, and experiments into one class
consumed by ``gui_gateway.handler``. Read methods are synchronous; control
methods that touch the runtime loop are async.
"""

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from athena.core.agent import settings
from athena.gui import experiments, graph, traces
from athena.research.report import build_final_report
from athena.research.runtime import ResearchRuntime
from athena.research.runtime_events import recent_user_texts

logger = logging.getLogger(__name__)


class GuiService:
    """Expose the full research runtime over JSON-friendly methods."""

    def __init__(self, runtime: ResearchRuntime) -> None:
        self._runtime = runtime
        self._rollout_dir: Path = runtime.tree_path.parent / "logs" / "agents"

    # ── 控制 ──────────────────────────────────────────────────────────────

    async def ping(self) -> dict[str, Any]:
        return {"pong": True}

    async def start(self) -> dict[str, Any]:
        await self._runtime.start()
        return {"started": True}

    async def start_task(self, task: str) -> dict[str, Any]:
        return {"status": await self._runtime.start_task(task)}

    async def message(self, text: str) -> dict[str, Any]:
        return {"response": await self._runtime.message(text)}

    async def pause(self) -> dict[str, Any]:
        return {"status": await self._runtime.message("/pause")}

    async def resume(self) -> dict[str, Any]:
        return {"status": await self._runtime.message("/resume")}

    async def stop(self) -> dict[str, Any]:
        return {"status": await self._runtime.message("/stop")}

    def _recent_user_texts(self, limit: int = 6) -> list[str]:
        """Return recent Human messages from the persisted session transcript."""
        replay = getattr(self._runtime, "replay_output_events", None)
        if replay is None:
            return []
        try:
            records = replay()
        except Exception:
            logger.warning("failed to replay session transcript", exc_info=True)
            return []
        return recent_user_texts(records, limit)

    async def parse_intent(self, message: str) -> dict[str, Any]:
        """Produce a supervisor-style task understanding from a research task.

        Mirrors the ``task_understanding.md`` sections (title / dataset / target /
        task type / primary metric / evaluation plan). Falls back to a keyword
        heuristic when no API key is configured or the LLM call fails, so the GUI
        keeps working in a degraded mode.

        断点续传：当前消息会追加进会话日志，并把最近几条 Human 消息一并交给模型，
        让“继续/重试”这类短消息也能沿用之前对话里的数据集/目标/指标。
        """
        # 断点续传：先把用户消息写入会话日志，重开目录后能还原完整对话。
        self._runtime.persist_user_message(message)
        context = self._recent_user_texts()
        try:
            client = settings.get_client()
            model = settings.model_name()
            chat_messages = [{"role": "system", "content": _TASK_UNDERSTANDING_SYSTEM}]
            chat_messages.extend({"role": "user", "content": text} for text in context)
            response = await client.chat.completions.create(
                model=model,
                messages=chat_messages,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
            content = response.choices[0].message.content
            if not content:
                return _heuristic_understanding(message)
            return TaskUnderstanding.model_validate(
                json.loads(content)
            ).model_dump(mode="json")
        except Exception:
            logger.warning("LLM task understanding failed; falling back to heuristic", exc_info=True)
            state = getattr(self._runtime, "state", None)
            existing = getattr(state, "task_understanding", None)
            if isinstance(existing, dict) and existing:
                return existing
            return _heuristic_understanding(message)

    async def start_search(self, config: dict[str, Any]) -> dict[str, Any]:
        task = config.get("task") or config.get("message") or config.get("task_type") or ""
        if isinstance(task, dict):
            task = json.dumps(task, ensure_ascii=False)
        if not isinstance(task, str) or not task.strip():
            raise ValueError("start_search requires a non-empty 'task' string")
        return {"status": await self._runtime.start_task(task)}

    async def start_validation(self) -> dict[str, Any]:
        """Run the VALIDATE phase and return the resulting runtime status."""
        return {"status": await self._runtime.start_validation()}

    async def generate_report(self) -> dict[str, Any]:
        """Produce a deterministic Markdown research report from the tree."""
        return {"status": "ok", "report": _build_report(self._runtime)}

    # ── 状态与树 ──────────────────────────────────────────────────────────

    def state_get(self) -> dict[str, Any]:
        """Capture the current projected state snapshot via subscribe/unsubscribe."""
        captured: dict[str, Any] = {}

        def emit(kind: str, payload: dict[str, Any]) -> None:
            if kind == "state":
                captured["snapshot"] = payload

        subscription_id = self._runtime.subscribe(emit)
        self._runtime.unsubscribe(subscription_id)
        return captured.get("snapshot", {})

    def tree_get(self) -> dict[str, Any]:
        return {"tree": self._runtime.tree.to_dict()}

    def tree_save(self) -> dict[str, Any]:
        return {"saved": True, "path": str(self._runtime.save_tree())}

    def tree_load(self) -> dict[str, Any]:
        return {"loaded": True, "tree": self._runtime.load_tree().to_dict()}

    # ── EDA 预览 ─────────────────────────────────────────────────────────

    def eda_report(self) -> dict[str, Any]:
        """读取 EDA 目录的 Markdown 报告与图表（base64），并把报告内相对图片引用替换为内嵌 data URL。"""
        eda_dir = self._runtime.state.eda_dir
        if not eda_dir:
            return {"eda_dir": None, "report": None, "figures": []}
        root = Path(self._runtime.settings()["project_root"])
        eda_path = Path(eda_dir)
        if not eda_path.is_absolute():
            eda_path = (root / eda_dir).resolve()
        if not eda_path.is_dir():
            return {"eda_dir": str(eda_path), "report": None, "figures": []}

        report, report_name = _find_eda_report(eda_path)

        figures: list[dict[str, Any]] = []
        for image in sorted(eda_path.rglob("*")):
            if not image.is_file() or image.suffix.lower() not in _IMAGE_SUFFIXES:
                continue
            try:
                figures.append(
                    {
                        "name": image.name,
                        "mime": (
                            "image/svg+xml"
                            if image.suffix.lower() == ".svg"
                            else f"image/{image.suffix.lower().lstrip('.')}"
                        ),
                        "data": base64.b64encode(image.read_bytes()).decode("ascii"),
                    }
                )
            except OSError:
                continue

        if report:
            report = _embed_images(report, figures)

        return {
            "eda_dir": str(eda_path),
            "report": report,
            "report_name": report_name,
            "figures": figures,
        }

    # ── 假设图与算法 ──────────────────────────────────────────────────────

    def hypothesis_graph(self) -> dict[str, Any]:
        return graph.build_hypothesis_graph(self._runtime.tree)

    def graph_algorithms(self) -> dict[str, Any]:
        return {"algorithms": graph.ALGORITHMS}

    def graph_algorithm(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        return graph.run_algorithm(self._runtime.tree, name, params)

    # ── 设置 ──────────────────────────────────────────────────────────────

    def settings_get(self) -> dict[str, Any]:
        return self._runtime.settings()

    async def settings_set(self, patch: dict[str, Any]) -> dict[str, Any]:
        return await self._runtime.apply_settings(patch)

    # ── LLM I/O 轨迹 ─────────────────────────────────────────────────────

    def traces_list(self) -> dict[str, Any]:
        return {"traces": traces.list_traces(self._rollout_dir)}

    def trace_get(self, agent_id: str) -> dict[str, Any]:
        return traces.read_trace(self._rollout_dir, agent_id)

    # ── 实验管理 ──────────────────────────────────────────────────────────

    def experiments_list(self, kind: str | None = None) -> dict[str, Any]:
        return {"experiments": experiments.list_experiments(self._runtime.tree, kind)}

    def experiment_get(self, experiment_id: str) -> dict[str, Any]:
        return experiments.get_experiment_detail(self._runtime.tree, experiment_id)

    def experiment_transition(
        self, experiment_id: str, status: str, error: str | None = None
    ) -> dict[str, Any]:
        detail = experiments.transition(self._runtime.tree, experiment_id, status, error)
        self._runtime.save_tree()
        return {"experiment": detail}

    def experiment_set_sota(self, experiment_id: str) -> dict[str, Any]:
        result = experiments.set_sota(self._runtime.tree, experiment_id)
        self._runtime.save_tree()
        return result


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}


def _find_eda_report(eda_path: Path) -> tuple[str | None, str | None]:
    """在 EDA 目录里定位 Markdown 报告：优先 experiment.json 声明的 report，再退回常见文件名与任意根级 .md。"""
    try:
        exp_file = eda_path / "experiment.json"
        if exp_file.is_file():
            exp = json.loads(exp_file.read_text(encoding="utf-8", errors="replace"))
            report_rel = (exp.get("outputs") or {}).get("report")
            if isinstance(report_rel, str) and report_rel:
                candidate = (eda_path / report_rel).resolve()
                if candidate.is_file():
                    return candidate.read_text(encoding="utf-8", errors="replace"), candidate.name
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    for name in ("report.md", "REPORT.md", "eda.md", "EDA.md", "analysis.md"):
        candidate = eda_path / name
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace"), candidate.name

    for candidate in sorted(eda_path.glob("*.md")):
        if candidate.name.upper() in {"RESEARCH_HANDOFF.MD", "HANDOFF.MD"}:
            continue
        return candidate.read_text(encoding="utf-8", errors="replace"), candidate.name

    return None, None


def _embed_images(markdown: str, figures: list[dict[str, Any]]) -> str:
    """把 Markdown/HTML 里引用到的图表相对路径替换成 base64 data URL。"""
    for fig in figures:
        url = f"data:{fig['mime']};base64,{fig['data']}"
        name = re.escape(fig["name"])
        # Markdown 图片：![alt](figures/xxx.png)
        markdown = re.sub(
            rf"!\[([^\]]*)\]\(([^)]*{name}[^)]*)\)",
            lambda m: f"![{m.group(1)}]({url})",
            markdown,
        )
        # HTML 图片：<img src="figures/xxx.png" ...>
        markdown = re.sub(
            rf'src="([^"]*{name}[^"]*)"',
            f'src="{url}"',
            markdown,
        )
    return markdown


def _guess_metric(lower: str) -> str | None:
    """Map task keywords to a likely primary metric name."""
    if any(k in lower for k in ("accuracy", "acc", "准确率", "精确率")):
        return "accuracy"
    if any(k in lower for k in ("f1", "f-score", "f1score")):
        return "f1"
    if any(k in lower for k in ("auc", "roc", "auc-roc")):
        return "auc"
    if any(k in lower for k in ("mse", "mean squared")):
        return "mse"
    if any(k in lower for k in ("mae", "mean absolute")):
        return "mae"
    if any(k in lower for k in ("rmse",)):
        return "rmse"
    return None


class TaskUnderstanding(BaseModel):
    """Supervisor-style task understanding (mirrors ``task_understanding.md``)."""

    title: str = ""
    dataset: str = ""
    target: str = ""
    task_type: str = "other"
    primary_metric: str = "accuracy"
    direction: str = "maximize"
    evaluation_plan: str = ""
    needs_configuration: bool = True


_TASK_UNDERSTANDING_SYSTEM = (
    "You are the research supervisor producing a task understanding for an ML research task. "
    "The user messages below are a conversation; the last message may be a short follow-up "
    "or retry instruction. Infer the task understanding from the earlier messages when the "
    "last message is not self-contained. Return a JSON object with exactly these keys:\n"
    '- "title": a short conversational title (at most 12 words) naming the task;\n'
    '- "dataset": one line describing the dataset (path/name and expected shape);\n'
    '- "target": the target column and its type (e.g. "churned: binary label"), or "" if unknown;\n'
    '- "task_type": one of classification, regression, vision, generation, other;\n'
    '- "primary_metric": a lowercased metric name (accuracy, f1, mse, mae, auc, rmse, ...);\n'
    '- "direction": "maximize" or "minimize";\n'
    '- "evaluation_plan": one sentence on how the primary metric is computed;\n'
    '- "needs_configuration": true if any field is uncertain.\n'
    "Respond with only the JSON object."
)


def _heuristic_understanding(message: str) -> dict[str, Any]:
    """Fallback heuristic (no LLM): guesses task type and metric from keywords."""
    lower = message.strip().lower()
    task_type = "other"
    if any(k in lower for k in ("classif", "分类", "cls", "label")):
        task_type = "classification"
    elif any(k in lower for k in ("regress", "回归", "predict", "预测")):
        task_type = "regression"
    elif any(k in lower for k in ("detect", "检测", "segment", "分割")):
        task_type = "vision"
    elif any(k in lower for k in ("generate", "生成", "llm", "nlg")):
        task_type = "generation"

    metric = _guess_metric(lower)
    direction = (
        "minimize"
        if any(k in lower for k in ("loss", "error", "误差", "损失", "降低", "减小"))
        else "maximize"
    )
    return {
        "title": message.strip()[:40] or "新任务",
        "dataset": "",
        "target": "",
        "task_type": task_type,
        "primary_metric": metric or "accuracy",
        "direction": direction,
        "evaluation_plan": "",
        "needs_configuration": task_type == "other" or metric is None,
    }


def _build_report(runtime: ResearchRuntime) -> str:
    """Render the research tree as a Markdown report.

    Reuses the Supervisor's shared report builder so ``generate_report`` and the
    auto-generated VALIDATE report are byte-identical for the same tree/result.
    """
    return build_final_report(runtime.tree, runtime.state.validation)
