import asyncio
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from athena.research.prepare import source_verification
from athena.research.prepare.baseline_research import BaselineResearchError
from athena.research.prepare.source_verification import (
    GitCloneEvidence,
    GitCloneVerifier,
    run_command,
)


@pytest.fixture(autouse=True)
def _stable_repository_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(host, port, *, family, type, proto):
        del host, family
        return [
            (
                socket.AF_INET,
                type,
                proto,
                "",
                ("93.184.216.34", port),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


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


def test_fresh_import_ignores_agent_writable_cwd_and_path(
    tmp_path: Path,
) -> None:
    trusted_git = source_verification._GIT_EXECUTABLE
    if trusted_git is None:
        pytest.skip("trusted Git executable is unavailable on this host")
    trusted_path = Path(trusted_git).resolve(strict=True)
    fake_bin = tmp_path / "agent-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / trusted_path.name
    shutil.copy2(trusted_path, fake_git)
    fake_git.chmod(0o755)
    environment = dict(os.environ)
    environment["PATH"] = str(fake_bin)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from athena.research.prepare.source_verification import "
            "_GIT_EXECUTABLE; print(_GIT_EXECUTABLE or '')",
        ],
        cwd=fake_bin,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(trusted_path)


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
    assert Path(clone_argv[0]).is_absolute()
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
    assert runner.calls[1][0][0] == clone_argv[0]


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
        "https://bad..example/x.git",
        "https://%65xample.com/x.git",
        "https://git.local/repo.git",
        "https://metadata.google.internal/repo.git",
        "https://intranet/repo.git",
        "https://example.com:0/repo.git",
        "https://example.com:0000/repo.git",
        "https://0177.0.0.1/repo.git",
        "https://0x7f.0.0.1/repo.git",
        "https://127.1/repo.git",
        "https://2130706433/repo.git",
        "https://017700000001/repo.git",
        "https://0x7f000001/repo.git",
        "https://127.0.1/repo.git",
        "https://127.0.0.01/repo.git",
        "https://0x7f.00.0.1/repo.git",
        "https://localhost/x.git",
        "https://api.localhost/x.git",
        "https://127.0.0.1/x.git",
        "https://10.0.0.5/x.git",
        "https://169.254.1.2/x.git",
        "https://0.0.0.0/x.git",
        "https://224.0.0.1/x.git",
        "https://[::1]/x.git",
        "https://[fd00::1]/x.git",
        "https://[fe80::1]/x.git",
        "https://[fec0::1]/x.git",
        "https://[ff0e::1]/x.git",
        "https://[::]/x.git",
        "https://[2001:db8::1]/x.git",
        "https://[::127.0.0.1]/repo.git",
        "https://[::7f00:1]/repo.git",
        "https://[::10.0.0.1]/repo.git",
        "https://[::a00:1]/repo.git",
        "https://[::169.254.1.2]/repo.git",
        "https://[::a9fe:102]/repo.git",
        "https://[::192.0.2.1]/repo.git",
        "https://[::c000:201]/repo.git",
        "https://[::ffff:127.0.0.1]/repo.git",
        "https://[::ffff:7f00:1]/repo.git",
        "https://[::ffff:10.0.0.1]/repo.git",
        "https://[::ffff:a00:1]/repo.git",
        "https://[::ffff:169.254.1.2]/repo.git",
        "https://[::ffff:a9fe:102]/repo.git",
        "https://[::ffff:0.0.0.0]/repo.git",
        "https://[::ffff:192.0.2.1]/repo.git",
        "https://[::ffff:c000:201]/repo.git",
        "https://[::ffff:224.0.0.1]/repo.git",
        "https://[::ffff:e000:1]/repo.git",
        "https://[::224.0.0.1]/repo.git",
        "https://[::e000:1]/repo.git",
        "https://example.com/repo\x00.git",
        "https://example.com/repo%40.git",
        "https://example.com/repo%3A.git",
        "https://example.com/repo%2F.git",
        "https://example.com/repo%3F.git",
        "https://example.com/repo%23.git",
        "https://example.com/repo%5C.git",
        "https://example.com/repo%25.git",
        "https://user%40example.com/repo.git",
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
async def test_git_verifier_idna_normalizes_global_domain() -> None:
    runner = FakeRunner()

    evidence = await GitCloneVerifier(runner=runner).verify(
        "https://例え.テスト/repo.git"
    )

    assert evidence.repository_url == "https://xn--r8jz45g.xn--zckzah/repo.git"
    assert runner.calls[0][0][runner.calls[0][0].index("--") + 1] == (
        "https://xn--r8jz45g.xn--zckzah/repo.git"
    )


@pytest.mark.asyncio
async def test_git_verifier_normalizes_default_https_port_and_host_case() -> None:
    runner = FakeRunner()

    evidence = await GitCloneVerifier(runner=runner).verify(
        "https://EXAMPLE.com:443/repo.git/"
    )

    assert evidence.repository_url == "https://example.com/repo.git"


@pytest.mark.asyncio
async def test_git_verifier_accepts_canonical_global_ip_literal() -> None:
    runner = FakeRunner()

    evidence = await GitCloneVerifier(runner=runner).verify("https://8.8.8.8/repo.git")

    assert evidence.repository_url == "https://8.8.8.8/repo.git"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository_url",
    [
        "https://[::8.8.8.8]/repo.git",
        "https://[::808:808]/repo.git",
        "https://[::ffff:8.8.8.8]/repo.git",
        "https://[::ffff:808:808]/repo.git",
    ],
)
async def test_git_verifier_accepts_embedded_global_ipv4_literal(
    repository_url: str,
) -> None:
    runner = FakeRunner()

    evidence = await GitCloneVerifier(runner=runner).verify(repository_url)

    assert evidence.repository_url == repository_url
    assert len(runner.calls) == 2


