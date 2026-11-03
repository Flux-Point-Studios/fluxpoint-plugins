// One sanctioned path out of the mailbox.
//
// A repo can hold a correct sender — one module that runs the prose gate,
// checks recipients, threads the reply and reads the receipt back — and still
// ship a broken message, because nothing stops a second script from calling the
// mail API directly. A gate the author can walk past is advice.
//
// This is a PreToolUse hook: the harness inspects the command BEFORE it runs and
// refuses anything that would reach a mail-send endpoint outside the sender the
// repo declares in .send-chokepoint.json. With no config it does nothing.
import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT = fileURLToPath(new URL("../scripts/send-chokepoint.mjs", import.meta.url));

// A real sender: talks to a provider host AND uses a send verb.
const SENDER_BODY = `
import urllib.request
GRAPH = "https://graph.microsoft.com/v1.0"
def go(tok, box, draft):
    urllib.request.urlopen(GRAPH + "/users/%s/messages/%s/send" % (box, draft))
`;
// A reader: same host, no send verb. Reading mail is not sending it.
const READER_BODY = `
import urllib.request
GRAPH = "https://graph.microsoft.com/v1.0"
def inbox(tok, box):
    urllib.request.urlopen(GRAPH + "/users/%s/mailFolders/inbox/messages" % box)
`;

/** A repo that has opted in, with `sender` as its one sanctioned entry point. */
function repo(config = { sender: "outbound_mail.py" }) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "choke-"));
  if (config) fs.writeFileSync(path.join(dir, ".send-chokepoint.json"), JSON.stringify(config));
  return dir;
}

function hook(command, cwd, toolName = "Bash", env = {}) {
  const payload = JSON.stringify({ tool_name: toolName, tool_input: { command }, cwd });
  // Claude Code runs the hook in the session's directory, so the hook here runs in the test's own repo, never in
  // whatever tree the suite was started from. It also runs the command when the hook outlives the 15 s timeout
  // hooks.json gives it, so a hook killed here has allowed the line.
  return spawnSync("node", [SCRIPT, "--hook"], { input: payload, encoding: "utf8", cwd,
    env: { ...process.env, ...env }, timeout: 15000 });
}

function file(dir, name, body) {
  const p = path.join(dir, name);
  fs.writeFileSync(p, body);
  return p;
}

/** One test per command, each in a fresh repo where blast.py reaches a send endpoint, other.py only reads mail and
 *  thin.py imports blast, asserting the hook's exit status when `tool` runs the command. */
function expectEach(status, title, commands, tool = "Bash") {
  for (const command of commands) {
    test(`${title}: ${command.includes("\n") ? JSON.stringify(command) : command}`, () => {
      const dir = repo();
      file(dir, "blast.py", SENDER_BODY);
      file(dir, "other.py", READER_BODY);
      file(dir, "thin.py", "import blast\n");
      assert.equal(hook(command, dir, tool).status, status);
    });
  }
}

// ------------------------------------------------------------- opt-in default
test("with no config the hook allows everything", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "choke-noconf-"));
  const p = file(dir, "blast.py", SENDER_BODY);
  assert.equal(hook(`python "${p}"`, dir).status, 0,
               "a plugin that blocked every repo by default would be switched off");
});

test("a config with no sender declared leaves the chokepoint off, and says so", () => {
  const dir = repo({ exempt: ["somewhere"] });
  const p = file(dir, "blast.py", SENDER_BODY);
  const r = hook(`python "${p}"`, dir);
  assert.equal(r.status, 0);
  assert.match(r.stdout, /no "sender" declared/);
});

test("malformed config is reported and ignored, never obeyed", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "choke-bad-"));
  fs.writeFileSync(path.join(dir, ".send-chokepoint.json"), "{ not json");
  const p = file(dir, "blast.py", SENDER_BODY);
  const r = hook(`python "${p}"`, dir);
  assert.equal(r.status, 0);
  assert.match(r.stdout, /not valid JSON/);
});

test("the verdicts do not depend on the directory the suite is run from", () => {
  // The laptop runs this suite inside a tree with a real .send-chokepoint.json above it.
  const dir = repo();
  const env = { ...process.env };
  delete env.NODE_TEST_CONTEXT;
  const r = spawnSync(process.execPath, ["--test", "--test-name-pattern=malformed config is reported",
    fileURLToPath(import.meta.url)], { cwd: dir, env, encoding: "utf8" });
  assert.equal(r.status, 0, r.stdout);
});

test("an unknown config key is reported without disabling the rest", () => {
  const dir = repo({ sender: "outbound_mail.py", frobnicate: true });
  const p = file(dir, "blast.py", SENDER_BODY);
  const r = hook(`python "${p}"`, dir);
  assert.equal(r.status, 2, "the declared sender still takes effect");
  assert.match(r.stdout, /unknown key "frobnicate"/);
});

// ------------------------------------------------------------------ the block
test("an ordinary command is never touched", () => {
  const dir = repo();
  for (const cmd of ["ls -la", "git status", "node --test tests/x.test.mjs",
                     "python -m pytest tests -q"]) {
    assert.equal(hook(cmd, dir).status, 0, `blocked an innocent command: ${cmd}`);
  }
});

test("an ad-hoc script that reaches a send endpoint is BLOCKED", () => {
  const dir = repo();
  const p = file(dir, "blast.py", SENDER_BODY);
  const r = hook(`python "${p}"`, dir);
  assert.equal(r.status, 2, `${r.stdout}${r.stderr}`);
  assert.match(r.stderr, /outbound_mail\.py/, "the refusal must name the sanctioned path");
  assert.match(r.stderr, /blast\.py/, "and the offending file");
});

test("the declared sender itself is allowed", () => {
  const dir = repo();
  const p = file(dir, "outbound_mail.py", SENDER_BODY);
  assert.equal(hook(`python "${p}" draft.txt --to a@example.com`, dir).status, 0);
});

test("an exempt directory is allowed to send", () => {
  const dir = repo({ sender: "outbound_mail.py", exempt: ["services/mailer"] });
  const deep = path.join(dir, "services", "mailer");
  fs.mkdirSync(deep, { recursive: true });
  const p = file(deep, "worker.py", SENDER_BODY);
  assert.equal(hook(`python "${p}"`, dir).status, 0);
});

test("an inline one-liner that reaches a send endpoint is BLOCKED", () => {
  const dir = repo();
  const r = hook(`python -c "import urllib.request;`
    + ` urllib.request.urlopen('https://graph.microsoft.com/v1.0/users/x/sendMail')"`, dir);
  assert.equal(r.status, 2, `${r.stdout}${r.stderr}`);
});

test("reading mail is not sending mail", () => {
  const dir = repo();
  const p = file(dir, "read_inbox.py", READER_BODY);
  assert.equal(hook(`python "${p}"`, dir).status, 0,
               "blocking readers is how a gate earns being removed");
});

test("a provider host with no send verb is allowed", () => {
  const dir = repo();
  assert.equal(hook(`curl -s "https://graph.microsoft.com/v1.0/users/x/messages"`, dir).status, 0);
});

test("a send verb with no provider host is allowed", () => {
  const dir = repo();
  assert.equal(hook(`curl -s https://example.com/api/send`, dir).status, 0);
});

