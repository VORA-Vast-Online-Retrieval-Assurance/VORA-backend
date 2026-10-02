"""Outbound URL validation shared by discovery and engine orchestration."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

from vora.shared.cache import MISSING, BoundedCache


def ensure_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in URLs are not allowed")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Only ports 80 and 443 are allowed")
    addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private or local network targets are not allowed")
    return url



_HOSTNAME = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")


_public_hosts = BoundedCache(max_entries=4096, ttl_seconds=300)


def address_is_public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def host_is_public(host: str) -> bool:
    """Whether every address a host name resolves to is on the public internet.

    A name that does not resolve is "not public" too: nothing can be reached through it, and refusing it keeps
    the rule simple. Results are cached for five minutes, so a page's many requests to one host cost one lookup.
    """
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host:
        return False
    cached = _public_hosts.get(host)
    if cached is not MISSING:
        return cached
    try:
        ipaddress.ip_address(host)
        result = address_is_public(host)           # a literal address: judge it directly
    except ValueError:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
            result = bool(addresses) and all(address_is_public(address) for address in addresses)
        except OSError:
            result = False
    _public_hosts.set(host, result)
    return result


MAX_REDIRECTS = 4


def safe_get(url: str, *, timeout: float = 10, max_bytes: int = 300_000, headers: dict | None = None):
    """GET a page the way the server should: public targets only at every hop, a few redirects, a size cap.

    Returns ``(final_url, status, text)``. Raises ValueError for a target that is not public, httpx errors otherwise.
    """
    import httpx

    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False, verify=False,
                      headers={"User-Agent": "Mozilla/5.0", **(headers or {})}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlparse(current)
            if parts.scheme not in {"http", "https"} or parts.username or parts.password \
                    or parts.port not in {None, 80, 443} or not host_is_public(parts.hostname or ""):
                raise ValueError("Only public web addresses are allowed")
            with client.stream("GET", current) as response:
                if response.is_redirect and response.headers.get("location"):
                    current = str(httpx.URL(current).join(response.headers["location"]))
                    continue
                body = b""
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) >= max_bytes:
                        break
                return current, response.status_code, body.decode(response.encoding or "utf-8", errors="replace")
    raise ValueError("Too many redirects")


def normalize_domain(value: str) -> str:
    """"https://www.CarDekho.com/cars?x" -> "cardekho.com". Raises ValueError if invalid."""
    text = str(value or "").strip().lower()
    if not text:
        raise ValueError("Empty domain")
    host = urlparse(text if "://" in text else f"http://{text}").hostname or ""
    host = host.removeprefix("www.").rstrip(".")
    if not _HOSTNAME.match(host):
        raise ValueError(f"Not a valid domain: {value!r}")
    return host


_DOMAIN_IN_TEXT = re.compile(r"(?<![\w@/.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])", re.I)
# A dotted word that ends like a file name is not a website ("report.pdf", "notes.txt").
_FILE_ENDINGS = frozenset({
    "txt", "pdf", "csv", "xls", "xlsx", "json", "xml", "html", "htm", "php", "aspx", "py", "js", "ts", "md",
    "doc", "docx", "ppt", "pptx", "png", "jpg", "jpeg", "gif", "svg", "zip", "gz", "tar", "exe", "log",
})


def goal_domains(text: str) -> list[str]:
    """Website addresses written in a request ("fetch data example.com" -> ["example.com"]).

    Full URLs and bare domains both count; file names and numbers do not.
    """
    text = str(text or "")
    found: list[str] = []
    for url in re.findall(r"https?://[^\s<>\]\[()]+", text, re.I):
        try:
            domain = normalize_domain(url)
        except ValueError:
            continue
        if domain not in found:
            found.append(domain)
    for match in _DOMAIN_IN_TEXT.finditer(re.sub(r"https?://[^\s<>\]\[()]+", " ", text, flags=re.I)):
        candidate = match.group(1)
        if candidate.rsplit(".", 1)[-1].lower() in _FILE_ENDINGS:
            continue
        try:
            domain = normalize_domain(candidate)
        except ValueError:
            continue
        if domain not in found:
            found.append(domain)
    return found


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def same_site(host: str, domain: str) -> bool:
    """True when ``host`` is ``domain`` or one of its subdomains."""
    return host == domain or host.endswith("." + domain)
