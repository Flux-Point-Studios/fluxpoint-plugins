#!/usr/bin/env python3
"""Assemble a graph launch/resume without copying repair notes into a tool call.

  resume.py --graph WORK.md --args initial.json
  resume.py --graph WORK.md --run wf_previous --set repairRelay=@repair.txt

stdout is a Workflow object. The compiler's deterministic rerun plan and
diagnostics go to stderr. This helper never dispatches an agent.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile

from embedded_args import canonical, recover
from evidence_pins import prepare_pinned_inputs, validate_pinned_inputs

HERE = Path(__file__).resolve().parent


def _module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("RESUME-JSON-DUPLICATE: " + key)
        out[key] = value
    return out


def _json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=_pairs,
                         parse_constant=lambda token: (_ for _ in ()).throw(ValueError("invalid JSON constant " + token)))


def _run(command, root):
    proc = subprocess.run(command, cwd=root, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8")
    if proc.returncode:
        raise ValueError("RESUME-COMMAND-FAILED: " + " ".join(str(x) for x in command) +
                         "\n" + (proc.stderr or proc.stdout).strip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    return proc.stdout


def fresh_base(root):
    def git(*args):
        return _run(["git", *args], root)
    return {"sha": git("rev-parse", "HEAD").strip(),
            "branch": git("branch", "--show-current").strip(),
            "porcelain": git("status", "--porcelain"),
            "root": git("rev-parse", "--show-toplevel").strip()}


def _safe_run(value):
    if (not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", value or "")
            or value.endswith(".") or value.split(".")[0].upper() in (
                {"CON", "PRN", "AUX", "NUL"} |
                {"COM" + str(i) for i in range(1, 10)} |
                {"LPT" + str(i) for i in range(1, 10)})):
        raise ValueError("RESUME-RUN-ID: unsafe run id")
    return value


def _artifact(root, run_id):
    base = Path(root) / ".claude/fluxpoint/runs"
    path = base / (_safe_run(run_id) + ".json")
    if path.resolve().parent != base.resolve():
        raise ValueError("RESUME-ARTIFACT: run artifact escapes canonical store")
    raw = path.read_bytes()
    artifact = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda token: (_ for _ in ()).throw(ValueError("invalid JSON constant " + token)))
    if (not isinstance(artifact, dict) or artifact.get("runId") != run_id
            or not isinstance(artifact.get("summary"), dict)):
        raise ValueError("RESUME-ARTIFACT: recorded run id/summary missing")
    return artifact["summary"], hashlib.sha256(raw).hexdigest()


def _updates(args):
    updates = {}
    for entries, structured in ((args.set, False), (args.set_json, True)):
        for entry in entries:
            key, separator, source = entry.partition("=")
            if not separator or not key or key.startswith("_"):
                raise ValueError("RESUME-SET: use a non-underscore key=@file")
            if key in updates:
                raise ValueError("RESUME-SET-DUPLICATE: " + key)
            if not source.startswith("@") or len(source) == 1:
                raise ValueError("RESUME-SET: " + key + " must name @file (text is carried exactly)")
            if structured:
                updates[key] = _json(source[1:])
            else:
                # newline='' preserves CRLF and trailing newlines in notes.
                with open(source[1:], encoding="utf-8", newline="") as handle:
                    updates[key] = handle.read()
    return updates


def assemble(args):
    root = os.path.realpath(args.root)
    graph = os.path.abspath(os.path.join(root, args.graph))
    compiler = _module("compile-graph")
    with open(graph, encoding="utf-8") as handle:
        ir = compiler.extract_ir(handle.read())
    resuming = args.run is not None
    prior, source_sha = _artifact(root, args.run) if resuming else (None, None)
    allowances = list(args.unpinned_ok)
    if resuming:
        carried = recover(root, prior)
        previous_allowances = prior.get("evidenceUnpinned") or []
        if not isinstance(previous_allowances, list):
            raise ValueError("EVIDENCE-UNPINNED-ALLOWANCE: recorded allowances must be a list")
        allowances = list(dict.fromkeys(previous_allowances + allowances))
        validate_pinned_inputs(root, carried, allowances)
    else:
        carried = _json(args.args)
        if not isinstance(carried, dict) or any(key.startswith("_") for key in carried):
            raise ValueError("RESUME-ARGS-INVALID: initial args must contain only non-underscore keys")
        # Freeze effective defaults in source too. A summary field absent
        # from ARGS otherwise has no immutable binding and could be injected
        # into a later resume without an explicit --set.
        defaults = ir.get("argDefaults") or {}
        if not isinstance(defaults, dict):
            raise ValueError("RESUME-ARGS-INVALID: IR argDefaults must be an object")
        carried = dict({key: value for key, value in defaults.items()
                        if not key.startswith("_")}, **carried)
    before = canonical(carried)
    updates = _updates(args)
    assembled = dict(carried, **updates)
    assembled = prepare_pinned_inputs(root, assembled, allowances)
    if resuming:
        original = json.loads(before)
        for key in set(original) | set(assembled):
            if key not in updates and (key not in original or key not in assembled or
                    canonical(original[key]) != canonical(assembled[key])):
                raise ValueError("RESUME-CARRIED-DRIFT: " + key + " changed without --set")

    # Read the base before writing local state. Never make a fresh launch stamp
    # for a resume: proofs and repair patch identity stay bound to the old one.
    launch_args = {"_base": fresh_base(root), "_replayLaunchNonce": secrets.token_hex(32)}
    requirements = compiler.launch_requirements(ir, embedded_args=assembled)["args"]
    needs_launch = any(item["name"] == "_launch" and item["required"] for item in requirements)
    if resuming:
        launch = prior.get("launch")
        if needs_launch and not isinstance(launch, dict):
            raise ValueError("RESUME-LAUNCH-MISSING: recorded summary.launch required; never mint a replacement on resume")
        if launch is not None:
            if not isinstance(launch, dict):
                raise ValueError("RESUME-LAUNCH-INVALID: recorded launch stamp must be an object")
            launch_args["_launch"] = launch
        launch_args["_resumeFromRunId"] = args.run
        launch_args["_resumeArtifactSha256"] = source_sha
        prior_results = prior.get("results") or {}
        if not isinstance(prior_results, dict):
            raise ValueError("RESUME-RESULTS-INVALID: recorded results must be an object")
        rejudge = assembled.get("rejudge")
        if isinstance(rejudge, dict) and rejudge:
            launch_args["_priorResults"] = {key: prior_results[key] for key in rejudge if key in prior_results}
        memo = _module("replay_memo").build_memo(root, args.run, launch,
                      compiler.compiler_version(), projects=args.projects,
                      artifact_sha256=source_sha)
        if not isinstance(memo, dict) or set(memo) - {"_memo", "_prefix"}:
            raise ValueError("RESUME-MEMO: loader returned unsupported launch fields")
        launch_args.update(memo)
    else:
        launch_args["_launch"] = _module("attest").stamp(root)
    for name, script in (("_ledger", "ledger"), ("_releases", "release")):
        if any(item["name"] == name for item in requirements):
            launch_args[name] = json.loads(_run([sys.executable, str(HERE / (script + ".py")),
                    "--root", root, "--load", "--campaign", ir["campaign"]], root))
    if allowances:
        launch_args["_evidenceUnpinned"] = sorted(set(allowances))
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", ir.get("name") or Path(graph).stem).strip(".") or "graph"
    output = os.path.abspath(os.path.join(root, args.out or ".claude/workflows/" + name + ".graph.js"))
    staging_dir = Path(root) / ".claude/fluxpoint/launch-args"
    staging_dir.mkdir(parents=True, exist_ok=True)
    descriptor, staged = tempfile.mkstemp(prefix="args-", suffix=".json", dir=str(staging_dir))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical(assembled))
        if resuming:
            command = [sys.executable, str(HERE / "compile-graph.py"), "--plan", graph,
                       "--resume", args.run, "--resume-artifact-sha256", source_sha,
                       "--args", staged, "--gates-root", root]
            if args.projects:
                command += ["--projects", args.projects]
            plan = _run(command, root)
            print(plan.rstrip(), file=sys.stderr)
        diagnostics = _run([sys.executable, str(HERE / "compile-graph.py"), graph,
                            "--args", staged, "--gates-root", root, "--out", output], root)
        if diagnostics.strip():
            print(diagnostics.rstrip(), file=sys.stderr)
        exact = _run([sys.executable, str(HERE / "compile-graph.py"), graph,
                      "--args", staged, "--gates-root", root, "--check",
                      "--check-script", output], root)
        if exact.strip():
            print(exact.rstrip(), file=sys.stderr)
    finally:
        os.unlink(staged)
    workflow = {"scriptPath": output, "args": launch_args}
    if resuming:
        workflow["resumeFromRunId"] = args.run
    return workflow


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".")
    parser.add_argument("--graph", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run", help="canonical recorded run id to resume")
    source.add_argument("--args", help="initial non-underscore args JSON, before the first launch")
    parser.add_argument("--set", action="append", default=[], help="key=@file; UTF-8 text is preserved exactly")
    parser.add_argument("--set-json", action="append", default=[], help="key=@file; structured JSON such as rejudge")
    parser.add_argument("--unpinned-ok", action="append", default=[], help="explicit absolute evidence path exception")
    parser.add_argument("--out", help="compiled output (default .claude/workflows/<name>.graph.js)")
    parser.add_argument("--projects", help="runtime projects directory for journal/transcript verification")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(assemble(args), ensure_ascii=False))
    except (OSError, ValueError, TypeError, KeyError, ImportError) as exc:
        print("resume: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
