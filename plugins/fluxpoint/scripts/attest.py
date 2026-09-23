#!/usr/bin/env python3
"""Execution attestation: exit codes minted by a hook, not typed by an agent.

The compiler's flagship ordering invariant — an independent node re-derives
the verdict, and its `haltWhen` guards the irreversible effect ordered after
it — is sound in structure and hollow in fidelity. The gate node *runs*
`scripts/harness.sh` and then *writes* `{"exit": 0}` into HarnessCheckV1 by
hand. The integer that stands between a campaign and an unrepeatable chain
write is a transcription, and `record-run.py` treats it as derived truth.

So the exit code gets recorded where it actually happens. A PostToolUse hook
on the Bash tool sees the command and the runtime's own `tool_response`, and
when the command is one this repo declared a gate, appends a row here. The
agent never touches the number.

  attest.py --record          append a row from a hook payload on stdin
  attest.py --list            what has been attested, for humans
  attest.py --verify          cross-check a run summary on stdin against the log
  attest.py --run GATE        run a declared gate and attest its exit itself
  attest.py --await TOKEN     wait, in bounded slices, for a --run to finish
  attest.py --ci --pr N       attest the forge's own commit statuses on PR N's head
                              (or --ref BRANCH; --sha names a commit the caller chose)
  attest.py --stamp           a launch stamp ({since, nonce}) for args._launch

Three witnesses mint rows, and each row names its own: `hook` (the Bash
PostToolUse payload), `wrapper` (--run, which executes the command the
manifest declares for a gate name — the agent names a gate, never a
command — so a gate longer than one tool call can be started in the
background and awaited under the 600 s cap, and a red exit the hook never
sees is recorded too), and `forge` (--ci, which asks the forge for the
statuses on a commit, the least forgeable evidence a merge rests on,
because the agent produces none of it).

Dormant by design: a repo with no `.fluxpoint-gates.json` attests nothing
and says nothing, exactly like the DoD gate in a repo with no harness.

Three limits, stated rather than papered over. The hook only sees executions
that go through the Bash tool, so a gate run some other way produces no row
— which is why an unattested claim is reported UNATTESTED and never as a
failure; treating absence as guilt would make this a false-red generator on
the first executor that does not route through the hook. And the log records
what a command exited with, not whether the command was worth running: a
declared gate that is itself weakened is `fpl_harness_modified`'s problem
and the proof-guard ratchet's, not this file's.

The third bounds everything above it: PostToolUse has been measured NOT to
fire when a Bash call fails, so in practice this log may contain only passes.
The reader below handles every failure shape the runtime can send, and that
branch is currently exercised by fixtures rather than by observed payloads.
Read the consequence carefully — UNATTESTED on a `prove:` node carries no
information about whether the gate passed, so the binding between a claimed
exit and the runtime's own holds for zeros and is silent about reds. A node
claiming green with no row is therefore the shape worth suspecting, and
record-run.py reports it separately for that reason.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time

GATES = ".fluxpoint-gates.json"
ATTEST = os.path.join(".claude", "fluxpoint", "attest.jsonl")
BACKGROUND = os.path.join(".claude", "fluxpoint", "attest-bg")
GATE_MANIFEST_FIELDS = {"version", "gates", "ci"}
CI_FIELDS = {"forge", "contexts"}
CI_FORGES = {"github"}
IDENT = re.compile(r"^[a-z][a-z0-9-]*$")
# One foreground tool call is killed at 600 s; --await returns before that
# with a verdict or a "still running", never mid-kill.
AWAIT_DEFAULT = 540


def path_for(root):
    return os.path.join(root, ATTEST)


def gates_path(root):
    return os.path.join(root, GATES)


# `FPL_ATTEST_NONCE=<token> <gate>`: how a graph node names the run it is
# executing for. One assignment, at the front (after an optional `cd`), and
# a token of plain characters, so it cannot smuggle a second command in.
NONCE_RE = re.compile(r"FPL_ATTEST_NONCE=([A-Za-z0-9_-]{1,64})\s+(?=\S)")
# ONE leading `cd <dir> &&` (or `;`); the directory is captured for the
# provenance of which tree the gate ran in.
CD_RE = re.compile(r"cd\s+([^;&|<>]+?)\s*(?:&&|;)\s*(?=\S)")


def _front(cmd):
    """(nonce, cd_dir, rest) of a command line's permitted prefix.

    The nonce may sit before or after the one leading `cd`: the compiled
    preamble tells a node to prefix the declared command, and a declared
    command may itself start with `cd backend &&` — the instructed form is
    then `FPL_ATTEST_NONCE=n cd backend && gate`, which used to keep its cd
    and match nothing. One nonce and one cd, never a chain of either.
    """
    s = " ".join(str(cmd or "").split())
    nonce = cd = ""
    m = NONCE_RE.match(s)
    if m:
        nonce, s = m.group(1), s[m.end():]
    m = CD_RE.match(s)
    if m:
        cd, s = m.group(1), s[m.end():]
    if not nonce:
        m = NONCE_RE.match(s)
        if m:
            nonce, s = m.group(1), s[m.end():]
    return nonce, cd, s


def nonce_of(cmd):
    """The run nonce a command line carries, or ''."""
    return _front(cmd)[0]


def normalize(cmd):
    """Canonical form of a command line, for comparison against a manifest.

    Whitespace is collapsed and a leading interpreter or `./` is dropped, so
    `bash scripts/harness.sh  --full` and `./scripts/harness.sh --full` are
    the same declared gate.

    A single leading `cd <dir> &&` or `cd <dir>;` is also dropped. That one is
    safe for the same reason the others are not: `cd` runs FIRST and the gate
    runs LAST, so the status the shell reports is still the gate's. Refusing it
    was not strictness, it was a hole — a green run went unwitnessed while
    looking witnessed, which is the precise failure this module exists to
    prevent, and it cost four real runs before anyone noticed. Only one `cd`,
    and only at the front: `cd a && cd b && gate` is a shape nobody needs and
    every extra allowance is somewhere for an exit code to hide.

    Nothing else is stripped, and that is the load-bearing part. A pipeline,
    a redirect, a trailing `|| true`, or ANY command after the gate changes the
    exit code the runtime reports, so none of them may borrow a gate's name:
    they simply do not match, and an unmatched command is attested as nothing
    at all.
    """
    # ONE prefix, never a chain: the match is non-greedy and applied once, so
    # `cd a && cd b && gate` still fails to match — the remainder is not the
    # declared command. An explicit multi-cd check would be dead weight AND
    # wrong, refusing a gate that legitimately starts with `cd` itself.
    # The run's nonce rides in as an environment assignment, which changes
    # nothing about the exit the shell reports. It is read by nonce_of(),
    # never matched as part of the gate.
    s = _front(cmd)[2]
    for prefix in ("bash ", "sh ", "zsh "):
        if s.startswith(prefix):
            s = s[len(prefix):].lstrip()
            break
    if s.startswith("./"):
        s = s[2:]
    return s


EXIT_RE = re.compile(r"\A\s*Error: Exit code (\d+)")
# Codex hands every completed Bash call to the hook as one string: a header
# of `Wall time: …` and `Process exited with code N` (the classic shell tool
# writes `Exit code: N`), then `Output:` and the output itself.
CODEX_EXIT_RE = re.compile(r"^(?:Process exited with code|Exit code:) (-?\d+)\s*$", re.M)


def exit_of(resp):
    """The exit status a PostToolUse Bash payload reports, or None.

    `tool_response` has no `exit_code` field. Claude Code reports the status
    in the SHAPE of the value: a success arrives as an object carrying
    `stdout`/`stderr`/`interrupted`, and a failure arrives as a plain string
    beginning `Error: Exit code N`. Codex reports it in a header line of the
    one string it always sends, `Process exited with code N`, for passes and
    failures alike. Reading only for `exit_code` meant every
    gate run since gates shipped was recorded as unreadable, and the tests
    did not catch it because their fixtures invent the field.

    None means "not readable", never "passed". An interrupted command and an
    unrecognized error string (a denied permission, a blocked path) must stay
    UNATTESTED rather than be recorded as a pass — an attestation that
    guesses is worse than an absent one, because record-run.py trusts it.
    """
    if isinstance(resp, str):
        m = EXIT_RE.match(resp)
        if m:
            return int(m.group(1))
        # Codex's shape. Only the header is read — the lines before
        # `Output:`, and never more than a few — so a command that PRINTS a
        # line shaped like the header cannot mint its own exit.
        head = "\n".join(resp.split("\nOutput:", 1)[0].splitlines()[:6])
        m = CODEX_EXIT_RE.search(head)
        return int(m.group(1)) if m else None
    if not isinstance(resp, dict):
        return None
    # Honored first, so the fix survives the field actually arriving one day.
    if isinstance(resp.get("exit_code"), int):
        return resp["exit_code"]
    if resp.get("interrupted"):
        return None
    return 0 if "stdout" in resp else None


def sha(text):
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()


def load_gates(root):
    """Return (gates, findings). Missing manifest is dormant, not an error.

    A malformed manifest is a finding rather than an empty gate set: a
    manifest that silently parses to nothing disarms attestation while
    looking configured, which is the failure mode this whole plugin exists
    to refuse.
    """
    p = gates_path(root)
    if not os.path.exists(p):
        return {}, []
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return {}, [f"{GATES} is not readable JSON ({e}) — attestation is DISARMED"]
    if not isinstance(doc, dict):
        return {}, [f"{GATES} must be an object"]
    f = [f"{GATES}: unknown field '{k}' — not part of the manifest"
         for k in sorted(set(doc) - GATE_MANIFEST_FIELDS)]
    if doc.get("version") != 1:
        f.append(f"{GATES}: version must be 1")
    f += _ci_problems(doc)
    gates = doc.get("gates")
    # A manifest whose only witness is the forge is a natural shape: an empty
    # `gates` beside a `ci` section declares exactly that.
    if not isinstance(gates, dict) or (not gates and not isinstance(doc.get("ci"), dict)):
        f.append(f"{GATES}: 'gates' must be a non-empty object of name -> command"
                 f" (or empty beside a top-level 'ci' section)")
        return {}, f
    out = Gates()
    out.dirs = {}
    for name, cmd in gates.items():
        if not IDENT.match(str(name)):
            f.append(f"{GATES}: gate name '{name}' must be lowercase kebab-case")
            continue
        if name == "ci":
            f.append(f"{GATES}: gate name 'ci' is reserved for the forge's commit "
                     f"statuses (prove:ci, attest.py --ci) since fluxpoint 1.43 — "
                     f"rename this gate (e.g. 'ci-local') and cite it as "
                     f"prove:ci-local; declare the forge under a top-level 'ci' section")
            continue
        if not isinstance(cmd, str) or not cmd.strip():
            f.append(f"{GATES}.{name}: command must be a non-empty string")
            continue
        out[name] = normalize(cmd)
        out.dirs[name] = _dir_parts(_front(cmd)[1])
    return ({} if f else out), f


def _ci_problems(doc):
    """Findings for the optional `ci` section: which forge, which contexts."""
    ci = doc.get("ci")
    if ci is None:
        return []
    if not isinstance(ci, dict):
        return [f"{GATES}: 'ci' must be an object"]
    f = [f"{GATES}: ci: unknown field '{k}'" for k in sorted(set(ci) - CI_FIELDS)]
    if ci.get("forge") not in CI_FORGES:
        f.append(f"{GATES}: ci.forge must be one of {', '.join(sorted(CI_FORGES))} — "
                 f"a forge this script cannot query would attest nothing while "
                 f"looking configured")
    ctx = ci.get("contexts")
    if ctx is not None and (not isinstance(ctx, list) or not ctx or not all(
            isinstance(c, str) and c.strip() for c in ctx)):
        f.append(f"{GATES}: ci.contexts must be a non-empty list of status or "
                 f"check names, or absent (every reported context must pass)")
    return f


def load_ci(root):
    """The manifest's `ci` section, or None. Findings are load_gates' job."""
    try:
        with open(gates_path(root), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    ci = doc.get("ci") if isinstance(doc, dict) else None
    return ci if isinstance(ci, dict) and not _ci_problems(doc) else None


def declared_command(root, gate):
    """The command a gate name declares, exactly as the manifest writes it."""
    with open(gates_path(root), encoding="utf-8") as fh:
        return (json.load(fh).get("gates") or {}).get(gate)


def _dir_parts(d):
    """A `cd` target as path components, quotes and `.` dropped."""
    d = str(d or "").strip().strip("'\"").replace("\\", "/")
    return [x for x in d.split("/") if x not in ("", ".")]


class Gates(dict):
    """{name: normalized command}, with each gate's declared leading `cd`
    directory (as components) in `.dirs`."""
    dirs = {}


def gate_for(gates, command):
    """The declared gate this exact command is, or None.

    normalize() drops one leading `cd`, so `cd /abs/project && gate` still
    matches. But a gate whose DECLARATION starts with `cd backend &&` is
    identified by that directory too: `cd frontend && npm test` normalizes
    to the same `npm test`, runs against another component of the same
    commit, and must not be attested as the backend's gate. The invoked
    directory has to end with the declared one (`backend`, `./backend`,
    `/abs/checkout/backend` and a worktree's `wt/backend` all do).
    """
    n = normalize(command)
    for name, declared in gates.items():
        if declared != n:
            continue
        want = (getattr(gates, "dirs", None) or {}).get(name) or []
        if want:
            got = _dir_parts(_front(command)[1])
            if len(got) < len(want) or got[len(got) - len(want):] != want:
                continue
        return name
    return None


def read(root):
    """Every attested execution, oldest first. A malformed line is fatal.

    Same discipline as the once-only ledger, for the same reason: skipping
    an unparseable row would silently shrink the set of executions known to
    have happened, and this file's whole job is to be the thing that cannot
    quietly lose one.
    """
    p = path_for(root)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"attest: {p}:{i} is not valid JSON: {e}")
    return out


def head_sha(root):
    """Best-effort HEAD, so a row says which tree the gate judged."""
    try:
        import subprocess
        r = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - provenance detail, never worth failing over
        return ""


def _tree_sha(root, payload, command):
    """HEAD of the tree a hook-witnessed gate ran in: the session's cwd, then
    the command's one leading `cd`. Provenance only — headSha, the project's
    HEAD, is what a citation is bound to, for every witness alike."""
    cd = _front(command)[1].strip().strip("'\"")
    base = str(payload.get("cwd") or root)
    tree = os.path.join(base, os.path.expanduser(cd)) if cd else base
    return head_sha(tree) if os.path.isdir(tree) else ""


def project_root(start="."):
    """Where the manifest and the attest log live, seen from `start`.

    A gate run from a linked worktree (git worktree add) belongs to the
    project it was checked out from: the hook attests into the project's log
    (it cds to CLAUDE_PROJECT_DIR), and so must the wrapper, or record-run
    looks in the project's log for a row written into the worktree's and
    files an honest node as TAMPERED-EXECUTION.
    """
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and os.path.exists(gates_path(env)):
        return env
    try:
        import subprocess

        def git(*a):
            r = subprocess.run(["git", "-C", start] + list(a),
                               capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        top = git("rev-parse", "--show-toplevel")
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir") \
            or git("rev-parse", "--git-common-dir")
        prefix = git("rev-parse", "--show-prefix") or ""
    except (OSError, ValueError):
        return start
    if not top or not common:
        return start
    common = os.path.abspath(os.path.join(start, common))
    if os.path.basename(common) != ".git":
        return start
    main = os.path.dirname(common)
    if os.path.normcase(os.path.realpath(main)) == os.path.normcase(os.path.realpath(top)):
        return start
    cand = os.path.join(main, prefix)
    return cand if os.path.exists(gates_path(cand)) else start


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp():
    """A launch stamp for args._launch: when the run starts, and its nonce.

    `since` bounds what a citation may be older than; the nonce names the
    run, so two runs overlapping on one commit and one gate cannot cite each
    other's executions. Minted here so its format is the rows' own.
    """
    import secrets
    return {"since": now(), "nonce": secrets.token_hex(8)}


def append_row(root, row):
    p = path_for(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row) + "\n")


def record(root, payload):
    """Append a row for a hook payload. Returns (row, findings).

    (None, []) means the command was not a declared gate — the common case,
    and deliberately silent.
    """
    gates, findings = load_gates(root)
    if findings or not gates:
        return None, findings
    tool = payload.get("tool_name")
    if tool and tool != "Bash":
        return None, []
    command = (payload.get("tool_input") or {}).get("command")
    gate = gate_for(gates, command)
    if not gate:
        return None, []
    resp = payload.get("tool_response")
    # A backgrounded launch returns IMMEDIATELY with a success-shaped payload
    # — stdout/stderr/interrupted and no exit code — before the gate has done
    # anything, and completion later arrives as a task notification, never as
    # another Bash PostToolUse. Reading that shape through exit_of would mint
    # exit 0 for a run that has not happened, which is the one direction this
    # file must never fail in: a gate that reports green before running is
    # worse than one that reports nothing. Covers both the deliberate
    # run_in_background and the auto-background-on-timeout path, which comes
    # back carrying backgroundTaskId in the response.
    if (payload.get("tool_input") or {}).get("run_in_background") \
       or (isinstance(resp, dict) and resp.get("backgroundTaskId")):
        return None, []
    code = exit_of(resp)
    if code is None:
        # The gate ran and its verdict was unreadable. Silence here would be
        # indistinguishable from the gate never running, so say so.
        return None, [
            f"gate '{gate}' ran but the hook payload carried no readable exit "
            f"status — nothing was attested for this run"]
    when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    norm = normalize(command)
    out = resp if isinstance(resp, dict) else {"stdout": str(resp)}
    log = f"{out.get('stdout') or ''}\n---\n{out.get('stderr') or ''}"
    session = str(payload.get("session_id") or "")
    row = {
        "attestId": "att_" + sha(f"{when}|{norm}|{code}|{session}"
                                 f"|{payload.get('tool_use_id') or ''}")[:12],
        "gate": gate,
        "command": norm,
        "commandSha": sha(norm),
        "exit": code,
        "logSha256": sha(log),
        "headSha": head_sha(root),
        "treeSha": _tree_sha(root, payload, command),
        "when": when,
        "sessionId": session,
        # Subagent Bash calls fire this hook too, and a gate run inside a
        # graph node is exactly the execution record/run needs to bind.
        "agent": str(payload.get("agent_type") or payload.get("agent_id") or ""),
        "witness": "hook",
        "nonce": nonce_of(command),
    }
    append_row(root, row)
    return row, []


# ------------------------------------------------------------ long gates
def _bg_dir(root):
    return os.path.join(root, BACKGROUND)


def _bg_read(root, token):
    try:
        with open(os.path.join(_bg_dir(root), f"{token}.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _bg_write(root, rec):
    d = _bg_dir(root)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{rec['token']}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rec, fh)
    os.replace(tmp, os.path.join(d, f"{rec['token']}.json"))


def run_gate(root, gate, detach=False, token=None, nonce="", tree=None):
    """Run a declared gate and attest its exit. Returns the gate's exit code.

    The command is the manifest's, resolved from the gate NAME, so nothing
    the caller types can widen what gets attested. The row is minted when
    the command ends, which is the point: a gate launched in the background
    finishes long after its Bash call returned, and the hook never sees
    that. `detach` re-launches this runner as its own background process
    and returns at once — for a caller with no background facility of its
    own; under Claude Code, `run_in_background` on a plain --run does the
    same. Either way the token is printed first, and --await is how the
    verdict is collected.

    `root` is where the manifest and the log live; `tree` is where the gate
    runs (root when not given) — a worktree of the campaign branch, say.
    """
    root = os.path.abspath(root)
    tree = os.path.abspath(tree or root)
    gates, findings = load_gates(root)
    for f in findings:
        print(f"attest: {f}", file=sys.stderr)
    if findings:
        return 2
    if gate not in gates:
        print(f"attest: '{gate}' names no gate in {GATES} (declared: "
              f"{', '.join(sorted(gates)) or 'none'}) — --run takes a gate name, "
              f"never a command", file=sys.stderr)
        return 2
    started = now()
    token = token or "bg_" + sha(f"{gate}|{started}|{os.getpid()}|{time.monotonic_ns()}")[:12]
    if detach:
        import subprocess
        # The record exists before the child does, so an --await issued the
        # moment this returns finds it. No pid yet: the child writes its own
        # when it starts, and never after this line could overwrite a DONE.
        _bg_write(root, {"token": token, "gate": gate, "status": "RUNNING",
                         "pid": None, "started": started})
        # Absolute paths: the child starts in `tree`, and a relative root
        # resolved a second time from there named a directory that does not
        # exist — the child exited on a missing manifest, silently.
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--root", root, "--tree", tree,
             "--run", gate, "--token", token] + (["--nonce", nonce] if nonce else []),
            cwd=tree, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"))
        print(f"attest: started {gate} as {token} — collect it with "
              f"attest.py --await {token}", flush=True)
        return 0
    return _execute(root, gate, started, token, nonce, tree)


def _bash():
    """The bash to run a gate with, as an absolute path where one is known.

    On Windows a bare "bash" is resolved by CreateProcess, which searches
    System32 before PATH: on a machine with WSL that is WSL's bash.exe, a
    different toolchain from the Git Bash the hooks and the Bash tool use.
    py.sh exports the bash it runs under as FPL_BASH.
    """
    import shutil
    for cand in (os.environ.get("FPL_BASH"), os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")):
        if cand and os.path.isfile(cand):
            return cand
    return shutil.which("bash") or "bash"


def _execute(root, gate, started, token, nonce="", tree=None):
    import subprocess
    tree = tree or root
    raw = declared_command(root, gate)
    norm = normalize(raw)
    # The project's HEAD, as the hook records it: one notion of the judged
    # commit for every witness. Where the gate actually ran is treeSha.
    head = head_sha(root)
    tree_head = head_sha(tree)
    log = os.path.join(_bg_dir(root), f"{token}.log")
    _bg_write(root, {"token": token, "gate": gate, "status": "RUNNING",
                     "pid": os.getpid(), "started": started, "headSha": head,
                     "log": log})
    print(f"attest: running {gate} as {token} — collect it with "
          f"attest.py --await {token}", flush=True)
    with open(log, "wb") as out:
        code = subprocess.call([_bash(), "-c", raw], cwd=tree, stdin=subprocess.DEVNULL,
                               stdout=out, stderr=subprocess.STDOUT)
    with open(log, "rb") as fh:
        text = fh.read().decode("utf-8", "replace")
    row = {
        "attestId": "att_" + sha(f"{started}|{norm}|{code}|{token}")[:12],
        "gate": gate, "command": norm, "commandSha": sha(norm), "exit": code,
        "logSha256": sha(f"{text}\n---\n"), "headSha": head, "treeSha": tree_head,
        "started": started, "when": now(), "sessionId": "", "agent": "",
        "witness": "wrapper", "token": token, "nonce": nonce,
    }
    append_row(root, row)
    _bg_write(root, {"token": token, "gate": gate, "status": "DONE", "exit": code,
                     "attestId": row["attestId"], "started": started,
                     "finished": row["when"], "headSha": head, "log": log})
    print(f"attest: {gate} exit {code} -> {row['attestId']}", flush=True)
    return code


def _alive(pid, started=""):
    if pid is None:
        # Launched, not yet started: alive for as long as a start can take.
        try:
            t = datetime.datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
        except (TypeError, ValueError):
            return False
        return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() < 60
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return _alive_nt(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _alive_nt(pid):
    """Windows: is the process still running? OpenProcess, then its exit
    code (STILL_ACTIVE while it runs). Answering True for every pid left a
    crashed runner RUNNING forever and --await answering 3 to every call."""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return k.GetLastError() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001 - no probe: the timeout bounds the wait
        return True


def await_gate(root, ref, timeout):
    """Wait for a --run to finish. 0 done, 3 still running, 4 died, 2 unknown.

    Exit 0 means the verdict is known, not that the gate passed: the gate's
    own exit is printed with the attestId to cite. A caller loops on 3 with
    fresh foreground calls, each under the tool's cap.
    """
    rec = _bg_read(root, ref)
    if rec is None and IDENT.match(ref or ""):
        # A gate name: the newest run of it.
        d = _bg_dir(root)
        cands = []
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if fn.endswith(".json"):
                r = _bg_read(root, fn[:-5])
                if r and r.get("gate") == ref:
                    cands.append(r)
        rec = max(cands, key=lambda r: str(r.get("started"))) if cands else None
    if rec is None:
        print(f"attest: no background run '{ref}' — start one with --run <gate>",
              file=sys.stderr)
        return 2
    deadline = time.monotonic() + max(0, timeout)
    while True:
        rec = _bg_read(root, rec["token"]) or rec
        if rec.get("status") == "DONE":
            print(f"attest: {rec['gate']} exit {rec['exit']} -> {rec['attestId']}")
            return 0
        if not _alive(rec.get("pid"), rec.get("started")):
            # The runner writes DONE and exits: between the read above and
            # the probe it may have done both.
            again = _bg_read(root, rec["token"]) or rec
            if again.get("status") == "DONE":
                print(f"attest: {again['gate']} exit {again['exit']} -> {again['attestId']}")
                return 0
            print(f"attest: the runner for {rec['token']} ({rec.get('gate')}) is gone "
                  f"and left no verdict — nothing was attested; run the gate again",
                  file=sys.stderr)
            return 4
        if time.monotonic() >= deadline:
            print(f"attest: {rec.get('gate')} ({rec['token']}) still running since "
                  f"{rec.get('started')} — call --await again")
            return 3
        time.sleep(min(2.0, max(0.05, deadline - time.monotonic())))


# ------------------------------------------------------------ the forge
def _gh_json(path):
    import subprocess
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"gh api {path} exited {r.returncode}")
    return json.loads(r.stdout or "{}")


def _gh_all(path, key, pages=50):
    """Every item of a paginated GitHub listing, or an error.

    The API answers 30 items a page by default. A matrix CI with more check
    runs than that had its failing job on page two read as absent, and every
    reported context passing — a red commit attested green. So the listing
    is walked to its end and held to the total the API itself reports.
    """
    items, total = [], None
    for page in range(1, pages + 1):
        sep = "&" if "?" in path else "?"
        got = _gh_json(f"{path}{sep}per_page=100&page={page}")
        batch = got.get(key) or []
        if total is None and isinstance(got.get("total_count"), int):
            total = got["total_count"]
        items.extend(batch)
        if len(batch) < 100 or (total is not None and len(items) >= total):
            break
    if total is not None and len(items) < total:
        raise RuntimeError(f"{path} listed {len(items)} of {total} {key} — "
                           f"a partial listing is no verdict")
    return items


def forge_contexts(sha_):
    """{context: 'success'|'failure'|'pending'} for a commit, from GitHub.

    Commit statuses and check runs are both CI's word on a commit, and a
    repo may use either, so both are read. A context reported by both is
    the worse of the two.
    """
    rank = {"success": 0, "pending": 1, "failure": 2}
    out = {}

    def put(name, state):
        if name not in out or rank[state] > rank[out[name]]:
            out[name] = state
    for st in _gh_all(f"repos/{{owner}}/{{repo}}/commits/{sha_}/status", "statuses"):
        s_ = st.get("state")
        put(str(st.get("context")), "success" if s_ == "success"
            else "pending" if s_ == "pending" else "failure")
    for cr in _gh_all(f"repos/{{owner}}/{{repo}}/commits/{sha_}/check-runs", "check_runs"):
        if cr.get("status") != "completed":
            put(str(cr.get("name")), "pending")
        elif cr.get("conclusion") in ("success", "neutral", "skipped"):
            put(str(cr.get("name")), "success")
        else:
            put(str(cr.get("name")), "failure")
    return out


def ci_verdict(contexts, required):
    """0 green, 1 red, None undecided. Missing required contexts are pending."""
    want = required or sorted(contexts)
    if not want:
        return None
    states = [contexts.get(c, "pending") for c in want]
    if "failure" in states:
        return 1
    if "pending" in states:
        return None
    return 0


def resolve_target(pr=None, ref=None):
    """(sha, target) the forge names for a pull request or a branch.

    The forge chooses the commit, not the caller: a row minted for a sha the
    node picked could be CI's verdict on any older green commit, cited over
    a merge of something else.
    """
    if pr is not None:
        head = (_gh_json(f"repos/{{owner}}/{{repo}}/pulls/{int(pr)}").get("head") or {})
        return str(head.get("sha") or ""), f"pr:{int(pr)}"
    got = _gh_json(f"repos/{{owner}}/{{repo}}/commits/{ref}")
    return str(got.get("sha") or ""), f"ref:{ref}"


def attest_ci(root, sha_, wait=False, timeout=AWAIT_DEFAULT, pr=None, ref=None, nonce=""):
    """Mint a `forge` row for a commit's CI verdict. 0/1 minted, 3 undecided.

    With `pr` or `ref` the forge resolves the commit and the row records
    which; a bare `sha` is the caller's choice, and a prove:ci citation
    refuses such a row.
    """
    gates, findings = load_gates(root)
    for f in findings:
        print(f"attest: {f}", file=sys.stderr)
    if findings:
        return 2
    ci = load_ci(root)
    if not ci:
        print(f"attest: {GATES} declares no 'ci' section — nothing says which "
              f"forge or which contexts decide a merge", file=sys.stderr)
        return 2
    target = None
    if pr is not None or ref:
        if ref and not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", ref):
            print("attest: --ref must be a branch or tag name", file=sys.stderr)
            return 2
        try:
            sha_, target = resolve_target(pr, ref)
        except (OSError, RuntimeError, ValueError) as e:
            print(f"attest: could not resolve {'PR ' + str(pr) if pr is not None else ref} "
                  f"on the forge: {e} — nothing attested", file=sys.stderr)
            return 3
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha_ or ""):
        print("attest: name the commit with --pr, --ref or --sha", file=sys.stderr)
        return 2
    required = ci.get("contexts")
    deadline = time.monotonic() + max(0, timeout)
    while True:
        try:
            contexts = forge_contexts(sha_)
        except (OSError, RuntimeError, ValueError) as e:
            print(f"attest: could not read the forge's statuses for {sha_}: {e} — "
                  f"nothing attested", file=sys.stderr)
            return 3
        code = ci_verdict(contexts, required)
        if code is not None:
            break
        if not wait or time.monotonic() >= deadline:
            missing = [c for c in (required or []) if c not in contexts]
            pend = sorted(c for c, st in contexts.items() if st == "pending")
            print(f"attest: CI on {sha_} has no verdict yet"
                  + (f"; pending: {', '.join(pend)}" if pend else "")
                  + (f"; not reported: {', '.join(missing)}" if missing else "")
                  + " — nothing attested", file=sys.stderr)
            return 3
        time.sleep(15)
    when = now()
    canon = json.dumps(contexts, sort_keys=True)
    row = {
        "attestId": "att_" + sha(f"{when}|ci|{sha_}|{code}|{canon}")[:12],
        "gate": "ci", "command": f"ci:{sha_}", "commandSha": sha(f"ci:{sha_}"),
        "exit": code, "logSha256": sha(canon), "headSha": sha_, "when": when,
        "sessionId": "", "agent": "", "witness": "forge", "contexts": contexts,
        "target": target, "nonce": nonce,
    }
    append_row(root, row)
    print(f"attest: ci on {sha_} exit {code} -> {row['attestId']}")
    return code


def _each(value):
    if isinstance(value, list):
        for v in value:
            if isinstance(v, dict):
                yield v
    elif isinstance(value, dict):
        yield value


def _same_commit(a, b):
    a, b = str(a or "").strip(), str(b or "").strip()
    return len(a) >= 7 and len(b) >= 7 and (a.startswith(b) or b.startswith(a))


def _check_cited(rows, node, gate, r, launch=None, head=None, used=None):
    """Hold a `prove:` node to the attestation it cited.

    The node names an attestId; the hook wrote that row when the command
    actually ran. Every way the two can disagree is a different thing to
    say, and collapsing them would either accuse an honest executor of
    tampering or let a real one through.

    A row that matches is still only evidence about THIS run if the run is
    what minted it. Comparing the citation to its row alone let a node that
    never executed its gate cite any earlier row of the same gate and exit —
    another campaign's, weeks old — and be filed ATTESTED. So a matching row
    is also held to the run: minted no earlier than the launch stamp the
    compiled graph carries, about the commit the campaign's tree guard read
    (a forge row is about the commit it names instead), and backing one node
    only. Failing any of those is STALE: the declared verification did not
    happen in this run, which is not the same sentence as a contradiction.
    """
    claimed = r.get("exit")
    cited = r.get("attestId")
    base = {"node": node, "gate": gate, "claimedExit": claimed,
            "declared": True, "attestId": cited}
    if not cited:
        return {**base, "status": "UNATTESTED",
                "detail": (f"declared prove:{gate} but cited no attestId — an "
                           f"exit code nothing witnessed")}
    row = next((a for a in rows if a.get("attestId") == cited), None)
    if row is None:
        return {**base, "status": "MISMATCH",
                "detail": (f"cites attestation {cited}, which is not in the log. "
                           f"Nothing recorded that execution.")}
    if row.get("gate") != gate:
        return {**base, "status": "MISMATCH",
                "detail": (f"cites {cited}, which attests gate "
                           f"'{row.get('gate')}', not the declared '{gate}'")}
    if row.get("exit") != claimed:
        return {**base, "status": "MISMATCH",
                "detail": (f"claims exit {claimed} citing {cited}, but the hook "
                           f"recorded exit {row.get('exit')} for that run")}
    if r.get("logSha256") and r["logSha256"] != row.get("logSha256"):
        return {**base, "status": "MISMATCH",
                "detail": (f"cites {cited} but quotes a different log than the "
                           f"one recorded for it")}
    since = launch.get("since") if isinstance(launch, dict) else None
    if not since:
        return {**base, "status": "STALE",
                "detail": (f"cites {cited}, but the run carries no launch stamp "
                           f"(args._launch) — nothing binds the citation to this "
                           f"run, so any earlier row of '{gate}' would pass")}
    minted = str(row.get("started") or row.get("when") or "")
    if minted < str(since):
        return {**base, "status": "STALE",
                "detail": (f"cites {cited}, minted {minted or 'at an unknown time'}, "
                           f"before this run launched ({since}) — an execution from "
                           f"another run, not this node's")}
    nonce = launch.get("nonce") if isinstance(launch, dict) else None
    if nonce and row.get("nonce") != nonce:
        return {**base, "status": "STALE",
                "detail": (f"cites {cited}, which "
                           + (f"another run minted (nonce {row['nonce']})" if row.get("nonce")
                              else "carries no run nonce")
                           + f" — this run's is {nonce}; a gate run for this node passes "
                           f"FPL_ATTEST_NONCE (or --nonce) so its row names the run")}
    if row.get("witness") == "forge":
        # CI's verdict is about the commit the forge named, and the claim must
        # say which commit that is, so the merge it guards can be pinned to it
        # (`gh pr merge --match-head-commit`). A sha the node chose could be
        # any older green commit.
        if not row.get("target"):
            return {**base, "status": "STALE",
                    "detail": (f"cites {cited}, CI on {str(row.get('headSha'))[:12]}, a commit "
                               f"the node named itself — mint it with --pr or --ref so the "
                               f"forge names the tip")}
        if not r.get("sha"):
            return {**base, "status": "STALE",
                    "detail": (f"cites {cited} but does not name the commit it judged (sha) — "
                               f"the merge cannot be pinned to CI's verdict")}
        if not _same_commit(r["sha"], row.get("headSha")):
            return {**base, "status": "MISMATCH",
                    "detail": (f"claims CI on {str(r['sha'])[:12]} citing {cited}, which the "
                               f"forge recorded for {str(row.get('headSha'))[:12]}")}
    if (head and row.get("witness") != "forge" and row.get("headSha")
            and not _same_commit(row["headSha"], head)):
        return {**base, "status": "STALE",
                "detail": (f"cites {cited}, which ran on commit "
                           f"{str(row['headSha'])[:12]}, but this campaign ran on "
                           f"{str(head)[:12]}")}
    if used is not None:
        if cited in used:
            return {**base, "status": "STALE",
                    "detail": (f"cites {cited}, which already backs node "
                               f"'{used[cited]}' — one execution cannot verify two "
                               f"nodes")}
        used[cited] = node
    return {**base, "status": "ATTESTED",
            "detail": f"exit {claimed} attested {cited}"
                      + (f" ({row.get('witness')})" if row.get("witness") not in (None, "hook")
                         else "")}


def verify_claims(root, summary):
    """Cross-check a run summary's claimed gate exits against the log.

    Returns a list of checks, one per node result that claims to have run a
    declared gate. Nodes that ran something else are not checked and not
    reported — this speaks only about commands the repo itself declared.
    """
    gates, findings = load_gates(root)
    # A CI-only manifest ({"gates": {}, "ci": ...}) still has a witness to
    # hold prove:ci citations to; returning early filed them unchecked.
    if findings or (not gates and not load_ci(root)):
        return [], findings
    rows = read(root)
    results = (summary or {}).get("results") or {}
    contracts = (summary or {}).get("contracts") or {}
    # Which nodes declared `verify: prove:<gate>`. Those opted into being
    # held to the hook's record; everything else is still only observed.
    proved = (summary or {}).get("prove") or {}
    # What binds a citation to this run: the launch stamp and the commit
    # the tree guard's first reading saw.
    launch = (summary or {}).get("launch")
    head = (((summary or {}).get("tree") or {}).get("baseline") or {}).get("head")
    used = {}
    checks = []
    for node, value in results.items():
        for r in _each(value):
            c = contracts.get(node)
            if c not in (None, "HarnessCheckV1", "ExecutionV1"):
                continue
            declared = proved.get(node)
            if declared:
                # An ExecutionV1 cites its attestation directly, so the check
                # is against the id rather than a command string it never
                # carries.
                checks.append(_check_cited(rows, node, declared, r, launch, head, used))
                continue
            if "exit" not in r or "command" not in r:
                continue
            gate = gate_for(gates, r.get("command"))
            if not gate:
                continue
            claimed = r.get("exit")
            mine = [a for a in rows if a.get("commandSha") == sha(normalize(r["command"]))]
            hit = next((a for a in mine if a.get("exit") == claimed), None)
            if hit:
                checks.append({
                    "node": node, "gate": gate, "claimedExit": claimed,
                    "status": "ATTESTED", "attestId": hit.get("attestId"),
                    "detail": f"exit {claimed} attested {hit.get('attestId')}"})
            elif mine:
                newest = mine[-1]
                checks.append({
                    "node": node, "gate": gate, "claimedExit": claimed,
                    "status": "MISMATCH", "attestId": newest.get("attestId"),
                    "detail": (f"node claims exit {claimed} for gate '{gate}', but no "
                               f"attested run of it exited {claimed}; the most recent "
                               f"exited {newest.get('exit')} "
                               f"({newest.get('attestId')} at {newest.get('when')})")})
            else:
                checks.append({
                    "node": node, "gate": gate, "claimedExit": claimed,
                    "status": "UNATTESTED", "attestId": None,
                    "detail": (f"gate '{gate}' has no attested execution at all — the "
                               f"exit code rests on the node's own report")})
    return checks, []


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None,
                    help="the project (manifest and log); default: the project the "
                         "current directory belongs to, a linked worktree's included")
    ap.add_argument("--tree", default=None,
                    help="with --run: the tree to run the gate in (default: the "
                         "current directory when --root is not given, else --root)")
    ap.add_argument("--detach", action="store_true",
                    help="with --run: start the gate as its own background process")
    ap.add_argument("--token", help=argparse.SUPPRESS)
    ap.add_argument("--timeout", type=int, default=AWAIT_DEFAULT,
                    help="seconds --await (or --ci --wait) blocks before answering")
    ap.add_argument("--sha", help="with --ci: the commit whose statuses decide")
    ap.add_argument("--wait", action="store_true",
                    help="with --ci: poll until every context has a verdict")
    ap.add_argument("--pr", type=int, help="with --ci: the pull request whose head the forge names")
    ap.add_argument("--ref", help="with --ci: the branch or tag whose tip the forge names")
    ap.add_argument("--nonce", default="",
                    help="with --run or --ci: the run nonce from args._launch")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--record", action="store_true")
    g.add_argument("--list", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--run", metavar="GATE")
    g.add_argument("--await", dest="await_", metavar="TOKEN")
    g.add_argument("--ci", action="store_true")
    g.add_argument("--stamp", action="store_true",
                   help="print a launch stamp for args._launch")
    g.add_argument("--last", metavar="GATE",
                   help="print the newest attested row of GATE (with --nonce: of this run)")
    a = ap.parse_args()
    tree = a.tree
    if a.root is None:
        a.root = project_root(".")
        tree = tree or "."

    if a.nonce and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", a.nonce):
        print("attest: --nonce must be 1-64 letters, digits, '-' or '_'", file=sys.stderr)
        return 2
    if a.stamp:
        print(json.dumps(stamp()))
        return 0
    if a.run:
        return run_gate(a.root, a.run, a.detach, a.token, a.nonce, tree)
    if a.last:
        mine = [r for r in read(a.root) if r.get("gate") == a.last
                and (not a.nonce or r.get("nonce") == a.nonce)]
        if not mine:
            print(f"attest: no attested run of '{a.last}'"
                  + (f" for nonce {a.nonce}" if a.nonce else "")
                  + " — a failed gate is not attested by the hook; run it again, or "
                    "through attest.py --run", file=sys.stderr)
            return 1
        r = mine[-1]
        print(f"{r['attestId']} gate {r['gate']} exit {r['exit']} when {r.get('when')} "
              f"witness {r.get('witness') or 'hook'} nonce {r.get('nonce') or '-'}")
        return 0
    if a.await_:
        return await_gate(a.root, a.await_, a.timeout)
    if a.ci:
        return attest_ci(a.root, a.sha, a.wait, a.timeout, a.pr, a.ref, a.nonce)

    if a.list:
        gates, findings = load_gates(a.root)
        for f in findings:
            print(f"attest: {f}", file=sys.stderr)
        if findings:
            return 1
        if not gates:
            print(f"attest: no {GATES} in this repo — attestation is dormant")
            return 0
        rows = read(a.root)
        print(f"attest: {len(gates)} declared gate(s), {len(rows)} attested execution(s)")
        for name, cmd in sorted(gates.items()):
            mine = [r for r in rows if r.get("gate") == name]
            last = (f"last {mine[-1]['when']} exit {mine[-1]['exit']} -> "
                    f"{mine[-1].get('attestId')}") if mine else "never run"
            print(f"  {name:<16} {cmd}")
            print(f"  {'':<16} {len(mine)} run(s), {last}")
        ci = load_ci(a.root)
        if ci:
            mine = [r for r in rows if r.get("gate") == "ci"]
            last = (f"last {mine[-1]['when']} exit {mine[-1]['exit']} on "
                    f"{str(mine[-1].get('headSha'))[:12]}") if mine else "never queried"
            print(f"  {'ci':<16} {ci['forge']} statuses: "
                  f"{', '.join(ci.get('contexts') or ['every reported context'])}")
            print(f"  {'':<16} {len(mine)} verdict(s), {last}")
        return 0

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(f"attest: input is not JSON: {e}", file=sys.stderr)
        return 1

    if a.record:
        row, findings = record(a.root, payload)
        for f in findings:
            print(f"attest: {f}", file=sys.stderr)
        if findings:
            return 1
        if row:
            print(f"attest: {row['gate']} exit {row['exit']} -> {row['attestId']}")
        return 0

    checks, findings = verify_claims(a.root, payload)
    for f in findings:
        print(f"attest: {f}", file=sys.stderr)
    if findings:
        return 1
    if not checks:
        print("attest: no declared gate claims in this summary")
        return 0
    bad = 0
    for c in checks:
        print(f"  [{c['status']}] {c['node']}: {c['detail']}")
        bad += c["status"] in ("MISMATCH", "STALE")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
