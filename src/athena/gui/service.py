"""Composition root for the GUI RPC surface over ``ResearchRuntime``.

Aggregates graph algorithms, settings, traces, and experiments into one class
consumed by ``gui_gateway.handler``. Read methods are synchronous; control
methods that touch the runtime loop are async.
"""

import base64
import json
import re
from pathlib import Path
from typing import Any

from athena.gui import experiments, graph, traces
from athena.research.report import build_final_report
from athena.research.runtime import ResearchRuntime


class GuiService:
    """Expose the full research runtime over JSON-friendly methods."""

    def __init__(self, runtime: ResearchRuntime) -> None:
        """Bind the service facade to its active research runtime."""
        self._runtime = runtime

    def ping(self) -> dict[str, Any]:
        """Return the small response used to verify the GUI RPC connection."""
        return {"pong": True}

    async def start(self) -> dict[str, Any]:
        """Start the runtime and report that startup completed."""
        await self._runtime.start()
        return {"started": True}

    async def start_task(self, task: str) -> dict[str, Any]:
        """Submit a task through the runtime's compatibility task entry point."""
        return {"status": await self._runtime.start_task(task)}

    async def message(self, text: str) -> dict[str, Any]:
        """Send a user or control message to the runtime."""
        return {"response": await self._runtime.message(text)}

    async def pause(self) -> dict[str, Any]:
        """Pause the active task through the runtime command interface."""
        return {"status": await self._runtime.message("/pause")}

    async def resume(self) -> dict[str, Any]:
        """Resume the runtime's current task."""
        return {"status": await self._runtime.resume_current_task()}

    async def stop(self) -> dict[str, Any]:
        """Stop the active task through the runtime command interface."""
        return {"status": await self._runtime.message("/stop")}

    async def start_search(self, request: dict[str, Any]) -> dict[str, Any]:
        """Start PREPARE through the confirmed clarification draft path."""
        draft_id = request.get("draft_id")
        revision = request.get("revision")
        acknowledge_unresolved = request.get("acknowledge_unresolved")
        if not isinstance(draft_id, str) or not draft_id.strip():
            raise ValueError("start_search requires a non-empty 'draft_id'")
        if revision is None:
            raise ValueError("start_search requires an integer 'revision'")
        if acknowledge_unresolved is None:
            raise ValueError("start_search requires 'acknowledge_unresolved'")
        result = await self._runtime.confirm_and_start(
            draft_id, int(revision), bool(acknowledge_unresolved)
        )
        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json")
        if isinstance(result, dict):
            return result
        return {"draft_id": draft_id, "result": result}

    @staticmethod
    def _clarification_response(result: object) -> dict[str, Any]:
        """Serialize a draft without translating its domain exception."""
        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json")
        if isinstance(result, dict):
            return result
        return {"result": result}

    async def task_clarification_start(self, task: str) -> dict[str, Any]:
        """Create or continue clarification for a task without starting research."""
        result = await self._runtime.task_clarification_start(task)
        return self._clarification_response(result)

    async def task_clarification_get(self, draft_id: str) -> dict[str, Any]:
        """Read one clarification draft by its canonical identifier."""
        result = await self._runtime.task_clarification_get(draft_id)
        return self._clarification_response(result)

    def task_clarification_current(self) -> dict[str, Any] | None:
        """Read the active session's persisted clarification draft, if present."""
        result = self._runtime.task_clarification_current()
        if result is None:
            return None
        return self._clarification_response(result)

    async def task_clarification_retry(
        self, draft_id: str, revision: int
    ) -> dict[str, Any]:
        """Retry a recoverable clarification failure at the expected revision."""
        result = await self._runtime.task_clarification_retry(draft_id, revision)
        return self._clarification_response(result)

    async def task_clarification_revise(
        self, draft_id: str, revision: int, instruction: str
    ) -> dict[str, Any]:
        """Apply an instruction to a ready draft and rerun task understanding."""
        result = await self._runtime.task_clarification_revise(
            draft_id, revision, instruction
        )
        return self._clarification_response(result)

    async def task_clarification_cancel(
        self, draft_id: str, revision: int
    ) -> dict[str, Any]:
        """Cancel an unconfirmed clarification draft at the expected revision."""
        result = await self._runtime.task_clarification_cancel(draft_id, revision)
        return self._clarification_response(result)

    async def start_validation(self) -> dict[str, Any]:
        """Run the VALIDATE phase and return the resulting runtime status."""
        return {"status": await self._runtime.start_validation()}

    def generate_report(self) -> dict[str, Any]:
        """Produce a deterministic Markdown research report from the tree."""
        report = build_final_report(
            self._runtime.tree,
            self._runtime.state.validation,
            validation_skipped=self._runtime.state.validation_skipped is True,
        )
        return {"status": "ok", "report": report}

    def state_get(self) -> dict[str, Any]:
        """Capture the current projected state snapshot via subscribe/unsubscribe."""
        captured: dict[str, Any] = {}

        def emit(kind: str, payload: dict[str, Any]) -> None:
            """Capture the synchronous state event from the subscription."""
            if kind == "state":
                captured["snapshot"] = payload

        subscription_id = self._runtime.subscribe(emit)
        self._runtime.unsubscribe(subscription_id)
        return captured.get("snapshot", {})

    def tree_get(self) -> dict[str, Any]:
        """Return the current experiment tree as a JSON-friendly mapping."""
        return {"tree": self._runtime.tree.to_dict()}

    def tree_save(self) -> dict[str, Any]:
        """Persist the current experiment tree and return its path."""
        return {"saved": True, "path": str(self._runtime.save_tree())}

    def tree_load(self) -> dict[str, Any]:
        """Reload the persisted experiment tree and return its projection."""
        return {"loaded": True, "tree": self._runtime.load_tree().to_dict()}

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
                # One unreadable figure must not hide the rest of the EDA report.
                continue

        if report:
            report = _embed_images(report, figures)

        return {
            "eda_dir": str(eda_path),
            "report": report,
            "report_name": report_name,
            "figures": figures,
        }

    def hypothesis_graph(self) -> dict[str, Any]:
        """Build the GUI graph projection for current hypotheses."""
        return graph.build_hypothesis_graph(self._runtime.tree)

    def graph_algorithms(self) -> dict[str, Any]:
        """List graph algorithms currently exposed by the GUI."""
        return {"algorithms": graph.ALGORITHMS}

    def graph_algorithm(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Run one named graph analysis against the current experiment tree."""
        return graph.run_algorithm(self._runtime.tree, name, params)

    def settings_get(self) -> dict[str, Any]:
        """Return the current runtime settings projection."""
        return self._runtime.settings()

    async def settings_set(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Validate and apply supported runtime settings from a GUI patch."""
        return await self._runtime.apply_settings(patch)

    def traces_list(self) -> dict[str, Any]:
        """List agent traces written under the active session."""
        rollout_dir = self._runtime.tree_path.parent / "logs" / "agents"
        return {"traces": traces.list_traces(rollout_dir)}

    def trace_get(self, agent_id: str) -> dict[str, Any]:
        """Read one agent trace by identifier from the active session."""
        rollout_dir = self._runtime.tree_path.parent / "logs" / "agents"
        return traces.read_trace(rollout_dir, agent_id)

    def experiments_list(self, kind: str | None = None) -> dict[str, Any]:
        """List experiments in the current tree, optionally filtered by kind."""
        return {"experiments": experiments.list_experiments(self._runtime.tree, kind)}

    def experiment_get(self, experiment_id: str) -> dict[str, Any]:
        """Return the detail projection for one experiment."""
        return experiments.get_experiment_detail(self._runtime.tree, experiment_id)

    def experiment_transition(
        self, experiment_id: str, status: str, error: str | None = None
    ) -> dict[str, Any]:
        """Apply and persist a status transition for one experiment."""
        detail = experiments.transition(
            self._runtime.tree, experiment_id, status, error
        )
        self._runtime.save_tree()
        return {"experiment": detail}

    def experiment_set_sota(self, experiment_id: str) -> dict[str, Any]:
        """Mark an experiment as the current state-of-the-art candidate."""
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
                    return (
                        candidate.read_text(encoding="utf-8", errors="replace"),
                        candidate.name,
                    )
    except (OSError, ValueError, json.JSONDecodeError):
        # Invalid experiment metadata falls back to conventional report names.
        pass

    for name in ("report.md", "REPORT.md", "eda.md", "EDA.md", "analysis.md"):
        candidate = eda_path / name
        if candidate.is_file():
            return (
                candidate.read_text(encoding="utf-8", errors="replace"),
                candidate.name,
            )

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
