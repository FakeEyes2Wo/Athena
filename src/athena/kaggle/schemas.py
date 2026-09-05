"""Kaggle 域的数据模型：API 元数据 + 流水线控制/报告。

EDA 与 SOTA 分析不在这里做——它们由 prepare/ideator 等 agent 用自己的
workspace + shell 代码生成流程完成（与本地数据集的流程一致）。Kaggle 模块
只负责「取数」：竞赛元数据、下载、notebook/discussion 证据、提交。
"""

from typing import Literal

from pydantic import BaseModel, Field


class Competition(BaseModel):
    ref: str
    title: str = ""
    description: str = ""
    evaluation_metric: str = ""
    reward: str = ""
    deadline: str = ""
    category: str = ""
    organization_name: str = ""
    url: str = ""


class NotebookSummary(BaseModel):
    ref: str
    title: str = ""
    author: str = ""
    total_votes: int = 0
    language: str = ""
    version: str = ""
    url: str = ""


class KaggleRunRequest(BaseModel):
    competition: str = Field(min_length=1)
    download_subdir: str = ""
    max_notebooks: int = Field(default=10, ge=0, le=50)
    download_data: bool = True


class KaggleRunReport(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    competition_ref: str
    status: str = "empty"
    competition: Competition | None = None
    downloaded_files: list[str] = Field(default_factory=list)
    data_manifest_ref: str = ""
    notebooks: list[NotebookSummary] = Field(default_factory=list)
    http_requests: int = 0
    total_seconds: float = 0.0
    warnings: list[str] = Field(default_factory=list)
