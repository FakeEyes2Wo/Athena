"""Restricted, non-interactive verification of public Git repositories."""

import asyncio
import ipaddress
import os
import re
import socket
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol
from urllib.parse import urlsplit

from pydantic import ValidationError

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
    titles_match,
)
from .repository_url import (
    _is_public_repository_address,
    normalize_public_https_repository_url,
)

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


def _trusted_git_candidates() -> tuple[tuple[Path, Path], ...]:
    """Return fixed installation candidates outside Agent-controlled search paths."""

    if os.name != "nt":
        return (
            (Path("/usr/bin"), Path("/usr/bin/git")),
            (Path("/bin"), Path("/bin/git")),
        )

    candidates: list[tuple[Path, Path]] = []
    seen_roots: set[Path] = set()
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(variable)
        if not value:
            continue
        try:
            root = Path(value).resolve(strict=True)
        except OSError:
            continue
        if not root.is_dir() or root in seen_roots:
            continue
        seen_roots.add(root)
        candidates.extend(
            (
                (root, root / "Git" / "cmd" / "git.exe"),
                (root, root / "Git" / "bin" / "git.exe"),
            )
        )
    return tuple(candidates)


def _capture_git_executable() -> str | None:
    """Capture Git only from fixed, controller-owned installation directories."""

    for trusted_root, candidate in _trusted_git_candidates():
        try:
            resolved_root = trusted_root.resolve(strict=True)
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if (
            resolved.is_file()
            and resolved.is_absolute()
            and resolved.is_relative_to(resolved_root)
        ):
            return str(resolved)
    return None


def _capture_platform_environment() -> Mapping[str, str]:
    """Retain only Windows process roots needed by native subprocesses."""

    captured: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.casefold() in {"systemroot", "windir"} and value:
            captured[key] = value
    return MappingProxyType(captured)


_GIT_EXECUTABLE: Final[str | None] = _capture_git_executable()
_PLATFORM_ENVIRONMENT: Final[Mapping[str, str]] = _capture_platform_environment()


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
    """Build Git's environment from a fixed allowlist, never ambient runtime state."""

    environment = dict(_PLATFORM_ENVIRONMENT)
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "LANG": "C",
            "LC_ALL": "C",
        }
    )
    return environment


