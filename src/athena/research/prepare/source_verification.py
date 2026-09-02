"""Restricted, non-interactive verification of public Git repositories."""

import asyncio
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

from .baseline_research import BaselineResearchError

_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_CREDENTIAL_URL_RE = re.compile(
    r"(?P<scheme>https?|ssh|git)://[^\s/@]+(?::[^\s/@]*)?@",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)\b(password|passwd|token|secret|authorization|bearer)(?:\s*=|\s*:)\s*[^\s,;]+"
)


class CommandRunner(Protocol):
    """Async adapter for the narrowly scoped subprocess invocation."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> Awaitable[subprocess.CompletedProcess[str]]: ...


async def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str],
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    """Run one command without a shell, blocking the worker thread only."""

    return await asyncio.to_thread(
        subprocess.run,
        list(argv),
        cwd=cwd,
        env=dict(env),
        timeout=timeout_s,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )


@dataclass(frozen=True)
class GitCloneEvidence:
    """The normalized repository URL and commit resolved by Git."""

    repository_url: str
    commit: str


def _bounded_diagnostic(value: object) -> str:
    """Collapse and redact command diagnostics before exposing them."""

    text = str(value)
    text = _CREDENTIAL_URL_RE.sub(
        lambda match: f"{match.group('scheme')}://[REDACTED]@", text
    )
    text = _SENSITIVE_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return " ".join(text.split())[:4000]


def _normalize_repository_url(repository_url: str) -> str:
    """Accept only a public HTTPS URL and return its canonical form."""

    if not isinstance(repository_url, str) or not repository_url:
        raise ValueError("repository URL must be a non-empty HTTPS URL")
    if repository_url != repository_url.strip() or any(
        character.isspace() for character in repository_url
    ):
        raise ValueError("repository URL must not contain whitespace")
    if "?" in repository_url or "#" in repository_url:
        raise ValueError("repository URL must not contain a query or fragment")
    if "\\" in repository_url:
        raise ValueError("repository URL must use URL path separators")

    try:
        parsed = urlsplit(repository_url)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError("repository URL is malformed") from exc

    if parsed.scheme.casefold() != "https":
        raise ValueError("repository URL must use HTTPS")
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("repository URL must not contain credentials")
    if parsed.netloc != parsed.netloc.strip() or not parsed.netloc:
        raise ValueError("repository URL must include a host")
    if parsed.path in ("", "/"):
        raise ValueError("repository URL must include a repository path")

    # Lower-case the host and omit the default HTTPS port for stable evidence.
    normalized_host = hostname.lower()
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    normalized_netloc = normalized_host
    if port is not None and port != 443:
        normalized_netloc += f":{port}"
    normalized_path = parsed.path.rstrip("/")
    if not normalized_path:
        raise ValueError("repository URL must include a repository path")
    return urlunsplit(("https", normalized_netloc, normalized_path, "", ""))


class GitCloneVerifier:
    """Prove that a public repository is reachable without reading its files."""

    def __init__(self, runner: CommandRunner = run_command, timeout_s: float = 60.0):
        self._runner = runner
        self._timeout_s = timeout_s

    async def verify(self, repository_url: str) -> GitCloneEvidence:
        """Clone a repository shallowly and return its resolved HEAD commit."""

        try:
            normalized_url = _normalize_repository_url(repository_url)
        except ValueError as exc:
            self._raise_failure(str(exc))

        environment = dict(os.environ)
        environment.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})

        with tempfile.TemporaryDirectory(prefix="athena-git-verify-") as temporary:
            temporary_root = Path(temporary)
            hooks_dir = temporary_root / "hooks"
            hooks_dir.mkdir()
            clone_dir = temporary_root / "clone"
            clone_dir.mkdir()

            git_config = [
                "-c",
                f"core.hooksPath={hooks_dir}",
                "-c",
                "protocol.file.allow=never",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "protocol.ssh.allow=never",
                "-c",
                "protocol.git.allow=never",
                "-c",
                "protocol.http.allow=never",
                "-c",
                "protocol.https.allow=always",
            ]
            clone_argv = [
                "git",
                *git_config,
                "clone",
                "--depth",
                "1",
                "--filter=blob:none",
                "--no-checkout",
                "--",
                normalized_url,
                str(clone_dir),
            ]
            clone_result = await self._run(
                clone_argv,
                cwd=temporary_root,
                env=environment,
                operation="clone",
            )
            if clone_result.returncode != 0:
                self._raise_failure(
                    f"git clone exited with status {clone_result.returncode}: "
                    f"{clone_result.stderr or clone_result.stdout or 'no diagnostic'}"
                )

            head_argv = ["git", "-C", str(clone_dir), "rev-parse", "HEAD"]
            head_result = await self._run(
                head_argv,
                cwd=temporary_root,
                env=environment,
                operation="rev-parse",
            )
            if head_result.returncode != 0:
                self._raise_failure(
                    f"git rev-parse exited with status {head_result.returncode}: "
                    f"{head_result.stderr or head_result.stdout or 'no diagnostic'}"
                )
            commit = (head_result.stdout or "").strip()
            if not _COMMIT_RE.fullmatch(commit):
                self._raise_failure("git rev-parse returned an invalid commit")
            return GitCloneEvidence(repository_url=normalized_url, commit=commit)

    async def _run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        operation: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return await self._runner(argv, cwd=cwd, env=env, timeout_s=self._timeout_s)
        except (asyncio.TimeoutError, subprocess.TimeoutExpired) as exc:
            self._raise_failure(
                f"git {operation} timed out after {self._timeout_s:g} seconds: {exc}"
            )
        except OSError as exc:
            self._raise_failure(f"git {operation} could not be started: {exc}")

    @staticmethod
    def _raise_failure(diagnostic: str) -> None:
        raise BaselineResearchError(
            "Git source verification failed",
            diagnostics=[_bounded_diagnostic(diagnostic)],
        )


__all__ = ["CommandRunner", "GitCloneEvidence", "GitCloneVerifier", "run_command"]
