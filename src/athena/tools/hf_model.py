"""HuggingFace model search and download tools.

Uses huggingface_hub Python API to search for pre-trained models
on HuggingFace Hub and download their weights locally.
"""

from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec

# HF API 客户端（延迟初始化，与 hf_dataset 共享同一全局变量）
_hf_api: HfApi | None = None


def _get_hf_api() -> HfApi:
    global _hf_api
    if _hf_api is None:
        _hf_api = HfApi()
    return _hf_api


class HFModelSearchTool(BaseTool):
    """在 HuggingFace Hub 上搜索预训练模型。"""

    def __init__(self, work_root: str = "work") -> None:
        # 构造器注入产物输出根目录，搜索结果的落盘目录
        self.output_dir = Path(work_root) / "hf_model_search"

    spec = ToolSpec(
        name="hf_model_search",
        description=(
            "Search HuggingFace Hub for pre-trained models matching "
            "the competition task type and modality."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_type": {
                    "type": "string",
                    "description": "Task type: image_classification, text_classification, etc.",
                },
                "modality": {
                    "type": "string",
                    "description": "Data modality: tabular, text, image, etc.",
                },
                "architecture_hint": {
                    "type": "string",
                    "description": "Optional architecture keyword (e.g. 'vit', 'bert', 'resnet').",
                    "default": "",
                },
                "n_results": {
                    "type": "integer",
                    "description": "Maximum number of results.",
                    "default": 10,
                },
            },
            "required": ["task_type", "modality"],
            "additionalProperties": False,
        },
        concurrency_safe=True,  # 只读搜索，可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        hf = _get_hf_api()
        task_type = input["task_type"]
        architecture_hint = input.get("architecture_hint", "")
        n_results = input.get("n_results", 10)

        # 搜索词：架构提示 + 任务类型
        search_terms = task_type.replace("_", " ")
        if architecture_hint:
            search_terms = f"{architecture_hint} {search_terms}"

        try:
            results = list(hf.list_models(search=search_terms, limit=n_results))
        except Exception as exc:
            return ToolResult(success=False, error=f"HF model search failed: {exc}")

        models = []
        for m in results:
            models.append({
                "id": getattr(m, "modelId", getattr(m, "id", "")),
                "pipeline_tag": getattr(m, "pipeline_tag", "") or "",
                "tags": getattr(m, "tags", []) or [],
                "downloads": getattr(m, "downloads", 0) or 0,
                "likes": getattr(m, "likes", 0) or 0,
            })

        suggestion = ""
        if not models:
            suggestion = (
                f"No models found for '{search_terms}'. "
                f"Try a broader architecture hint or omit it."
            )

        return ToolResult(
            data={"models": models, "count": len(models), "suggestion": suggestion}
        )


class HFModelDownloadTool(BaseTool):
    """从 HuggingFace Hub 下载预训练模型权重到本地。"""

    def __init__(self, work_root: str = "work") -> None:
        # 构造器注入产物输出根目录，模型权重的落盘目录
        self.output_dir = Path(work_root) / "hf_model_download"

    spec = ToolSpec(
        name="hf_model_download",
        description=(
            "Download pre-trained model weights from HuggingFace Hub. "
            "Returns the local path to the downloaded model files."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "hf_model_id": {
                    "type": "string",
                    "description": "HuggingFace model ID (e.g. 'google/vit-base-patch16-224').",
                },
                "output_dir": {
                    "type": "string",
                    "description": "Local directory to save model weights.",
                },
            },
            "required": ["hf_model_id", "output_dir"],
            "additionalProperties": False,
        },
        concurrency_safe=False,  # 下载写入文件系统，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        model_id = input["hf_model_id"]
        output_dir = input["output_dir"]

        Path(output_dir).mkdir(parents=True, exist_ok=True)

        try:
            local_path = snapshot_download(repo_id=model_id, local_dir=output_dir)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"HF model download failed for '{model_id}': {exc}",
            )

        return ToolResult(
            data={"model_id": model_id, "model_path": local_path, "status": "downloaded"}
        )
