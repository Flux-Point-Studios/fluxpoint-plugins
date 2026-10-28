#!/usr/bin/env python3
"""Guard ratchet: a safety guard must be provable, and proven guards may only go up.

The companion to proof-guard.py, which counts the ways a proof can be waved through.
This counts the opposite thing: guards that stop money moving wrongly, and whether
each one is actually held down by a test.

WHY. Three guards failed the same way inside one week, on the same product:

  - a validator's fee bound had never been evaluated by a ledger, because every
    on-chain proof took the client-escape branch and short-circuited before reaching it
  - a tool deciding what a client is asked to WITNESS had a suite that went red the day
    the thing it guards changed shape, and nothing ran it for a fortnight
  - a co-signer's deployed-key pin had NO test in any commit, so a refactor deleted it
    along with the module it sat in and 95 tests still passed

None of these was a bug in the guard. Each was a guard nobody could see was gone. A
guard whose only proof lives in the file being refactored dies with it in silence, and
a green suite is what you get either way.

WHAT THIS DOES THAT A CHECKLIST DOES NOT. Registering a guard costs nothing and rots
quietly, so --verify does the thing by hand nobody keeps doing: it DISABLES each guard,
runs the one test that names it, and requires that test to FAIL. A guard whose proof
still passes with the guard disabled is decoration, and this says so. That is the whole
mechanism; everything else is bookkeeping around it.

  guard-guard.py --scan       list registered guards and their proofs
  guard-guard.py --check      fast: every guard and proof still present, count >= floor
  guard-guard.py --verify     slow: mutate each guard, require its proof to redden
  guard-guard.py --baseline   record the current count as the floor

--check belongs in every CI run; it is a grep and costs nothing. --verify runs the test
suite once per guard, so it belongs wherever the full suite already runs.

MANIFEST (.fluxpoint-guards.json), committed beside the code:

  {"guards": [
    {"id": "cosigner-deployed-key-pin",
     "protects": "serving an unpinned key produces addresses no client can rebuild",
     "guard":  {"file": "server.py", "contains": "if self.vkh != pinned_vkh:"},
     "proof":  {"file": "test_signer_pin.py",
                "test": "test_a_key_that_does_not_match_its_pin_is_fatal"},
     "mutation": {"find": "if self.vkh != pinned_vkh:", "replace": "if False:"},
     "expect": "does not match its pin",
     "run": "{py} -m pytest -q {file}::{test}"}
  ]}

`protects` is prose and is never parsed. It is there because the next person to read a
failure needs to know what the guard was FOR, and a rule nobody understands gets deleted.
"""
import argparse
import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

from process_tree import shell_argv

MANIFEST = ".fluxpoint-guards.json"
BASELINE = ".fluxpoint-guards-baseline.json"
# Resolved by RUNNING each candidate, never by name: Windows ships a `python3`
# App Execution Alias that satisfies `command -v` on a machine with no python3
# and exits 49 for every argument. A hardcoded `python3` there makes every
# proof fail command-not-found, which -- before the intact run below existed --
# read as "every guard is real".
def _python():
    for cand in ("python3", "python"):
        try:
            if subprocess.run([cand, "-c", "import sys"],
                              capture_output=True).returncode == 0:
                return cand
        except OSError:
            continue
    return sys.executable or "python3"


DEFAULT_RUN = "{py} -m pytest -q {file}::{test}"

# A guard is disabled on disk for the length of one proof run. `finally` covers
# an exception and Ctrl-C; it does NOT cover SIGTERM, which is what CI
# cancellation sends -- and a killed run left `if False:` in place of a
# money-moving guard with no breadcrumb, since the backup was a random name in
# the system temp dir. These two make the window survivable: the pending
# restore is replayed from a signal handler and at interpreter exit, and while
# it is open a sentinel sits in the repo where a human or a later --check will
# see it.
SENTINEL = ".fluxpoint-guards-restoring"
_PENDING = {}


