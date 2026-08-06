"""数据准备工具 — EDA 分析和数据清洗代码生成。

data_analyze: 扫描 work_root 下的数据目录，读取样本，交给 LLM 生成 EDA 报告。
data_clean_code_gen: 基于 EDA 报告生成清洗脚本，产出 cleaned data DataCard。
"""

import csv
import json
from pathlib import Path

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.utils.single_turn_chat import single_turn_chat

# ── 工具输出目录（扫描数据时排除，避免把生成产物当数据） ──

_TOOL_OUTPUT_DIRS = {
    "data_analyze",
    "data_clean_code_gen",
    "hf_dataset_search",
    "hf_model_search",
}

_ARTIFACT_FILES = {"result.json"}
"""扫描数据时跳过的工具产物文件名（MCP 统一落盘结果）。"""

# ── 系统提示词 ──

_EDA_SYSTEM_PROMPT = """\
You are a data analyst. Given a directory structure and data samples from
downloaded datasets, produce an Exploratory Data Analysis (EDA) report as JSON.

If samples show column names and values, describe their distributions.
If only directory structure is available (no readable samples), describe what
data appears to be present and note that deeper analysis requires loading.

Output JSON with these fields:
- dataset_overview: what datasets were found (names, formats, sizes)
- distributions: per-column distribution description (if columns visible)
- missing_values: columns with missing values and counts (if detectable)
- outliers: columns with outlier issues and description
- correlations: key pairwise correlations (if tabular)
- class_balance: target variable distribution (if classification)

Be quantitative where possible. Note data quality issues that need cleaning.
If samples are insufficient, state what additional information is needed.
"""

# 清洗代码生成提示词：让 LLM 基于 EDA 报告生成可运行的清洗脚本
_CLEAN_SYSTEM_PROMPT = """\
You are a data engineer. Given an EDA report, generate a Python cleaning
script that:

1. Handles missing values (drop or impute with justification)
2. Removes or caps outliers
3. Encodes categorical variables
4. Normalizes/scales numeric features if needed

Output the COMPLETE runnable Python script as a code block. The script
should read data from INPUT_PATH, clean it, and write to OUTPUT_PATH.
"""


# ── 目录扫描 & 样本读取辅助函数 ──


def _collect_data_files(work_root: Path, max_depth: int = 4) -> dict[Path, list[Path]]:
    """递归收集 work_root 下的数据文件，跳过工具输出目录与产物文件。

    Returns:
        {目录: [该目录下的数据文件]}。
    """
    collected: dict[Path, list[Path]] = {}

    def _walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        children = sorted(d.iterdir())
        files = [f for f in children if f.is_file() and f.name not in _ARTIFACT_FILES]
        if files:
            collected[d] = files
        for child in children:
            if child.is_dir() and child.name not in _TOOL_OUTPUT_DIRS:
                _walk(child, depth + 1)

    _walk(work_root, 0)
    return collected


def _read_sample(filepath: Path) -> str:
    """Read a small sample from a data file for LLM analysis."""
    suffix = filepath.suffix.lower()

    try:
        if suffix == ".csv":
            with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                reader = csv.reader(fh)
                rows = []
                for i, row in enumerate(reader):
                    if i >= 6:  # header + 5 data rows
                        break
                    rows.append(row)
                if rows:
                    return (
                        f"[CSV: {len(rows)} rows (header + {len(rows) - 1} data)]\n"
                        + "\n".join(",".join(r) for r in rows)
                    )
                return "[CSV: empty file]"

        elif suffix == ".jsonl":
            with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                lines = []
                for i, line in enumerate(fh):
                    if i >= 3:
                        break
                    lines.append(line.rstrip("\n")[:300])
                return "[JSONL: first 3 lines]\n" + "\n".join(lines)

        elif suffix == ".json":
            with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                data = json.loads(fh.read(4096))
            if isinstance(data, dict):
                keys = list(data.keys())[:30]
                return f"[JSON object with keys]: {keys}"
            elif isinstance(data, list):
                preview = (
                    json.dumps(data[0], ensure_ascii=False)[:300] if data else "empty"
                )
                return f"[JSON array: {len(data)} items, first item]: {preview}"
            return f"[JSON]: {json.dumps(data, ensure_ascii=False)[:500]}"

        elif suffix in (".txt", ".md", ".py", ".yaml", ".yml", ".cfg", ".toml"):
            with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                return f"[text file, first 500 chars]:\n{fh.read(500)}"

        elif suffix in (".parquet", ".arrow", ".feather"):
            return f"[binary {suffix} file, {filepath.stat().st_size} bytes — schema not loaded]"

        else:
            return f"[{suffix or 'no ext'} file, {filepath.stat().st_size} bytes]"

    except Exception as exc:
        return f"[could not read {filepath.name}: {exc}]"


def _scan_work_dir(work_root: Path, max_samples: int = 8) -> tuple[str, str]:
    """Scan work_root for actual data files (recursively, excluding tool outputs).

    Returns:
        (tree_summary, samples_text) — both empty strings if no data found.
    """
    lines: list[str] = []
    samples: list[str] = []
    sample_count = 0

    # 递归收集数据文件（跳过工具输出目录与 MCP 产物）
    dirs = _collect_data_files(work_root)

    if not dirs:
        return "", ""

    for d, files in sorted(dirs.items()):
        if d == work_root:
            rel = "(root)"
        else:
            rel = str(d.relative_to(work_root))
        label = f"{rel}/ ({len(files)} files)"
        lines.append(label)

        for f in files:
            size_kb = f.stat().st_size / 1024
            lines.append(f"  {f.name}  ({size_kb:.1f} KB)")

            if sample_count < max_samples:
                sample = _read_sample(f)
                samples.append(f"--- {f.name} (in {rel}/) ---\n{sample}\n")
                sample_count += 1

    if not any(line.startswith("  ") for line in lines):
        return "", ""

    return "\n".join(lines), "\n".join(samples)


