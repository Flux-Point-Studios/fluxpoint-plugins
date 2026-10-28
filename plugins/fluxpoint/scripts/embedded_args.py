"""Recover and check canonical ARGS from immutable compiled source archives."""
import hashlib
import json
import os
import re

ARCHIVES = os.path.join(".claude", "fluxpoint", "compiled")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def args_hash(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def parse_script(text):
    args = re.findall(r"^const ARGS = (.*);$", text, re.MULTILINE)
    digests = re.findall(r'^const ARGS_SHA256 = "([0-9a-f]{64})";$', text, re.MULTILINE)
    if len(args) != 1 or len(digests) != 1:
        raise ValueError("RESUME-ARGS-MISSING: compiled script must have one ARGS and ARGS_SHA256 constant")
    try:
        value = json.loads(args[0])
    except ValueError as exc:
        raise ValueError("RESUME-ARGS-INVALID: compiled ARGS is not JSON") from exc
    if not isinstance(value, dict) or any(key.startswith("_") for key in value):
        raise ValueError("RESUME-ARGS-INVALID: ARGS must contain only non-underscore keys")
    if canonical(value) != args[0] or args_hash(value) != digests[0]:
        raise ValueError("RESUME-ARGS-HASH: compiled ARGS differs from ARGS_SHA256/canonical bytes")
    return value, digests[0]


def recover(root, summary):
    """Recover embedded bytes, then cross-check every key against run inputs.

    Archives sharing an args digest may hold different graphs. Their argument
    bytes must all be identical; the full source digest in the filename is
    verified before any source is parsed. We recover context, not replay
    authority; replay_memo verifies the journal/transcript separately.
    """
    digest = summary.get("argsSha256")
    if not isinstance(digest, str) or not _SHA.fullmatch(digest):
        raise ValueError("RESUME-ARGS-MISSING: run has no embedded args digest; compile/launch once with --args before structured resume")
    folder = os.path.join(root, ARCHIVES)
    candidates = sorted(name for name in os.listdir(folder) if name.startswith(digest + "-") and name.endswith(".graph.js")) if os.path.isdir(folder) else []
    if not candidates:
        raise ValueError("RESUME-SOURCE-MISSING: no immutable compiled source archive for " + digest)
    recovered = None
    for name in candidates:
        source_digest = name[len(digest) + 1:-len(".graph.js")]
        if not _SHA.fullmatch(source_digest):
            raise ValueError("RESUME-SOURCE-HASH: invalid archive filename " + name)
        with open(os.path.join(folder, name), "rb") as handle:
            source = handle.read()
        if hashlib.sha256(source).hexdigest() != source_digest:
            raise ValueError("RESUME-SOURCE-HASH: immutable compiled archive changed: " + name)
        value, checked = parse_script(source.decode("utf-8"))
        if checked != digest or (recovered is not None and canonical(recovered) != canonical(value)):
            raise ValueError("RESUME-ARGS-HASH: archived sources disagree with run args digest")
        recovered = value
    effective = summary.get("inputs")
    if not isinstance(effective, dict) or any(key.startswith("_") for key in effective):
        raise ValueError("RESUME-INPUTS-INVALID: recorded summary.inputs must be a non-underscore object")
    unbound = set(effective) - set(recovered)
    if unbound:
        raise ValueError("RESUME-INPUT-UNBOUND: recorded inputs absent from immutable ARGS: " +
                         ", ".join(sorted(unbound)) + "; launch fresh with fully embedded args")
    for key, value in recovered.items():
        if key not in effective or canonical(effective[key]) != canonical(value):
            raise ValueError("RESUME-INPUT-DRIFT: recorded input " + key + " differs from the embedded source")
    # Every carried value must be bound to immutable source bytes. The
    # assembler embeds effective defaults on the initial launch as well.
    return json.loads(canonical(recovered))
