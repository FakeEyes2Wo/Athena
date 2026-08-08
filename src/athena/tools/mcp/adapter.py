"""MCP 工具适配器 —— 把发现的 MCP 工具包装成 Athena 的 BaseTool。"""

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec

from athena.tools.mcp.client import McpClientManager

_DOWNLOAD_KEYWORDS = ("download", "get_file")
"""工具名包含这些关键字时，把结果里的 URL/资源块当作文件下载处理。"""

_DOWNLOAD_URL_KEYS = ("download_url", "redirect_url", "url")

_URL_RE = re.compile(r"https?://[^\s'\"]+")


class McpToolAdapter(BaseTool):
    """由一个 MCP 工具定义构建的通用工具。

    每个实例对应一个发现的 MCP 工具：execute() 转发 call_tool，结果归一化后
    统一落盘到 {work_root}/mcp/{server}/{tool}/result.json。
    """

    def __init__(self, manager: McpClientManager, tool: Any, work_root: str) -> None:
        self._manager = manager
        self._mcp_tool = tool
        self._is_downloader = any(kw in tool.name.lower() for kw in _DOWNLOAD_KEYWORDS)
        self.output_dir = Path(work_root) / "mcp" / manager.cfg.name / tool.name
        self.spec = ToolSpec(
            name=manager.full_name(tool.name),
            description=self._build_description(tool),
            input_schema=tool.inputSchema or {"type": "object", "properties": {}},
            concurrency_safe=False,  # 落盘 + 网络调用，不可并行
        )

    def _build_description(self, tool: Any) -> str:
        desc = (tool.description or "").strip()
        return desc or f"MCP 工具 {tool.name}（来源 {self._manager.cfg.name}）"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """转发 MCP call_tool 调用，把结果归一化并落盘后返回 ToolResult。"""
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 对 sandbox 工具打印执行信息到终端（stdout，与 demo 输出同流）
        _print_sandbox_input(self._mcp_tool.name, input)

        try:
            result = await self._manager.call_tool(self._mcp_tool.name, arguments=input)
            payload, saved_files = await self._normalize(result, ctx)
        except Exception as exc:
            # MCP 调用失败或归一化/下载抛错（超时/断连/HTTP 错误）→ 落盘错误并返回失败
            err_msg = f"{type(exc).__name__}: {exc}"
            print(f"\n⚠ MCP 工具 [{self.spec.name}] 连接/下载失败: {err_msg}")
            return self._persist(
                {"error": err_msg},
                success=False,
                error=str(exc),
            )

        # 对 sandbox 工具打印返回结果到终端
        _print_sandbox_result(self._mcp_tool.name, payload)

        if getattr(result, "isError", False):
            text = _collect_text(result)
            raw_err = text or "MCP 工具返回 isError"
            enhanced = _enhance_error(
                raw_err,
                self._manager.cfg.name,
                self._mcp_tool.name,
            )
            print(f"\n⚠ MCP 工具 [{self.spec.name}] 返回错误: {raw_err}")
            if enhanced != raw_err:
                hint = enhanced.split("\n", 1)[1] if "\n" in enhanced else enhanced
                print(f"  {hint}")
            return self._persist(
                {"error": enhanced},
                success=False,
                error=enhanced,
            )

        return self._persist({**payload, "files": saved_files}, success=True)

        return self._persist({**payload, "files": saved_files}, success=True)

    async def _normalize(self, result: Any, ctx: ToolContext) -> tuple[dict, list[str]]:
        """把 MCP 结果归一化为 payload（文本/结构化/文件），返回保存的文件列表。"""
        payload: dict[str, Any] = {}
        text_parts: list[str] = []
        saved_files: list[str] = []

        structured = getattr(result, "structuredContent", None)
        if structured:
            payload["structured_content"] = structured

        for block in getattr(result, "content", []):
            btype = getattr(block, "type", "")
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype in ("image", "audio"):
                saved_files.append(
                    _save_binary_block(block, self.output_dir, ctx.call_id)
                )
            elif btype == "resource":
                saved_files.append(
                    _save_resource_block(block, self.output_dir, ctx.call_id)
                )

        if text_parts:
            payload["text"] = "\n".join(text_parts)

        if self._is_downloader:
            url = _extract_download_url(payload)
            if url:
                saved_files += await self._download_to_disk(url)
        return payload, saved_files

    async def _download_to_disk(self, url: str) -> list[str]:
        """跟随重定向下载文件到输出目录，返回保存的文件名。"""
        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            fname = _infer_filename(resp, url)
            (self.output_dir / fname).write_bytes(resp.content)
            return [fname]

    def _persist(
        self,
        payload: dict,
        *,
        success: bool = True,
        error: str | None = None,
    ) -> ToolResult:
        """把调用结果落盘到 result.json，并构造返回给 LLM 的 ToolResult。"""
        record = {
            "server": self._manager.cfg.name,
            "tool": self.spec.name,
            "success": success,
            "error": error,
            "output_dir": str(self.output_dir),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **payload,
        }
        (self.output_dir / "result.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        data = {
            "server": self._manager.cfg.name,
            "tool": self.spec.name,
            "text_preview": _preview(payload),
            "files": payload.get("files", []),
            "output_dir": str(self.output_dir),
        }
        if not success:
            return ToolResult(success=False, error=error, data=data)
        return ToolResult(data=data)