test("a repo can declare its own provider", () => {
  const dir = repo({ sender: "outbound_mail.py", providers: ["mail.internal.example"] });
  const p = file(dir, "blast.py",
    `import requests\nrequests.post("https://mail.internal.example/v1/mail.send", json={})\n`);
  assert.equal(hook(`python "${p}"`, dir).status, 2);
});

test("a path in the command that does not exist is not a reason to block", () => {
  const dir = repo();
  assert.equal(hook("python /nope/does-not-exist.py", dir).status, 0);
});

test("a malformed payload never blocks the session", () => {
  const r = spawnSync("node", [SCRIPT, "--hook"], { input: "not json", encoding: "utf8" });
  assert.equal(r.status, 0, "a broken hook must fail open on its own input");
});

test("PowerShell is covered too, not just Bash", () => {
  const dir = repo();
  const p = file(dir, "blast.py", SENDER_BODY);
  assert.equal(hook(`python "${p}"`, dir, "PowerShell").status, 2);
});

// ------------------------------------------------------------- relative paths
// The shape people actually type. A bare token resolved against the HOOK's cwd
// instead of the command's finds nothing and lets the send through.
test("a relative path after cd is resolved against the cd target", () => {
  const dir = repo();
  file(dir, "blast.py", SENDER_BODY);
  assert.equal(hook(`cd "${dir}" && python blast.py`, dir).status, 2);
});

test("a relative path is resolved against the payload cwd", () => {
  const dir = repo();
  file(dir, "blast.py", SENDER_BODY);
  assert.equal(hook("python blast.py", dir).status, 2);
});

test("cd into one directory does not make an unrelated absolute path relative", () => {
  const dir = repo();
  const sender = file(dir, "blast.py", SENDER_BODY);
  const other = fs.mkdtempSync(path.join(os.tmpdir(), "choke-other-"));
  assert.equal(hook(`cd "${other}" && python "${sender}"`, dir).status, 2,
               "an absolute path must still be judged on its own");
});

// ------------------------------------------------------ the thin-script shape
// The scripts that bypass a sender import a mail helper and call it, so the
// provider host never appears in the file the command names. Self-contained
// fixtures pass while every real script sails through.
test("a thin script that imports a local sender is BLOCKED", () => {
  const dir = repo();
  file(dir, "mailer.py", SENDER_BODY);
  const thin = file(dir, "notify.py", "import mailer as m\nm.go(1, 2, 3)\n");
  const r = hook(`python "${thin}"`, dir);
  assert.equal(r.status, 2, `${r.stdout}${r.stderr}`);
  assert.match(r.stderr, /mailer\.py/, "name the module that does the sending");
});

test("from-import of a local sender is caught too", () => {
  const dir = repo();
  file(dir, "mailer.py", SENDER_BODY);
  const thin = file(dir, "blast.py", "from mailer import go\ngo(1, 2, 3)\n");
  assert.equal(hook(`python "${thin}"`, dir).status, 2);
});

test("importing a local module that does NOT send is still allowed", () => {
  const dir = repo();
  file(dir, "reader.py", READER_BODY);
  const thin = file(dir, "check.py", "import reader\nreader.inbox(1, 2)\n");
  assert.equal(hook(`python "${thin}"`, dir).status, 0,
               "following imports must not turn readers into senders");
});

test("a thin script importing the SANCTIONED sender is allowed", () => {
  const dir = repo();
  file(dir, "outbound_mail.py", SENDER_BODY);
  const thin = file(dir, "verify.py", "import outbound_mail\noutbound_mail.check()\n");
  assert.equal(hook(`python "${thin}"`, dir).status, 0);
});

// ------------------------------------------------- impersonation and discovery
// Three ways the chokepoint could be walked past, all found by adversarial
// review of the first cut. Each one is a file that DOES send being allowed to
// run, which is the only failure that matters here.

test("a file merely NAMED like the sender cannot impersonate it", () => {
  // "sender": "outbound_mail.py" must mean THAT file, not any file with that
  // basename. Matching on basename alone lets /tmp/outbound_mail.py send.
  const dir = repo({ sender: "tools/outbound_mail.py" });
  const tools = path.join(dir, "tools");
  fs.mkdirSync(tools, { recursive: true });
  file(tools, "outbound_mail.py", SENDER_BODY);
  const elsewhere = fs.mkdtempSync(path.join(os.tmpdir(), "choke-imposter-"));
  const imposter = file(elsewhere, "outbound_mail.py", SENDER_BODY);
  assert.equal(hook(`python "${imposter}"`, dir).status, 2,
               "an unrelated file with the sender's name must not be allowed");
  assert.equal(hook(`python "${path.join(tools, "outbound_mail.py")}"`, dir).status, 0,
               "the real declared sender must still run");
});

test("a quoted path containing spaces is still read", () => {
  // Windows user directories routinely contain spaces. A token regex that
  // stops at the space matches only the tail, finds no file, and allows it.
  const dir = repo();
  const spaced = path.join(dir, "space dir");
  fs.mkdirSync(spaced, { recursive: true });
  const p = file(spaced, "blast.py", SENDER_BODY);
  assert.equal(hook(`python "${p}"`, dir).status, 2, "a spaced path must be judged");
});

test("the config is found from the command's directory, not only the session's", () => {
  // `cd repo && python blast.py` run from a parent directory: the config lives
  // in repo/, so searching only the payload cwd finds none and the hook exits
  // before the cd is ever considered.
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "choke-parent-"));
  const inner = path.join(parent, "repo");
  fs.mkdirSync(inner, { recursive: true });
  fs.writeFileSync(path.join(inner, ".send-chokepoint.json"),
                   JSON.stringify({ sender: "outbound_mail.py" }));
  file(inner, "blast.py", SENDER_BODY);
  assert.equal(hook(`cd "${inner}" && python blast.py`, parent).status, 2,
               "the cd target's config must govern the command");
});

// ------------------------------------------------- reading a sender is not sending
// A command made only of read-only tools (print, search, hash, compare, list) cannot
// run the file it names, so it is never judged; the operator must be able to read,
// grep and hash the very files the gate protects. Anything that can run a file keeps
// the full judgment.
expectEach(0, "a read-only command on a sender is allowed", [
  "sha256sum blast.py",
  "cat blast.py",
  "head -n 40 blast.py | grep -n send",
  "grep -n sendMail blast.py",
  "wc -l blast.py && sha256sum blast.py",
  "timeout 60 sha256sum blast.py",
  "diff blast.py other.py",
  'grep -rn "graph.microsoft.com/v1.0/me/sendMail" .',
  'echo "$(sha256sum blast.py)"',
  "wc -l blast.py;",
  "cat blast.py > copy.txt",
  "cat blast.py > copy.txt 2>&1",
  "sha256sum blast.py 2>&1 | uniq",
  "cat blast.py &>/dev/null",
  "cat blast.py &>> log.txt",
  "grep -n send blast.py >&2",
]);

