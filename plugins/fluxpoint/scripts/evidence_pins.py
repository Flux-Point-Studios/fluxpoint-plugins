"""Harness-side evidence pins for embedded graph inputs (Python 3.9+).

Pins use ``path#sha256=<full digest>``; quote a reference containing spaces.
The content store is local run state, never an authorization source.
"""
import hashlib
import ntpath
import os
import re
import tempfile

STORE = os.path.join(".claude", "fluxpoint", "evidence")
_PIN = re.compile(
    r'''(?P<quote>["'`])(?P<qpath>[^\r\n]*?)#sha256=(?P<qsha>[^\s"'`]+)(?P=quote)'''
    r'''|(?P<path>[^\s<>"'`]+)#sha256=(?P<sha>[^\s<>"'`)\],;.:!?]+)''')
_ABS = re.compile(
    r'''(?P<quote>["'`])(?P<quoted>(?:[A-Za-z]:[\\/]|\\\\|/)[^\r\n]*?)(?P=quote)'''
    r'''|(?<![\w:/\\])(?P<path>(?:[A-Za-z]:[\\/]|\\\\|/)[^\s<>"'`]+)''')
_HASH = re.compile(r"[0-9a-fA-F]{64}\Z")


def _strings(value, trail=()):
    if isinstance(value, str):
        yield trail, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, trail + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, trail + (index,))


def _pins(text):
    matches = list(_PIN.finditer(text))
    for marker in re.finditer(r"#sha256=", text):
        if not any(m.start() <= marker.start() < m.end() for m in matches):
            raise ValueError("EVIDENCE-PIN-INVALID: malformed pin near " + text[max(0, marker.start() - 60):marker.end() + 64])
    for match in matches:
        path = match.group("qpath") if match.group("quote") else match.group("path").lstrip("([{“")
        digest = match.group("qsha") if match.group("quote") else match.group("sha")
        if not path or not _HASH.fullmatch(digest):
            raise ValueError("EVIDENCE-PIN-INVALID: " + match.group(0))
        yield match, path, digest.lower()


def _absolute(path):
    return os.path.isabs(path) or ntpath.isabs(path)


def unpinned_paths(inputs):
    """Absolute local references, excluding pinned references and URL paths."""
    found = set()
    for _, text in _strings(inputs):
        spans = [(m.start(), m.end()) for m, _, _ in _pins(text)]
        for match in _ABS.finditer(text):
            if any(start <= match.start() < end for start, end in spans):
                continue
            path = match.group("quoted") or match.group("path").rstrip(".,;:!?)]}")
            # Plugin slash commands are instructions, not local file refs.
            # An additional slash keeps /tmp/file and similar paths strict.
            if re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_-]*:[A-Za-z0-9][A-Za-z0-9_-]*", path):
                continue
            if path:
                found.add(path)
    return sorted(found)


def _digest(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, ValueError) as exc:
        raise ValueError("EVIDENCE-PIN-MISSING: " + path + ": " + str(exc)) from exc
    return digest.hexdigest()


def _checked(path, expected):
    actual = _digest(path)
    if actual != expected:
        raise ValueError("EVIDENCE-PIN-MISMATCH: " + path + " expected " + expected + " got " + actual)


def _allow(inputs, unpinned_ok):
    if not isinstance(unpinned_ok, (list, tuple, set)) or any(
            not isinstance(path, str) or not _absolute(path) for path in unpinned_ok):
        raise ValueError("EVIDENCE-UNPINNED-ALLOWANCE: expected explicit absolute paths")
    missing = set(unpinned_paths(inputs)) - set(unpinned_ok)
    if missing:
        raise ValueError("EVIDENCE-UNPINNED: " + "; ".join(sorted(missing)) +
                         " (pin each file or name it with --unpinned-ok)")


def validate_pinned_inputs(root, inputs, unpinned_ok=()):
    """Rehash all pins; recorded/embedded references must name the local store."""
    _allow(inputs, unpinned_ok)
    store = os.path.abspath(os.path.join(root, STORE))
    records = []
    for trail, text in _strings(inputs):
        for _, path, digest in _pins(text):
            resolved = os.path.abspath(path if _absolute(path) else os.path.join(root, path))
            try:
                inside = os.path.commonpath((store, resolved)) == store
            except ValueError:
                inside = False
            if not inside or os.path.dirname(resolved) != store:
                raise ValueError("EVIDENCE-PIN-NOT-STORED: " + path + " (assemble with resume.py first)")
            if not re.fullmatch(re.escape(digest) + r"(?:\.[A-Za-z0-9_-]{1,20})?", os.path.basename(resolved)):
                raise ValueError("EVIDENCE-PIN-NAME: " + path + " does not name its full hash")
            _checked(resolved, digest)
            records.append({"input": list(trail), "path": resolved, "sha256": digest})
    return records


def prepare_pinned_inputs(root, inputs, unpinned_ok=()):
    """Copy verified pin bytes and rewrite references, preserving all other text."""
    _allow(inputs, unpinned_ok)
    store = os.path.abspath(os.path.join(root, STORE))

    def rewrite(text):
        replacements = []
        for match, path, digest in _pins(text):
            source = os.path.abspath(path if _absolute(path) else os.path.join(root, path))
            extension = os.path.splitext(path)[1]
            if not re.fullmatch(r"\.[A-Za-z0-9_-]{1,20}", extension or ""):
                extension = ""
            destination = os.path.join(store, digest + extension)
            _checked(source, digest)
            os.makedirs(store, exist_ok=True)
            if os.path.lexists(destination):
                _checked(destination, digest)
            else:
                # Rehash the exact bytes copied, closing a source-mutation race.
                descriptor, staging = tempfile.mkstemp(prefix=".pin-", dir=store)
                try:
                    with os.fdopen(descriptor, "wb") as target, open(source, "rb") as original:
                        for chunk in iter(lambda: original.read(1024 * 1024), b""):
                            target.write(chunk)
                    _checked(staging, digest)
                    # Existing content is always verified again; never overwrite
                    # a corrupt store entry just to make the refusal disappear.
                    try:
                        os.link(staging, destination)
                    except FileExistsError:
                        _checked(destination, digest)
                    except (AttributeError, NotImplementedError):
                        raise ValueError("EVIDENCE-STORE-UNSUPPORTED: atomic file publication unavailable")
                finally:
                    os.unlink(staging)
            _checked(destination, digest)
            reference = destination + "#sha256=" + digest
            if match.group("quote"):
                reference = match.group("quote") + reference + match.group("quote")
            else:
                prefix = match.group("path")[:-len(path)] if path else ""
                # Store paths containing spaces must remain a single reference.
                reference = prefix + (('"' + reference + '"') if re.search(r"\s", reference) else reference)
            replacements.append((match.start(), match.end(), reference))
        for start, end, replacement in reversed(replacements):
            text = text[:start] + replacement + text[end:]
        return text

    def walk(value):
        if isinstance(value, str):
            return rewrite(value)
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    prepared = walk(inputs)
    validate_pinned_inputs(root, prepared, unpinned_ok)
    return prepared
