"""RunGuard —— Agent ReAct loop 硬约束。

在运行时层面防止 Agent 陷入死循环：相同调用拒绝、连续失败限制、
相同错误模式累计检测。不依赖 System Prompt。
"""

import json
import threading
from dataclasses import dataclass, field

class GuardError(RuntimeError):
    """RunGuard 拦截死循环/重复调用时抛出。

    被 Agent.run() 捕获后返回 AgentOutcome（带 error 文本），
    Turn 终止但 Thread 存活。
    """


@dataclass
class RunGuard:
    """每个 Agent.run() 调用创建一个 fresh 实例。

    三条规则：
    1. 完全相同的调用 → 拒绝
    2. 同一工具连续失败超过 N 次 → GuardError
    3. 同一 (tool, error_type) 累计 3 次 → GuardError
    """

    max_identical_calls: int = 1
    """同一 (tool, args) 最多允许的调用次数。"""

    max_consecutive_failures: int = 3
    """同一工具连续失败上限。"""

    max_same_approach_failures: int = 3
    """同一 (tool, error_type) 模式累计上限。"""

    _call_hashes: set[tuple[str, int]] = field(default_factory=set)
    """已调用过的 (tool_name, args_hash) 集合。"""

    _consecutive_failures: dict[str, int] = field(default_factory=dict)
    """tool_name → 当前连续失败次数。"""

    _error_patterns: dict[tuple[str, str], int] = field(default_factory=dict)
    """(tool_name, err_key) → 累计出现次数。"""

    def __post_init__(self) -> None:
        """初始化线程锁。"""
        self._lock = threading.Lock()

    def check_enter_loop(self) -> None:
        """进入下一轮采样前的准入检查。"""
        pass

    def check_before_call(self, tool_name: str, args: dict) -> None:
        """工具调用前准入检查。

        Args:
            tool_name: 工具名
            args: 工具参数

        Raises:
            GuardError: 相同调用已执行过
        """
        with self._lock:
            call_hash = (tool_name, _hash_args(args))
            if call_hash in self._call_hashes:
                raise GuardError(
                    f"完全相同的调用 {tool_name}({_summarize_args(args)}) 已执行过。"
                    f"请改变参数或换一种方式。"
                )
            self._call_hashes.add(call_hash)

    def record_result(
        self, tool_name: str, success: bool, error: str | None
    ) -> None:
        """工具返回后更新计数器。

        Args:
            tool_name: 工具名
            success: 调用是否成功
            error: 失败时的错误信息

        Raises:
            GuardError: 连续失败或错误模式循环触及上限
        """
        with self._lock:
            if success:
                # 成功后重置该工具的连续失败计数
                self._consecutive_failures.pop(tool_name, None)
                return

            # 连续失败计数
            self._consecutive_failures[tool_name] = (
                self._consecutive_failures.get(tool_name, 0) + 1
            )
            if self._consecutive_failures[tool_name] > self.max_consecutive_failures:
                raise GuardError(
                    f"{tool_name} 已连续失败 {self.max_consecutive_failures} 次。"
                    f"请换一种完全不同的方式。"
                )

            # 错误模式计数
            err_key = _classify_error(error)
            pattern = (tool_name, err_key)
            self._error_patterns[pattern] = (
                self._error_patterns.get(pattern, 0) + 1
            )
            if self._error_patterns[pattern] > self.max_same_approach_failures:
                raise GuardError(
                    f"{tool_name} 反复遇到 '{err_key}' 错误。"
                    f"当前策略无效，请改变策略。"
                )


def _hash_args(args: dict) -> int:
    """对工具参数字典做稳定哈希。"""
    return hash(
        json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    )


def _classify_error(error: str | None) -> str:
    """从 error 消息中提取错误分类键。"""
    if not error:
        return "unknown"
    for key in (
        "Timeout", "MemoryError", "ImportError", "NameError",
        "ValueError", "TypeError", "FileNotFoundError", "KeyError",
        "PermissionError", "ConnectionError", "McpClientError",
    ):
        if key in error:
            return key
    return "other"


def _summarize_args(args: dict) -> str:
    """生成参数摘要字符串用于错误消息。"""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 50:
            s = s[:47] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)
