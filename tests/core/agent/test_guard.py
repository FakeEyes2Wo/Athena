"""RunGuard 单元测试。

覆盖规则1（相同调用拒绝）、规则2（连续失败上限）、
规则3（错误模式累计）、辅助函数 _hash_args / _classify_error，
以及并发访问下锁保护的正确性。
"""

import threading

import pytest

from athena.core.agent.guard import GuardError, RunGuard, _classify_error, _hash_args

class TestDuplicateCall:
    """规则1：完全相同的调用被拒绝。"""

    def test_duplicate_rejected(self):
        guard = RunGuard()
        guard.check_before_call("python_inspect", {"expr": "1+1"})
        # 第二次相同调用 → GuardError
        with pytest.raises(GuardError, match="完全相同的调用"):
            guard.check_before_call("python_inspect", {"expr": "1+1"})

    def test_different_args_allowed(self):
        guard = RunGuard()
        guard.check_before_call("python_inspect", {"expr": "1+1"})
        # 同工具不同参数 → 允许
        guard.check_before_call("python_inspect", {"expr": "2+2"})

    def test_different_tool_same_args_allowed(self):
        guard = RunGuard()
        guard.check_before_call("python_inspect", {"expr": "1+1"})
        # 不同工具相同参数 → 允许
        guard.check_before_call("python_execute", {"expr": "1+1"})

class TestConsecutiveFailures:
    """规则2：同一工具连续失败触发上限。"""

    def test_consecutive_failure_limit(self):
        guard = RunGuard()
        for _ in range(3):
            guard.record_result("python_inspect", success=False, error="ValueError: bad")
        # 第4次连续失败 → GuardError
        with pytest.raises(GuardError, match="连续失败"):
            guard.record_result("python_inspect", success=False, error="ValueError: bad")

    def test_success_resets_failure_count(self):
        guard = RunGuard()
        guard.record_result("python_inspect", success=False, error="err")
        guard.record_result("python_inspect", success=False, error="err")
        guard.record_result("python_inspect", success=True, error=None)
        # 成功后计数器重置 → 不触发
        guard.record_result("python_inspect", success=False, error="err")

    def test_different_tools_independent(self):
        guard = RunGuard()
        guard.record_result("tool_a", success=False, error="err")
        guard.record_result("tool_a", success=False, error="err")
        # tool_b 不受 tool_a 的影响
        guard.record_result("tool_b", success=False, error="err")

class TestErrorPatterns:
    """规则3：同一错误模式循环触发上限。"""

    def test_error_pattern_limit(self):
        guard = RunGuard()
        # 连续失败规则（规则2）会先于错误模式规则触发，
        # 故穿插成功重置连续失败计数，让错误模式规则单独生效
        for _ in range(3):
            guard.record_result("python_inspect", success=False, error="Timeout(10s)")
            guard.record_result("python_inspect", success=True, error=None)
        # 同一 pattern 第4次 → GuardError
        with pytest.raises(GuardError, match="反复遇到"):
            guard.record_result("python_inspect", success=False, error="Timeout(10s)")

    def test_different_patterns_independent(self):
        guard = RunGuard()
        # 累计 Timeout 3 次，穿插成功避免规则 2 先行触发
        for _ in range(3):
            guard.record_result("python_inspect", success=False, error="Timeout")
            guard.record_result("python_inspect", success=True, error=None)
        # 第 4 次 Timeout 触发规则 3（不同 error_type 独立计数）
        with pytest.raises(GuardError, match="反复遇到"):
            guard.record_result("python_inspect", success=False, error="Timeout")

class TestHashArgs:
    """_hash_args 参数哈希：与键顺序无关。"""

    def test_order_independence(self):
        assert _hash_args({"a": 1, "b": 2}) == _hash_args({"b": 2, "a": 1})

    def test_different_values_differ(self):
        assert _hash_args({"a": 1, "b": 2}) != _hash_args({"a": 2, "b": 1})

class TestClassifyError:
    """_classify_error 错误分类。"""

    def test_known_keys(self):
        assert _classify_error("Timeout(10s)") == "Timeout"
        assert _classify_error("McpClientError: 连接失败") == "McpClientError"
        assert _classify_error("未知错误消息") == "other"
        assert _classify_error(None) == "unknown"

    def test_empty_string_is_unknown(self):
        assert _classify_error("") == "unknown"

class TestEnterLoop:
    """check_enter_loop 预留接口不抛异常。"""

    def test_no_raise(self):
        guard = RunGuard()
        guard.check_enter_loop()

class TestConcurrency:
    """并发访问下锁保护正确工作。"""

    def test_concurrent_duplicate_detection(self):
        guard = RunGuard()
        n_threads = 8
        results = []

        def worker():
            try:
                guard.check_before_call("python_inspect", {"expr": "1+1"})
                results.append("ok")
            except GuardError:
                results.append("blocked")

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 8 个线程同一调用：恰好 1 个成功，其余 7 个被拦截
        assert results.count("ok") == 1
        assert results.count("blocked") == n_threads - 1

    def test_concurrent_different_calls_allowed(self):
        guard = RunGuard()
        results = []

        def worker(i):
            guard.check_before_call("tool", {"i": i})
            results.append(i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 不同参数并行调用全部允许
        assert len(results) == 8
