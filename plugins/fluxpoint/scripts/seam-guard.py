#!/usr/bin/env python3
"""Ratchet test doubles and hold their JSON fixtures to recorded responses.

  --scan                         list JS/TS and Python doubles
  --baseline                     record the counts in the shared proof baseline
  --check                        check recordings and fail on unrecorded growth
  --conform DOUBLE RECORDING      compare two JSON files, recursively

.fluxpoint-recordings.json declares captures with provenance and the JSON
fixtures derived from each capture. Every declared fixture is checked, even
without tests or a seam baseline. Missing keys, array entries and JSON type
mismatches fail at their JSON path; scalar values are deliberately not compared.
Identity/hash values still need a reviewer to compare against recorded values.

Module mocks, Python patches and HTTP/process doubles count. Partial JS module
mocks that keep the original module in play remain exempt. Declared provider
modules have a separate category; a test can justify growth by naming a checked
recording with `fluxpoint-recording: ID`. Both all provider doubles and the
unrecorded subset are measured, so existing recorded doubles cannot buy room
for a new unrecorded double. Other first/third-party counts still only decrease.

Dormant without supported test files and without a recordings manifest.
"""

import argparse
import ast
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

BASELINE = ".fluxpoint-proof-baseline.json"
MANIFEST = ".fluxpoint-recordings.json"
SECTION = "seams"
CATEGORIES = ("first_party", "third_party", "provider", "provider_unrecorded")
TEST_SUFFIXES = tuple(".%s.%s" % (kind, ext)
                      for kind in ("test", "spec")
                      for ext in ("ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts")) + ("_test.py",)
MOCK = re.compile(r"\b(?:vi|jest)\s*\.\s*(?:do)?[Mm]ock\s*\(\s*(['\"`])([^'\"`]+)\1")
HTTP = re.compile(r"\b(?:vi|jest)\s*\.\s*stubGlobal\s*\(\s*(['\"])fetch\1"
                  r"|\b(?:vi|jest)\s*\.\s*spyOn\s*\(\s*(?:globalThis|window|global)\s*,\s*(['\"])fetch\2"
                  r"|\b(?:globalThis|window|global)\s*\.\s*fetch\s*=(?!=)")
PARTIAL = re.compile(r"\bimportActual\b|\brequireActual\b|\bimportOriginal\b")
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)


def strip_comments(text):
    text = BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    return LINE_COMMENT.sub("", text)


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"non-JSON numeric constant {value}")

    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh, object_pairs_hook=unique, parse_constant=invalid)
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f"{path}: not readable JSON ({exc})") from exc


