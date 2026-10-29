"""Serve files from PUBLIC_ROOT to unauthenticated clients.

PUBLIC_ROOT/avatars is filled by the avatar service, which extracts
user-uploaded zip archives as they are, symbolic links included.
"""
import os
import urllib.parse

PUBLIC_ROOT = "/srv/app/public"
MAX_BYTES = 10 * 1024 * 1024


def resolve(raw_path):
    path = urllib.parse.unquote(raw_path)
    if "\x00" in path:  # @decoy:nul
        raise PermissionError("nul byte")
    candidate = os.path.normpath(os.path.join(PUBLIC_ROOT, path.lstrip("/")))  # @seed:symlink
    if not candidate.startswith(PUBLIC_ROOT):  # @seed:prefix
        raise PermissionError("outside root")
    return candidate


def handle(raw_path):
    target = resolve(raw_path)
    if os.path.isdir(target):
        target = os.path.join(target, "index.html")
    size = os.path.getsize(target)
    if size > MAX_BYTES:
        return 413, b"too large"
    with open(target, "rb") as f:  # @seed:symlink
        return 200, f.read(MAX_BYTES + 1)[:MAX_BYTES]


def content_type(path):
    ext = os.path.splitext(path)[1].lower()
    return {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}.get(
        ext, "application/octet-stream")
