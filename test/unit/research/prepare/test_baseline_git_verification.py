import asyncio
import subprocess
from pathlib import Path

import pytest

from athena.research.prepare.baseline_research import BaselineResearchError
from athena.research.prepare.source_verification import (
    GitCloneEvidence,
    GitCloneVerifier,
    run_command,
)


class FakeRunner:
    def __init__(self, *, clone_code: int = 0, stderr: str = "") -> None:
        self.clone_code = clone_code
        self.stderr = stderr
        self.calls: list[tuple[list[str], Path | None, dict[str, str], float]] = []

    async def __call__(self, argv, *, cwd, env, timeout_s):
        self.calls.append((list(argv), cwd, dict(env), timeout_s))
        if "clone" in argv:
            return subprocess.CompletedProcess(argv, self.clone_code, "", self.stderr)
        return subprocess.CompletedProcess(argv, 0, "a" * 40 + "\n", "")


@pytest.mark.asyncio
async def test_git_verifier_uses_restricted_shallow_no_checkout_clone() -> None:
    runner = FakeRunner()

    evidence = await GitCloneVerifier(runner=runner).verify(
        "https://github.com/pytorch/vision.git"
    )

    clone_argv, clone_cwd, clone_env, timeout_s = runner.calls[0]
    assert isinstance(evidence, GitCloneEvidence)
    assert evidence.repository_url == "https://github.com/pytorch/vision.git"
    assert evidence.commit == "a" * 40
    assert clone_argv[:1] == ["git"]
    assert "clone" in clone_argv
    assert "--no-checkout" in clone_argv
    assert "--filter=blob:none" in clone_argv
    assert clone_argv[
        clone_argv.index("--depth") : clone_argv.index("--depth") + 2
    ] == [
        "--depth",
        "1",
    ]
    assert clone_argv[clone_argv.index("--") + 1] == evidence.repository_url
    assert clone_cwd is not None
    assert timeout_s > 0
    assert clone_env["GIT_TERMINAL_PROMPT"] == "0"
    assert clone_env["GCM_INTERACTIVE"] == "Never"
    assert len(runner.calls) == 2


@pytest.mark.asyncio
async def test_git_verifier_disables_non_https_protocols_and_hooks() -> None:
    runner = FakeRunner()

    await GitCloneVerifier(runner=runner).verify("https://github.com/org/repo.git/")

    clone_argv = runner.calls[0][0]
    assert "protocol.file.allow=never" in clone_argv
    assert "protocol.ext.allow=never" in clone_argv
    assert "protocol.ssh.allow=never" in clone_argv
    assert "protocol.git.allow=never" in clone_argv
    assert "protocol.http.allow=never" in clone_argv
    assert "protocol.https.allow=always" in clone_argv
    hooks_values = [
        value for value in clone_argv if value.startswith("core.hooksPath=")
    ]
    assert len(hooks_values) == 1
    assert hooks_values[0].split("=", 1)[1]
    assert runner.calls[0][0][runner.calls[0][0].index("--") + 1] == (
        "https://github.com/org/repo.git"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository_url",
    [
        "http://example.com/x.git",
        "ssh://host/x.git",
        "git://host/x.git",
        "git@github.com:org/repo.git",
        "file:///tmp/repo",
        ".\\repo",
        "https://user:secret@example.com/x.git",
        "https://example.com/x.git?token=secret",
        "https://example.com/x.git#fragment",
        "https://example.com",
    ],
)
async def test_git_verifier_rejects_non_public_https_urls(repository_url: str) -> None:
    runner = FakeRunner()

    with pytest.raises(BaselineResearchError) as caught:
        await GitCloneVerifier(runner=runner).verify(repository_url)

    assert str(caught.value) == "Git source verification failed"
    assert caught.value.diagnostics
    assert len(runner.calls) == 0
    assert "secret" not in " ".join(caught.value.diagnostics)


@pytest.mark.asyncio
async def test_git_verifier_reports_bounded_redacted_clone_failure() -> None:
    secret = "super-secret-token"
    stderr = (
        "fatal: could not read Password for "
        f"'https://alice:{secret}@example.com/x.git': " + (" noisy\noutput\t" * 5000)
    )
    runner = FakeRunner(clone_code=128, stderr=stderr)

    with pytest.raises(BaselineResearchError) as caught:
        await GitCloneVerifier(runner=runner).verify("https://example.com/x.git")

    diagnostic = " ".join(caught.value.diagnostics)
    assert len(diagnostic) <= 4000
    assert secret not in diagnostic
    assert "alice" not in diagnostic
    assert "  " not in diagnostic
    assert len(runner.calls) == 1


class TimeoutRunner:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    async def __call__(self, argv, *, cwd, env, timeout_s):
        self.calls += 1
        raise self.exc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        asyncio.TimeoutError("operation took too long"),
        subprocess.TimeoutExpired(["git"], 1),
    ],
)
async def test_git_verifier_translates_timeout(error: BaseException) -> None:
    runner = TimeoutRunner(error)

    with pytest.raises(BaselineResearchError) as caught:
        await GitCloneVerifier(runner=runner).verify("https://example.com/x.git")

    assert str(caught.value) == "Git source verification failed"
    assert "timed out" in " ".join(caught.value.diagnostics).lower()
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_git_verifier_rejects_invalid_head_and_cleans_temporary_directory() -> (
    None
):
    class InvalidHeadRunner(FakeRunner):
        async def __call__(self, argv, *, cwd, env, timeout_s):
            self.calls.append((list(argv), cwd, dict(env), timeout_s))
            if "clone" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, "not-a-commit\n", "")

    runner = InvalidHeadRunner()
    with pytest.raises(BaselineResearchError):
        await GitCloneVerifier(runner=runner).verify("https://example.com/x.git")

    clone_dir = Path(runner.calls[0][0][-1])
    assert not clone_dir.exists()


@pytest.mark.asyncio
async def test_run_command_uses_list_args_and_non_shell_subprocess() -> None:
    captured = {}

    async def fake_to_thread(function, *args, **kwargs):
        captured["function"] = function
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args[0], 0, "", "")

    original_to_thread = asyncio.to_thread
    asyncio.to_thread = fake_to_thread
    try:
        argv = ("git", "--version")
        environment = {"GIT_TERMINAL_PROMPT": "0"}
        result = await run_command(argv, cwd=None, env=environment, timeout_s=3.5)
    finally:
        asyncio.to_thread = original_to_thread

    assert result.returncode == 0
    assert captured["function"] is subprocess.run
    assert captured["args"][0] == ["git", "--version"]
    assert captured["kwargs"] == {
        "cwd": None,
        "env": environment,
        "timeout": 3.5,
        "check": False,
        "capture_output": True,
        "text": True,
        "shell": False,
    }


@pytest.mark.asyncio
async def test_git_verifier_cleans_temporary_directory_after_success() -> None:
    runner = FakeRunner()

    await GitCloneVerifier(runner=runner).verify("https://example.com/x.git")

    clone_dir = Path(runner.calls[0][0][-1])
    assert not clone_dir.exists()
