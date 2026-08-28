"""Headless runner contracts for Windows-safe output and interruption."""

import argparse
import asyncio
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_headless


def test_headless_search_defaults_remain_backward_compatible() -> None:
    args = run_headless._parse(["--project", "project", "--task", "task"])

    assert args.search_limit == 10
    assert args.ideator_count == 3
    assert args.hypotheses_per_ideator == 2
    assert args.pro_reasoning is False


@pytest.mark.asyncio
async def test_headless_explicit_search_and_pro_reasoning_are_forwarded(
    monkeypatch, tmp_path
) -> None:
    captured: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)
            self.state = SimpleNamespace(
                phase="VALIDATE",
                status="COMPLETED",
                search_limit=kwargs["search_limit"],
                ideator_count=kwargs["ideator_count"],
                hypotheses_per_ideator=kwargs["hypotheses_per_ideator"],
            )

        def subscribe(self, _callback):
            return "subscription"

        async def start(self):
            async def completed():
                return None

            return asyncio.create_task(completed())

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(run_headless, "ResearchRuntime", FakeRuntime)
    monkeypatch.setattr(
        run_headless,
        "settings",
        SimpleNamespace(
            model_name=lambda: "deepseek-flash",
            pro_model_name=lambda: "deepseek-pro",
        ),
    )
    args = run_headless._parse(
        [
            "--project",
            str(tmp_path),
            "--task",
            "task",
            "--search-limit",
            "3",
            "--ideator-count",
            "3",
            "--hypotheses-per-ideator",
            "5",
            "--pro-reasoning",
        ]
    )

    assert await run_headless._run(args) == 0
    assert captured == {
        "project_root": Path(tmp_path),
        "model": "deepseek-flash",
        "reasoning_model": "deepseek-pro",
        "reasoning_thinking": True,
        "task": "task",
        "auto_validate": True,
        "search_limit": 3,
        "ideator_count": 3,
        "hypotheses_per_ideator": 5,
    }


@pytest.mark.asyncio
async def test_headless_rejects_silent_resume_setting_mismatch(
    monkeypatch, tmp_path
) -> None:
    state_path = tmp_path / ".athena" / "state.json"
    state_path.parent.mkdir()
    state_path.write_text("{}", encoding="utf-8")
    calls: list[str] = []

    class FakeRuntime:
        def __init__(self, **_kwargs) -> None:
            self.state = SimpleNamespace(
                phase="SEARCH",
                status="STOPPED",
                search_limit=2,
                ideator_count=3,
                hypotheses_per_ideator=2,
            )

        async def aclose(self) -> None:
            calls.append("aclose")

    monkeypatch.setattr(run_headless, "ResearchRuntime", FakeRuntime)
    monkeypatch.setattr(
        run_headless,
        "settings",
        SimpleNamespace(model_name=lambda: "flash", pro_model_name=lambda: "pro"),
    )
    args = run_headless._parse(
        [
            "--project",
            str(tmp_path),
            "--task",
            "task",
            "--search-limit",
            "3",
            "--hypotheses-per-ideator",
            "5",
            "--pro-reasoning",
        ]
    )

    assert await run_headless._run(args) == 2
    assert calls == ["aclose"]


def test_configure_utf8_stdio_preserves_legacy_pipe_encoding(monkeypatch) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(run_headless, "_parent_console_encoding", lambda: "gbk")
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)

    run_headless._configure_utf8_stdio()
    print("任务理解中：UTF-8 probe ✓", file=stream, flush=True)

    assert stream.encoding.lower() == "gbk"
    decoded = buffer.getvalue().decode("gbk")
    assert "任务理解中" in decoded
    assert "\\u2713" in decoded


def test_configure_utf8_stdio_matches_utf8_console(monkeypatch) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(run_headless, "_parent_console_encoding", lambda: "utf-8")
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)

    run_headless._configure_utf8_stdio()
    print("任务理解中：✓", file=stream, flush=True)

    assert stream.encoding.lower().replace("-", "") == "utf8"
    assert "任务理解中：✓" in buffer.getvalue().decode("utf-8")


def test_configure_stdio_honors_explicit_utf8_over_console_code_page(
    monkeypatch,
) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(run_headless, "_parent_console_encoding", lambda: "gbk")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    monkeypatch.setenv("PYTHONUTF8", "1")

    run_headless._configure_utf8_stdio()
    print("任务理解中：✓", file=stream, flush=True)

    assert stream.encoding.lower().replace("-", "") == "utf8"
    assert "任务理解中：✓" in buffer.getvalue().decode("utf-8")


@pytest.mark.asyncio
async def test_cancelled_headless_run_persists_stopped_state(monkeypatch) -> None:
    calls: list[str] = []

    class FakeRuntime:
        def __init__(self, **_kwargs) -> None:
            self.state = SimpleNamespace(
                phase="SEARCH",
                status="RUNNING",
                search_limit=_kwargs["search_limit"],
                ideator_count=_kwargs["ideator_count"],
                hypotheses_per_ideator=_kwargs["hypotheses_per_ideator"],
            )

        def subscribe(self, _callback):
            return "subscription"

        async def start(self):
            async def cancelled():
                raise asyncio.CancelledError

            return asyncio.create_task(cancelled())

        async def message(self, text: str) -> str:
            calls.append(text)
            self.state.status = "STOPPED"
            return "STOPPED"

        async def aclose(self) -> None:
            calls.append("aclose")

    monkeypatch.setattr(run_headless, "ResearchRuntime", FakeRuntime)
    monkeypatch.setattr(
        run_headless, "settings", SimpleNamespace(model_name=lambda: "fake")
    )
    args = argparse.Namespace(
        project="project",
        task="task",
        data=None,
        search_limit=2,
        ideator_count=3,
        hypotheses_per_ideator=2,
        pro_reasoning=False,
    )

    exit_code = await run_headless._run(args)

    assert exit_code == 130
    assert calls == ["/stop", "aclose"]