expectEach(2, "a command that can run the sender keeps the full judgment", [
  "cat blast.py | python3",
  'python3 -c "$(cat blast.py)"',
  "cat blast.py && python blast.py",
  "sha256sum blast.py; python3 blast.py",
  "timeout 60 python3 blast.py",
  "LC_ALL=C python3 blast.py",
  "sed -n 1p blast.py",
  "awk 1 blast.py",
  "echo $(python3 blast.py)",
  "echo `python3 blast.py`",
  "git submodule foreach python3 blast.py",
  "python3 blast.py 2>&1",
  "cat blast.py 2>&1 & python3 blast.py",
  "sha256sum blast.py >&2 && python3 blast.py",
  "cat blast.py &>/dev/null; python3 blast.py",
  "&>cat python3 blast.py",
]);

// A word the reader looks up must not reach an Object method: the hook fails open on its own errors, so a lookup
// that throws would allow the whole line, sender and all.
expectEach(2, "a word named like an Object method does not open the gate", [
  "toString blast.py && python3 blast.py",
  "valueOf x; python3 blast.py",
  "hasOwnProperty a b | python3 blast.py",
]);

// ------------------------------------------------- the exemption trusts nothing that runs a program
// Every git subcommand honours repository configuration an agent can write: core.fsmonitor runs on status,
// diff.external and textconv drivers on diff, show and log -p, core.pager under --paginate.
expectEach(2, "git is never exempt", [
  "git status -- blast.py",
  "git diff -- blast.py",
  "git log -p -- blast.py",
  "git --paginate show HEAD -- blast.py",
  "git -C . diff -- blast.py",
  "git grep --open-files-in-pager=python3 send -- blast.py",
]);

// rg --pre, ugrep --filter and sort --compress-program name a program to run; less and more run LESSOPEN
// filters and shell escapes; a path-qualified name is whatever file sits at that path.
expectEach(2, "a tool that can run a program is never exempt", [
  "rg --pre python3 . blast.py",
  'ugrep --filter="py:python3 %" x blast.py',
  "sort --compress-program=python3 blast.py",
  "less blast.py",
  "more blast.py",
  "./cat blast.py",
  "bin/sha256sum blast.py",
]);

// grep in the Claude Code shell runs ugrep, whose --filter, --pager and --view name a command and whose --config or
// --- loads a file that can; GNU diff --paginate runs pr, and file -z runs decompressors found on PATH. The exemption
// refuses these options itself, in any abbreviation, rather than lean on the host's grep wrapper, and refuses an
// expansion or a glob among these tools' arguments, which could produce one.
expectEach(2, "an option that runs a program is judged", [
  'grep --filter="py:python3 %" x blast.py',
  "egrep --filt=py:python3 x blast.py",
  "fgrep --pager=python3 x blast.py",
  "grep --view=python3 x blast.py",
  "grep --config x blast.py",
  "grep ---cfg x blast.py",
  "grep $(echo --filter=py:python3) x blast.py",
  "grep -n send blast.py *",
  "diff -l blast.py other.py",
  "diff -ul blast.py other.py",
  "diff --pag blast.py other.py",
  "file -z blast.py",
  "file -bZ blast.py",
  "file --uncompress blast.py",
]);

// ugrep's -Q starts a TUI that runs $PAGER or the --view command on a key press, and --save-config writes a file
// wherever it is told. xxd and uniq write their second operand, so each of their arguments must name an ordinary file
// or none, as a redirection target must.
expectEach(2, "an option or operand that prompts or writes is judged", [
  "grep -Q x blast.py",
  "grep -nQ x blast.py",
  "grep --query x blast.py",
  "grep --save-config=/proc/self/fd/1 x blast.py",
  "xxd blast.py /dev/ttyS0",
  "xxd -r blast.py /proc/self/fd/1",
  "uniq blast.py /dev/ttyS0",
  "cat blast.py > /sys/kernel/notes",
]);

expectEach(0, "xxd and uniq keep the exemption on ordinary files", [
  "xxd blast.py",
  "xxd -c 16 blast.py | head",
  "xxd blast.py dump.txt",
  "uniq -c blast.py",
  "uniq blast.py out.txt",
  "grep --quiet send blast.py",
]);

expectEach(0, "grep, diff and file keep the exemption with their other options", [
  "grep --color=never --count send blast.py",
  "grep --file=patterns.txt blast.py",
  "diff -u blast.py other.py",
  "diff --unified=3 --label=a blast.py other.py",
  "file -b --mime-type blast.py",
]);

// A redirection opens its target, and bash opens /dev/tcp/HOST/PORT and /dev/udp/HOST/PORT as sockets: data needs no
// interpreter to leave. A target is exempt only when it is an ordinary file or /dev/null, /dev/stdin, /dev/stdout,
// /dev/stderr, written out literally; an expansion could name anything.
expectEach(2, "a redirection to anything but an ordinary file is judged", [
  "cat blast.py > /dev/tcp/127.0.0.1/25",
  "cat blast.py >/dev/udp/127.0.0.1/25",
  "cat blast.py 1<>/dev/tcp/127.0.0.1/25",
  "cat blast.py &> /dev/tcp/127.0.0.1/25",
  "cat blast.py >& /dev/tcp/127.0.0.1/25",
  'cat blast.py >"/dev/tcp/127.0.0.1/25"',
  "cat blast.py >/dev/tc''p/127.0.0.1/25",
  "cat blast.py > /dev//tcp/127.0.0.1/25",
  "cat blast.py > ../../../../../../../../../dev/tcp/127.0.0.1/25",
  "cat blast.py > /dev/ttyS0",
  "cat blast.py > /proc/self/fd/1",
  "cat < /dev/tcp/127.0.0.1/25 blast.py",
  "cat blast.py > ${T:-/dev/tcp/127.0.0.1/25}",
  "cat blast.py > $T",
  "cat blast.py > ~root/out.txt",
  "> cat python3 blast.py",
  "2> cat python3 blast.py",
]);

// bash reads the `-` after `<&` or `>&` as a token of its own that closes the descriptor, so the text after it starts a
// new word: `<&-python3 <&-sender.py` runs python3 on sender.py with stdin closed. The full judgment reads that path
// without the dash too.
expectEach(2, "a descriptor close does not hide the word after it", [
  "<&-python3 <&-blast.py",
  "<&-python3 <&-'blast.py'",
  ">&-python3 >&-blast.py",
  "2>&-python3 2>&-blast.py",
  "cat other.py <&-; <&-python3 <&-blast.py",
  "<&-curl <&--X <&-POST <&-https://graph.microsoft.com/v1.0/me/sendMail",
]);

expectEach(0, "closing a descriptor keeps the exemption", [
  "cat blast.py <&-",
  "sha256sum blast.py 2>&- | uniq",
]);

// bash expands the word after `>&`, and when the result is neither a descriptor number nor `-` it expands that result
// again as the file to open, so quoting does not keep a substitution in it from running: `>&'$(cmd)'` runs cmd. A
// descriptor other than 0, 1 and 2 was inherited from whatever started the shell, and may be a socket.
expectEach(2, "a >& target is read as bash expands it", [
  "cat blast.py >&a'$(python3 blast.py)'",
  "cat blast.py >&2x'${x:-$(python3 blast.py)}'",
  'cat other.py >&"\\$(python3 blast.py)"',
  "cat blast.py >&12",
  "cat blast.py 2>&10",
  "cat blast.py >&3-",
]);

