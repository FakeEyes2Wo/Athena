import subprocess
from pathlib import Path

import pytest

from athena.research.prepare.baseline_research import BaselineResearchError
from athena.research.prepare.source_verification import (
    GitCloneEvidence,
    GitCloneVerifier,
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
async def test_git_verifier_uses_shallow_no_checkout_and_no_prompt() -> None:
    runner = FakeRunner()
    evidence = await GitCloneVerifier(runner=runner).verify(
        "https://github.com/pytorch/vision.git"
    )

    clone_argv, _, clone_env, _ = runner.calls[0]
    assert "--no-checkout" in clone_argv
    assert "--filter=blob:none" in clone_argv
    assert ["--depth", "1"] == clone_argv[
        clone_argv.index("--depth") : clone_argv.index("--depth") + 2
    ]
    assert clone_env["GIT_TERMINAL_PROMPT"] == "0"
    assert clone_env["GCM_INTERACTIVE"] == "Never"
    assert evidence == GitCloneEvidence(
        repository_url="https://github.com/pytorch/vision.git", commit="a" * 40
    )
    assert len(runner.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x.git",
        "ssh://host/x.git",
        "git@github.com:org/repo.git",
        "file:///tmp/repo",
        "C:/work/repo",
        "https://user:pass@github.com/org/repo.git",
        "https://github.com/org/repo.git?ref=main",
        "https://github.com/org/repo.git#main",
    ],
)
async def test_git_verifier_rejects_non_public_https_urls(url: str) -> None:
    with pytest.raises(BaselineResearchError):
        await GitCloneVerifier(runner=FakeRunner()).verify(url)


@pytest.mark.asyncio
async def test_git_verifier_reports_clone_failure_with_bounded_diagnostic() -> None:
    runner = FakeRunner(clone_code=128, stderr="fatal: " + "x" * 20_000)

    with pytest.raises(BaselineResearchError) as caught:
        await GitCloneVerifier(runner=runner).verify("https://github.com/org/repo.git")

    assert len(caught.value.diagnostics) == 1
    assert len(caught.value.diagnostics[0]) <= 4_000


@pytest.mark.asyncio
async def test_git_verifier_rejects_invalid_head_and_removes_temporary_directory() -> (
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
        await GitCloneVerifier(runner=runner).verify("https://github.com/org/repo.git")

    temporary_root = Path(runner.calls[0][0][-1]).parent
    assert not temporary_root.exists()


@pytest.mark.asyncio
async def test_git_verifier_converts_timeout_to_research_error() -> None:
    class TimeoutRunner:
        async def __call__(self, argv, *, cwd, env, timeout_s):
            raise TimeoutError("timed out")

    with pytest.raises(BaselineResearchError, match="Git source verification failed"):
        await GitCloneVerifier(runner=TimeoutRunner()).verify(
            "https://github.com/org/repo.git"
        )