def _resolve_public_host_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve every A/AAAA address and reject the complete set on any unsafe entry."""

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_repository_address(literal):
            raise ValueError("repository hostname resolved to a non-public address")
        return (str(literal),)

    try:
        records = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError:
        raise ValueError("repository hostname resolution failed") from None

    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for family, _socket_type, _protocol, _canonical_name, sockaddr in records:
        if family not in {socket.AF_INET, socket.AF_INET6} or not sockaddr:
            continue
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            raise ValueError("repository hostname resolution was invalid") from None
        if (family == socket.AF_INET) != isinstance(address, ipaddress.IPv4Address):
            raise ValueError("repository hostname resolution was invalid")
        if not _is_public_repository_address(address):
            raise ValueError("repository hostname resolved to a non-public address")
        addresses.add(address)
    if not addresses:
        raise ValueError("repository hostname did not resolve to an A or AAAA address")
    return tuple(
        str(address)
        for address in sorted(addresses, key=lambda item: (item.version, item.packed))
    )


def _curlopt_resolve_value(host: str, port: int, addresses: Sequence[str]) -> str:
    """Render one permanent libcurl DNS-cache entry, bracketing IPv6 addresses."""

    rendered = ",".join(
        f"[{address}]" if ":" in address else address for address in addresses
    )
    return f"http.curloptResolve={host}:{port}:{rendered}"


@dataclass(slots=True)
class GitCloneVerifier:
    """Prove that a public repository is reachable without reading its files."""

    runner: CommandRunner = run_command
    timeout_s: float = 60.0

    async def verify(self, repository_url: str) -> GitCloneEvidence:
        """Clone a repository shallowly and return its resolved HEAD commit."""

        if _GIT_EXECUTABLE is None:
            self._raise_failure("trusted Git executable is unavailable")
        try:
            normalized_url = normalize_public_https_repository_url(repository_url)
        except ValueError as exc:
            self._raise_failure(str(exc))
        parsed_url = urlsplit(normalized_url)
        host = parsed_url.hostname
        if host is None:
            self._raise_failure("repository URL must include a host")
        port = parsed_url.port or 443
        try:
            ipaddress.ip_address(host)
        except ValueError:
            host_is_literal = False
        else:
            host_is_literal = True
        try:
            addresses = await asyncio.to_thread(
                _resolve_public_host_addresses,
                host,
                port,
            )
            if not host_is_literal:
                repeated_addresses = await asyncio.to_thread(
                    _resolve_public_host_addresses,
                    host,
                    port,
                )
                if repeated_addresses != addresses:
                    raise ValueError("repository hostname resolution changed")
        except (OSError, ValueError) as exc:
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
                "-c",
                "http.followRedirects=false",
                "-c",
                "http.emptyAuth=false",
                "-c",
                "http.proactiveAuth=none",
                "-c",
                "http.delegation=none",
                "-c",
                "http.cookieFile=",
                "-c",
                "http.saveCookies=false",
                "-c",
                "http.extraHeader=",
                "-c",
                "http.sslCert=",
                "-c",
                "http.sslKey=",
                "-c",
                "http.sslCertPasswordProtected=false",
                "-c",
                "http.proxy=",
                "-c",
                "http.proxySSLCert=",
                "-c",
                "http.proxySSLKey=",
                "-c",
                "http.sslVerify=true",
            ]
            if not host_is_literal:
                git_config.extend(["-c", _curlopt_resolve_value(host, port, addresses)])
            clone_argv = [
                _GIT_EXECUTABLE,
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
                _GIT_EXECUTABLE,
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
            return await self.runner(argv, cwd=cwd, env=env, timeout_s=self.timeout_s)
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            self._raise_failure(
                f"git {operation} timed out after {self.timeout_s:g} seconds: {exc}"
            )
        except (OSError, ValueError) as exc:
            self._raise_failure(f"git {operation} could not be started: {exc}")

    @staticmethod
    def _raise_failure(diagnostic: str) -> None:
        raise BaselineResearchError(
            "Git source verification failed",
            diagnostics=[_bounded_diagnostic(diagnostic)],
        )


def _joined_diagnostic(error: BaselineResearchError) -> str:
    """Join the verifier's already bounded diagnostics into one attempt note."""

    diagnostic = " ".join(error.diagnostics) or str(error)
    return " ".join(diagnostic.split())[:4000]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class BaselineSourceVerifier:
    """Verify a selected source via public Git, then OpenAlex metadata if needed."""

    git: GitCloneVerifier
    openalex: OpenAlexLookup
    now: Callable[[], datetime] = _now

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
                        route="git", success=False, diagnostic=_joined_diagnostic(error)
                    )
                )

        if selected.paper_locator:
            try:
                work = await self.openalex.fetch_work(selected.paper_locator)
            except Exception as error:  # noqa: BLE001 - external lookup degradation
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
                elif not work.openalex_id.strip() or not titles_match(
                    work.title, work.title
                ):
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
                    try:
                        verification = BaselineVerification(
                            schema_version=2,
                            research_sha256=research_sha256(artifacts.raw_research),
                            design_sha256=design_sha256(artifacts.raw_design),
                            selected_candidate_id=selected.candidate_id,
                            route="openalex",
                            verified_at=self.now(),
                            paper_locator=selected.paper_locator,
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
                    except ValidationError:
                        attempts.append(
                            VerificationAttempt(
                                route="openalex",
                                success=False,
                                diagnostic="OpenAlex returned incomplete work identity",
                            )
                        )
                    else:
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
