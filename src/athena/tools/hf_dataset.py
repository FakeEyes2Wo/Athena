"""HuggingFace dataset search and download tools.

Uses huggingface_hub Python API to search for similar datasets on
HuggingFace Hub and download them locally. Downloaded datasets are
ingested via dataset_service.create_data_card to produce DataCard.
"""

from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec

# HF API 客户端（延迟初始化，与 hf_model 共享）
_hf_api: HfApi | None = None


def _get_hf_api() -> HfApi:
    global _hf_api
    if _hf_api is None:
        _hf_api = HfApi()
    return _hf_api


class HFDatasetSearchTool(BaseTool):
    """在 HuggingFace Hub 上搜索与比赛任务相关的数据集。"""

    spec = ToolSpec(
        name="hf_dataset_search",
        description=(
            "Search HuggingFace Hub for datasets related to the competition "
            "task. Use keywords, modality filter, and result count to find "
            "datasets that can augment the competition data."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_keywords": {
                    "type": "string",
                    "description": "Space-separated keywords describing the task.",
                },
                "modality": {
                    "type": "string",
                    "description": "Data modality: tabular, text, image, etc.",
                },
                "n_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                    "default": 10,
                },
            },
            "required": ["task_keywords", "modality"],
            "additionalProperties": False,
        },
        concurrency_safe=True,  # 只读搜索，可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        hf = _get_hf_api()
        keywords = input["task_keywords"]
        n_results = input.get("n_results", 10)

        try:
            results = list(hf.list_datasets(search=keywords, limit=n_results))
        except Exception as exc:
            return ToolResult(success=False, error=f"HF dataset search failed: {exc}")

        datasets = []
        for ds in results:
            datasets.append({
                "id": getattr(ds, "id", ""),
                "description": getattr(ds, "description", "") or "",
                "tags": getattr(ds, "tags", []) or [],
                "downloads": getattr(ds, "downloads", 0) or 0,
                "likes": getattr(ds, "likes", 0) or 0,
            })

        # 空结果时给 LLM 搜索建议
        suggestion = ""
        if not datasets:
            suggestion = (
                f"No datasets found for '{keywords}'. "
                f"Consider broadening keywords or removing modality filter."
            )

        return ToolResult(
            data={"datasets": datasets, "count": len(datasets), "suggestion": suggestion}
        )


class HFDatasetDownloadTool(BaseTool):
    """从 HuggingFace Hub 下载数据集并保存到本地。"""

    spec = ToolSpec(
        name="hf_dataset_download",
        description=(
            "Download a dataset from HuggingFace Hub by its dataset ID. "
            "Saves data to the specified output directory."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "hf_dataset_id": {
                    "type": "string",
                    "description": "HuggingFace dataset ID (e.g. 'user/dataset-name').",
                },
                "output_dir": {
                    "type": "string",
                    "description": "Local directory path to save the dataset.",
                },
            },
            "required": ["hf_dataset_id", "output_dir"],
            "additionalProperties": False,
        },
        concurrency_safe=False,  # 写入文件系统，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ds_id = input["hf_dataset_id"]
        output_dir = input["output_dir"]

        Path(output_dir).mkdir(parents=True, exist_ok=True)

        try:
            local_path = snapshot_download(
                repo_id=ds_id, repo_type="dataset", local_dir=output_dir
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"HF dataset download failed for '{ds_id}': {exc}",
            )

        return ToolResult(
            data={
                "dataset_id": ds_id,
                "local_path": local_path,
                "status": "downloaded",
            }
        )
