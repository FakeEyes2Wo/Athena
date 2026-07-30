"""Kaggle MCP wrapper tools — 比赛信息搜索和 discussion 搜索。

通过 Kaggle MCP (Model Context Protocol) 服务与 Kaggle 平台交互。
该 MCP 服务负责页面抓取、鉴权和 API 调用，本模块只做薄封装。
"""

import json
from typing import Any

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.workflows.prepare.task_parser import parse_competition_info


# ── Kaggle MCP 客户端获取 ──

def _get_kaggle_mcp_client() -> Any:
    """获取 Kaggle MCP 客户端。

    当前实现为 stub：TODO 后续接入实际 MCP 服务。
    """
    # TODO: 与实际 Kaggle MCP 服务建立连接
    raise NotImplementedError("Kaggle MCP client not yet configured")


# ── 比赛信息搜索工具 ──

class KaggleCompetitionSearchTool(BaseTool):
    """搜索 Kaggle 比赛基本信息：描述、评价指标、数据格式、提交要求。"""

    spec = ToolSpec(
        name="kaggle_competition_search",
        description=(
            "Search Kaggle competition for description, evaluation metric, "
            "data format, and submission requirements. Returns structured "
            "TaskMetaData and full description."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "competition_url": {
                    "type": "string",
                    "description": "Kaggle competition URL or slug.",
                }
            },
            "required": ["competition_url"],
            "additionalProperties": False,
        },
        concurrency_safe=False,  # 外部 HTTP 调用，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        competition_url = input["competition_url"]

        # 通过 Kaggle MCP 拉取比赛页面内容
        mcp = _get_kaggle_mcp_client()
        raw_html = await mcp.call_tool(
            "fetch_competition_page", {"url": competition_url}
        )

        # 用 LLM 解析为结构化 TaskMetaData
        metadata = await parse_competition_info(raw_html, competition_url)

        return ToolResult(
            data={
                "task_type": metadata.task_type,
                "data_type": metadata.data_type,
                "target_vars": metadata.target_vars,
                "primary_metric": {
                    "name": metadata.primary_metric.name,
                    "direction": metadata.primary_metric.direction,
                },
                "constraints": metadata.constraints,
                "source_url": competition_url,
                "description_text": raw_html,
            }
        )


# ── Discussion 搜索工具 ──

class KaggleDiscussionSearchTool(BaseTool):
    """搜索 Kaggle Discussion/Notebook 中的 Top 方案思路。"""

    spec = ToolSpec(
        name="kaggle_discussion_search",
        description=(
            "Search Kaggle competition discussions and notebooks for top "
            "solutions, feature engineering approaches, and model ideas. "
            "Returns a list of summarized entries with scores and links."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "competition_url": {
                    "type": "string",
                    "description": "Kaggle competition URL or slug.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of top discussions to return.",
                    "default": 10,
                },
            },
            "required": ["competition_url"],
            "additionalProperties": False,
        },
        concurrency_safe=False,  # 外部 HTTP 调用，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        competition_url = input["competition_url"]
        top_k = input.get("top_k", 10)

        mcp = _get_kaggle_mcp_client()
        raw = await mcp.call_tool(
            "search_discussions",
            {"url": competition_url, "top_k": top_k},
        )
        discussions = json.loads(raw) if isinstance(raw, str) else raw

        # 构建结构化摘要列表
        summaries = []
        for d in discussions.get("discussions", []):
            summaries.append({
                "title": d.get("title", ""),
                "author": d.get("author", ""),
                "approach_summary": d.get("summary", ""),
                "score": d.get("score"),
                "url": d.get("url", ""),
            })

        return ToolResult(
            data={
                "discussion_count": len(summaries),
                "discussions": summaries,
            }
        )


# ── 数据集下载工具 ──

class KaggleDatasetDownloadTool(BaseTool):
    """通过 Kaggle MCP 下载比赛数据集并生成 DataCard。"""

    spec = ToolSpec(
        name="kaggle_dataset_download",
        description=(
            "Download the competition dataset via Kaggle MCP. Saves data "
            "to the specified output directory and returns a DataCard with "
            "content fingerprint and schema information."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "competition_ref": {
                    "type": "string",
                    "description": "Kaggle competition URL or slug.",
                },
                "output_dir": {
                    "type": "string",
                    "description": "Local directory path to save the dataset.",
                },
            },
            "required": ["competition_ref", "output_dir"],
            "additionalProperties": False,
        },
        concurrency_safe=False,  # 下载写入文件系统，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        competition_ref = input["competition_ref"]
        output_dir = input["output_dir"]

        # 通过 Kaggle MCP 下载数据集
        mcp = _get_kaggle_mcp_client()
        download_result = await mcp.call_tool(
            "download_competition_data",
            {"url": competition_ref, "output_dir": output_dir},
        )

        # 对每个下载的数据文件生成 DataCard
        data_cards = []
        for file_path in download_result.get("files", []):
            # 注意：此处需要 ArtifactStore 实例，由调用上下文注入
            # 作为初始实现，返回文件路径让 Agent 后续处理
            data_cards.append({
                "file": file_path,
                "status": "downloaded",
            })

        return ToolResult(
            data={
                "competition_ref": competition_ref,
                "output_dir": output_dir,
                "files": data_cards,
                "status": "downloaded",
            }
        )
