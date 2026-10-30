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
  return spawnSync("node", [SCRIPT, "--hook"], { input: payload, encoding: "utf8" });
}

function file(dir, name, body) {
  const p = path.join(dir, name);
  fs.writeFileSync(p, body);
  return p;
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
// A command made only of read-only tools (print, search, hash, compare, list, git
// read subcommands) cannot run the file it names, so it is never judged; the
// operator must be able to read, grep and hash the very files the gate protects.
// Anything that can run a file keeps the full judgment.
for (const command of [
  "sha256sum blast.py",
  "cat blast.py",
  "head -n 40 blast.py | grep -n send",
  "grep -n sendMail blast.py",
  "wc -l blast.py && sha256sum blast.py",
  "timeout 60 sha256sum blast.py",
  "LC_ALL=C grep -c graph blast.py",
  "diff blast.py other.py",
  "git show HEAD:blast.py",
  "git diff -- blast.py",
  'grep -rn "graph.microsoft.com/v1.0/me/sendMail" .',
  'echo "$(sha256sum blast.py)"',
  "wc -l blast.py;",
]) {
  test(`a read-only command on a sender is allowed: ${command}`, () => {
    const dir = repo();
    file(dir, "blast.py", SENDER_BODY);
    file(dir, "other.py", READER_BODY);
    assert.equal(hook(command, dir).status, 0, "reading or hashing a file is not a send");
  });
}

for (const command of [
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
]) {
  test(`a command that can run the sender keeps the full judgment: ${command}`, () => {
    const dir = repo();
    file(dir, "blast.py", SENDER_BODY);
    assert.equal(hook(command, dir).status, 2);
  });
}

test("an inline send is still BLOCKED when it rides with a read-only command", () => {
  const dir = repo();
  const r = hook("cat notes.txt; curl -X POST https://graph.microsoft.com/v1.0/me/sendMail", dir);
  assert.equal(r.status, 2);
});
