#!/usr/bin/env python3
"""The secret-handling skill's shapes, executed against a canary.

A skill that says "never leak the key" is prose, and nothing checks prose.
The shapes it teaches are code, so they are extracted from SKILL.md and run
here against a file whose entire contents are a canary string. Then the only
question that matters gets asked: did the canary reach any output.

Every case carries its control — the naive shape the skill exists to
replace, asserted to LEAK. A leak detector that cannot detect a leak passes
on an empty document, which is how a doc test rots into decoration.

The traceback cases render with traceback.TracebackException(
capture_locals=True), which is the stdlib renderer pytest --showlocals is
built on. That is the vector the real incident turned on: nothing printed
the key, a frame merely held it, and rendering the frame printed it in
full.

The headline box is run in a shell, as a reader would run it, because the
shape with the most readers used to be the only shape nothing executed —
and it leaked the whole seed phrase the first time a key file failed to
parse. That needs `bash` on PATH, which the rest of this suite needs
anyway.
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
SKILL_DIR = os.path.join(PLUGIN, "skills", "secret-handling")
SKILL = os.path.join(SKILL_DIR, "SKILL.md")
HOOKS = os.path.join(PLUGIN, "hooks", "hooks.json")

# Two canaries: one for the key material, one for a second config secret, so
# a filter that happens to withhold the first by luck still fails.
CANARY = "canary-abandon-artwork-9f1c0d-never-in-output"
CANARY2 = "canary-projectid-77ab31-never-in-output"

# What the stub derives from the canary: the address a restore must match,
# so the box's --equals shape is exercised end to end and not just parsed.
LIVE = "addr1" + hashlib.sha256(CANARY.encode()).hexdigest()[:28]

# A skill nobody finishes is a skill nobody applies, so the length is
# measured in two parts. Prose is what a reader must actually read, and it
# is held near where it is: a shape can earn its lines, a paragraph has to
# argue for them. The total stays under graph-engineering, the plugin's
# longest skill at 384 lines, which covers a whole execution model where
# this one covers a single rule and the four shapes that carry it.
MAX_LINES = 370
MAX_PROSE = 235

# Every python fence in the skill must be one this suite actually runs. Two
# of four used to be tested, and the untested pair included the block that
# states the rule.
RUN_TAGS = {"python secret-barrier", "python secret-filter",
            "python secret-precondition"}

passed = failed = 0


def report(name, ok, detail):
    global passed, failed
    print(f"{'PASS' if ok else 'FAIL'}  {name:<56} -> {detail}")
    passed, failed = (passed + ok, failed + (not ok))


def source():
    if not os.path.exists(SKILL):
        return None
    with open(SKILL, encoding="utf-8") as fh:
        return fh.read()


def block(src, tag):
    """The fenced block whose info string is `tag`.

    Tagged fences are already how this plugin marks machine-read code in
    markdown (```json graph-ir in WORK.md). Reusing that means the snippet
    a reader copies and the code under test are the same bytes, so a shape
    cannot rot in the doc while the test keeps passing against its own copy.
    """
    m = re.search(r"^```" + re.escape(tag) + r"\n(.*?)^```", src, re.S | re.M)
    return m.group(1) if m else None


STUB_SRC = '''"""pycardano's surface, including the edge that makes this necessary.

PaymentSigningKey.__repr__ emits the raw private key as CBOR JSON, so this
stub's repr does too — a stub that reprs politely would let a leaky shape
pass here and leak in production.

FAIL_AT picks which real failure vector fires. 'load' puts the file's
contents inside an exception message; the other three raise after the key
object is bound to a frame local — 'derive' with an ordinary Exception,
'interrupt' and 'cancelled' with the two BaseExceptions that actually end a
derivation in the field: Ctrl-C on a slow KDF, and a publisher task the
supervisor cancelled.
"""
import asyncio
import hashlib
import os

FAIL_AT = os.environ.get("SECRET_STUB_FAIL_AT") or None


class VerificationKey:
    def __init__(self, payload):
        self.payload = payload

    def hash(self):
        # One-way and shorter than its input: the definition of a public
        # derivation, and the reason the canary cannot survive it.
        return hashlib.sha256(self.payload.encode()).hexdigest()[:28]


class PaymentSigningKey:
    def __init__(self, payload):
        self.payload = payload

    @classmethod
    def load(cls, path, passphrase=None):
        with open(path, encoding="utf-8") as fh:
            payload = fh.read().strip()
        if FAIL_AT == "load":
            raise ValueError(f"malformed key file: {payload}")
        return cls(payload)

    def __repr__(self):
        return ('{"type": "PaymentSigningKeyShelley_ed25519", '
                f'"cborHex": "{self.payload}"}}')

    def to_verification_key(self):
        if FAIL_AT == "derive":
            raise ValueError("unsupported key type for this network")
        if FAIL_AT == "interrupt":
            raise KeyboardInterrupt()
        if FAIL_AT == "cancelled":
            raise asyncio.CancelledError()
        return VerificationKey(self.payload)


class Address:
    def __init__(self, part, network=None):
        self.part = part

    def __str__(self):
        return "addr1" + self.part


class Network:
    MAINNET = "mainnet"
'''

LOADER_SRC = '''"""Control: a loader that quotes the file into its own exception.

pycardano, bip_utils and cardano-cli all do this. A wrong word count, a
BOM, CRLF, a .skey where a mnemonic was expected — and the offending bytes
are in the message, which the default excepthook prints. It is the reason
the box's safe shapes call an entrypoint instead of the loader.
"""


def load_wallet(path):
    with open(path, encoding="utf-8") as fh:
        payload = fh.read().strip()
    raise ValueError(f"invalid mnemonic checksum: {payload!r}")
'''


def stubs(fail_at=None):
    """The stub module as an object graph, for in-process execution."""
    ns = {"__name__": "stubcardano"}
    exec(compile(STUB_SRC, "<stubcardano>", "exec"), ns)
    ns["FAIL_AT"] = fail_at
    return ns


def load_block(src, tag, fail_at=None):
    ns = dict(stubs(fail_at))
    ns["__name__"] = tag
    exec(compile(block(src, tag), f"<{tag}>", "exec"), ns)
    return ns


def fences(src):
    """(info strings of every opening fence, lines outside all fences).

    One walk answers both questions the doc's shape raises: whether any
    block is untagged — an untagged fence is a snippet nothing extracts,
    which is where the NameError and the unguarded one-liner both lived —
    and how much of the file is prose a reader must read rather than code
    a reader skims.
    """
    info, prose, inside = [], 0, False
    for line in src.splitlines():
        if line.startswith("```"):
            if not inside:
                info.append(line[3:].strip())
            inside = not inside
        elif not inside:
            prose += 1
    return info, prose, inside


def rendered(exc):
    """What --showlocals shows: frames, their locals, and the honored chain."""
    return "".join(traceback.TracebackException.from_exception(
        exc, capture_locals=True).format())


def keyfile(tmp):
    path = os.path.join(tmp, "vault_backup_b.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(CANARY + "\n")
    return path


src = source()
if src is None:
    print(f"FAIL  the skill exists                                       "
          f"-> {SKILL} not found")
    print("\n0 passed, 1 failed")
    sys.exit(1)

# ---- discovery: a skill the model never loads is a skill that never ran ----
fm = re.match(r"^---\n(.*?)\n---\n", src, re.S)
report("frontmatter is present and closed", bool(fm),
       "parsed" if fm else "no --- block")
if fm:
    name = re.search(r"^name:\s*(\S+)", fm.group(1), re.M)
    desc = re.search(r"^description:\s*(.+)$", fm.group(1), re.M)
    report("name matches the skill directory",
           bool(name) and name.group(1) == "secret-handling",
           name.group(1) if name else "absent")
    # Discovery is by description alone. The other two skills carry a long
    # trigger vocabulary for exactly this reason; a terse one is a skill that
    # loads only when the user already knows its name.
    text = desc.group(1).lower() if desc else ""
    triggers = [t for t in ("credential", "mnemonic", "signing key", "token",
                            "permission", "wallet") if t in text]
    report("description carries the trigger vocabulary", len(triggers) >= 4,
           f"{len(triggers)} of 6: {triggers}")
    report("description is long enough to route on", len(text) >= 200,
           f"{len(text)} chars")

info, prose, unclosed = fences(src)
report("the skill stays short enough to read",
       len(src.splitlines()) <= MAX_LINES and prose <= MAX_PROSE,
       f"{len(src.splitlines())} lines / {prose} prose "
       f"(ceilings {MAX_LINES} / {MAX_PROSE})")

# A doc that names a script rots the moment the script is renamed, and the
# reader who follows the citation is the one who finds out.
cited = sorted(set(re.findall(r"`((?:scripts|tests)/[\w.-]+)`", src)))
missing = [p for p in cited if not os.path.exists(os.path.join(PLUGIN, p))]
report("every plugin path the skill cites resolves", not missing,
       f"{len(cited)} cited" if not missing else f"missing: {missing}")

# Resolving is not the same as being armed. This doc described secret-guard.py
# as a live PreToolUse hook while hooks.json had no PreToolUse block at all,
# and the citation check passed on it — an agent reading only the skill would
# have relaxed behind a control that was never installed. So a script the
# skill discusses as a hook must either BE wired, or say that it is not.
with open(HOOKS, encoding="utf-8") as fh:
    wiring = fh.read()
with open(os.path.join(PLUGIN, "scripts", "native-hook.mjs"), encoding="utf-8") as fh:
    wiring += fh.read()
overclaimed = []
for para in re.split(r"\n\s*\n|\n(?=- )", src):
    if "hook" not in para:
        continue
    for script in re.findall(r"`(scripts/[\w.-]+)`", para):
        if os.path.basename(script) not in wiring and "not installed" not in para:
            overclaimed.append(script)
report("no unwired hook is described as installed", not overclaimed,
       "wired or disclaimed" if not overclaimed else f"claimed: {overclaimed}")

report("no fenced block is untagged", all(info) and not unclosed,
       f"{len(info)} fences: {info}")
py_tags = [t for t in info if t.startswith("python ")]
report("every python fence is one this suite runs", set(py_tags) <= RUN_TAGS,
       f"{len(py_tags)} fences: {sorted(set(py_tags))}")

# The classification is the part a reader applies from memory, so the values
# and channels that got past it once are pinned here by name.
covered = [t for t in ("signed transaction", "witness", "acct_xvk",
                       "chain code", "passphrase", "process table",
                       "Read, Grep", "already leaked")
           if t in src]
report("the skill names what got past its classification",
       len(covered) == 8, f"{len(covered)} of 8: {covered}")

# ---- the headline box, run as a reader would run it -------------------------
shapes = block(src, "sh secret-shapes")
report("the headline shapes are a runnable tagged block", shapes is not None,
       "found" if shapes else "MISSING")
report("the barrier shape is a runnable tagged block",
       block(src, "python secret-barrier") is not None,
       "found" if block(src, "python secret-barrier") else "MISSING")
report("the filter shape is a runnable tagged block",
       block(src, "python secret-filter") is not None,
       "found" if block(src, "python secret-filter") else "MISSING")
report("the refusal shape is a runnable tagged block",
       block(src, "python secret-precondition") is not None,
       "found" if block(src, "python secret-precondition") else "MISSING")

PY = sys.executable.replace("\\", "/")


def as_run(line):
    """One line of the box with its placeholders bound to this run.

    The doc's absolute path becomes the canary file (the shell runs in the
    directory holding it) and `python` becomes this interpreter, so what
    executes is otherwise byte-identical to what a reader copies.
    """
    return re.sub(r"D:/[\w/.-]+", "vault_backup_b.txt",
                  line).replace("python ", f'"{PY}" ')


def pick_shell(tmp):
    """The bash that can actually see this directory.

    On Windows the `bash` first on PATH is usually WSL's, which cannot run
    in a Windows working directory and reports a relay error rather than a
    missing file — a silent wrong answer for every case below. So the shell
    is probed against the real cwd, not chosen by name.
    """
    tried = []
    for cand in (os.environ.get("SHELL"), "C:/Program Files/Git/bin/bash.exe",
                 shutil.which("bash"), "/bin/bash"):
        if not cand or cand in tried:
            continue
        tried.append(cand)
        try:
            done = subprocess.run([cand, "-c", "printf ok"], cwd=tmp,
                                  capture_output=True, text=True)
        except OSError:
            continue
        if done.returncode == 0 and done.stdout.strip() == "ok":
            return cand
    return None


def shell(sh, line, tmp, vector=None):
    env = dict(os.environ, LIVE_ADDRESS=LIVE, PASSPHRASE="a-25th-word")
    env.pop("SECRET_STUB_FAIL_AT", None)
    if vector:
        env["SECRET_STUB_FAIL_AT"] = vector
    done = subprocess.run([sh, "-c", as_run(line)], cwd=tmp, env=env,
                          capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


if shapes and block(src, "python secret-barrier"):
    safe = [l.split(None, 1)[1] for l in shapes.splitlines()
            if l.startswith("SAFE")]
    unsafe = [l.split(None, 1)[1] for l in shapes.splitlines()
              if l.startswith("UNSAFE")]
    report("the box shows the replacements and what they replace",
           len(safe) >= 3 and len(unsafe) >= 2,
           f"{len(safe)} safe, {len(unsafe)} unsafe")

    if safe and unsafe:
        with tempfile.TemporaryDirectory() as tmp:
            keyfile(tmp)
            sh = pick_shell(tmp)
            os.makedirs(os.path.join(tmp, "ops"))
            os.makedirs(os.path.join(tmp, "custody"))

            def write(rel, text):
                with open(os.path.join(tmp, rel), "w", encoding="utf-8") as fh:
                    fh.write(text)

            write("stubcardano.py", STUB_SRC)
            write(os.path.join("ops", "__init__.py"), "")
            # The stub import is the only line the reader does not have; the
            # rest of ops/derive.py is the skill's barrier block verbatim.
            write(os.path.join("ops", "derive.py"),
                  "from stubcardano import PaymentSigningKey, Address, Network\n"
                  + block(src, "python secret-barrier"))
            write(os.path.join("custody", "__init__.py"), "")
            write(os.path.join("custody", "wallet.py"), LOADER_SRC)

            report("a shell can run the box", sh is not None,
                   sh or "the box's shapes are shell lines; no usable bash")

            for i, line in enumerate(safe if sh else [], 1):
                rc, out = shell(sh, line, tmp)
                emitted = out.strip().splitlines()[-1] if out.strip() else ""
                report(f"safe shape {i} emits only a public derivation",
                       rc == 0 and CANARY not in out
                       and (emitted.startswith("addr1")
                            or emitted in ("True", "False")),
                       f"rc={rc} {emitted[:28] or '(nothing)'}")

                # Two vectors here, not four: the barrier suite below pins
                # all four in-process, and these runs exist to prove the
                # box's own line survives an error path — the loader that
                # quotes the file, and the BaseException that used to walk
                # straight past the barrier.
                dirty = [v for v in ("load", "interrupt")
                         if CANARY in shell(sh, line, tmp, v)[1]]
                report(f"safe shape {i} holds on every failure vector",
                       not dirty, "clean" if not dirty else f"LEAKED on {dirty}")

            for i, line in enumerate(unsafe if sh else [], 1):
                leaked = CANARY in shell(sh, line, tmp, "load")[1]
                report(f"control: unsafe shape {i} does leak", leaked,
                       "leaked, as it must" if leaked
                       else "did not leak — the control has stopped controlling")

# ---- the barrier shape -----------------------------------------------------
if block(src, "python secret-barrier"):
    # Every render below happens inside a function on purpose: at module
    # scope f_locals IS the module globals, so the canary constant itself
    # would appear in every traceback and the check would fail whatever the
    # barrier does. The frames under test must be the only ones that matter.
    def call_and_render(fn, arg):
        """Return (exception, what a --showlocals reader would see).

        BaseException, not Exception: catching narrowly here would let a
        KeyboardInterrupt escaping the barrier crash this suite instead of
        failing the case it is meant to fail.
        """
        try:
            fn(arg)
        except BaseException as e:
            return e, rendered(e)
        return None, ""

    def leaky_message(path):
        """Control: the same handler without the scrub, chaining the cause."""
        s = stubs(fail_at="load")
        try:
            return str(s["Address"](s["PaymentSigningKey"].load(path)
                                    .to_verification_key().hash()))
        except Exception as e:
            raise RuntimeError(f"could not derive: {e}")

    def leaky_locals(path, vector="derive"):
        """Control: one function, so the key is a local of the frame that fails."""
        s = stubs(fail_at=vector)
        key = s["PaymentSigningKey"].load(path)
        return str(s["Address"](key.to_verification_key().hash()))

    with tempfile.TemporaryDirectory() as tmp:
        path = keyfile(tmp)

        addr = load_block(src, "python secret-barrier")["publisher_address"](path)
        report("the barrier derives a real public address",
               isinstance(addr, str) and addr.startswith("addr1")
               and len(addr) > 20, addr[:24] + "...")
        report("the derived address carries none of the key",
               CANARY not in addr, "clean")

        # Vector 1: the loader quotes the file's contents into its own message.
        ns = load_block(src, "python secret-barrier", fail_at="load")
        exc, shown = call_and_render(ns["publisher_address"], path)
        report("a bad key file raises", exc is not None,
               type(exc).__name__ if exc else "returned normally")
        report("the scrubbed message drops the quoted contents",
               exc is not None and CANARY not in str(exc), "clean")
        report("nothing --showlocals renders holds the contents",
               CANARY not in shown, "clean")

        _, shown = call_and_render(leaky_message, path)
        report("control: an unscrubbed message does leak",
               CANARY in shown, "leaked, as it must")

        # Vector 2: the key object is bound to a frame local when the raise
        # happens, so anything that renders that frame prints the key in full.
        # The last two are BaseExceptions, which an `except Exception` barrier
        # never sees: Ctrl-C on a slow KDF, and a cancelled publisher task.
        for vector, kind in (("derive", "a failed derivation"),
                             ("interrupt", "a Ctrl-C"),
                             ("cancelled", "a cancelled task")):
            ns = load_block(src, "python secret-barrier", fail_at=vector)
            exc, shown = call_and_render(ns["publisher_address"], path)
            report(f"{kind} leaves the barrier scrubbed",
                   isinstance(exc, RuntimeError),
                   type(exc).__name__ if exc else "returned normally")
            report(f"{kind} renders no key-holding frame", CANARY not in shown,
                   "clean" if CANARY not in shown else "LEAKED the key")

            _, shown = call_and_render(
                lambda p, v=vector: leaky_locals(p, v), path)
            report(f"control: {kind} with no barrier does leak",
                   CANARY in shown, "leaked, as it must")

# ---- the filter shape ------------------------------------------------------
if block(src, "python secret-filter"):
    cfg = {"NETWORK": "mainnet", "PUBLISHER_ADDRESS": "addr1q" + "x" * 90,
           "PUBLISHER_MNEMONIC": CANARY, "BLOCKFROST_PROJECT_ID": CANARY2}
    ns = load_block(src, "python secret-filter")
    out = ns["summarize"](cfg)
    report("the filter names every key it saw",
           all(k in out for k in cfg), "all four named")
    report("no secret value crosses the filter",
           CANARY not in out and CANARY2 not in out, "clean")
    report("allowlisted values stay readable",
           "mainnet" in out and "addr1q" + "x" * 90 in out, "readable")
    report("control: dumping the config does leak",
           CANARY in json.dumps(cfg), "leaked, as it must")

    # A name is not evidence. `publisher_mnemonic.txt` held a retired key;
    # mid-rotation, a PUBLISHER_ADDRESS holding key material is the same lie.
    lying = {"PUBLISHER_ADDRESS": "ed25519e_sk1" + CANARY, "NETWORK": "mainnet"}
    out = ns["summarize"](lying)
    clean = CANARY not in out and "disagree" in out
    report("a value that is not its name's shape is withheld", clean,
           "withheld" if clean else "printed a key under a public name")

    def render_call(fn, arg):
        try:
            fn(arg)
        except BaseException as e:
            return rendered(e)
        return ""

    def leaky_parse(stream):
        """Control: json.load with no barrier.

        Its message is clean — JSONDecodeError does not quote the document —
        which is exactly why this one gets missed. The whole dump is a frame
        local inside the decoder, and --showlocals prints it from two frames.
        """
        return json.load(stream)

    dump = 'Project: example-oracle\n{"PUBLISHER_MNEMONIC": "%s"}' % CANARY
    report("the filter reads its stdin behind the barrier too",
           "redact_stream" in ns, "found" if "redact_stream" in ns
           else "the pipeline decodes with no barrier")
    if "redact_stream" in ns:
        shown = render_call(ns["redact_stream"], io.StringIO(dump))
        report("an unparseable config dump leaves nothing in the frames",
               CANARY not in shown,
               "clean" if CANARY not in shown else "LEAKED the whole dump")
    shown = render_call(leaky_parse, io.StringIO(dump))
    report("control: an unbarriered json.load does leak", CANARY in shown,
           "leaked, as it must")

# ---- the refusal shape -----------------------------------------------------
if block(src, "python secret-precondition"):
    ns = load_block(src, "python secret-precondition")
    guard = ns["require_network_match"]

    def unguarded(built, claimed):
        """Control: the same verifier with the precondition left out.

        It derives, it compares, both succeed — and it certifies mainnet off
        a preprod build, which is the document the drill refused to produce.
        """
        return f"custody of the {claimed} publisher key is PROVEN from backup"

    refusal = None
    try:
        guard("preprod", "mainnet")
    except SystemExit as e:
        refusal = str(e)
    report("a checkout that built another network is refused",
           bool(refusal) and "CHECKOUT" in refusal,
           (refusal or "certified anyway")[:34] + "...")

    matched = True
    try:
        guard("mainnet", "mainnet")
    except BaseException as e:
        matched = False
    report("a matching checkout is allowed to proceed", matched,
           "proceeds" if matched else "refused its own network")
    report("control: with no precondition the wrong bytes certify",
           "PROVEN" in unguarded("preprod", "mainnet"), "certified, as it must")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
