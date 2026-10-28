#!/usr/bin/env python3
"""Blueprint conformance: the builder may not hand the chain a shape the validator rejects.

A validator can be proved correct and still lose the funds it guards,
because the agent that attacks a protocol does not call the validator. It
calls the API, the indexer and the transaction builder, and the validator
signs whatever that builder constructs. Every proof in this repo is about
the on-chain predicate; nothing until now held the off-chain encoder to
anything.

The usual answer is a shared type or a unified codebase. Types do not cross
the wire, CBOR does. The eight defects that caused `seam-guard.py` to be
written were all of this shape — a CBOR tag, the ledger's escaping, a
wallet's error class — each one believed rather than captured.

`plutus.json` is the one artifact both sides can be held to. `aiken build`
regenerates it from the validator, so it cannot drift from the on-chain
truth, and CIP-57 already says what a legal datum and redeemer look like.
That makes it an oracle: decode what the builder produced and ask the
blueprint whether the validator would recognise it.

  blueprint-guard.py --scan      what the blueprint declares, and what it does not
  blueprint-guard.py --conform   check PlutusData hex against a declared schema
  blueprint-guard.py --check     the gate, driven by .fluxpoint-blueprint.json

The gate reds on three things, and each is a defect rather than a style:

  * a datum or redeemer the builder produced that the blueprint refuses
  * a manifest naming a validator or purpose the blueprint does not have,
    which is a corpus that silently checks nothing
  * a `$ref` in the blueprint that does not resolve, which is a blueprint
    no reader can trust

What it deliberately does NOT red on, because that would make it
unadoptable in the repos that need it most: a schema that is opaque. A
validator taking untyped `Data` has no specification to check against, and
saying so every run is more useful than refusing to run. `requireTyped` in
the manifest turns those into failures once a repo is ready.

Three honesty rules, since a conformance checker that guesses is worse
than none:

  * A schema construct this parser does not know is reported as UNCHECKED
    and never as conformant.
  * A `$ref` that does not resolve is an error, never an empty schema that
    everything satisfies.
  * Opaque `Data` conforms by definition, and every conformant value is
    told which fields were checked against a real schema and which rode
    through on opacity.

Standard library only. The CBOR reader covers the PlutusData subset:
integers including bignums, byte strings including indefinite chunks,
lists, maps and constructors under tags 121-127, 1280-1400 and 102.
"""
import argparse
import json
import os
import re
import subprocess
import sys

from process_tree import shell_argv

MANIFEST = ".fluxpoint-blueprint.json"
BLUEPRINT = "plutus.json"
PURPOSES = ("datum", "redeemer")
MAX_DEPTH = 64
MIN_REASON = 20


class CborError(Exception):
    """Malformed CBOR, or CBOR that is not PlutusData."""


class Constr:
    __slots__ = ("index", "fields")

    def __init__(self, index, fields):
        self.index, self.fields = index, fields


class PMap:
    __slots__ = ("pairs",)

    def __init__(self, pairs):
        self.pairs = pairs


class PList:
    __slots__ = ("items",)

    def __init__(self, items):
        self.items = items


def render(v, depth=0):
    """A short, readable form of a decoded value, for an error message."""
    if depth > 4:
        return "..."
    if isinstance(v, Constr):
        inner = ", ".join(render(f, depth + 1) for f in v.fields[:4])
        more = ", ..." if len(v.fields) > 4 else ""
        return f"Constr({v.index}, [{inner}{more}])"
    if isinstance(v, PList):
        inner = ", ".join(render(x, depth + 1) for x in v.items[:4])
        more = ", ..." if len(v.items) > 4 else ""
        return f"[{inner}{more}]"
    if isinstance(v, PMap):
        inner = ", ".join(f"{render(k, depth + 1)}: {render(x, depth + 1)}"
                          for k, x in v.pairs[:3])
        more = ", ..." if len(v.pairs) > 3 else ""
        return f"{{{inner}{more}}}"
    if isinstance(v, bytes):
        return f"h'{v.hex()[:32]}{'...' if len(v) > 16 else ''}'"
    return str(v)


# --------------------------------------------------------------- CBOR reader