def _restore_pending():
    """Put every disabled guard back, and READ IT BACK.

    A restore that did not restore is worse than no restore: the mutant stays
    in place with the sentinel gone, and every later proof runs against a
    guard that is off. So the bytes are compared after the copy; a mismatch
    keeps the backup and the sentinel on disk and says so on stderr.
    """
    problems = []
    for target, backup in list(_PENDING.items()):
        try:
            shutil.copyfile(backup, target)
            with open(backup, "rb") as fh:
                want = fh.read()
            with open(target, "rb") as fh:
                got = fh.read()
            if got != want:
                problems.append(f"{target} differs from its backup {backup} after restore")
                continue
            os.unlink(backup)
        except OSError as e:
            problems.append(f"{target}: restore failed ({e}); backup kept at {backup}")
            continue
        _PENDING.pop(target, None)
    if not _PENDING:
        try:
            os.unlink(_PENDING_SENTINEL[0])
        except (OSError, IndexError):
            pass
        _PENDING_SENTINEL.clear()
    for p in problems:
        print(f"guard-guard: RESTORE DID NOT RESTORE — {p}; the sentinel stays on "
              f"disk", file=sys.stderr)


_PENDING_SENTINEL = []
atexit.register(_restore_pending)


def _on_signal(signum, _frame):
    _restore_pending()
    raise SystemExit(128 + signum)


for _sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)):
    if _sig is not None:
        try:
            signal.signal(_sig, _on_signal)
        except (OSError, ValueError):
            pass


def _die(msg):
    print(f"guard-guard: {msg}", file=sys.stderr)
    raise SystemExit(2)


def load_manifest(root):
    path = os.path.join(root, MANIFEST)
    if not os.path.exists(path):
        _die(f"no {MANIFEST} at {root}. Register the guards this repo relies on, or "
             f"state in the PR that it has none worth naming.")
    try:
        data = json.load(open(path))
    except json.JSONDecodeError as exc:
        _die(f"{MANIFEST} is not valid JSON: {exc}")
    guards = data.get("guards")
    if not isinstance(guards, list):
        _die(f"{MANIFEST} must contain a 'guards' list")
    seen = set()
    real_root = os.path.normcase(os.path.realpath(root))
    for g in guards:
        for field in ("id", "protects", "guard", "proof", "mutation", "expect"):
            if field not in g:
                _die(f"guard {g.get('id', '<unnamed>')!r} is missing {field!r}")
        if g["id"] in seen:
            _die(f"duplicate guard id {g['id']!r}")
        seen.add(g["id"])
        # The verifier WRITES the mutation to the guard path and reads the
        # proof path, and os.path.join hands an absolute or ../-escaping
        # entry the whole filesystem: a manifest arriving by PR is reviewed
        # as data, and a reviewer scanning a "file" field is not primed to
        # read it as "this path gets overwritten". Refuse anything that
        # resolves outside the repo, here where the other shape checks live.
        for kind in ("guard", "proof"):
            rel = str((g.get(kind) or {}).get("file", ""))
            real = _resolve(root, rel)
            try:
                inside = os.path.commonpath([real, real_root]) == real_root
            except ValueError:
                inside = False
            if not inside:
                _die(f"guard {g['id']!r} names a file outside the repo: {rel}")
    return guards


def _resolve(root, rel):
    return os.path.normcase(os.path.realpath(os.path.join(root, rel)))


