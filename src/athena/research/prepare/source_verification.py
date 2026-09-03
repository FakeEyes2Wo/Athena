"""Restricted, non-interactive verification of public Git repositories."""

import asyncio
import os
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Protocol, Sequence

from athena.research.literature.paper_source.http import (
    HostRateLimiter,
    UrllibTransport,
)
from athena.research.literature.paper_source.openalex import (
    OpenAlexClient,
    OpenAlexWork,
)

from .baseline_research import (
    AUTHORITY_CITATION_THRESHOLD,
    BaselineArtifacts,
    BaselineResearchError,
    BaselineVerification,
    VerificationAttempt,
    assert_verification_matches_artifacts,
    design_sha256,
    research_sha256,
)
from .repository_url import normalize_public_https_repository_url

_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)\b(password|passwd|token|secret|authorization|bearer)(?:\s*=|\s*:)\s*[^\s,;]+"
)
_CREDENTIAL_HEADER_RE = re.compile(
    r"(?im)\b(?P<name>authorization|proxy-authorization)\s*:\s*[^\r\n]*"
)
_AUTHORIZATION_SCHEME_RE = re.compile(r"(?i)\b(?P<scheme>Bearer|Basic)\s+[^\s\r\n]+")
_URL_CANDIDATE_RE = re.compile(
    r"(?P<scheme>https?|ssh|git)://[^\s'\"<>]+", re.IGNORECASE
)
_UNTRUSTED_ENV_NAMES = {
    "ALL_PROXY",
    "GIT_ALLOW_PROTOCOL",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_ASKPASS",
    "GIT_DIR",
    "GIT_EDITOR",
    "GIT_EXEC_PATH",
    "GIT_EXTERNAL_DIFF",
    "GIT_EXT_SERVICE",
    "GIT_EXT_SERVICE_NOP",
    "GIT_HTTP_PROXY",
    "GIT_HTTPS_PROXY",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PROXY_COMMAND",
    "GIT_SEQUENCE_EDITOR",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_SSH_VARIANT",
    "GIT_WORK_TREE",
    "GIT_PAGER",
    "GIT_PROTOCOL",
    "GIT_SSL_CIPHER_LIST",
    "GIT_SSL_NO_VERIFY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSH_ASKPASS",
}


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


class OpenAlexLookup(Protocol):
    """The free OpenAlex metadata lookup used by the authority exception."""

    async def fetch_work(self, locator: str) -> OpenAlexWork | None:
        """Resolve one DOI or OpenAlex work ID to its metadata."""


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
    text = _CREDENTIAL_HEADER_RE.sub(
        lambda match: f"{match.group('name')}=[REDACTED]", text
    )
    text = _URL_CANDIDATE_RE.sub(_redact_url_candidate, text)
    text = _AUTHORIZATION_SCHEME_RE.sub(
        lambda match: f"{match.group('scheme')} [REDACTED]", text
    )
    text = _SENSITIVE_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return " ".join(text.split())[:4000]


def _redact_url_candidate(match: re.Match[str]) -> str:
    """Redact an entire URL when its authority can contain userinfo."""

    candidate = match.group(0)
    authority = candidate.split("://", 1)[1].split("/", 1)[0]
    if "@" in authority or "%" in authority:
        return f"{match.group('scheme')}://[REDACTED]"
    return candidate


def _clean_environment() -> dict[str, str]:
    """Copy only a safe process environment for Git's two commands."""

    environment = dict(os.environ)
    untrusted_casefolded = {name.casefold() for name in _UNTRUSTED_ENV_NAMES}
    for key in list(environment):
        if (
            key.casefold() == "git_config"
            or key.casefold().startswith("git_config_")
            or key.casefold() in untrusted_casefolded
            or key.casefold().startswith("git_trace")
        ):
            environment.pop(key, None)
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
    )
    return environment


class GitCloneVerifier:
    """Prove that a public repository is reachable without reading its files."""

    def __init__(self, runner: CommandRunner = run_command, timeout_s: float = 60.0):
        self._runner = runner
        self._timeout_s = timeout_s

    async def verify(self, repository_url: str) -> GitCloneEvidence:
        """Clone a repository shallowly and return its resolved HEAD commit."""

        try:
            normalized_url = normalize_public_https_repository_url(repository_url)
        except ValueError as exc:
            self._raise_failure(str(exc))

        environment = _clean_environment()

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
                "protocol.allow=never",
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
                "-c",
                "credential.helper=",
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

            head_argv = [
                "git",
                *git_config,
                "-C",
                str(clone_dir),
                "rev-parse",
                "HEAD",
            ]
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
        except (OSError, ValueError) as exc:
            self._raise_failure(f"git {operation} could not be started: {exc}")

    @staticmethod
    def _raise_failure(diagnostic: str) -> None:
        raise BaselineResearchError(
            "Git source verification failed",
            diagnostics=[_bounded_diagnostic(diagnostic)],
        )