expectEach(0, "a >& to a standard descriptor or an ordinary file keeps the exemption", [
  "cat blast.py >&2",
  "grep -n send blast.py 2>&1 >&2 <&0",
  "cat blast.py >& copy.txt",
]);

// Quoting is read as bash reads it: a quoted ; or | separates nothing, and quoting cannot hide an option or an
// assignment from the reader.
expectEach(0, "a quoted separator or an ordinary redirection keeps the exemption", [
  'grep -E "send|reply" blast.py',
  'grep -n "a;b" blast.py > hits.txt',
  "cat blast.py > /dev/null 2>/dev/stderr",
  "grep -c send < blast.py >> counts.txt",
  "cat blast.py > sub/../copy.txt",
  "c''at blast.py",
]);

expectEach(2, "quoting and grouping cannot hide a command from the reader", [
  'LESSOPEN="|python3 %s" cat blast.py',
  "printf -''v PATH %s ./bin; cat blast.py",
  "p''rintf -v PATH %s ./bin; cat blast.py",
  'grep "x" blast.py; python3 blast.py',
  "cat 'blast.py' | python3",
  "cat blast.py > >(python3)",
  "diff <(cat blast.py) <(python3 blast.py)",
  "(python3 blast.py)",
  "{ python3 blast.py; }",
  "cat blast.py; $X blast.py",
  "echo $((1)); python3 blast.py",
]);

// env -S splits its value into a new command line, so an env prefix with any option is judged. timeout and nice
// have no option that runs a program, and keep the exemption with the options they document.
expectEach(2, "an env prefix with an option is judged", [
  "env -Spython3 cat blast.py",
  "env --split-string=python3 cat blast.py",
  "env -i cat blast.py",
  "env -- cat blast.py",
]);

// An assignment can change what any later word runs: LD_PRELOAD loads code into cat itself, PATH picks which file a
// bare name is, LESSOPEN and GIT_* name programs. printf -v and a {name}> redirection assign as well.
expectEach(2, "an assignment makes the command judged", [
  "LD_PRELOAD=./shim.so cat blast.py",
  'LESSOPEN="|python3 %s" cat blast.py',
  "LC_ALL=C grep -c graph blast.py",
  "PATH=./bin; cat blast.py",
  "env PATH=./bin cat blast.py",
  "printf -v PATH %s ./bin; cat blast.py",
  "true {PATH}>x.txt; cat blast.py",
]);

expectEach(0, "an assignment-shaped argument is not an assignment", [
  "echo PATH=./bin blast.py",
  'printf "%s\\n" "$(sha256sum blast.py)"',
  "printf -- -v blast.py",
]);

expectEach(0, "a timeout, nice or bare env prefix keeps the exemption", [
  "env cat blast.py",
  "nice -n 5 sha256sum blast.py",
  "nice -n5 cat blast.py",
  "timeout -k 5 -s KILL 60 sha256sum blast.py",
  "timeout --foreground --kill-after=5 60s cat blast.py",
  "nice timeout 60 env cat blast.py",
]);

// ------------------------------------------------- a heredoc body is data
// A line whose commands are all read-only is not judged when it writes a heredoc:
// writing a script that NAMES a sender is not running it. Any other line is judged
// on its full text, bodies included, and a heredoc that bash would expand, or that
// cannot be delimited for certain, leaves the line judged.
const INLINE_SEND = 'urllib.request.urlopen("https://graph.microsoft.com/v1.0/me/sendMail")';
expectEach(0, "a heredoc into a read-only command is not judged", [
  "cat > probe.py <<EOF\n# provenance: thin.py\nimport other\nEOF",
  "cat > probe.py <<EOF\n# copied from blast.py\nEOF",
  "cat > probe.py <<'EOF'\n# copied from blast.py\nEOF",
  'cat > probe.py <<"EOF"\n# copied from blast.py\nEOF',
  "cat > probe.py <<-EOF\n\t# copied from blast.py\n\tEOF",
  "cat > a.py <<A; cat > b.py <<B\n# blast.py\nA\n# thin.py\nB",
  `cat > notes.md <<EOF\n${INLINE_SEND}\nEOF`,
  "cat<<EOF > probe.py\n# copied from blast.py\nEOF",
  "cat <<<x > a.txt; cat > probe.py <<EOF\n# copied from blast.py\nEOF",
  'echo "say \\"hi\\"" > a.txt; cat > probe.py <<EOF\n# copied from blast.py\nEOF',
  "echo 'c:\\' > a.txt; cat > probe.py <<EOF\n# copied from blast.py\nEOF",
]);

expectEach(2, "a heredoc that runs, or cannot be delimited, keeps the full judgment", [
  `python3 <<EOF\n${INLINE_SEND}\nEOF`,
  `cat <<EOF | python3\n${INLINE_SEND}\nEOF`,
  `cat > p.py <<EOF\n${INLINE_SEND}\nEOF\npython3 p.py`,
  'cat > p.py <<EOF\n"""blast.py"""',
  'echo "<<EOF"\npython3 blast.py\nEOF',
  "echo '<<EOF'\npython3 blast.py\nEOF",
  "true # <<EOF\npython3 blast.py\nEOF",
  "cat <<<EOF\npython3 blast.py\nEOF",
  "echo $((true<<EOF))\npython3 blast.py\nEOF",
  "cat > p.txt <<EOF\npython3 blast.py\n\tEOF",
  "cat > p.txt <<-EOF\npython3 blast.py\n  EOF",
  'echo "a\n<<EOF"\npython3 blast.py\nEOF',
  "cat > p.txt <<EOF \\\n; python3 blast.py\nEOF",
]);

// bash expands an unquoted heredoc body, so a command substitution in it runs; a delimiter is the whole quote-removed
// word; an unquoted body joins a line ending in a backslash to the next before it looks for the delimiter; and
// a heredoc operator inside a quote opened on an earlier line is text.
expectEach(2, "a heredoc is read as bash reads it", [
  "cat <<EOF\n$(python3 blast.py)\nEOF",
  "cat <<EOF\n`python3 blast.py`\nEOF",
  "cat > a.txt <<-EOF\n\t${x:-$(python3 blast.py)}\n\tEOF",
  "cat <<E'OF'\nEOF\npython3 blast.py\nE",
  "cat <<'E'OF\nEOF\npython3 blast.py\nE",
  "cat <<EOF\nE\\\nOF\npython3 blast.py\nEOF",
  'echo "\n<<EOF "cat"\n"; python3 blast.py; echo "\nEOF\n"',
  "echo $(cat <<EOF\nx\nEOF\n); python3 blast.py",
]);