@pytest.mark.asyncio
async def test_git_verifier_preserves_valid_alternate_https_port() -> None:
    runner = FakeRunner()

    evidence = await GitCloneVerifier(runner=runner).verify(
        "https://EXAMPLE.com:8443/repo.git/"
    )

    assert evidence.repository_url == "https://example.com:8443/repo.git"
    assert runner.calls[0][0][runner.calls[0][0].index("--") + 1] == (
        "https://example.com:8443/repo.git"
    )


def test_repository_dns_resolver_collects_all_a_and_aaaa_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, int, int, int]] = []

    def getaddrinfo(host, port, *, family, type, proto):
        calls.append((host, port, family, type, proto))
        return [
            (socket.AF_INET6, type, proto, "", ("2606:4700:4700::1111", port, 0, 0)),
            (socket.AF_INET, type, proto, "", ("8.8.8.8", port)),
            (socket.AF_INET, type, proto, "", ("8.8.8.8", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    resolver = getattr(source_verification, "_resolve_public_host_addresses", None)

    assert callable(resolver)
    assert resolver("example.com", 8443) == (
        "8.8.8.8",
        "2606:4700:4700::1111",
    )
    assert calls == [
        (
            "example.com",
            8443,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
    ]


@pytest.mark.asyncio
async def test_git_verifier_rejects_mixed_public_private_dns_before_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def getaddrinfo(_host, port, *, family, type, proto):
        del family
        return [
            (socket.AF_INET, type, proto, "", ("8.8.8.8", port)),
            (socket.AF_INET, type, proto, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    runner = FakeRunner()

    with pytest.raises(BaselineResearchError, match="Git source verification failed"):
        await GitCloneVerifier(runner=runner).verify("https://example.com/repo.git")

    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolved_family", "address"),
    [
        (socket.AF_INET, "224.0.0.1"),
        (socket.AF_INET6, "ff0e::1"),
        (socket.AF_INET6, "::ffff:224.0.0.1"),
        (socket.AF_INET6, "::224.0.0.1"),
    ],
)
async def test_git_verifier_rejects_multicast_dns_forms_before_git(
    resolved_family: int,
    address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def getaddrinfo(_host, port, *, family, type, proto):
        del family
        sockaddr = (address, port) if ":" not in address else (address, port, 0, 0)
        return [(resolved_family, type, proto, "", sockaddr)]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    runner = FakeRunner()

    with pytest.raises(BaselineResearchError, match="Git source verification failed"):
        await GitCloneVerifier(runner=runner).verify("https://example.com/repo.git")

    assert runner.calls == []


@pytest.mark.asyncio
async def test_git_verifier_rejects_dns_failure_before_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def getaddrinfo(_host, _port, *, family, type, proto):
        del family, type, proto
        raise socket.gaierror("controlled resolver failure")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    runner = FakeRunner()

    with pytest.raises(BaselineResearchError, match="Git source verification failed"):
        await GitCloneVerifier(runner=runner).verify("https://example.com/repo.git")

    assert runner.calls == []


@pytest.mark.asyncio
async def test_git_verifier_rejects_changed_dns_snapshot_before_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["8.8.8.8", "1.1.1.1"])

    def getaddrinfo(_host, port, *, family, type, proto):
        del family
        return [(socket.AF_INET, type, proto, "", (next(answers), port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    runner = FakeRunner()

    with pytest.raises(BaselineResearchError, match="Git source verification failed"):
        await GitCloneVerifier(runner=runner).verify("https://example.com/repo.git")

    assert runner.calls == []


@pytest.mark.asyncio
async def test_git_verifier_pins_all_addresses_and_disables_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def getaddrinfo(_host, port, *, family, type, proto):
        del family
        return [
            (socket.AF_INET6, type, proto, "", ("2606:4700:4700::1111", port, 0, 0)),
            (socket.AF_INET, type, proto, "", ("8.8.8.8", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    runner = FakeRunner()

    await GitCloneVerifier(runner=runner).verify("https://EXAMPLE.com:8443/repo.git")

    expected_resolve = (
        "http.curloptResolve=example.com:8443:" "8.8.8.8,[2606:4700:4700::1111]"
    )
    assert len(runner.calls) == 2
    for argv, _, _, _ in runner.calls:
        assert expected_resolve in argv
        assert "http.followRedirects=false" in argv
        assert "http.emptyAuth=false" in argv
        assert "http.proactiveAuth=none" in argv
        assert "http.delegation=none" in argv


@pytest.mark.asyncio
async def test_git_verifier_rejects_control_characters_without_running_commands() -> (
    None
):
    runner = FakeRunner()

    with pytest.raises(BaselineResearchError) as caught:
        await GitCloneVerifier(runner=runner).verify("https://example.com/repo\x00.git")

    assert str(caught.value) == "Git source verification failed"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_git_verifier_reports_bounded_redacted_clone_failure() -> None:
    secret = "super-secret-token"
    bearer_secret = "actual-bearer-secret"
    stderr = (
        "fatal: could not read Password for "
        f"'https://alice:{secret}@example.com/x.git': "
        f"Authorization: Bearer {bearer_secret}; " + (" noisy\noutput\t" * 5000)
    )
    runner = FakeRunner(clone_code=128, stderr=stderr)

    with pytest.raises(BaselineResearchError) as caught:
        await GitCloneVerifier(runner=runner).verify("https://example.com/x.git")

    diagnostic = " ".join(caught.value.diagnostics)
    assert len(diagnostic) <= 4000
    assert secret not in diagnostic
    assert bearer_secret not in diagnostic
    assert "alice" not in diagnostic
    assert "  " not in diagnostic
    assert len(runner.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential_diagnostic",
    [
        "Authorization: Bearer bearer-token-value",
        "authorization: Basic basic-credential-value",
        "fatal: https://alice%3Aescaped-secret%40example.com/repo.git",
    ],
)
async def test_git_verifier_redacts_complete_authorization_and_escaped_url_credentials(
    credential_diagnostic: str,
) -> None:
    runner = FakeRunner(clone_code=128, stderr=credential_diagnostic)

    with pytest.raises(BaselineResearchError) as caught:
        await GitCloneVerifier(runner=runner).verify("https://example.com/x.git")

    diagnostic = " ".join(caught.value.diagnostics)
    assert "bearer-token-value" not in diagnostic
    assert "basic-credential-value" not in diagnostic
    assert "escaped-secret" not in diagnostic
    assert "alice" not in diagnostic


@pytest.mark.asyncio
async def test_git_verifier_isolates_git_environment_and_config_for_both_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inherited = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "url.https://evil.example/.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://example.com/",
        "GIT_SSH_COMMAND": "malicious-helper --proxy evil",
        "GIT_PROXY_COMMAND": "malicious-proxy",
        "GIT_ASKPASS": "malicious-askpass",
        "SSH_ASKPASS": "malicious-askpass",
        "GIT_EXEC_PATH": "C:\\malicious",
        "GIT_ALLOW_PROTOCOL": "file:ssh",
        "HTTP_PROXY": "http://proxy.invalid",
        "HTTPS_PROXY": "http://proxy.invalid",
        "ALL_PROXY": "http://proxy.invalid",
        "http_proxy": "http://proxy.invalid",
        "https_proxy": "http://proxy.invalid",
        "no_proxy": "example.com",
        "GIT_SSL_CERT": "client-cert-secret.pem",
        "GIT_SSL_KEY": "client-key-secret.pem",
        "GIT_SSL_CAINFO": "private-ca-secret.pem",
        "GIT_SSL_CAPATH": "private-ca-directory-secret",
        "GIT_SSL_VERSION": "tlsv1.0-secret",
        "GIT_PROXY_SSL_CERT": "proxy-client-cert-secret.pem",
        "GIT_PROXY_SSL_KEY": "proxy-client-key-secret.pem",
        "CURL_CA_BUNDLE": "curl-ca-secret.pem",
        "SSL_CERT_FILE": "tls-ca-secret.pem",
        "SSL_CERT_DIR": "tls-ca-directory-secret",
        "GIT_TRACE_CURL": "trace-secret.log",
        "ATHENA_GIT_ENV_SECRET": "arbitrary-environment-secret",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    runner = FakeRunner()

    await GitCloneVerifier(runner=runner).verify("https://example.com/x.git")

    assert len(runner.calls) == 2
    for argv, _, environment, _ in runner.calls:
        assert inherited.keys().isdisjoint(environment)
        assert "PATH" not in environment
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert environment["GIT_CONFIG_GLOBAL"]
        assert environment["GIT_CONFIG_SYSTEM"]
        assert {key.casefold() for key in environment} <= {
            "gcm_interactive",
            "git_config_global",
            "git_config_nosystem",
            "git_config_system",
            "git_terminal_prompt",
            "lang",
            "lc_all",
            "systemroot",
            "windir",
        }
        assert "protocol.allow=never" in argv
        assert "protocol.https.allow=always" in argv
        assert "credential.helper=" in argv
        assert "http.followRedirects=false" in argv
        assert "http.emptyAuth=false" in argv
        assert "http.proactiveAuth=none" in argv
        assert "http.delegation=none" in argv
        assert "http.cookieFile=" in argv
        assert "http.saveCookies=false" in argv
        assert "http.extraHeader=" in argv
        assert "http.sslCert=" in argv
        assert "http.sslKey=" in argv
        assert "http.sslCertPasswordProtected=false" in argv
        assert "http.proxy=" in argv


@pytest.mark.asyncio
async def test_git_verifier_does_not_expose_tls_environment_in_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "tls-environment-marker-must-not-leak"
    for name in (
        "GIT_SSL_CERT",
        "GIT_SSL_KEY",
        "GIT_SSL_CAINFO",
        "GIT_PROXY_SSL_CERT",
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
    ):
        monkeypatch.setenv(name, secret)

    class EchoEnvironmentRunner(FakeRunner):
        async def __call__(self, argv, *, cwd, env, timeout_s):
            self.calls.append((list(argv), cwd, dict(env), timeout_s))
            return subprocess.CompletedProcess(argv, 128, "", repr(dict(env)))

    runner = EchoEnvironmentRunner()
    with pytest.raises(BaselineResearchError) as caught:
        await GitCloneVerifier(runner=runner).verify("https://example.com/repo.git")

    assert secret not in " ".join(caught.value.diagnostics)
    assert secret not in repr(runner.calls[0][2])


@pytest.mark.asyncio
async def test_git_verifier_ignores_runtime_path_git_shim_with_real_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / ("git.exe" if os.name == "nt" else "git")
    launcher = Path(os.environ["COMSPEC"]) if os.name == "nt" else Path("/bin/sh")
    shutil.copy2(launcher, fake_git)
    fake_git.chmod(0o755)
    sentinel = tmp_path / "fake-git-ran"
    if os.name == "nt":
        probe_script = tmp_path / "probe_git_shim.cmd"
        probe_script.write_text('@echo executed>"%~1"\n', encoding="utf-8")
        probe_arguments = ["/d", "/c", str(probe_script), str(sentinel)]
    else:
        probe_script = tmp_path / "probe_git_shim.sh"
        probe_script.write_text('printf executed > "$1"\n', encoding="utf-8")
        probe_arguments = [str(probe_script), str(sentinel)]
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.chdir(fake_bin)

    class SubprocessProbeRunner(FakeRunner):
        async def __call__(self, argv, *, cwd, env, timeout_s):
            await run_command(
                [argv[0], *probe_arguments],
                cwd=cwd,
                env=env,
                timeout_s=timeout_s,
            )
            return await super().__call__(
                argv,
                cwd=cwd,
                env=env,
                timeout_s=timeout_s,
            )

    runner = SubprocessProbeRunner()
    await GitCloneVerifier(runner=runner).verify("https://example.com/repo.git")

    assert not sentinel.exists()
    assert len(runner.calls) == 2
    assert Path(runner.calls[0][0][0]).is_absolute()
    assert runner.calls[1][0][0] == runner.calls[0][0][0]


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