# ── EDA 分析工具 ──


class DataAnalyzeTool(BaseTool):
    """对数据集进行探索性数据分析（EDA），生成 JSON 格式的 EDA 报告。"""

    def __init__(self, work_root: str = "work") -> None:
        # 构造器注入产物输出根目录，EDA 报告的落盘目录
        self.output_dir = Path(work_root) / "data_analyze"

    spec = ToolSpec(
        name="data_analyze",
        description=(
            "Perform exploratory data analysis (EDA) on datasets given "
            "their DataCard references. Returns an EDA report covering "
            "distributions, missing values, outliers, correlations, and "
            "class balance."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "data_card_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of DataCard artifact references.",
                }
            },
            "required": ["data_card_refs"],
            "additionalProperties": False,
        },
        concurrency_safe=False,  # 落盘 EDA 报告，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        refs = input["data_card_refs"]
        if not refs:
            return ToolResult(
                success=False,
                data=None,
                error="At least one DataCard ref is required",
            )

        work_root = self.output_dir.parent

        # ── 扫描 work_root 下的实际数据文件 ──
        tree_summary, samples_text = _scan_work_dir(work_root)
        if not tree_summary:
            return ToolResult(
                success=False,
                data=None,
                error=(
                    "No data files found in the work directory. "
                    "You MUST download a dataset first (via mcp_search_tools "
                    "and the appropriate download tool) before calling "
                    "data_analyze. "
                    "Do NOT fabricate data_card_refs — only pass "
                    "references to actual downloaded data."
                ),
            )

        # ── 用真实数据构建分析 prompt ──
        user_prompt = (
            "Analyze the following downloaded datasets and produce an EDA report.\n\n"
            "## Directory Structure\n"
            f"{tree_summary}\n\n"
            "## Data Samples\n"
            f"{samples_text or '(binary or unreadable files only — infer from file names and sizes)'}"
        )

        # 调用 LLM 生成 EDA 报告，要求 JSON 格式输出
        eda_result = await single_turn_chat(
            system_prompt=_EDA_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format={"type": "json_object"},
        )

        # 解析 EDA 报告为 JSON artifact
        report = json.loads(eda_result)

        # 落盘 EDA 报告到 data_analyze/eda_report.json
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "eda_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return ToolResult(
            data={
                "eda_report": report,
                "data_card_refs": refs,
                "output_dir": str(self.output_dir),
            }
        )


# ── 数据清洗代码生成工具 ──


class DataCleanCodeGenTool(BaseTool):
    """基于 EDA 报告生成数据清洗 Python 脚本，返回脚本内容和元信息。"""

    def __init__(self, work_root: str = "work") -> None:
        # 构造器注入产物输出根目录，清洗脚本的落盘目录
        self.output_dir = Path(work_root) / "data_clean_code_gen"

    spec = ToolSpec(
        name="data_clean_code_gen",
        description=(
            "Generate a data cleaning Python script based on an EDA report. "
            "The script handles missing values, outliers, encoding, and "
            "normalization. Returns the script and cleaned data path."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "eda_report_ref": {
                    "type": "string",
                    "description": "Artifact reference to the EDA report JSON.",
                },
                "data_card_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of DataCard artifact references.",
                },
            },
            "required": ["eda_report_ref", "data_card_refs"],
            "additionalProperties": False,
        },
        concurrency_safe=False,  # 生成脚本后可能执行写入文件系统，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        eda_ref = input["eda_report_ref"]
        data_refs = input["data_card_refs"]

        # ── 校验 EDA 报告已生成 ──
        work_root = self.output_dir.parent
        eda_report_path = work_root / "data_analyze" / "eda_report.json"
        if not eda_report_path.exists():
            return ToolResult(
                success=False,
                data=None,
                error=(
                    f"EDA report not found at {eda_report_path}. "
                    f"You MUST run data_analyze successfully before calling "
                    f"data_clean_code_gen. The EDA report provides the data "
                    f"quality analysis needed to generate a meaningful cleaning script."
                ),
            )

        # 基于 EDA 报告用 LLM 生成清洗脚本

        user_prompt = (
            f"EDA report reference: {eda_ref}\n"
            f"Dataset references: {', '.join(data_refs)}\n\n"
            f"Generate a Python cleaning script for these datasets."
        )

        # 调用 LLM 生成清洗代码，不要求 JSON（输出为 Python 脚本）
        clean_script = await single_turn_chat(
            system_prompt=_CLEAN_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        # 从 LLM 响应中提取 Python 代码块
        from athena.tools._code_utils import extract_code_block

        # 无代码块时返回失败，附上原始响应片段便于排查
        code = extract_code_block(clean_script)
        if code is None:
            return ToolResult(
                success=False,
                error="No code block found in LLM response",
                data={"raw_response": clean_script[:500]},
            )

        # 落盘清洗脚本到 data_clean_code_gen/clean_script.py
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "clean_script.py").write_text(code, encoding="utf-8")

        return ToolResult(
            data={
                "clean_script": clean_script,
                "eda_report_ref": eda_ref,
                "data_card_refs": data_refs,
                "status": "script_generated",
                "output_dir": str(self.output_dir),
            }
        )
