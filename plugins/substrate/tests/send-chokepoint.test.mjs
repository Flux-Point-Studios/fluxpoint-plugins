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

function hook(command, cwd, toolName = "Bash") {
  const payload = JSON.stringify({ tool_name: toolName, tool_input: { command }, cwd });
  // Claude Code runs the hook in the session's directory, so the hook here runs in the test's own repo, never in
  // whatever tree the suite was started from.
  return spawnSync("node", [SCRIPT, "--hook"], { input: payload, encoding: "utf8", cwd });
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

test("an inline send is still BLOCKED when it rides with a read-only command", () => {
  const dir = repo();
  const r = hook("cat notes.txt; curl -X POST https://graph.microsoft.com/v1.0/me/sendMail", dir);
  assert.equal(r.status, 2);
});
