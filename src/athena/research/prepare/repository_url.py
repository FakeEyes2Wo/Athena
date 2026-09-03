"""Canonicalization for repository URLs crossing the baseline trust boundary."""

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

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


def normalize_public_https_repository_url(value: str) -> str:
    """Accept only a public HTTPS repository URL and return its canonical form."""

    if not isinstance(value, str) or not value:
        raise ValueError("repository URL must be a non-empty HTTPS URL")
    if value != value.strip() or any(character.isspace() for character in value):
        raise ValueError("repository URL must not contain whitespace")
    if any(ord(character) <= 0x1F or ord(character) == 0x7F for character in value):
        raise ValueError("repository URL must not contain control characters")
    if "?" in value or "#" in value:
        raise ValueError("repository URL must not contain a query or fragment")
    if "\\" in value:
        raise ValueError("repository URL must use URL path separators")
    if _STRUCTURAL_ESCAPE_RE.search(value):
        raise ValueError("repository URL must not encode structural delimiters")

    try:
        parsed = urlsplit(value)
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
    if address is not None and not _is_public_repository_address(address):
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


def _is_public_repository_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Classify literals and embedded IPv4 forms without trusting is_global alone."""

    routed = _routable_address(address)
    return bool(
        routed.is_global
        and not address.is_multicast
        and not routed.is_multicast
        and not getattr(address, "is_site_local", False)
        and not getattr(routed, "is_site_local", False)
    )


__all__ = ["normalize_public_https_repository_url"]
