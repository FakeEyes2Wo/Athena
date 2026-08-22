"""HuggingFace dataset search and download tools.

Uses huggingface_hub Python API to search for similar datasets on
HuggingFace Hub and download them locally. Downloaded datasets are
ingested via dataset_service.create_data_card to produce DataCard.
"""

import asyncio
import json
import os
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec

# HF API 客户端（延迟初始化，与 hf_model 共享）
_hf_api: HfApi | None = None


def _get_hf_api() -> HfApi:
    """获取 HfApi 客户端，显式读取 HF_ENDPOINT 环境变量。

    不使用 HfApi() 的默认行为（依赖库内部读取环境变量），
    而是显式传参，与 run_diagnostics 保持一致。
    """
    global _hf_api
    if _hf_api is None:
        endpoint = os.environ.get("HF_ENDPOINT")
        _hf_api = HfApi(endpoint=endpoint)
    return _hf_api


class HFDatasetSearchTool(BaseTool):
    """在 HuggingFace Hub 上搜索与比赛任务相关的数据集。"""

    def __init__(self, work_root: str = "work") -> None:
        # 构造器注入产物输出根目录，搜索结果的落盘目录
        self.output_dir = Path(work_root) / "hf_dataset_search"

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
        concurrency_safe=False,  # 落盘搜索结果，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        hf = _get_hf_api()
        keywords = input["task_keywords"]
        modality = input.get("modality", "")
        n_results = input.get("n_results", 10)

        # 渐进式回退搜索：keywords+modality → keywords → 返回空
        queries: list[str] = []
        if modality:
            queries.append(f"{keywords} {modality}".strip())
        queries.append(keywords.strip())

        datasets: list[dict] = []
        tried_queries: list[str] = []
        error_msg: str | None = None

        for i, search_query in enumerate(queries):
            tried_queries.append(search_query)
            remaining = queries[i + 1:]

            try:
                results = await asyncio.to_thread(
                    lambda: list(hf.list_datasets(search=search_query, limit=n_results))
                )
            except Exception as exc:
                error_msg = f"HF dataset search failed: {exc}"
                break

            for ds in results:
                datasets.append({
                    "id": getattr(ds, "id", ""),
                    "description": getattr(ds, "description", "") or "",
                    "tags": getattr(ds, "tags", []) or [],
                    "downloads": getattr(ds, "downloads", 0) or 0,
                    "likes": getattr(ds, "likes", 0) or 0,
                })

            if datasets:
                break  # 当前查询有结果，遵守 n_results 限制，不继续回退

        # 空结果时给 LLM 搜索建议
        suggestion = ""
        if not datasets:
            suggestion = (
                f"No datasets found for {tried_queries}. "
                f"Consider broader keywords or a different task description."
            )

        data = {
            "datasets": datasets,
            "count": len(datasets),
            "search_query_used": tried_queries[-1] if tried_queries else "",
            "fallback_chain": tried_queries,
            "suggestion": suggestion,
            "error": error_msg,
        }

        # 落盘 search_results.json（无论成功或失败）
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "search_results.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        if error_msg:
            return ToolResult(success=False, data=None, error=error_msg)

        return ToolResult(data={**data, "output_dir": str(self.output_dir)})



class HFDatasetDownloadTool(BaseTool):
    """从 HuggingFace Hub 下载数据集并保存到本地。"""

    def __init__(self, work_root: str = "work") -> None:
        # 构造器注入产物输出根目录，下载数据的落盘目录
        self.output_dir = Path(work_root) / "hf_dataset_download"

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
        # 使用构造器注入的 output_dir，忽略 LLM 传入的路径（安全原因）
        download_dir = str(self.output_dir)

        Path(download_dir).mkdir(parents=True, exist_ok=True)

        try:
            local_path = await asyncio.to_thread(
                lambda: snapshot_download(
                    repo_id=ds_id, repo_type="dataset", local_dir=download_dir
                )
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                data=None,
                error=f"HF dataset download failed for '{ds_id}': {exc}",
            )

        return ToolResult(
            data={
                "dataset_id": ds_id,
                "local_path": local_path,
                "status": "downloaded",
                "output_dir": download_dir,
            }
        )