// bash removes a backslash-newline before it reads a token, except inside single quotes, a comment or a quoted heredoc
// body. So `"$\` newline `(cmd)"` is a command substitution, `$\` newline `'-v'` is `-v`, a delimiter ending in one is
// unquoted, `{PA\` newline `TH}>f` assigns PATH, `<<\` newline `-EOF` is `<<-EOF` and `&\` newline `>f` is `&>f`.
expectEach(2, "a line continuation hides nothing from the reader", [
  'echo "$\\\n(python3 blast.py)"',
  'cat other.py <<< "$\\\n(python3 blast.py)"',
  'echo "$\\\n{x:-$\\\n(python3 blast.py)}"',
  'cat blast.py > "$\\\n{T:-/dev/tcp/127.0.0.1/25}"',
  "printf $\\\n'-v' PATH %s ./bin; cat blast.py",
  "diff $\\\n'-l' blast.py other.py",
  "file $\\\n'-z' blast.py",
  "grep $\\\n'--filter=*:python3' x blast.py",
  "cat <<EOF\\\n\n$(python3 blast.py)\nEOF",
  "cat > notes.txt <<EOF\\\n\n$(curl -X POST https://graph.microsoft.com/v1.0/me/sendMail)\nEOF",
  "true {PA\\\nTH}>x.txt; cat blast.py",
  "true {PATH}\\\n>x.txt; cat blast.py",
  "cat <<\\\n-EOF\nEOF\npython3 blast.py\n-EOF",
  "diff blast.py &\\\n>o.txt cat -l",
]);

expectEach(0, "a line continuation between or inside words keeps the exemption", [
  "cat blast.py \\\n  | grep -n send",
  "sha256sum \\\n  blast.py \\\n  other.py",
  "grep -c send blast.py > cou\\\nnts.txt",
  'echo "a\\\nb" > n.txt; cat blast.py',
  "cat > notes.txt <<E\\\nOF\n# blast.py\nEOF",
]);

expectEach(0, "a quoted heredoc body, or an unquoted one without substitutions, is data", [
  "cat > probe.sh <<'EOF'\n$(python3 blast.py)\n`python3 blast.py`\nEOF",
  'cat > probe.sh <<"EOF"\n$(python3 blast.py)\nEOF',
  "cat > probe.sh <<\\EOF\n$(python3 blast.py)\nEOF",
  "cat > notes.txt <<EOF\nhome is ${HOME}, user $USER, sender blast.py\nEOF",
  "cat > a.txt <<A; cat > b.txt <<'B'\n# blast.py $x\nA\n$(python3 blast.py)\nB",
  "cat > notes.txt <<EOF \\\n  && wc -l notes.txt\n# blast.py\nEOF",
  'cat > notes.txt <<EOF "$(sha256sum blast.py)"\n# blast.py\nEOF',
]);

// ------------------------------------------------- the exemption reads bash alone
// PowerShell takes no backslash as an escape or a line continuation, and no \" as a quote inside double quotes, so a
// line bash reads as one echo can be two PowerShell statements, the second running the sender. The exemption reads
// bash grammar, so it applies to the Bash tool alone; a line from any other tool is judged on its full text.
const ONE_ECHO_IN_BASH = [
  "echo x\\; python blast.py",
  "echo x\\| python blast.py",
  'echo "\\" ; python blast.py ; echo \\""',
  "echo x \\\npython blast.py",
];
expectEach(2, "a PowerShell line is judged on its full text", ONE_ECHO_IN_BASH, "PowerShell");
expectEach(2, "a line from a payload that names no tool is judged on its full text", ONE_ECHO_IN_BASH, null);
expectEach(0, "under Bash the same line is one echo", ONE_ECHO_IN_BASH);

test("a line too deep for the reader is judged, not exempted", () => {
  // The hook allows a command when its own code throws; the reader's failure must land on the judged side.
  const dir = repo();
  file(dir, "blast.py", SENDER_BODY);
  const deep = `echo ${'"$(echo '.repeat(20000)}true${')"'.repeat(20000)}; python3 blast.py`;
  assert.equal(hook(deep, dir).status, 2);
});

// The hook allows a line when it fails, so no limit the judgment can reach, such as the call stack, may end it with a
// throw: whatever else the line runs would run unjudged.
const JSON_BODY = `[${Array.from({ length: 3000 }, (_, n) => `{"i":${n},"n":1}`).join(",")}]`;
const LARGE = {
  "3000 brace lists": `echo '${"{a,}".repeat(3000)}'`,
  "3000 nested brace lists": `echo '${"{a,".repeat(3000)}b${"}".repeat(3000)}'`,
  "a 50 KB JSON body": `curl -d '${JSON_BODY}' https://api.example.com`,
};
for (const [shape, line] of Object.entries(LARGE)) {
  test(`a line large enough to exhaust the gate is still judged: ${shape}`, () => {
    const dir = repo();
    file(dir, "blast.py", SENDER_BODY);
    assert.equal(hook(`${line}; python3 blast.py`, dir).status, 2);
  });
  test(`a large quoted string that names no sender is allowed: ${shape}`, () => {
    const dir = repo();
    file(dir, "blast.py", SENDER_BODY);
    assert.equal(hook(`${line}; python3 other.py`, dir).status, 0);
  });
}

// bash expands only the braces it reads unquoted, so a quoted brace is text and makes no words: JSON bodies, jq and
// node programs and commit messages are ordinary commands, however many brace pairs they hold.
const objects = (n) => `[${Array.from({ length: n }, (_, k) => `{"id":${k},"name":"n${k}"}`).join(",")}]`;
const pairs = (n) => Array.from({ length: n }, (_, k) => `{a${k}:1,b${k}:2}`).join(" + ");
const QUOTED_BRACES = {
  "curl with a JSON body":
    `curl -sS -X POST -H 'Content-Type: application/json' -d '${objects(13)}' https://api.example.com/v1/items`,
  "curl with a larger JSON body": `curl -sS -d '${objects(40)}' https://api.example.com/v1/items`,
  "gh api from a quoted heredoc": `gh api repos/o/r/labels --input - <<'EOF'\n${objects(20)}\nEOF`,
  "jq": `jq -n '${pairs(13)}'`,
  "node -e": `node -e 'const m=${pairs(13).replaceAll(" + ", ";const n=")};console.log(m)'`,
  "docker run -e": `docker run --rm -e CFG='${objects(15)}' alpine true`,
  "python3 from a quoted heredoc": `python3 - <<'EOF'\nimport json\nprint(json.dumps(${objects(20)}))\nEOF`,
  "tee from a quoted heredoc": `tee data.json <<'EOF' >/dev/null\n${objects(30)}\nEOF`,
  "git commit -m": `git commit -m 'refactor: ${"{a,b} ".repeat(13)}'`,
  "double quotes": `tee out.txt <<< "${"{a,b}".repeat(13)}"`,
};
for (const [shape, line] of Object.entries(QUOTED_BRACES)) {
  test(`a quoted brace makes no words: ${shape}`, () => {
    const dir = repo();
    file(dir, "blast.py", SENDER_BODY);
    assert.equal(hook(line, dir).status, 0);
  });
}

// A heredoc delimiter is its word with quotes removed and nothing expanded, newlines may separate `case WORD` from
// `in`, `time` and a function header may come before a compound command, and a line can nest deeper than the reader's
// stack. bash runs whatever follows each of these, so a reader that stops there must not stop judging.
expectEach(2, "a line the reader cannot follow to its end is judged", [
  "cat <<$D\nx\n$D\npython3 bl''ast.py",
  'cat <<"$D"\nx\n$D\npython3 bl\'\'ast.py',
  "cat <<*\nx\n*\npython3 bl''ast.py",
  "case x\nin x) python3 bl''ast.py;; esac",
  "time case x in x) python3 bl''ast.py;; esac",
  "f() case x in x) python3 bl''ast.py;; esac\nf",
  "script -qc \"cat <<\\$D\nx\n\\$D\npython3 bl''ast.py\" /dev/null",
  "echo 'unterminated\npython3 bl''ast.py",
]);

