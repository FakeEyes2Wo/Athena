"""Test CodeEngine iteration and failure boundaries."""

import os
import tempfile
import pytest
from athena.code.engine import CodeEngine, EngineResult
from athena.code.backends.base import CodeBackend, GenerationResult, ExecutionOutput
from athena.code.monitor import AgentMonitor
from athena.code.output_specs import OutputSpec


class MockBackend(CodeBackend):
    """Backend that writes a fixed script on first call, then stops."""

    def __init__(self):
        self.call_count = 0

    async def generate(self, prompt, target_dir, previous_outputs, history):
        self.call_count += 1
        if self.call_count == 1:
            script = os.path.join(target_dir, "script.py")
            with open(script, "w") as f:
                f.write(
                    "print('round 1')\nwith open('out.txt','w') as fh: fh.write('data')\n"
                )
            return GenerationResult(files_created=["script.py"])
        return GenerationResult(output="Done, no more changes needed.")


@pytest.mark.asyncio
async def test_engine_single_round():
    """Engine runs at least one round: generate→execute→observe."""
    with tempfile.TemporaryDirectory() as tmp:
        backend = MockBackend()
        monitor = AgentMonitor()
        engine = CodeEngine(backend=backend, monitor=monitor)
        result = await engine.run(
            prompt="Write a test script", target_dir=tmp, max_rounds=3
        )
        assert result.rounds >= 1
        assert result.success
        assert backend.call_count >= 1


@pytest.mark.asyncio
async def test_engine_stops_when_backend_says_done():
    """Engine stops iterating when backend returns empty file list and 'Done' output."""
    with tempfile.TemporaryDirectory() as tmp:
        backend = MockBackend()
        monitor = AgentMonitor()
        engine = CodeEngine(backend=backend, monitor=monitor)
        result = await engine.run(
            prompt="Write a test script", target_dir=tmp, max_rounds=3
        )
        assert result.rounds <= 2  # Should stop after 2nd call returns empty
        assert result.success


@pytest.mark.asyncio
async def test_engine_respects_max_rounds():
    """Engine stops at max_rounds without fabricating completion."""

    class NeverDoneBackend(CodeBackend):
        async def generate(self, prompt, target_dir, previous_outputs, history):
            p = os.path.join(target_dir, f"file_{len(previous_outputs)}.py")
            with open(p, "w") as f:
                f.write("raise RuntimeError('not complete')\n")
            return GenerationResult(files_created=[os.path.basename(p)])

    with tempfile.TemporaryDirectory() as tmp:
        engine = CodeEngine(backend=NeverDoneBackend(), monitor=AgentMonitor())
        result = await engine.run(prompt="Keep going", target_dir=tmp, max_rounds=3)
        assert result.rounds == 3
        assert result.success is False


@pytest.mark.asyncio
async def test_engine_does_not_fabricate_success_after_all_rounds_fail(
    tmp_path,
) -> None:
    """Repeated execution failures produce a failed result at exhaustion."""

    class AlwaysFailBackend(CodeBackend):
        async def generate(self, prompt, target_dir, previous_outputs, history):
            script = os.path.join(target_dir, "fail.py")
            with open(script, "w", encoding="utf-8") as file:
                file.write("raise RuntimeError('failed round')\n")
            return GenerationResult(files_modified=["fail.py"])

    engine = CodeEngine(backend=AlwaysFailBackend(), monitor=AgentMonitor())

    result = await engine.run(prompt="x", target_dir=str(tmp_path), max_rounds=2)

    assert result.rounds == 2
    assert result.success is False
    assert result.final_output is not None
    assert result.final_output.returncode != 0


@pytest.mark.asyncio
async def test_engine_fails_when_required_output_is_missing(tmp_path) -> None:
    """A clean process exit is insufficient when a required output is absent."""

    class MissingOutputBackend(CodeBackend):
        async def generate(self, prompt, target_dir, previous_outputs, history):
            script = os.path.join(target_dir, "work.py")
            with open(script, "w", encoding="utf-8") as file:
                file.write("print('ok')\n")
            return GenerationResult(files_created=["work.py"])

    engine = CodeEngine(backend=MissingOutputBackend(), monitor=AgentMonitor())

    result = await engine.run(
        prompt="x",
        target_dir=str(tmp_path),
        max_rounds=1,
        output_spec=OutputSpec(must_exist=["REPORT.md"]),
    )

    assert result.success is False
    assert result.final_output is not None
    assert "REPORT.md" in result.final_output.stderr


