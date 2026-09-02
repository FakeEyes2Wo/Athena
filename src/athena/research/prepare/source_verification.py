"""Restricted, non-interactive verification of public Git repositories."""

import asyncio
import ipaddress
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
_STRUCTURAL_ESCAPE_RE = re.compile(r"%(?:23|25|2f|3a|3f|40|5c)", re.IGNORECASE)
_NUMERIC_IPV4_COMPONENT_RE = re.compile(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)")
_IPV4_COMPATIBLE_NETWORK = ipaddress.IPv6Network("::/96")
_NON_PUBLIC_HOST_SUFFIXES = (
    ".invalid",
    ".example",
    ".internal",
    ".local",
    ".localhost",
    ".test",
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


def _normalize_repository_url(repository_url: str) -> str:
    """Accept only a public HTTPS URL and return its canonical form."""

    if not isinstance(repository_url, str) or not repository_url:
        raise ValueError("repository URL must be a non-empty HTTPS URL")
    if repository_url != repository_url.strip() or any(
        character.isspace() for character in repository_url
    ):
        raise ValueError("repository URL must not contain whitespace")
    if any(
        ord(character) <= 0x1F or ord(character) == 0x7F for character in repository_url
    ):
        raise ValueError("repository URL must not contain control characters")
    if "?" in repository_url or "#" in repository_url:
        raise ValueError("repository URL must not contain a query or fragment")
    if "\\" in repository_url:
        raise ValueError("repository URL must use URL path separators")
    if _STRUCTURAL_ESCAPE_RE.search(repository_url):
        raise ValueError("repository URL must not encode structural delimiters")

    try:
        parsed = urlsplit(repository_url)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError("repository URL is malformed") from exc

    if parsed.scheme.casefold() != "https":
        raise ValueError("repository URL must use HTTPS")
    if port == 0:
        raise ValueError("repository URL port must be between 1 and 65535")
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("repository URL must not contain credentials")
    if "%" in parsed.netloc:
        raise ValueError("repository authority must not be percent-encoded")
    if parsed.netloc != parsed.netloc.strip() or not parsed.netloc:
        raise ValueError("repository URL must include a host")
    if parsed.path in ("", "/"):
        raise ValueError("repository URL must include a repository path")

    try:
        normalized_host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("repository hostname is not valid IDNA") from exc
    if normalized_host.endswith("."):
        normalized_host = normalized_host[:-1]
    if "." not in normalized_host and ":" not in normalized_host:
        raise ValueError("repository hostname must be a public DNS name")
    if normalized_host == "localhost" or normalized_host.endswith(
        _NON_PUBLIC_HOST_SUFFIXES
    ):
        raise ValueError("repository hostname must not use a local or reserved suffix")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        address = None
    if address is not None and not _routable_address(address).is_global:
        raise ValueError("repository IP address must be globally routable")
    if address is None and _looks_like_numeric_ipv4(normalized_host):
        raise ValueError("repository hostname uses a non-canonical numeric IPv4 form")
    if address is None:
        labels = normalized_host.split(".")
        if any(
            not label or label.startswith("-") or label.endswith("-")
            for label in labels
        ) or not re.fullmatch(r"[a-z0-9.-]+", normalized_host):
            raise ValueError("repository hostname is malformed")

    # Lower-case the host and omit the default HTTPS port for stable evidence.
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    normalized_netloc = normalized_host
    if port is not None and port != 443:
        normalized_netloc += f":{port}"
    normalized_path = parsed.path.rstrip("/")
    if not normalized_path:
        raise ValueError("repository URL must include a repository path")
    return urlunsplit(("https", normalized_netloc, normalized_path, "", ""))


def _looks_like_numeric_ipv4(hostname: str) -> bool:
    """Detect libcurl's one-to-four component decimal/octal/hex IPv4 syntax."""

    components = hostname.split(".")
    return bool(
        1 <= len(components) <= 4
        and all(
            _NUMERIC_IPV4_COMPONENT_RE.fullmatch(component) for component in components
        )
    )


def _routable_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Return the address whose routability controls an IP literal."""

    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address in _IPV4_COMPATIBLE_NETWORK:
            return ipaddress.IPv4Address(address.packed[-4:])
    return address


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


__all__ = ["CommandRunner", "GitCloneEvidence", "GitCloneVerifier", "run_command"]