def repo_path(root, value, label):
    """Manifest paths are portable repo-relative files, including after symlinks."""
    if (not isinstance(value, str) or not value or "\\" in value
            or value.startswith("/") or re.match(r"^[A-Za-z]:", value)
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError(f"{label}: expected a repo-relative file path without traversal")
    root_path = Path(root).resolve()
    path = root_path / value
    try:
        path.resolve().relative_to(root_path)
    except ValueError:
        raise ValueError(f"{label}: path escapes the repository") from None
    if not path.is_file():
        raise ValueError(f"{label}: file does not exist: {value}")
    return path


def json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return {str: "string", list: "array", dict: "object"}[type(value)]


def child_path(path, key):
    return f"{path}.{key}" if re.fullmatch(r"[A-Za-z_$][\w$]*", key) else f"{path}[{json.dumps(key)}]"


def conform(double, recording):
    """Compare shapes exactly; never infer absent fields or discard array rows."""
    findings = []
    pending = [("$", double, recording)]
    while pending:
        path, actual, captured = pending.pop()
        got, want = json_type(actual), json_type(captured)
        if got != want:
            findings.append(f"{path}: JSON type mismatch: double {got}, recording {want}")
        elif isinstance(actual, dict):
            for key in sorted(actual.keys() - captured.keys()):
                findings.append(f"{child_path(path, key)}: key only in double")
            for key in sorted(captured.keys() - actual.keys()):
                findings.append(f"{child_path(path, key)}: key only in recording")
            pending.extend((child_path(path, key), actual[key], captured[key])
                           for key in sorted(actual.keys() & captured.keys(), reverse=True))
        elif isinstance(actual, list):
            if len(actual) != len(captured):
                findings.append(f"{path}: array length mismatch: double {len(actual)}, recording {len(captured)}")
            pending.extend((f"{path}[{i}]", actual[i], captured[i])
                           for i in range(min(len(actual), len(captured)) - 1, -1, -1))
    return findings


def load_recordings(root):
    path = os.path.join(root, MANIFEST)
    if not os.path.lexists(path):
        return None
    # Validate the manifest's own path as well as its content paths.
    repo_path(root, MANIFEST, MANIFEST)
    doc = read_json(path)
    if not isinstance(doc, dict) or set(doc) - {"recordings", "providerModules"}:
        raise ValueError(f"{MANIFEST}: expected an object with recordings and optional providerModules")
    rows = doc.get("recordings")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{MANIFEST}: recordings must be a nonempty list")
    modules = doc.get("providerModules", [])
    if (not isinstance(modules, list) or any(not isinstance(m, str) or not m.strip() for m in modules)
            or len(set(modules)) != len(modules)):
        raise ValueError(f"{MANIFEST}: providerModules must be a list of unique nonempty targets")
    doc["providerModules"] = modules
    ids = set()
    pairs = set()
    for index, row in enumerate(rows):
        label = f"{MANIFEST}.recordings[{index}]"
        fields = {"id", "provider", "endpoint", "capturedAt", "body", "doubles"}
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError(f"{label}: expected exactly {', '.join(sorted(fields))}")
        for field in ("id", "provider", "endpoint", "capturedAt"):
            if not isinstance(row[field], str) or not row[field].strip():
                raise ValueError(f"{label}.{field}: must be a nonempty string")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", row["id"]) or row["id"] in ids:
            raise ValueError(f"{label}.id: must be a unique identifier (letters, digits, _, ., -)")
        ids.add(row["id"])
        try:
            stamp = datetime.fromisoformat(row["capturedAt"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or "T" not in row["capturedAt"]:
                raise ValueError("timezone missing")
        except ValueError:
            raise ValueError(f"{label}.capturedAt: must be an ISO-8601 timestamp with timezone") from None
        capture = repo_path(root, row["body"], label + ".body")
        row["capture"] = read_json(capture)
        doubles = row["doubles"]
        if not isinstance(doubles, list) or not doubles:
            raise ValueError(f"{label}.doubles: must be a nonempty list of JSON fixture paths")
        row["fixtures"] = []
        for double in doubles:
            fixture = repo_path(root, double, label + ".doubles")
            if fixture.samefile(capture):
                raise ValueError(f"{label}: a double must be a different file from its recording")
            if (row["id"], double) in pairs:
                raise ValueError(f"{label}: duplicate double {double!r}")
            pairs.add((row["id"], double))
            row["fixtures"].append((double, read_json(fixture)))
    return doc


def check_recordings(manifest):
    findings = []
    if manifest:
        for row in manifest["recordings"]:
            for path, value in row["fixtures"]:
                findings.extend(f"recording {row['id']} ({row['provider']} {row['endpoint']}, "
                                f"captured {row['capturedAt']}), {path}: {why}"
                                for why in conform(value, row["capture"]))
    return findings


def tracked_files(root):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                             capture_output=True, text=True, check=True).stdout
        out += subprocess.run(["git", "-C", root, "ls-files", "-z", "--others", "--exclude-standard"],
                              capture_output=True, text=True, check=True).stdout
        names = [n for n in out.split("\0") if n]
    except (OSError, subprocess.SubprocessError):
        names = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in
                           ("node_modules", ".git", "dist", "build", "out", "coverage", ".venv", "__pycache__")]
            names.extend(os.path.relpath(os.path.join(dirpath, f), root) for f in filenames)
    return sorted(set(n for n in names if n.endswith(TEST_SUFFIXES)
                      or (n.endswith(".py") and os.path.basename(n).startswith("test_"))))


def is_first_party(spec, root=None):
    if spec.startswith((".", "/", "@/", "~/", "#")):
        return True
    # Python patch targets name attributes after the local module. Resolve
    # progressively shorter prefixes under the two usual import roots.
    if root and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", spec):
        parts = spec.split(".")
        for length in range(len(parts), 0, -1):
            for base in (Path(root), Path(root) / "src"):
                module = base.joinpath(*parts[:length])
                if module.with_suffix(".py").is_file() or (module / "__init__.py").is_file():
                    return True
    return False


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return prefix + "." + node.attr if prefix else node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return "<dynamic>"


def python_doubles(text, rel):
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as exc:
        raise ValueError(f"{rel}:{exc.lineno}: cannot scan Python test ({exc.msg})") from exc
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name] = name.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for name in node.names:
                aliases[name.asname or name.name] = node.module + "." + name.name

    def qualified(node):
        name = dotted(node)
        prefix, _, rest = name.partition(".")
        return aliases.get(prefix, prefix) + ("." + rest if rest else "")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = qualified(node.func)
        if (name.endswith(".patch.object") or name == "patch.object"
                or name.endswith(".monkeypatch.setattr") or name == "monkeypatch.setattr"):
            target = qualified(node.args[0]) if node.args else "<dynamic>"
            yield node.lineno, target
        elif name in ("patch", "mock.patch", "unittest.mock.patch", "mocker.patch"):
            yield node.lineno, dotted(node.args[0]) if node.args else "<dynamic>"
        elif name.startswith("respx.") and name.rsplit(".", 1)[1] in (
                "mock", "Router", "get", "post", "put", "patch", "delete", "head", "options", "route"):
            yield node.lineno, "httpx"
        elif name.startswith("responses.") and name.rsplit(".", 1)[1] in (
                "RequestsMock", "add", "get", "post", "put", "patch", "delete", "head", "options", "replace", "add_callback"):
            yield node.lineno, "requests"