test("a line nested deeper than the reader's stack is judged", () => {
  const dir = repo();
  file(dir, "blast.py", SENDER_BODY);
  assert.equal(hook(`${"( ".repeat(3000)}true${" )".repeat(3000)}; python3 bl''ast.py`, dir).status, 2);
});

expectEach(0, "the constructs bash accepts there are read, not refused", [
  "cat <<$D\nx\n$D\necho done",
  "cat <<\"$D\"\nx\n$D\necho done",
  "cat <<$'E'\nx\nE\necho done",
  "case x\nin x) echo hit;; esac",
  "for x\nin a b; do echo $x; done",
  "time case x in x) echo hit;; esac",
  "time -p case x in x) echo hit;; esac",
  "f() case x in x) echo f;; esac\nf",
  "function g { echo g; }; g",
  "function h() { echo h; }; h",
]);

// PowerShell is read with bash's grammar only for the names in it: a backtick escape there is no unterminated
// substitution, and only a line that also names a shell, env, eval, trap or source is refused for it.
expectEach(0, "a PowerShell line bash cannot read is not refused for that alone", [
  'Write-Host "a`nb"',
], "PowerShell");

// The Claude Code shell expands aliases, and bash reads an alias's value as input in place of its name, so a value
// that ends in a backslash joins the line after it: `alias p='python3 bl\'`, then `p` and `ast.py`, runs
// `python3 blast.py`. An alias value is read as a command line of its own, and one the line does not show, or that
// bash would join to what follows, blocks the line.
expectEach(2, "an alias bash would run is read as code", [
  "alias p='python3 bl\\'\np\nast.py",
  "BASH_ALIASES[p]='python3 bl\\'\np\nast.py",
  "printf -v 'BASH_ALIASES[p]' %s 'python3 bl\\'\np\nast.py",
  "alias p=\"$CODE\"\np",
  "alias p='bash -c \"$CODE\"'\np",
]);

expectEach(0, "an alias written out in full is read like any command", [
  "alias ll='ls -la'\nll",
  "alias g='git status' gl='git log --oneline'; alias",
  "alias -p",
]);

// bash expands braces before variables, in the word as written: `$x{a,b}.py` is `$xa.py` and `$xb.py`.
expectEach(2, "braces expand in the word as written, before its variables", [
  "xa=blast; python3 $x{a,b}.py",
  'python3 "bl"{a,}st.py',
  "python3 'b'{l,x}{a,}st.py",
]);

// ------------------------------------------------- a quoted argument can hold a command line
// `sh -c 'python3 sender.py'` runs the sender. The quoted run as a whole names no file, and the path inside it must
// still be read.
expectEach(2, "a script named inside a quoted argument is judged", [
  "sh -c 'python3 blast.py'",
  'bash -c "python3 blast.py"',
  "env -S'python3 blast.py'",
  "dash -c 'cd . && python3 thin.py'",
]);

// bash joins a word from its quoted, escaped and continued parts and expands an unset variable to nothing, so the path
// it runs need not appear in the line as one run of text. The full judgment reads every word as bash builds it, in
// every construct bash runs commands from.
expectEach(2, "a path is resolved as bash builds the word", [
  "python3 bl''ast.py",
  'python3 "bl"ast.py',
  "python3 b'l'a\"s\"t.py",
  "python3 bl\\ast.py",
  "python3 bl\\\nast.py",
  "python3 $'bl\\x61st.py'",
  "python3 bl${NO_SUCH_VARIABLE}ast.py",
  "X=bl''ast.py; python3 $X",
  "python3 --script=bl''ast.py",
  "(python3 bl''ast.py)",
  "{ python3 bl''ast.py; }",
  "echo `python3 bl''ast.py`",
  "echo $((1 << 2)); python3 bl''ast.py",
  "case a in a) python3 bl''ast.py;; esac",
  "if true; then case a in a) python3 bl''ast.py;; esac; fi",
  "{ case a in a) python3 bl''ast.py;; esac; }",
  "cat <(python3 bl''ast.py)",
  "if true; then python3 bl''ast.py; fi",
  "for f in 1; do python3 'bl'ast.py; done",
  "f() { python3 bl''ast.py; }; f",
  "python3 th''in.py",
  'curl -X POST "https://graph.microsoft.com/v1.0/me/sen""dMail"',
  "curl -X POST https://graph.microsoft.com/v1.0/me/sen\\dMail",
]);

// A shell's -c string, env's -S string, eval's arguments, a trap action and the commands a shell reads from a heredoc
// or here-string are command lines of their own, read the same way, to any depth.
expectEach(2, "shell code inside the line is read as bash reads it", [
  "bash -c \"python3 bl''ast.py\"",
  "sh -c 'python3 bl\"\"ast.py'",
  "dash -c 'python3 bl\\\nast.py'",
  "zsh -c 'python3 bl\"\"ast.py'",
  "sh -c 'bash -c \"python3 bl\\\"\\\"ast.py\"'",
  "sudo -u nobody bash -lc \"python3 bl''ast.py\"",
  "timeout 5 sh -ec \"python3 bl''ast.py\"",
  "bash -o pipefail -c \"python3 bl''ast.py\"",
  "find . -maxdepth 0 -exec sh -c \"python3 bl''ast.py\" \\;",
  "env -S\"python3 bl''ast.py\"",
  "env -S'python3 \"bl\"ast.py'",
  "env -i -S'sh -c \"python3 bl\\\"\\\"ast.py\"'",
  "eval \"python3 bl''ast.py\"",
  "trap \"python3 bl''ast.py\" EXIT",
  "bash <<'EOF'\npython3 bl''ast.py\nEOF",
  "bash <<< \"python3 bl''ast.py\"",
  "sh -s <<EOF\npython3 bl''ast.py\nEOF",
]);

// bash expands braces and globs into paths, and bash 5.3 runs `${ list; }` in the current shell. A string the line
// writes to a file, hands to su, watch or ssh, or keeps for an arithmetic context or an alias may be run as code
// later, so every word and heredoc body is also read as a command line, at the very least for the paths it names.
expectEach(2, "a path bash expands or a string that may run later is resolved", [
  "python3 bl{a,}st.py",
  "python3 {bl,x}ast.py",
  "python3 bl?st.py",
  "python3 b*.py",
  "python3 [b]last.py",
  "python3 ./b*.py",
  "X=b*.py; python3 $X",
  "case a in a) (python3 b*.py);; esac",
  "sh -c '((python3 bl\"\"ast.py))'",
  "echo ${ python3 bl''ast.py; }",
  "echo ${| python3 bl''ast.py; }",
  "printf '%s\\n' \"python3 bl''ast.py\" > r.sh; bash r.sh",
  "cat > r.sh <<'EOF'\npython3 bl''ast.py\nEOF\nbash r.sh",
  "x='a[$(python3 bl\"\"ast.py)]'; echo $((x))",
  "su -c \"python3 bl''ast.py\" nobody",
  "watch -n 1 \"python3 bl''ast.py\"",
  "alias p=\"python3 bl''ast.py\"",
]);