def titles_match(reported: str, resolved: str) -> bool:
    """Compare titles after deterministic Unicode and punctuation normalization."""

    normalized_reported = _normalize_title(reported)
    normalized_resolved = _normalize_title(resolved)
    if normalized_reported == normalized_resolved:
        return True
    if min(len(normalized_reported), len(normalized_resolved)) < 20:
        return False
    return (
        SequenceMatcher(None, normalized_reported, normalized_resolved).ratio() >= 0.90
    )


def _normalize_title(value: str) -> str:
    """Join Unicode-normalized alphanumeric title tokens."""

    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def _has_substantive_openalex_proof(work: OpenAlexWork) -> bool:
    """Require meaningful work and title identities before recording authority proof."""

    return bool(_normalize_title(work.openalex_id)) and bool(
        _normalize_title(work.title)
    )


def joined_diagnostic(error: BaselineResearchError) -> str:
    """Join the verifier's already bounded diagnostics into one attempt note."""

    diagnostic = " ".join(error.diagnostics) or str(error)
    return " ".join(diagnostic.split())[:4000]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BaselineSourceVerifier:
    """Verify a selected source via public Git, then OpenAlex metadata if needed."""

    def __init__(
        self,
        git: GitCloneVerifier,
        openalex: OpenAlexLookup,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self.git = git
        self.openalex = openalex
        self.now = now

    async def verify(self, artifacts: BaselineArtifacts) -> BaselineVerification:
        """Return the first qualifying Git or authority-metadata proof."""

        selected = artifacts.selected
        attempts: list[VerificationAttempt] = []
        if selected.repository_url is not None:
            try:
                evidence = await self.git.verify(str(selected.repository_url))
                verification = BaselineVerification(
                    schema_version=2,
                    research_sha256=research_sha256(artifacts.raw_research),
                    design_sha256=design_sha256(artifacts.raw_design),
                    selected_candidate_id=selected.candidate_id,
                    route="git",
                    verified_at=self.now(),
                    repository_url=evidence.repository_url,
                    commit=evidence.commit,
                    attempts=[
                        *attempts,
                        VerificationAttempt(
                            route="git", success=True, diagnostic="clone verified"
                        ),
                    ],
                )
                assert_verification_matches_artifacts(artifacts, verification)
                return verification
            except BaselineResearchError as error:
                attempts.append(
                    VerificationAttempt(
                        route="git", success=False, diagnostic=joined_diagnostic(error)
                    )
                )

        if selected.paper_locator:
            try:
                work = await self.openalex.fetch_work(selected.paper_locator)
            except Exception as error:
                attempts.append(
                    VerificationAttempt(
                        route="openalex",
                        success=False,
                        diagnostic=_bounded_diagnostic(
                            f"OpenAlex lookup failed: {error}"
                        ),
                    )
                )
            else:
                if work is None:
                    attempts.append(
                        VerificationAttempt(
                            route="openalex",
                            success=False,
                            diagnostic="OpenAlex did not resolve the paper locator",
                        )
                    )
                elif not _has_substantive_openalex_proof(work):
                    attempts.append(
                        VerificationAttempt(
                            route="openalex",
                            success=False,
                            diagnostic="OpenAlex returned incomplete work identity",
                        )
                    )
                elif not titles_match(selected.title, work.title):
                    attempts.append(
                        VerificationAttempt(
                            route="openalex",
                            success=False,
                            diagnostic="OpenAlex title does not match selected source",
                        )
                    )
                elif work.cited_by_count < AUTHORITY_CITATION_THRESHOLD:
                    attempts.append(
                        VerificationAttempt(
                            route="openalex",
                            success=False,
                            diagnostic=(
                                f"OpenAlex citation count {work.cited_by_count} is below "
                                f"{AUTHORITY_CITATION_THRESHOLD}"
                            ),
                        )
                    )
                else:
                    verification = BaselineVerification(
                        schema_version=2,
                        research_sha256=research_sha256(artifacts.raw_research),
                        design_sha256=design_sha256(artifacts.raw_design),
                        selected_candidate_id=selected.candidate_id,
                        route="openalex",
                        verified_at=self.now(),
                        openalex_id=work.openalex_id,
                        title=work.title,
                        publication_year=work.publication_year,
                        cited_by_count=work.cited_by_count,
                        attempts=[
                            *attempts,
                            VerificationAttempt(
                                route="openalex",
                                success=True,
                                diagnostic="authority threshold verified",
                            ),
                        ],
                    )
                    assert_verification_matches_artifacts(artifacts, verification)
                    return verification

        raise BaselineResearchError(
            "selected candidate has no qualifying source",
            diagnostics=[attempt.diagnostic for attempt in attempts],
        )


def build_default_source_verifier() -> BaselineSourceVerifier:
    """Build the verifier with free OpenAlex metadata access only."""

    return BaselineSourceVerifier(
        git=GitCloneVerifier(),
        openalex=OpenAlexClient(HostRateLimiter(UrllibTransport())),
    )


__all__ = [
    "BaselineSourceVerifier",
    "CommandRunner",
    "GitCloneEvidence",
    "GitCloneVerifier",
    "OpenAlexLookup",
    "build_default_source_verifier",
    "joined_diagnostic",
    "run_command",
    "titles_match",
]