def scan(root, files, manifest=None):
    # Kept compatible with ratchet.py's scan(root, files) measurement API.
    if manifest is None:
        manifest = load_recordings(root)
    counts = dict.fromkeys(CATEGORIES, 0)
    where = {key: [] for key in CATEGORIES}
    modules = manifest["providerModules"] if manifest else []
    for rel in files:
        path = repo_path(root, rel, "test file")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"cannot read test {rel}: {exc}") from exc
        backed = manifest and any(re.search(r"fluxpoint-recording:\s*" + re.escape(row["id"]) + r"(?![\w.-])", text)
                                  for row in manifest["recordings"])
        if rel.endswith(".py"):
            doubles = python_doubles(text, rel)
        else:
            cleaned = strip_comments(text)
            matches = list(MOCK.finditer(cleaned))
            doubles = []
            for i, match in enumerate(matches):
                end = matches[i + 1].start() if i + 1 < len(matches) else len(cleaned)
                if not PARTIAL.search(cleaned, match.start(), end):
                    doubles.append((cleaned.count("\n", 0, match.start()) + 1, match.group(2)))
            doubles.extend((cleaned.count("\n", 0, m.start()) + 1, "fetch") for m in HTTP.finditer(cleaned))
        for line, target in doubles:
            provider = any(target == m or target.startswith(m + ".") or target.startswith(m + "/") for m in modules)
            key = "provider" if provider else "first_party" if is_first_party(target, root if rel.endswith(".py") else None) else "third_party"
            entry = f"{rel}:{line}  {target}"
            counts[key] += 1
            where[key].append(entry)
            if provider and not backed:
                counts["provider_unrecorded"] += 1
                where["provider_unrecorded"].append(entry)
    return counts, where