@pytest.mark.asyncio
async def test_engine_passes_review_failure_to_next_revision_round(tmp_path) -> None:
    """Missing-output feedback reaches the backend's next generation call."""

    class RevisingBackend(CodeBackend):
        def __init__(self) -> None:
            self.feedback = ""

        async def generate(self, prompt, target_dir, previous_outputs, history):
            if not previous_outputs:
                script = os.path.join(target_dir, "work.py")
                with open(script, "w", encoding="utf-8") as file:
                    file.write("print('ok')\n")
                return GenerationResult(files_created=["work.py"])

            self.feedback = previous_outputs[-1].stderr
            with open(
                os.path.join(target_dir, "REPORT.md"), "w", encoding="utf-8"
            ) as file:
                file.write("complete\n")
            return GenerationResult(output="Done")

    backend = RevisingBackend()
    engine = CodeEngine(backend=backend, monitor=AgentMonitor())

    result = await engine.run(
        prompt="x",
        target_dir=str(tmp_path),
        max_rounds=2,
        output_spec=OutputSpec(must_exist=["REPORT.md"]),
    )

    assert result.success is True
    assert "REPORT.md" in backend.feedback


@pytest.mark.asyncio
async def test_engine_does_not_treat_done_as_recovery_from_execution_failure(
    tmp_path,
) -> None:
    """A done message cannot turn a failed script into success."""

    class FailThenDoneBackend(CodeBackend):
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, prompt, target_dir, previous_outputs, history):
            self.calls += 1
            if self.calls == 1:
                with open(
                    os.path.join(target_dir, "REPORT.md"), "w", encoding="utf-8"
                ) as file:
                    file.write("present before execution\n")
                script = os.path.join(target_dir, "fail.py")
                with open(script, "w", encoding="utf-8") as file:
                    file.write("raise RuntimeError('still broken')\n")
                return GenerationResult(files_created=["fail.py"])
            return GenerationResult(output="Done")

    engine = CodeEngine(backend=FailThenDoneBackend(), monitor=AgentMonitor())

    result = await engine.run(
        prompt="x",
        target_dir=str(tmp_path),
        max_rounds=2,
        output_spec=OutputSpec(must_exist=["REPORT.md"]),
    )

    assert result.success is False
    assert result.final_output is not None
    assert "still broken" in result.final_output.stderr


@pytest.mark.asyncio
async def test_engine_done_without_script_requires_all_outputs(tmp_path) -> None:
    """A done response without a script cannot bypass missing required outputs."""

    class DoneBackend(CodeBackend):
        async def generate(self, prompt, target_dir, previous_outputs, history):
            return GenerationResult(output="Done")

    engine = CodeEngine(backend=DoneBackend(), monitor=AgentMonitor())

    result = await engine.run(
        prompt="x",
        target_dir=str(tmp_path),
        max_rounds=1,
        output_spec=OutputSpec(must_exist=["REPORT.md"]),
    )

    assert result.success is False
    assert result.final_output is not None
    assert "REPORT.md" in result.final_output.stderr


@pytest.mark.asyncio
async def test_engine_completes_after_successful_required_output(tmp_path) -> None:
    """A successful script completes immediately once required outputs exist."""

    class SuccessfulBackend(CodeBackend):
        async def generate(self, prompt, target_dir, previous_outputs, history):
            script = os.path.join(target_dir, "work.py")
            with open(script, "w", encoding="utf-8") as file:
                file.write("with open('REPORT.md', 'w') as report: report.write('ok')\n")
            return GenerationResult(files_created=["work.py"])

    engine = CodeEngine(backend=SuccessfulBackend(), monitor=AgentMonitor())

    result = await engine.run(
        prompt="x",
        target_dir=str(tmp_path),
        max_rounds=1,
        output_spec=OutputSpec(must_exist=["REPORT.md"]),
    )

    assert result.success is True
    assert result.final_output is not None
    assert result.final_output.returncode == 0


@pytest.mark.asyncio
async def test_engine_completes_successful_script_without_output_spec(tmp_path) -> None:
    """A clean execution needs no extra completion round when no outputs are required."""

    class SuccessfulBackend(CodeBackend):
        async def generate(self, prompt, target_dir, previous_outputs, history):
            script = os.path.join(target_dir, "work.py")
            with open(script, "w", encoding="utf-8") as file:
                file.write("print('ok')\n")
            return GenerationResult(files_created=["work.py"])

    engine = CodeEngine(backend=SuccessfulBackend(), monitor=AgentMonitor())

    result = await engine.run(
        prompt="x",
        target_dir=str(tmp_path),
        max_rounds=1,
    )

    assert result.success is True
    assert result.final_output is not None
    assert result.final_output.returncode == 0
