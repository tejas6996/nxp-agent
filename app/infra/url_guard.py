"""
Outbound URL guard (server-side request forgery protection).

The agent fetches pages it is told about: newsroom links an editor adds in the EE
Herald admin, links found on those pages, press-release links pasted into the admin.
None of that may reach the server's own network: localhost, private ranges, the
cloud metadata address (169.254.169.254) or anything else that is not the public
internet.

Every fetch is checked here first, every redirect hop again, and the connection is
pinned to the address that was checked (so a DNS answer cannot change between the
check and the request).
"""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit


class UnsafeURLError(ValueError):
    """The URL is not a public http(s) address the agent may fetch."""


ALLOWED_PORTS = frozenset({80, 443})
_BLOCKED_SUFFIXES = (
    ".localhost", ".local", ".internal", ".intranet", ".lan", ".home", ".corp",
    ".localdomain", ".home.arpa",
)
_MAX_URL_LENGTH = 2048


def normalise_user_url(raw: str) -> str:
    """Tidy a link typed or pasted by a person: trim, add https:// when missing,
    drop the #fragment. Raises UnsafeURLError for anything that is not a web link."""
    text = re.sub(r"\s+", "", str(raw or ""))
    if not text:
        raise UnsafeURLError("Please enter a link.")
    if len(text) > _MAX_URL_LENGTH:
        raise UnsafeURLError("That link is too long.")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        if re.match(r"^[a-z][a-z0-9+.-]*:", text, re.I) and not re.match(r"^[^:/]+:\d+", text):
            raise UnsafeURLError("Only web links (http or https) can be added.")
        text = "https://" + text.lstrip("/")
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsafeURLError("Only web links (http or https) can be added.")
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, ""))


def basic_check(url: str) -> tuple[str, int]:
    """Syntax rules: http(s), no user:password@, a real host name, port 80/443.

    Returns (host, port)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError("That is not a valid link.") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UnsafeURLError("Only http and https links can be fetched.")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("Links with a user name or password are not allowed.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURLError("That link has no website address.")
    port = port or (443 if scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise UnsafeURLError("Only standard web ports (80 and 443) are allowed.")
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES) or "." not in host and not _is_ip(host):
        raise UnsafeURLError("That address is not on the public internet.")
    return host, port


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _public(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def resolve_public(url: str) -> tuple[str, int, str]:
    """Check a URL and resolve its host. Every address the name resolves to must be
    public (a mix of public and private answers is refused).

    Returns (host, port, ip) - the ip to pin the connection to (IPv4 preferred)."""
    host, port = basic_check(url)
    bare = host.strip("[]")
    if _is_ip(bare):
        if not _public(bare):
            raise UnsafeURLError("That address is not on the public internet.")
        return host, port, bare
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            bare, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeURLError(f"The website {host} could not be found.") from exc
    ips = list(dict.fromkeys(info[4][0] for info in infos))
    if not ips:
        raise UnsafeURLError(f"The website {host} could not be found.")
    if not all(_public(ip) for ip in ips):
        raise UnsafeURLError("That address is not on the public internet.")
    v4 = [ip for ip in ips if ":" not in ip]
    return host, port, (v4 or ips)[0]


def curl_resolve_entry(host: str, port: int, ip: str) -> str:
    """CURLOPT_RESOLVE entry pinning host:port to the checked ip."""
    return f"{host}:{port}:{'[' + ip + ']' if ':' in ip else ip}"