def report_problems(problems):
    for problem in problems:
        print(f"seam-guard: FAIL — {problem}", file=sys.stderr)
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    modes = ap.add_mutually_exclusive_group(required=True)
    modes.add_argument("--scan", action="store_true")
    modes.add_argument("--baseline", action="store_true")
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--tighten", action="store_true")
    modes.add_argument("--conform", nargs=2, metavar=("DOUBLE", "RECORDING"))
    args = ap.parse_args()
    if args.tighten:
        from ratchet import cli
        return cli("seam", args.root)
    root = os.path.abspath(args.root)
    bpath = os.path.join(root, BASELINE)
    try:
        if args.conform:
            paths = [repo_path(root, value, "--conform") for value in args.conform]
            findings = conform(*(read_json(path) for path in paths))
            if findings:
                return report_problems(findings)
            print("seam-guard: conforms — JSON keys, types and array structure match")
            return 0
        manifest = load_recordings(root)
        findings = check_recordings(manifest)
        if findings:
            return report_problems(findings)
        files = tracked_files(root)
        if not files and manifest is None:
            print("seam-guard: no JS/TS or Python test files; dormant — nothing "
                  + ("recorded" if args.baseline else "checked" if args.check else "to scan"))
            return 0
        counts, where = scan(root, files, manifest)
    except ValueError as exc:
        return report_problems([str(exc)])
    if manifest:
        pairs = sum(len(row["fixtures"]) for row in manifest["recordings"])
        print(f"seam-guard: {pairs} double fixture(s) conform to {len(manifest['recordings'])} recording(s)")
    if args.scan:
        for key in CATEGORIES:
            print(f"seam-guard: {key} = {counts[key]}")
            for line in where[key]:
                print("    " + line)
        return 0

    doc = {}
    if os.path.exists(bpath):
        try:
            with open(bpath, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            # The file is shared with proof-guard and spec-guard. Reading a
            # corrupt one as absent would silently disarm this ratchet on
            # --check and destroy the siblings' sections on --baseline — a
            # baseline that parses to nothing while looking configured is the
            # failure mode this plugin exists to refuse.
            print("seam-guard: %s is not readable JSON (%s)" % (bpath, e),
                  file=sys.stderr)
            return 1
        if not isinstance(doc, dict):
            print("seam-guard: %s must be a JSON object" % bpath, file=sys.stderr)
            return 1

    if args.baseline:
        doc[SECTION] = counts
        with open(bpath, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("seam-guard: baseline armed at " + " ".join(f"{key}={counts[key]}" for key in CATEGORIES))
        return 0

    base = doc.get(SECTION)
    if base is None:
        print("seam-guard: no baseline recorded; run --baseline to arm", file=sys.stderr)
        return 0
    if (not isinstance(base, dict) or any(type(base.get(k, 0)) is not int
                                        or base.get(k, 0) < 0 for k in counts)):
        return report_problems([f"the '{SECTION}' section of {BASELINE} is not usable; re-arm with --baseline"])
    # A recorded provider double may grow; the unrecorded subset may never
    # grow, even when removing a recorded one keeps the total unchanged.
    risen = [(k, base.get(k, 0), counts[k]) for k in counts
             if k != "provider" and counts[k] > base.get(k, 0)]
    if not risen:
        print("seam-guard: OK — " + " ".join(f"{key}={counts[key]}" for key in CATEGORIES))
        return 0
    for key, was, now in risen:
        print(f"seam-guard: FAIL — {key} mocks rose {was} -> {now}", file=sys.stderr)
        for line in where[key]:
            print("    " + line, file=sys.stderr)
    print("Drive owned modules for real. Hold provider/HTTP fixtures to production "
          "captures in .fluxpoint-recordings.json and name the recording in the "
          "test with 'fluxpoint-recording: ID'. Re-arm other justified count "
          "increases with --baseline and explain the change in the commit.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
