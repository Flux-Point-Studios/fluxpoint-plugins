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
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile

from process_tree import identity, shell_argv

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

# Signals and atexit provide immediate restoration when the interpreter can run.
# Native Windows termination and os._exit run neither, so a durable journal and
# exact-byte backup are published before the atomic mutation. The next invocation
# recovers only a dead owner's known mutation, preserving unknown edits.
SENTINEL = ".fluxpoint-guards-restoring"
_PENDING = {}


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path, data):
    fd, temp = tempfile.mkstemp(prefix=".fluxpoint-guard-write-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, os.stat(path).st_mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _restore_pending():
    problems = []
    for target, backup in list(_PENDING.items()):
        try:
            with open(backup, "rb") as stream:
                want = stream.read()
            _atomic_write(target, want)
            with open(target, "rb") as stream:
                if stream.read() != want:
                    raise OSError("restored bytes differ from the backup")
            os.unlink(backup)
            _PENDING.pop(target, None)
        except OSError as exc:
            problems.append(f"{target}: restore failed ({exc}); backup kept at {backup}")
    if not _PENDING:
        try:
            os.unlink(_PENDING_SENTINEL[0])
        except (OSError, IndexError):
            pass
        _PENDING_SENTINEL.clear()
    for problem in problems:
        print(f"guard-guard: RESTORE DID NOT RESTORE — {problem}; the sentinel stays on disk", file=sys.stderr)
    return not problems


def recover(root):
    sentinel = os.path.join(root, SENTINEL)
    if not os.path.lexists(sentinel):
        return
    try:
        if os.path.islink(sentinel):
            raise ValueError("restore journal cannot be a symlink")
        with open(sentinel, encoding="utf-8") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("restore journal is too large")
        journal = json.loads(raw)
        if not isinstance(journal, dict) or journal.get("version") != 1:
            raise ValueError("unsupported restore journal; preserve its backup for manual recovery")
        target, backup = journal["target"], journal["backup"]
        real_root = _resolve(root, ".")
        if (not isinstance(target, str) or os.path.isabs(target)
                or ".." in target.replace("\\", "/").split("/")
                or os.path.commonpath([_resolve(root, target), real_root]) != real_root):
            raise ValueError("restore target is outside the repository")
        if (not isinstance(backup, str) or os.path.basename(backup) != backup
                or not backup.startswith(".fluxpoint-guard-backup-")):
            raise ValueError("invalid restore backup path")
        target, backup = os.path.join(root, target), os.path.join(root, backup)
        if os.path.islink(target) or os.path.islink(backup):
            raise ValueError("restore paths cannot be symlinks")
        if not isinstance(journal["owner"], str) or not journal["owner"]:
            raise ValueError("restore owner identity is missing")
        if type(journal["pid"]) is not int or journal["pid"] <= 0:
            raise ValueError("restore owner pid is invalid")
        if identity(journal["pid"]) == journal["owner"]:
            raise ValueError("the guard verifier is still running")
        with open(target, "rb") as stream:
            current = stream.read()
        if _digest(current) not in (journal["original"], journal["mutated"]):
            raise ValueError("guard changed since the interrupted verification; preserve edits and backup")
        # A kill between deleting the backup and deleting the journal is already
        # restored; the original digest still lets the next invocation finish.
        if not os.path.exists(backup) and _digest(current) == journal["original"]:
            os.unlink(sentinel)
            return
        with open(backup, "rb") as stream:
            original = stream.read()
        if _digest(original) != journal["original"]:
            raise ValueError("restore backup digest does not match the journal")
        _PENDING[target] = backup
        _PENDING_SENTINEL[:] = [sentinel]
        if not _restore_pending():
            raise ValueError("restore could not be confirmed")
        print("guard-guard: recovered an interrupted guard verification", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _die(f"cannot recover {SENTINEL}: {exc}")


def _journal_mutation(root, rel, original, mutated):
    owner = identity(os.getpid())
    if not owner:
        raise OSError("cannot identify the guard verifier process")
    fd, backup = tempfile.mkstemp(prefix=".fluxpoint-guard-backup-", dir=root)
    sentinel = os.path.join(root, SENTINEL)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        journal = dict(version=1, target=rel, backup=os.path.basename(backup), pid=os.getpid(),
                       owner=owner, original=_digest(original), mutated=_digest(mutated))
        # Exclusive creation also prevents two verifiers publishing mutations at once.
        with open(sentinel, "x", encoding="utf-8") as stream:
            json.dump(journal, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        os.unlink(backup)
        raise
    _PENDING[os.path.join(root, rel)] = backup
    _PENDING_SENTINEL[:] = [sentinel]
    return backup


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
    never ran. Restores on exit where possible; a later invocation recovers abrupt termination.
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

    with open(path, "rb") as stream:
        exact_original = stream.read()
    _journal_mutation(root, rel, exact_original, mutated.encode("utf-8"))
    try:
        _atomic_write(path, mutated.encode("utf-8"))
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
        if not _restore_pending():
            return False, "guard restore could not be confirmed; preserve the journal and backup"



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
    root = os.path.abspath(args.root)
    recover(root)
    if args.tighten:
        from ratchet import cli
        return cli("guard", args.root)


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