def _head(buf, i):
    """(major, argument, next_index). argument is None for indefinite length."""
    if i >= len(buf):
        raise CborError("truncated: expected another item")
    ib = buf[i]
    major, minor, i = ib >> 5, ib & 0x1F, i + 1
    if minor < 24:
        return major, minor, i
    width = {24: 1, 25: 2, 26: 4, 27: 8}.get(minor)
    if width:
        if i + width > len(buf):
            raise CborError("truncated: argument runs past the end")
        return major, int.from_bytes(buf[i:i + width], "big"), i + width
    if minor == 31:
        return major, None, i
    raise CborError(f"reserved additional information {minor}")


def _fields_of(v, tag):
    if not isinstance(v, PList):
        raise CborError(f"tag {tag} must wrap an array, got {render(v)}")
    return v.items


def _decode(buf, i, depth=0):
    if depth > MAX_DEPTH:
        raise CborError(f"nested deeper than {MAX_DEPTH}")
    major, arg, i = _head(buf, i)

    if major == 0:
        return arg, i
    if major == 1:
        return -1 - arg, i

    if major == 2:
        if arg is None:
            out = b""
            while True:
                m, a, i = _head(buf, i)
                if m == 7 and a is None:
                    return out, i
                if m != 2 or a is None:
                    raise CborError("indefinite byte string: bad chunk")
                out, i = out + buf[i:i + a], i + a
        if i + arg > len(buf):
            raise CborError("truncated byte string")
        return buf[i:i + arg], i + arg

    if major == 3:
        raise CborError("a text string is not Plutus data (bytes carry text on chain)")

    if major in (4, 5):
        items, want_pairs = [], major == 5
        if arg is None:
            while True:
                if i >= len(buf):
                    raise CborError("truncated: indefinite container never closed")
                if buf[i] == 0xFF:
                    i += 1
                    break
                k, i = _decode(buf, i, depth + 1)
                if want_pairs:
                    v, i = _decode(buf, i, depth + 1)
                    items.append((k, v))
                else:
                    items.append(k)
        else:
            for _ in range(arg):
                k, i = _decode(buf, i, depth + 1)
                if want_pairs:
                    v, i = _decode(buf, i, depth + 1)
                    items.append((k, v))
                else:
                    items.append(k)
        return (PMap(items) if want_pairs else PList(items)), i

    if major == 6:
        inner, i = _decode(buf, i, depth + 1)
        if 121 <= arg <= 127:
            return Constr(arg - 121, _fields_of(inner, arg)), i
        if 1280 <= arg <= 1400:
            return Constr(arg - 1280 + 7, _fields_of(inner, arg)), i
        if arg == 102:
            parts = _fields_of(inner, arg)
            if len(parts) != 2 or not isinstance(parts[0], int):
                raise CborError("tag 102 must wrap [index, fields]")
            return Constr(parts[0], _fields_of(parts[1], arg)), i
        if arg in (2, 3):
            if not isinstance(inner, bytes):
                raise CborError(f"bignum tag {arg} must wrap a byte string")
            n = int.from_bytes(inner, "big")
            return (n if arg == 2 else -1 - n), i
        raise CborError(f"tag {arg} is not a Plutus data constructor")

    raise CborError(f"major type {major} is not Plutus data")


def decode_hex(text):
    """Decode one hex string into a PlutusData value, trailing bytes refused."""
    s = text.strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    if not s or len(s) % 2 or re.search(r"[^0-9a-fA-F]", s):
        raise CborError("not an even-length hex string")
    value, i = _decode(bytes.fromhex(s), 0)
    if i != len(s) // 2:
        raise CborError(f"{len(s) // 2 - i} trailing byte(s) after the value")
    return value


# ------------------------------------------------------------ CIP-57 schemas

def _unptr(token):
    """JSON Pointer unescaping: ~1 is a slash, ~0 a tilde, in that order."""
    return token.replace("~1", "/").replace("~0", "~")


def resolve(schema, defs, seen=()):
    """Follow $ref to the schema it names. Raises on a ref that does not resolve."""
    hops = 0
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        hops += 1
        if hops > 32:
            raise CborError(f"$ref cycle at {ref}")
        if not isinstance(ref, str) or not ref.startswith("#/definitions/"):
            raise CborError(f"$ref {ref!r} is not #/definitions/<name>")
        key = _unptr(ref[len("#/definitions/"):])
        if key not in defs:
            raise CborError(f"$ref {ref!r} names no definition in the blueprint")
        schema = defs[key]
    return schema