def _read(root, rel):
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def check(root, guards):
    """Structural: the guard is still in the code and the proof still names it."""
    problems = []
    for g in guards:
        gf, pf = g["guard"]["file"], g["proof"]["file"]
        gsrc, psrc = _read(root, gf), _read(root, pf)

        if gsrc is None:
            problems.append(f"{g['id']}: guard file {gf} is gone")
        elif g["guard"]["contains"] not in gsrc:
            problems.append(
                f"{g['id']}: {gf} no longer contains the guard\n"
                f"      looked for: {g['guard']['contains']}\n"
                f"      it protects: {g['protects']}")

        if psrc is None:
            problems.append(f"{g['id']}: proof file {pf} is gone — the guard is now unwatched")
        elif not re.search(rf"\b{re.escape(g['proof']['test'])}\b", psrc):
            problems.append(
                f"{g['id']}: {pf} no longer defines {g['proof']['test']}")

        # Resolved paths, not strings: "server.py", "./server.py" and
        # "tests/../server.py" name one file, and on Windows so does "Server.py".
        # Comparing the manifest spellings lets the exact arrangement this
        # refuses walk straight past it.
        if _resolve(root, gf) == _resolve(root, pf):
            problems.append(
                f"{g['id']}: the proof lives in the same file as the guard ({gf}). "
                f"A refactor that deletes one deletes the other — that is the failure "
                f"this ratchet exists for. Move the proof to its own file.")
    return problems


