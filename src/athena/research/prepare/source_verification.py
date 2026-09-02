"""Verify public baseline sources without executing third-party code."""

import asyncio
import ipaddress
import os
import re
import subprocess
import tempfile
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol
from unicodedata import normalize

from athena.research.literature.paper_source.http import (
    HostRateLimiter,
    UrllibTransport,
)
from athena.research.literature.paper_source.openalex import (
    OpenAlexClient,
    OpenAlexWork,
)
from athena.research.prepare.baseline_research import (
    BaselineArtifacts,
    BaselineResearchError,
    BaselineVerification,
    VerificationAttempt,
    research_sha256,
)

MAX_DIAGNOSTIC = 4_000
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


class CommandRunner(Protocol):
    """Async command boundary used by the Git verifier and its tests."""

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str],
        timeout_s: float,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command with an explicit working directory and environment."""


@dataclass(frozen=True)
class GitCloneEvidence:
    """Evidence returned after a shallow public repository clone."""

    repository_url: str
    commit: str


class OpenAlexLookup(Protocol):
    """Metadata lookup needed by the citation-based source exception."""

    async def fetch_work(self, locator: str) -> OpenAlexWork | None:
        """Resolve a paper locator to OpenAlex metadata."""


async def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str],
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    """Run one non-shell command in a worker thread."""
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


def _diagnostic(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = URL_RE.sub(lambda match: _redact_url(match.group(0)), text)
    return text[:MAX_DIAGNOSTIC]


def _redact_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.username is None and parsed.password is None:
        return value
    host = parsed.hostname or ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
    )


def _validate_repository_url(value: str) -> str:
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise BaselineResearchError(
            "Git source verification failed", ["repository URL is not public HTTPS"]
        )
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or "?" in value
        or "#" in value
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path
        or _is_local_host(parsed.hostname)
    ):
        raise BaselineResearchError(
            "Git source verification failed", ["repository URL is not public HTTPS"]
        )
    return value


def _is_local_host(hostname: str) -> bool:
    normalized = hostname.rstrip(".").casefold()
    if normalized in {"localhost", "localhost.localdomain"}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        # hostname 不是 IP 字面量 → 按普通公网域名继续。
        return False
    return any(
        (
            address.is_loopback,
            address.is_private,
            address.is_link_local,
            address.is_reserved,
        )
    )


class GitCloneVerifier:
    """Qualify a public repository using a shallow, no-checkout clone."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self._runner = runner or run_command
        self._timeout_s = timeout_s

    async def verify(self, repository_url: str) -> GitCloneEvidence:
        """Clone a public HTTPS repository and return its resolved HEAD."""
        url = _validate_repository_url(repository_url)
        environment = os.environ.copy()
        environment.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
        with tempfile.TemporaryDirectory(prefix="athena-baseline-") as temporary:
            root = Path(temporary)
            hooks = root / "hooks"
            hooks.mkdir()
            clone_dir = root / "repo"
            git_config = [
                "-c",
                f"core.hooksPath={hooks}",
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
                url,
                str(clone_dir),
            ]
            try:
                clone = await self._runner(
                    clone_argv, cwd=None, env=environment, timeout_s=self._timeout_s
                )
            except (TimeoutError, subprocess.TimeoutExpired) as error:
                # Git clone 超时 → 将网络故障转换为来源校验诊断
                raise BaselineResearchError(
                    "Git source verification failed", [_diagnostic("clone timed out")]
                ) from error
            if clone.returncode != 0:
                message = (
                    clone.stderr
                    or clone.stdout
                    or f"clone exited with {clone.returncode}"
                )
                raise BaselineResearchError(
                    "Git source verification failed", [_diagnostic(message)]
                )
            head_argv = ["git", "-C", str(clone_dir), "rev-parse", "HEAD"]
            try:
                head = await self._runner(
                    head_argv, cwd=None, env=environment, timeout_s=self._timeout_s
                )
            except (TimeoutError, subprocess.TimeoutExpired) as error:
                # HEAD 查询超时 → 保持来源未验证并返回有限诊断
                raise BaselineResearchError(
                    "Git source verification failed",
                    [_diagnostic("HEAD lookup timed out")],
                ) from error
            commit = (head.stdout or "").strip()
            if head.returncode != 0 or not COMMIT_RE.fullmatch(commit):
                message = head.stderr or head.stdout or "invalid repository HEAD"
                raise BaselineResearchError(
                    "Git source verification failed", [_diagnostic(message)]
                )
            return GitCloneEvidence(url, commit.lower())