# CIP-57's built-in opaque types, keyed by what a conforming value must be.
BUILTIN = {
    "#unit": "constructor", "#boolean": "boolean", "#integer": "integer",
    "#bytes": "bytes", "#string": "bytes", "#pair": "pair", "#list": "list",
}
KNOWN = {"integer", "bytes", "list", "map", "constructor"} | set(BUILTIN)


def is_opaque(schema, defs):
    """True when the schema constrains nothing, so any Plutus data satisfies it."""
    try:
        s = resolve(schema, defs)
    except CborError:
        return False
    if not isinstance(s, dict):
        return True
    return not ({"dataType", "anyOf", "oneOf", "allOf"} & set(s))


class Result:
    """Findings from one conformance walk, plus what went unchecked."""

    def __init__(self):
        self.errors = []
        self.opaque = []
        self.unchecked = []

    def ok(self):
        return not self.errors and not self.unchecked


def conform(value, schema, defs, res, path="datum", depth=0):
    """Append findings to `res`. A construct we cannot read is never a pass."""
    if depth > MAX_DEPTH:
        res.errors.append((path, "schema nested too deep"))
        return
    try:
        s = resolve(schema, defs)
    except CborError as e:
        res.errors.append((path, str(e)))
        return
    if not isinstance(s, dict):
        res.errors.append((path, f"schema is {type(s).__name__}, not an object"))
        return

    alts = s.get("anyOf") or s.get("oneOf")
    if alts:
        if not isinstance(alts, list) or not alts:
            res.errors.append((path, "anyOf/oneOf must be a non-empty list"))
            return
        # One alternative has to fit. Which one failed, and why, is the whole
        # diagnostic: every Aiken record type is a one-alternative anyOf, so
        # collapsing that to "matches none of 1" throws away the only useful
        # sentence. A single alternative reports its own findings, and a sum
        # type reports the branch whose constructor index the value actually
        # claimed. Only a value matching no index at all gets the summary.
        tried, index_of = [], {}
        for n, alt in enumerate(alts):
            trial = Result()
            conform(value, alt, defs, trial, path, depth + 1)
            if trial.ok():
                res.opaque += trial.opaque
                return
            tried.append(trial)
            try:
                a = resolve(alt, defs)
                if isinstance(a, dict) and a.get("dataType") == "constructor":
                    index_of[n] = a.get("index", 0)
            except CborError:
                pass
        pick = None
        if len(alts) == 1:
            pick = 0
        elif isinstance(value, Constr):
            hits = [n for n, idx in index_of.items() if idx == value.index]
            if len(hits) == 1:
                pick = hits[0]
        if pick is not None:
            res.errors += tried[pick].errors
            res.unchecked += tried[pick].unchecked
            return
        offered = [str(i) for i in index_of.values()]
        got = (f"constructor {value.index}" if isinstance(value, Constr)
               else render(value))
        res.errors.append((path, f"matches none of the {len(alts)} alternative(s)"
                                 + (f" (indices {', '.join(offered)}); got {got}"
                                    if offered else f"; got {got}")))
        return

    dt = s.get("dataType")
    if dt is None:
        # CIP-57: no dataType and no alternatives is opaque `Data`. Anything
        # conforms, and the caller is told where that happened.
        res.opaque.append(path)
        return
    if dt not in KNOWN:
        res.unchecked.append((path, f"dataType {dt!r} is not one this checker knows"))
        return

    kind = BUILTIN.get(dt, dt)

    if kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            res.errors.append((path, f"expected an integer, got {render(value)}"))
        return
    if kind == "bytes":
        if not isinstance(value, bytes):
            res.errors.append((path, f"expected bytes, got {render(value)}"))
        return
    if kind == "boolean":
        if not isinstance(value, Constr) or value.index not in (0, 1) or value.fields:
            res.errors.append((path, f"expected a boolean constructor, got {render(value)}"))
        return
    if kind == "pair":
        if not isinstance(value, PList) or len(value.items) != 2:
            res.errors.append((path, f"expected a 2-element pair, got {render(value)}"))
            return
        for idx, key in ((0, "left"), (1, "right")):
            if key in s:
                conform(value.items[idx], s[key], defs, res, f"{path}.{key}", depth + 1)
            else:
                res.opaque.append(f"{path}.{key}")
        return

    if kind == "list":
        if not isinstance(value, PList):
            res.errors.append((path, f"expected a list, got {render(value)}"))
            return
        items = s.get("items")
        if items is None:
            res.opaque.append(f"{path}[]")
            return
        if isinstance(items, list):
            # A fixed-arity tuple. Arity is part of the specification.
            if len(value.items) != len(items):
                res.errors.append(
                    (path, f"expected {len(items)} element(s), got {len(value.items)}"))
                return
            for n, (v, sub) in enumerate(zip(value.items, items)):
                conform(v, sub, defs, res, f"{path}[{n}]", depth + 1)
            return
        for n, v in enumerate(value.items):
            conform(v, items, defs, res, f"{path}[{n}]", depth + 1)
        return

    if kind == "map":
        if not isinstance(value, PMap):
            res.errors.append((path, f"expected a map, got {render(value)}"))
            return
        for n, (k, v) in enumerate(value.pairs):
            if "keys" in s:
                conform(k, s["keys"], defs, res, f"{path}<key {n}>", depth + 1)
            else:
                res.opaque.append(f"{path}<key {n}>")
            if "values" in s:
                conform(v, s["values"], defs, res, f"{path}<value {n}>", depth + 1)
            else:
                res.opaque.append(f"{path}<value {n}>")
        return

    # constructor, and CIP-57's #unit which is constructor 0 with no fields
    if not isinstance(value, Constr):
        res.errors.append((path, f"expected a constructor, got {render(value)}"))
        return
    want = s.get("index", 0) if dt == "constructor" else 0
    if value.index != want:
        res.errors.append((path, f"expected constructor {want}, got {value.index}"))
        return
    fields = s.get("fields")
    if fields is None:
        res.opaque.append(f"{path}(...)")
        return
    if not isinstance(fields, list):
        res.errors.append((path, "constructor fields must be a list"))
        return
    if len(value.fields) != len(fields):
        res.errors.append(
            (path, f"constructor {want} takes {len(fields)} field(s), got {len(value.fields)}"))
        return
    for n, (v, sub) in enumerate(zip(value.fields, fields)):
        name = sub.get("title") if isinstance(sub, dict) else None
        conform(v, sub, defs, res, f"{path}.{name or n}", depth + 1)