def _enhance_error(raw: str, server: str, tool: str) -> str:
    """根据 MCP 服务返回的原始错误，给出可操作的指导信息。"""
    lower = raw.lower()
    if "unauthenticated" in lower:
        return (
            f"{raw}\n\n"
            f"[操作提示] 该操作需要额外授权，原因可能是：\n"
            f"  1. 未在 {server} 网站接受该竞赛/资源的规则条款\n"
            f"  2. 当前 API 凭证没有该操作的权限\n"
            f"请前往 {server} 网站完成授权后重试"
        )
    if "need to agree" in lower:
        return (
            f"{raw}\n\n"
            f"[操作提示] 你需要先在 {server} 网站上同意该竞赛的规则条款，"
            f"然后才能下载数据。请前往 {server} 网站登录并接受规则后重试"
        )
    if "not found" in lower:
        return (
            f"{raw}\n\n"
            f"[操作提示] 该资源未找到，请检查参数是否正确，"
            f"或先用 mcp_search_tools 搜索确认该能力是否存在"
        )
    return raw


def _collect_text(result: Any) -> str:
    """拼接 MCP 结果中的全部文本块。"""
    parts = [
        getattr(b, "text", "")
        for b in getattr(result, "content", [])
        if getattr(b, "type", "") == "text"
    ]
    return "\n".join(p for p in parts if p)


def _save_binary_block(block: Any, output_dir: Path, call_id: str) -> str:
    """把 image/audio 二进制块解码保存，返回文件名。"""
    ext = "png" if getattr(block, "type", "") == "image" else "bin"
    fname = f"{_safe_call_id(call_id)}.{ext}"
    data = getattr(block, "data", "")
    (output_dir / fname).write_bytes(base64.b64decode(data))
    return fname


def _save_resource_block(block: Any, output_dir: Path, call_id: str) -> str:
    """把嵌入式资源块保存到磁盘，返回文件名。"""
    uri = getattr(block, "uri", "") or ""
    ext = (getattr(block, "mimeType", "") or "").split("/")[-1] or "bin"
    fname = f"{_safe_call_id(call_id)}-{Path(uri).name or 'resource'}.{ext}"
    data = getattr(block, "data", None)
    if data is None:
        # 无内联数据，仅记录文件名，不写盘
        return fname
    raw = base64.b64decode(data) if isinstance(data, str) else data
    (output_dir / fname).write_bytes(raw)
    return fname


def _safe_call_id(call_id: str) -> str:
    """把 call_id 规整为安全文件名。"""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in call_id)


def _extract_download_url(payload: dict) -> str | None:
    """从工具自身返回的结果里提取下载 URL；只在结果字段中查找。"""
    structured = payload.get("structured_content")
    candidates: list[Any] = []
    if isinstance(structured, dict):
        candidates.extend(structured.get(k) for k in _DOWNLOAD_URL_KEYS)
    text = payload.get("text", "")
    if text:
        m = _URL_RE.search(text)
        if m:
            candidates.append(m.group(0))
    for val in candidates:
        if isinstance(val, str) and val.startswith(("http://", "https://")):
            return val
    return None


def _infer_filename(resp: Any, url: str) -> str:
    """从 Content-Disposition 或 URL 推断下载文件名。"""
    disposition = resp.headers.get("content-disposition", "")
    if "filename=" in disposition:
        candidate = disposition.split("filename=")[-1].strip('"').split(";")[0].strip()
        if candidate:
            return candidate
    name = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return name or "download.bin"


def _preview(payload: dict) -> str:
    """生成返回给 LLM 的文本预览（最多 2000 字符）。"""
    text = payload.get("text", "")
    if text:
        return text[:2000]
    structured = payload.get("structured_content")
    if isinstance(structured, dict):
        return json.dumps(structured, ensure_ascii=False)[:2000]
    return ""


_SANDBOX_TOOLS = {"python_inspect", "python_execute"}
_SEP = "─" * 50


def _print_sandbox_input(tool_name: str, args: dict) -> None:
    """对 sandbox 工具，打印即将执行的代码到终端 stdout。"""
    if tool_name not in _SANDBOX_TOOLS:
        return
    cwd = args.get("cwd", ".")
    if tool_name == "python_inspect":
        expr = args.get("expr", "")
        print(f"\n{_SEP}\n[SANDBOX INSPECT] expr: {expr}\n[SANDBOX INSPECT] cwd:  {cwd}\n{_SEP}", flush=True)
    elif tool_name == "python_execute":
        script = args.get("script", "")
        lines = script.count("\n") + 1 if script else 0
        print(f"\n{_SEP}\n[SANDBOX EXECUTE] ({lines} lines, cwd={cwd})\n{script}\n{_SEP}", flush=True)


def _print_sandbox_result(tool_name: str, payload: dict) -> None:
    """对 sandbox 工具，解析 JSON 结果并打印 stdout/stderr/error 到终端。"""
    if tool_name not in _SANDBOX_TOOLS:
        return
    text = payload.get("text", "")
    if not text:
        return
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return
    stdout = data.get("stdout", "")
    stderr = data.get("stderr", "")
    error = data.get("error", "")
    value = data.get("value", "")
    ok = data.get("ok", True)
    if not ok:
        print(f"[SANDBOX ERROR] {error}", flush=True)
    if stdout:
        print(f"[SANDBOX STDOUT]\n{stdout.strip()}", flush=True)
    if stderr:
        print(f"[SANDBOX STDERR]\n{stderr.strip()}", flush=True)
    # inspect 的结果值
    if value and value != "None":
        print(f"[SANDBOX VALUE] {value}", flush=True)
    print(_SEP, flush=True)