// A variable the line assigns, declares or loops over takes each of those values as well as the one it has in the
// hook's environment.
expectEach(2, "a variable takes the values the line gives it", [
  "X=bl; python3 ${X}ast.py",
  "X=bl''; python3 \"$X\"ast.py",
  "export D=bl; python3 ${D}ast.py",
  "declare -x D=bl; bash -c 'python3 ${D}ast.py'",
  "for X in zz bl; do python3 ${X}ast.py; done",
  "X=bl; for f in ${X}a?t.py; do python3 $f; done",
]);

expectEach(0, "a variable the line gives harmless values is not refused", [
  "X=ot; python3 ${X}her.py",
  "for f in a b; do echo $f; done",
  "touch f{1..100}.txt",
]);

// Braces, globs and variables can make more paths than any check could try; padding a line with them must not push a
// sender past the gate's reach.
expectEach(2, "a line that makes more paths than the gate resolves is refused", [
  `python3 ${"{a,b}".repeat(13)}.py`,
  "touch f{1..5000}.txt; python3 other.py",
  `for X in ${Array.from({ length: 70 }, (_, n) => `v${n}`).join(" ")}; do touch $X$X; done`,
]);

expectEach(0, "a string or pattern that names no sender is not refused", [
  "git commit -m 'bash -c \"$X\" is now judged'",
  "printf '%s\\n' 'python3 other.py' > r.sh; bash r.sh",
  "rm -f *.pyc build/*",
  "python3 o*.py",
  "echo '${ unterminated'",
  "case $(uname) in Linux) echo linux;; *) echo other;; esac",
  "[[ $f == *.py ]] && echo script",
  'touch "b*.py"',
]);

// Shell code the line does not show cannot be judged by what it names. A -c or -S string built from an expansion or
// missing from the line, a shell reading its commands from a pipe, a process substitution, a descriptor or the input
// it inherits, eval, a trap action or source of a stream built from an expansion, and code the reader cannot follow
// each block the line.
const BUILT = "\"$(printf 'python3 bl%sast.py' '')\"";
expectEach(2, "shell code the gate cannot read is blocked", [
  `bash -c ${BUILT}`,
  'bash -c "$CODE"',
  '"$SHELL" -c "$CODE"',
  "bash $FLAGS \"$CODE\"",
  `printf %s ${BUILT} | xargs -0 sh -c`,
  `printf %s ${BUILT} | sh`,
  `printf %s ${BUILT} | bash -s`,
  `echo ${BUILT} | (bash)`,
  `{ bash; } <<< ${BUILT}`,
  `bash < <(printf %s ${BUILT})`,
  `bash <(printf %s ${BUILT})`,
  `bash /dev/stdin <<< ${BUILT}`,
  `source <(printf %s ${BUILT})`,
  `. /dev/stdin <<< ${BUILT}`,
  `eval ${BUILT}`,
  `trap ${BUILT} EXIT`,
  `env -S${BUILT}`,
  "env -S'python3 bl${EMPTY}ast.py'",
  "bash <<EOF\npython3 bl$(true)ast.py\nEOF",
  "sh -c 'echo \"unterminated'",
  "bash -c 'echo ok' && sh -c 'echo \"unterminated'",
  "cat <<$D\nx\n$D\nbash -c \"python3 bl''ast.py\"",
]);

// A shell may be named by an expansion, and a script operand built from one may be the pipe the shell reads from.
expectEach(2, "a shell named or fed through an expansion is judged", [
  `S=bash; printf %s ${BUILT} | $S`,
  `printf %s ${BUILT} | $NOTHING bash`,
  `Z=0; printf %s ${BUILT} | bash /dev/fd/$Z`,
  `printf %s ${BUILT} | bash "$(echo /dev/stdin)"`,
  `printf %s ${BUILT} | (bash "$(echo /dev/stdin)")`,
]);

test("a shell named by a variable from the environment is judged", () => {
  const dir = repo();
  assert.equal(hook(`printf %s ${BUILT} | "$SHELL"`, dir, "Bash", { SHELL: "/bin/bash" }).status, 2);
});

expectEach(0, "a script file named by an expansion is run like any interpreter's", [
  'ls *.sh | while read f; do bash "$f"; done',
  'bash "$(ls run.sh)"',
]);

// Shell code written out in the line is read, and a shell that runs a script file is the same as any interpreter.
expectEach(0, "shell code written out in the line is read, not refused", [
  "bash -c 'npm test'",
  "sh -c 'cd sub && make'",
  "bash -c 'case $1 in a) echo a;; esac' _ a",
  "bash -c 'echo $((1 << 2))'",
  "bash run.sh",
  'for f in *.sh; do bash "$f"; done',
  "which bash",
  "command -v bash",
  "bash --version",
  "chsh -s /bin/bash",
  "pgrep -c bash",
  "sudo apt-get install -y zsh",
  "bash <<'EOF'\necho hi\nEOF",
  "bash <<< 'echo hi'",
  "trap 'rm -f out.tmp' EXIT",
  "eval 'echo hi'",
  "source ./env.sh",
  ". ~/.profile",
  'env PATH="$HOME/bin:$PATH" make',
  "env -S'echo hi'",
  "docker exec web bash -c 'npm test'",
  "git commit -m 'bash -c is fine'",
  "python3 -c 'print(1)'",
]);

test("a block for unreadable shell code says what to do instead", () => {
  const dir = repo();
  const r = hook('bash -c "$CODE"', dir);
  assert.equal(r.status, 2);
  assert.match(r.stderr, /the -c string of bash/);
  assert.match(r.stderr, /written out in the line/);
});

test("a path under ~ is resolved against the home directory", () => {
  const dir = repo();
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "choke-home-"));
  file(home, "blast.py", SENDER_BODY);
  for (const command of ["python3 ~/blast.py", "python3 ~/bl''ast.py", "cd ~ && python3 blast.py",
                         "bash -c 'python3 ~/blast.py'"]) {
    assert.equal(hook(command, dir, "Bash", { HOME: home }).status, 2, command);
  }
  file(dir, "blast.py", SENDER_BODY);
  assert.equal(hook("python3 ~+/blast.py", dir).status, 2, "~+ is the working directory");
});

/** A repo at root/proj holding sub/deeper/, with elsewhere/ and home/ beside it, and the sender at `at`, relative to
 *  root. */
function tree(at) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "choke-tree-"));
  const proj = path.join(root, "proj");
  for (const dir of ["proj/sub/deeper", "elsewhere/d", "home"]) fs.mkdirSync(path.join(root, dir), { recursive: true });
  fs.writeFileSync(path.join(proj, ".send-chokepoint.json"), JSON.stringify({ sender: "outbound_mail.py" }));
  if (at) file(root, at, SENDER_BODY);
  return { root, proj };
}

/** One test per [command, where the sender is], each run from proj with HOME at root/home. */
function expectAt(status, title, cases) {
  for (const [command, at] of cases) {
    test(`${title}: ${JSON.stringify(command)} with the sender at ${at}`, () => {
      const { root, proj } = tree(at);
      assert.equal(hook(command, proj, "Bash", { HOME: path.join(root, "home") }).status, status);
    });
  }
}