# ------------------------------------------------------------------ blueprint

def load_blueprint(root):
    p = os.path.join(root, BLUEPRINT)
    if not os.path.exists(p):
        return None, None
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"blueprint-guard: {BLUEPRINT} is not readable JSON: {e}")
    if not isinstance(doc, dict) or not isinstance(doc.get("validators"), list):
        raise SystemExit(f"blueprint-guard: {BLUEPRINT} has no validators list")
    return doc, doc.get("definitions") or {}


def find(doc, title, purpose):
    """(schema, problem). A purpose the validator does not declare is a problem."""
    matches = [v for v in doc["validators"] if v.get("title") == title]
    if not matches:
        have = ", ".join(sorted(v.get("title", "?") for v in doc["validators"])[:8])
        return None, f"no validator titled {title!r} in the blueprint (have: {have})"
    entry = matches[0].get(purpose)
    if not isinstance(entry, dict) or "schema" not in entry:
        return None, f"{title} declares no {purpose} schema"
    return entry["schema"], None


def load_manifest(root):
    p = os.path.join(root, MANIFEST)
    if not os.path.exists(p):
        return None, []
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return None, [f"{MANIFEST} is not readable JSON ({e}) — nothing is checked"]
    if not isinstance(doc, dict):
        return None, [f"{MANIFEST} must be an object"]
    problems = []
    corpora = doc.get("corpora")
    if corpora is None:
        corpora = []
    if not isinstance(corpora, list):
        return None, [f"{MANIFEST}: 'corpora' must be a list"]
    for n, c in enumerate(corpora):
        if not isinstance(c, dict):
            problems.append(f"{MANIFEST}[{n}] is not an object")
            continue
        missing = [k for k in ("validator", "purpose", "produce")
                   if not isinstance(c.get(k), str) or not c[k]]
        if missing:
            problems.append(f"{MANIFEST}[{n}] is missing {', '.join(missing)}")
        elif c["purpose"] not in PURPOSES:
            problems.append(f"{MANIFEST}[{n}]: purpose must be one of {', '.join(PURPOSES)}")
    waived = doc.get("waived") or {}
    if not isinstance(waived, dict):
        problems.append(f"{MANIFEST}: 'waived' must be an object of title: reason")
        waived = {}
    doc["corpora"], doc["waived"] = corpora, waived
    return doc, problems