def verify_one(root, g, quiet=False):
    """Prove the guard bites: the proof must pass intact, then fail disabled.

    Two runs, because one cannot tell a guard that bites from a proof that
    never ran. Always restores, including on SIGTERM.
    """
    rel = g["guard"]["file"]
    path = os.path.join(root, rel)
    original = _read(root, rel)
    if original is None:
        return False, f"guard file {rel} is gone"

    find, replace = g["mutation"]["find"], g["mutation"]["replace"]
    # Exactly once. A `replace(..., 1)` on the first of several hits mutates
    # a site the proof may never reach and reads as "the guard is real" for
    # a guard that was never disabled; an anchor that misses is a no-op that
    # reads the same way. Both are the anchor trap named in issue #62.
    n = original.count(find)
    if n != 1:
        return False, (f"mutation anchor matched {n} time(s) in {rel}; it must match "
                       f"exactly once, or the proof is measured against the wrong "
                       f"site (or none): {find!r}")
    mutated = original.replace(find, replace, 1)
    if mutated == original:
        return False, f"mutation changed nothing in {rel}"

    cmd = g.get("run", DEFAULT_RUN).format(
        py=_python(), file=g["proof"]["file"], test=g["proof"]["test"])
    expect = g["expect"]

    # The intact run. Without it, ANY non-zero exit read as "the guard is
    # real", so a proof that could not run at all -- a broken import, a
    # collection error, a missing interpreter, a suite red for a fortnight --
    # verified green. That is this tool's own motivating failure, passing
    # itself. A proof must first be shown to PASS on a healthy tree; only then
    # does its failure under mutation testify to anything.
    try:
        intact = subprocess.run(shell_argv(cmd), cwd=root, capture_output=True,
                                text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return False, f"the proof did not finish in 900s with the guard intact: {cmd}"
    if intact.returncode != 0:
        tail = (intact.stdout or intact.stderr).strip().splitlines()[-3:]
        return False, ("the proof does not pass with the guard INTACT "
                       f"(rc={intact.returncode}) — a proof that cannot pass cannot "
                       "testify that its guard bites. Fix the proof, or the run "
                       "command, before trusting this ratchet.\n"
                       f"      command: {cmd}\n"
                       + "\n".join(f"      {line}" for line in tail))
    if not quiet:
        print("    intact   -> proof passed")

    backup = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
    backup.write(original)
    backup.close()
    sentinel = os.path.join(root, SENTINEL)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(mutated)
        # Registered only once the write is CLOSED, not before it: a restore that
        # fires from the signal handler while this handle is still open is undone
        # by the flush on close, which puts the mutation back and leaves the guard
        # disabled. Registering after is what makes the window survivable.
        _PENDING[path] = backup.name
        with open(sentinel, "w", encoding="utf-8") as fh:
            fh.write(f"{g['id']}\n{rel}\n")
        _PENDING_SENTINEL[:] = [sentinel]
        proc = subprocess.run(shell_argv(cmd), cwd=root, capture_output=True, text=True, timeout=900)
        out = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        tail = out.strip().splitlines()[-3:]
        if proc.returncode == 0:
            return False, ("the proof PASSED with the guard disabled — it proves nothing.\n"
                           f"      command: {cmd}\n"
                           + "\n".join(f"      {line}" for line in tail))
        # A non-zero exit is not evidence by itself. The intact run above closes
        # "the proof cannot run"; this closes its twin — "the proof failed for a
        # reason that has nothing to do with the guard". A mutation that breaks
        # the module reddens the proof on an import error, and a killed proof
        # reddens without evaluating anything. Both would otherwise be reported
        # as "the guard is real", which is the exact decoration this tool exists
        # to name.
        if proc.returncode < 0 or proc.returncode >= 128:
            return False, (f"the proof was KILLED with the guard disabled (rc={proc.returncode}) "
                           "— a proof that did not finish evaluated nothing, and cannot "
                           "testify that its guard bites. A CI cancellation, a job timeout "
                           "and an OOM kill all land here.\n"
                           f"      command: {cmd}")
        if proc.returncode == 127:
            return False, ("the proof command was NOT FOUND with the guard disabled "
                           f"(rc=127) — nothing ran.\n      command: {cmd}")
        if expect not in out:
            return False, ("the proof failed with the guard disabled, but not for the "
                           f"declared reason: {expect!r} is absent from its output. The "
                           "mutation may have broken the file rather than the behaviour — "
                           "an import or compile error reddens the proof without the guard "
                           "being exercised at all.\n"
                           f"      command: {cmd}\n"
                           + "\n".join(f"      {line}" for line in tail))
        if not quiet:
            print(f"    disabled -> proof failed (rc={proc.returncode}) on {expect!r} — the guard is real")
        return True, None
    except subprocess.TimeoutExpired:
        return False, f"the proof did not finish in 900s: {cmd}"
    finally:
        # Restore unconditionally: an interrupted run must never leave a guard
        # disabled. Routed through the same replay the signal handler uses, so a
        # SIGTERM that already restored makes this a no-op instead of a
        # FileNotFoundError on the deleted backup — which would mask the verdict.
        # setdefault covers the write above failing before anything registered.
        _PENDING.setdefault(path, backup.name)
        _restore_pending()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--tighten", action="store_true")
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--baseline", action="store_true")
    args = ap.parse_args()
    if args.tighten:
        from ratchet import cli
        return cli("guard", args.root)


    root = os.path.abspath(args.root)
    guards = load_manifest(root)

    if args.scan:
        print(f"{len(guards)} registered guard(s) in {root}:\n")
        for g in guards:
            print(f"  {g['id']}")
            print(f"      protects: {g['protects']}")
            print(f"      guard:    {g['guard']['file']}")
            print(f"      proof:    {g['proof']['file']}::{g['proof']['test']}\n")
        return 0

    if args.baseline:
        with open(os.path.join(root, BASELINE), "w", encoding="utf-8") as fh:
            json.dump({"count": len(guards), "ids": sorted(g["id"] for g in guards)}, fh, indent=2)
            fh.write("\n")
        print(f"recorded {len(guards)} guard(s) to {BASELINE}")
        return 0

    problems = check(root, guards)

    # The floor. Guards may be added freely; removing one has to be a diff someone signs.
    base_path = os.path.join(root, BASELINE)
    if os.path.exists(base_path):
        base = json.load(open(base_path))
        missing = sorted(set(base.get("ids", [])) - {g["id"] for g in guards})
        if missing:
            problems.append(
                "guards were removed from the manifest: " + ", ".join(missing) +
                "\n      Adding a guard is free. Removing one means something that used to be "
                "\n      proven no longer is — re-record with --baseline and say why.")

    if args.verify and not problems:
        print(f"verifying {len(guards)} guard(s) by disabling each one:\n")
        for g in guards:
            print(f"  {g['id']}")
            ok, why = verify_one(root, g)
            if not ok:
                problems.append(f"{g['id']}: {why}")

    if problems:
        print("\nguard-guard: FAILED\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("", file=sys.stderr)
        return 1

    print(f"\nguard-guard: {len(guards)} guard(s) OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