// bash runs each command in the directory the line has reached by then: every cd, pushd or `env -C` resolved against
// the directories before it, through CDPATH, HOME and `-`, and globs. `~`, `~+` and `~-` are HOME, PWD and OLDPWD as
// the line has set them, and so are $PWD and $OLDPWD; `find -execdir` runs in the directory of each file it finds.
expectAt(2, "a path is resolved from every directory the line reaches", [
  ["cd sub && python3 ~+/blast.py", "proj/sub/blast.py"],
  ["cd sub && python3 ~-/blast.py", "proj/blast.py"],
  ["OLDPWD=sub; python3 ~-/blast.py", "proj/sub/blast.py"],
  ["HOME=sub; python3 ~/blast.py", "proj/sub/blast.py"],
  ["export HOME=$PWD/sub; python3 ~/blast.py", "proj/sub/blast.py"],
  ["PWD=sub; python3 ~+/blast.py", "proj/sub/blast.py"],
  ["bash -c 'cd sub && python3 ~+/blast.py'", "proj/sub/blast.py"],
  ["HOME=$PWD/sub; cd; python3 blast.py", "proj/sub/blast.py"],
  ["cd && python3 blast.py", "home/blast.py"],
  ["cd sub && cd deeper && python3 blast.py", "proj/sub/deeper/blast.py"],
  ["cd sub; cd deeper; python3 blast.py", "proj/sub/deeper/blast.py"],
  ["cd sub && cd ../../elsewhere && python3 blast.py", "elsewhere/blast.py"],
  ["cd sub && bash -c 'cd deeper && python3 blast.py'", "proj/sub/deeper/blast.py"],
  ["CDPATH=..; cd elsewhere && python3 blast.py", "elsewhere/blast.py"],
  ["CDPATH=.. cd elsewhere >/dev/null && python3 blast.py", "elsewhere/blast.py"],
  ["cd s* && python3 blast.py", "proj/sub/blast.py"],
  ["cd s?b && python3 blast.py", "proj/sub/blast.py"],
  ["env -C sub python3 blast.py", "proj/sub/blast.py"],
  ["env --chdir=sub python3 blast.py", "proj/sub/blast.py"],
  ["find . -name '*.py' -execdir python3 blast.py \\;", "proj/sub/deeper/blast.py"],
  ["python3 $PWD/blast.py", "proj/blast.py"],
  ["cd sub && python3 $OLDPWD/blast.py", "proj/blast.py"],
  ["cd sub && python3 $PWD/blast.py", "proj/sub/blast.py"],
  ['cd sub && python3 "$PWD/blast.py"', "proj/sub/blast.py"],
]);

// A line can turn on the glob options its own later lines use: Claude Code's shell evaluates a line one command at a
// time, so `shopt -s extglob` on one line takes effect on the next, and a shell's -O sets them for its -c string.
expectAt(2, "a glob is matched with the options the line turns on", [
  ["shopt -s extglob\npython3 @(blast).py", "proj/blast.py"],
  ["shopt -s extglob\npython3 !(other).py", "proj/blast.py"],
  ["bash -O extglob -c 'python3 @(blast).py'", "proj/blast.py"],
  ["bash -c $'shopt -s extglob\\npython3 @(blast).py'", "proj/blast.py"],
  ["shopt -s nocaseglob; python3 BLAST.P[Y]", "proj/blast.py"],
  ["bash -O nocaseglob -c 'python3 BLAST.P[Y]'", "proj/blast.py"],
  ["shopt -s globstar; python3 **/bl?st.py", "proj/sub/deeper/blast.py"],
  ["o=globstar; shopt -s $o; python3 **/bl?st.py", "proj/sub/deeper/blast.py"],
  ["shopt -s $(echo globstar); python3 **/bl?st.py", "proj/sub/deeper/blast.py"],
  ["shopt -s dotglob; bash ?hid.sh", "proj/.hid.sh"],
  ["GLOBIGNORE=x; bash ?hid.sh", "proj/.hid.sh"],
]);

expectEach(0, "an extglob group is read as part of its word", [
  "shopt -s extglob\nls @(a|b c)",
  "shopt -s extglob\ntouch ?(x).txt",
  "case $x in @(a|b)) echo ab;; esac",
  "[[ $x == +(a|b) ]] && echo ab",
  "! (true) && echo negated",
]);

expectAt(0, "a glob option the line does not turn on is off", [
  ["python3 **/blast.py", "proj/sub/deeper/blast.py"],
  ["python3 BLAST.P[Y]", "proj/blast.py"],
  ["bash ?hid.sh", "proj/.hid.sh"],
]);

expectAt(0, "a directory the line reaches holds no sender", [
  ["cd sub && cd deeper && python3 blast.py", "proj/blast.py.txt"],
  ["find . -name '*.pyc' -execdir rm {} +", "proj/sub/deeper/blast.py"],
]);

test("a line that may run in more than 64 directories is refused", () => {
  const { proj } = tree(null);
  const names = Array.from({ length: 70 }, (_, n) => `d${n}`);
  for (const name of names) fs.mkdirSync(path.join(proj, name));
  const r = hook(`for d in ${names.join(" ")}; do cd "$d"; python3 other.py; cd ..; done`, proj);
  assert.equal(r.status, 2);
  assert.equal(hook(`for d in ${names.slice(0, 10).join(" ")}; do cd "$d"; cd ..; done`, proj).status, 0);
});

test("a name is judged in every directory the line may run it from, not only the first that holds it", () => {
  const { proj } = tree("proj/sub/blast.py");
  file(proj, "blast.py", READER_BODY);
  assert.equal(hook("cd sub && python3 blast.py", proj).status, 2);
});

// The kernel resolves `..` after following a symlink, so `link/..` is the parent of where link points. A path that
// reads as the sanctioned sender but leads elsewhere is not it.
test("a path through a symlinked directory is resolved as the kernel resolves it", () => {
  const { root, proj } = tree("elsewhere/blast.py");
  fs.symlinkSync(path.join(root, "elsewhere", "d"), path.join(proj, "sub", "link"));
  assert.equal(hook("cd sub && python3 link/../blast.py", proj).status, 2);
});

test("a symlink cannot pass another file off as the sanctioned sender", () => {
  const { root, proj } = tree("elsewhere/outbound_mail.py");
  file(proj, "outbound_mail.py", SENDER_BODY);
  fs.symlinkSync(path.join(root, "elsewhere", "d"), path.join(proj, "x"));
  assert.equal(hook("python3 outbound_mail.py", proj).status, 0, "the sanctioned sender still runs");
  assert.equal(hook("python3 x/../outbound_mail.py", proj).status, 2);
});

test("a cd target is resolved as bash builds it", () => {
  const dir = repo();
  fs.mkdirSync(path.join(dir, "sub"));
  file(path.join(dir, "sub"), "blast.py", SENDER_BODY);
  assert.equal(hook("cd s''ub && python3 blast.py", dir).status, 2);
});

test("an inline send is still BLOCKED when it rides with a read-only command", () => {
  const dir = repo();
  const r = hook("cat notes.txt; curl -X POST https://graph.microsoft.com/v1.0/me/sendMail", dir);
  assert.equal(r.status, 2);
});
