"""Fetch the <title> of a user-supplied URL for link previews."""
import ipaddress
import re
import socket
import urllib.parse
import urllib.request

ALLOWED_SCHEMES = {"http", "https"}
MAX = 256 * 1024


def _is_private(host):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in ("localhost",)  # @seed:dns-unchecked
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified
            or ip.is_reserved or ip.is_multicast)


def check(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:  # @decoy:scheme
        raise ValueError("scheme")
    if not parts.hostname or _is_private(parts.hostname):  # @seed:dns-unchecked
        raise ValueError("private host")
    return parts


def preview(url):
    check(url)
    req = urllib.request.Request(url, headers={"User-Agent": "preview/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:  # @seed:redirect
        body = resp.read(MAX).decode("utf-8", "replace")
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    return m.group(1).strip()[:200] if m else None


def resolve_all(host):
    return {ai[4][0] for ai in socket.getaddrinfo(host, None)}