def _title_tokens(value: str) -> str:
    text = normalize("NFKC", value).casefold()
    return "".join(char for char in text if char.isalnum())


def titles_match(reported: str, resolved: str) -> bool:
    """Compare titles with exact normalized matching and a long-title fuzzy fallback."""
    left = _title_tokens(reported)
    right = _title_tokens(resolved)
    if left == right:
        return True
    if min(len(left), len(right)) < 20:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.90


def _attempt(route: str, success: bool, diagnostic: str) -> VerificationAttempt:
    return VerificationAttempt(
        route=route, success=success, diagnostic=_diagnostic(diagnostic)
    )


def _error_diagnostics(error: BaselineResearchError) -> str:
    return "; ".join(error.diagnostics) or str(error)


class BaselineSourceVerifier:
    """Verify the selected candidate through Git first, then OpenAlex metadata."""

    def __init__(
        self,
        git: GitCloneVerifier,
        openalex: OpenAlexLookup,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._git = git
        self._openalex = openalex
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def verify(self, artifacts: BaselineArtifacts) -> BaselineVerification:
        """Return platform-owned evidence for the selected baseline source."""
        selected = artifacts.selected
        attempts: list[VerificationAttempt] = []
        if selected.repository_url is not None:
            try:
                evidence = await self._git.verify(str(selected.repository_url))
            except BaselineResearchError as error:
                # Git rejection → retain the bounded diagnostic before OpenAlex fallback
                attempts.append(_attempt("git", False, _error_diagnostics(error)))
            else:
                attempts.append(_attempt("git", True, "clone verified"))
                return BaselineVerification(
                    research_sha256=research_sha256(artifacts.raw_research),
                    selected_candidate_id=selected.candidate_id,
                    route="git",
                    verified_at=self._now(),
                    repository_url=evidence.repository_url,
                    commit=evidence.commit,
                    attempts=attempts,
                )
        if selected.paper_locator:
            try:
                work = await self._openalex.fetch_work(selected.paper_locator)
            except Exception as error:
                # OpenAlex 网络或解析故障 → 记录后让来源校验统一失败
                attempts.append(_attempt("openalex", False, _diagnostic(error)))
            else:
                if (
                    work is not None
                    and bool(work.openalex_id)
                    and titles_match(selected.title, work.title)
                    and work.cited_by_count >= 100
                ):
                    attempts.append(
                        _attempt("openalex", True, "authority threshold verified")
                    )
                    return BaselineVerification(
                        research_sha256=research_sha256(artifacts.raw_research),
                        selected_candidate_id=selected.candidate_id,
                        route="openalex",
                        verified_at=self._now(),
                        openalex_id=work.openalex_id,
                        title=work.title,
                        publication_year=work.publication_year,
                        cited_by_count=work.cited_by_count,
                        attempts=attempts,
                    )
                reason = "work not found"
                if work is not None:
                    reason = (
                        "title mismatch"
                        if not titles_match(selected.title, work.title)
                        else "citation threshold below 100"
                    )
                attempts.append(_attempt("openalex", False, reason))
        raise BaselineResearchError(
            "selected candidate has no qualifying source",
            [attempt.diagnostic for attempt in attempts],
        )


def build_default_source_verifier() -> BaselineSourceVerifier:
    """Build a verifier backed by free Git and OpenAlex metadata access."""
    limiter = HostRateLimiter(transport=UrllibTransport())
    return BaselineSourceVerifier(GitCloneVerifier(), OpenAlexClient(limiter))
