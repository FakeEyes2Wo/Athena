"""Focused tests for the fail-closed authority preflight boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from athena.core.tool_types import ToolContext
from athena.research.prepare.authority import BaselineAuthorityError
from athena.research.runtime import environment_repair


class _Authority:
    def __init__(self, failures: list[BaseException] | None = None) -> None:
        self.failures = list(failures or [])
        self.loads = 0

    async def load(self):
        self.loads += 1
        if self.failures:
            raise self.failures.pop(0)


def _runtime(authority: Any, *, provider: object | None = object()):
    audit: list[str] = []

    async def publish_output(**kwargs: Any) -> None:
        audit.append(kwargs["text"])

    return SimpleNamespace(
        baseline_authority=authority,
        provider=provider,
        registry=SimpleNamespace(),
        agents=SimpleNamespace(),
        store=object(),
        publish_output=publish_output,
        audit=audit,
    )


@pytest.mark.asyncio
async def test_missing_authority_fails_before_any_repair() -> None:
    runtime = _runtime(None)
    with pytest.raises(BaselineAuthorityError, match="external baseline authority"):
        await environment_repair.preflight(runtime, {"refresh": lambda: None})
    assert runtime.audit == []


@pytest.mark.asyncio
async def test_transient_failure_repairs_then_rechecks_authority(monkeypatch) -> None:
    runtime = _runtime(_Authority([ConnectionError("secret transport detail")]))
    attempts: list[dict[str, Any]] = []

    async def fake_repair(rt, actions):
        attempts.append(dict(actions))

    monkeypatch.setattr(environment_repair, "_run_repair_agent", fake_repair)
    await environment_repair.preflight(runtime, {"refresh": lambda: None})

    assert runtime.baseline_authority.loads == 2
    assert len(attempts) == 1
    assert list(attempts[0]) == ["refresh"]
    assert all("secret transport detail" not in message for message in runtime.audit)
    assert any("status=repairing" in message for message in runtime.audit)


@pytest.mark.asyncio
async def test_repeated_transient_failure_is_bounded_to_two_repairs(
    monkeypatch,
) -> None:
    runtime = _runtime(
        _Authority([ConnectionError("x"), TimeoutError("y"), ConnectionError("z")])
    )
    repairs = 0

    async def fake_repair(_runtime, _actions):
        nonlocal repairs
        repairs += 1

    monkeypatch.setattr(environment_repair, "_run_repair_agent", fake_repair)
    with pytest.raises(BaselineAuthorityError, match="bounded repair"):
        await environment_repair.preflight(runtime, {"refresh": lambda: None})
    assert repairs == environment_repair.MAX_REPAIR_ATTEMPTS
    assert runtime.baseline_authority.loads == 3


@pytest.mark.asyncio
async def test_nonrecoverable_authority_error_never_starts_repair(monkeypatch) -> None:
    runtime = _runtime(_Authority([BaselineAuthorityError("contract failure")]))
    started = False

    async def fake_repair(_runtime, _actions):
        nonlocal started
        started = True

    monkeypatch.setattr(environment_repair, "_run_repair_agent", fake_repair)
    with pytest.raises(BaselineAuthorityError, match="nonrepairable"):
        await environment_repair.preflight(runtime, {"refresh": lambda: None})
    assert not started
    assert all("contract failure" not in message for message in runtime.audit)
    assert any("status=nonrepairable" in message for message in runtime.audit)


@pytest.mark.asyncio
async def test_repair_tool_is_single_allowlisted_operation_and_redacts_callback_error() -> (
    None
):
    calls: list[str] = []

    def callback() -> None:
        calls.append("called")
        raise RuntimeError("controller secret")

    tools = environment_repair._callback_tools({"refresh": callback})
    assert [spec.name for spec in tools.specs] == ["perform_operation"]

    async def emit(*_args: Any) -> None:
        return None

    context = ToolContext("perform_operation", "call-1", emit, asyncio.Event())
    tool = tools.resolve("perform_operation")
    first = await tool.ainvoke(context, operation="refresh")
    second = await tool.ainvoke(context, operation="refresh")
    assert first.data == {"status": "failed"}
    assert second.data == {"status": "already_used"}
    assert first.error is None
    assert calls == ["called"]