def run_producer(root, cmd):
    """(hex_lines, error). The producer prints one PlutusData hex per line."""
    try:
        r = subprocess.run(shell_argv(cmd), cwd=root, capture_output=True,
                           text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"could not run {cmd!r}: {e}"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        return [], (f"{cmd!r} exited {r.returncode}"
                    + (f": {tail[-1][:160]}" if tail else ""))
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    if not lines:
        return [], f"{cmd!r} printed nothing — a corpus of zero checks nothing"
    return lines, None


def check_corpus(root, doc, defs, c):
    """(checked, findings) for one corpus entry."""
    title, purpose = c["validator"], c["purpose"]
    schema, problem = find(doc, title, purpose)
    if problem:
        return 0, [f"{title}/{purpose}: {problem}"]
    lines, err = run_producer(root, c["produce"])
    if err:
        return 0, [f"{title}/{purpose}: {err}"]
    findings = []
    for n, hexline in enumerate(lines, 1):
        try:
            value = decode_hex(hexline)
        except CborError as e:
            findings.append(f"{title}/{purpose} line {n}: not decodable as Plutus data — {e}")
            continue
        res = Result()
        conform(value, schema, defs, res, purpose)
        for path, why in res.errors:
            findings.append(f"{title}/{purpose} line {n}: at {path}: {why}")
        for path, why in res.unchecked:
            findings.append(f"{title}/{purpose} line {n}: at {path}: UNCHECKED — {why}")
    return len(lines), findings


# ----------------------------------------------------------------- the modes

def do_scan(root, doc, defs):
    print(f"blueprint-guard: {len(doc['validators'])} validator entr(ies) in {BLUEPRINT}")
    typed = opaque = 0
    for v in doc["validators"]:
        title = v.get("title", "<untitled>")
        bits = []
        for purpose in PURPOSES:
            entry = v.get(purpose)
            if not isinstance(entry, dict) or "schema" not in entry:
                bits.append(f"{purpose}: none")
                continue
            try:
                resolve(entry["schema"], defs)
            except CborError as e:
                bits.append(f"{purpose}: BROKEN REF ({e})")
                continue
            if is_opaque(entry["schema"], defs):
                bits.append(f"{purpose}: OPAQUE Data (nothing to check)")
                opaque += 1
            else:
                bits.append(f"{purpose}: typed")
                typed += 1
        print(f"  {title}")
        print(f"      {' | '.join(bits)}")
    print(f"blueprint-guard: {typed} typed schema(s), {opaque} opaque")
    if opaque:
        print("  An opaque schema is a validator taking untyped Data. There is no "
              "specification\n  to hold the builder to there, so this gate is blind "
              "to it — say so in review\n  rather than reading the green as coverage.")
    return 0


def do_conform(root, doc, defs, a):
    schema, problem = find(doc, a.validator, a.purpose)
    if problem:
        print(f"blueprint-guard: {problem}", file=sys.stderr)
        return 1
    if a.src:
        try:
            with open(os.path.join(root, a.src), encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as e:
            print(f"blueprint-guard: cannot read {a.src}: {e}", file=sys.stderr)
            return 1
    else:
        raw = sys.stdin.read()
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        print("blueprint-guard: no hex on stdin — nothing checked, which is not a pass",
              file=sys.stderr)
        return 2
    bad = 0
    for n, hexline in enumerate(lines, 1):
        try:
            value = decode_hex(hexline)
        except CborError as e:
            print(f"  line {n}: NOT PLUTUS DATA — {e}", file=sys.stderr)
            bad += 1
            continue
        res = Result()
        conform(value, schema, defs, res, a.purpose)
        if res.ok():
            note = f" ({len(res.opaque)} opaque field(s) unchecked)" if res.opaque else ""
            print(f"  line {n}: conforms{note}")
            continue
        bad += 1
        print(f"  line {n}: REFUSED — {render(value)}", file=sys.stderr)
        for path, why in res.errors:
            print(f"      at {path}: {why}", file=sys.stderr)
        for path, why in res.unchecked:
            print(f"      at {path}: UNCHECKED — {why}", file=sys.stderr)
    print(f"blueprint-guard: {len(lines) - bad}/{len(lines)} conform to "
          f"{a.validator}/{a.purpose}")
    return 1 if bad else 0


def do_check(root, doc, defs):
    manifest, problems = load_manifest(root)
    findings = list(problems)
    checked = 0
    if manifest is None and not problems:
        print(f"blueprint-guard: {BLUEPRINT} is tracked but {MANIFEST} is absent, so "
              f"nothing\n  holds the builder to it. Run --scan, then declare a corpus.",
              file=sys.stderr)
        return 0  # bootstrapping must not block; arming is a deliberate step
    if manifest:
        for c in manifest["corpora"]:
            if not isinstance(c, dict) or not all(
                    isinstance(c.get(k), str) for k in ("validator", "purpose", "produce")):
                continue
            n, f = check_corpus(root, doc, defs, c)
            checked += n
            findings += f
        # A blueprint whose refs do not resolve is one no reader can trust,
        # whether or not a corpus happens to exercise that branch.
        for v in doc["validators"]:
            title = v.get("title", "<untitled>")
            for purpose in PURPOSES:
                entry = v.get(purpose)
                if isinstance(entry, dict) and "schema" in entry:
                    try:
                        resolve(entry["schema"], defs)
                    except CborError as e:
                        findings.append(f"{title}/{purpose}: unreadable schema — {e}")
        covered = {(c["validator"], c["purpose"]) for c in manifest["corpora"]
                   if isinstance(c, dict) and isinstance(c.get("validator"), str)}
        waived = manifest["waived"]
        blind = []
        for v in doc["validators"]:
            title = v.get("title", "<untitled>")
            if title in waived:
                continue
            for purpose in PURPOSES:
                entry = v.get(purpose)
                if not isinstance(entry, dict) or "schema" not in entry:
                    continue
                if (title, purpose) in covered:
                    continue
                blind.append(f"{title}/{purpose}"
                             + (" (opaque)" if is_opaque(entry["schema"], defs) else ""))
        if blind:
            line = ("blueprint-guard: declared but unchecked — no corpus holds the "
                    "builder to: " + ", ".join(sorted(blind)))
            if manifest.get("requireTyped"):
                findings.append(line)
            else:
                print(line)
        for title, reason in sorted(waived.items()):
            if not isinstance(reason, str) or len(reason.strip()) < MIN_REASON:
                findings.append(f"{MANIFEST}: the waiver for {title} needs at least "
                                f"{MIN_REASON} characters saying why")

    if not findings:
        print(f"blueprint-guard: green — {checked} encoded value(s) conform to "
              f"{BLUEPRINT}")
        return 0
    print("\nblueprint-guard: RED — the builder and the blueprint disagree\n",
          file=sys.stderr)
    for f in findings:
        print(f"  {f}", file=sys.stderr)
    print(
        "\n  A validator proved correct still signs whatever the builder constructs, so\n"
        "  a datum the blueprint refuses is a transaction the chain refuses or, worse,\n"
        "  one it accepts and reads differently. Fix the encoder, or re-declare the\n"
        "  corpus if the schema legitimately moved.",
        file=sys.stderr)
    return 1


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--validator", help="blueprint title, e.g. vault.spend")
    ap.add_argument("--purpose", choices=PURPOSES, default="datum")
    ap.add_argument("--from", dest="src", help="file of hex lines (default stdin)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan", action="store_true")
    g.add_argument("--conform", action="store_true")
    g.add_argument("--check", action="store_true")
    a = ap.parse_args()

    doc, defs = load_blueprint(a.root)
    if doc is None:
        print(f"blueprint-guard: no {BLUEPRINT} — nothing built to check against")
        return 0

    if a.scan:
        return do_scan(a.root, doc, defs)
    if a.conform:
        if not a.validator:
            print("blueprint-guard: --conform needs --validator <title>", file=sys.stderr)
            return 2
        return do_conform(a.root, doc, defs, a)
    return do_check(a.root, doc, defs)


if __name__ == "__main__":
    sys.exit(main())
