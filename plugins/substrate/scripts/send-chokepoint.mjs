#!/usr/bin/env node
// send-chokepoint.mjs — one sanctioned path out, enforced before the command runs.
//
// WHY. A repo can have a correct way to send mail — one module that runs its
// prose gate, checks recipients, threads the reply and reads the receipt back —
// and still ship a broken message, because nothing stops a second script from
// calling the mail API directly. A gate the author can walk past is advice. The
// correct implementation existing does not make it the only one.
//
// WHAT IT DOES. A PreToolUse hook on shell tools. It refuses a command that
// would reach a mail-SEND endpoint from anywhere except the sender the repo
// declares.
//
// OFF BY DEFAULT, ON PURPOSE. With no `.send-chokepoint.json` above the working
// directory this hook allows everything. A plugin that blocked every mail call
// in every repo it was installed into would be switched off within a day, and a
// disabled gate protects nothing. A repo opts in by declaring its own sender.
//
// HOW IT DECIDES, and why it is narrow. A file or an inline command counts as a
// sender only when it carries BOTH the provider host AND a send verb. Either
// alone is meaningless: "/send" appears in unrelated URLs, and the host appears
// in every script that READS a mailbox. Reading mail is not sending it, and
// blocking readers is how a gate earns being removed.
//
// FAILS OPEN ON ITSELF, CLOSED ON THE SEND. A malformed payload, an unreadable
// file or a missing config exits 0: a broken hook must never wedge a session. A
// file it can read that is a sender outside the declared allowlist exits 2.
//
// CONFIG — `.send-chokepoint.json`, found by walking up from the command's
// working directory:
//
//   {
//     "sender":    "tools/outbound_mail.py",   // the one sanctioned entry point
//     "exempt":    ["services/mailer"],        // path fragments allowed to send
//     "providers": ["graph.microsoft.com"]     // mail hosts to watch (optional)
//   }
//
// Exit codes: 0 = allow, 2 = block (stderr is shown to the author).
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const CONFIG_BASENAME = ".send-chokepoint.json";
const CONFIG_KEYS = new Set(["sender", "exempt", "providers"]);

// Hosts that accept an outbound message. Overridable per repo.
const DEFAULT_PROVIDERS = [
  "graph.microsoft.com",
  "gmail.googleapis.com",
  "api.sendgrid.com",
  "api.mailgun.net",
  "api.postmarkapp.com",
  "api.resend.com",
];
// The verbs that mean "this one SENDS", as opposed to reading a mailbox.
const SEND_VERB = /\/sendMail\b|\/messages\/[^"'\s]*\/send\b|%s\/send\b|\{\}\/send\b|\/send["']|createReply|\/mail\.send\b|\/v3\/mail\/send\b|\/messages\.json\b/i;

const MAX_READ_BYTES = 2_000_000;
// Bare tokens, plus quoted runs that may contain spaces. Without the quoted
// forms, `python "space dir/blast.py"` matches only the tail after the space,
// which resolves to nothing and lets an unsanctioned sender run — and Windows
// user directories contain spaces as a matter of course.
const PATH_TOKEN = /"([^"\n]+\.(?:py|mjs|js|ts|ps1|sh|rb))"|'([^'\n]+\.(?:py|mjs|js|ts|ps1|sh|rb))'|([A-Za-z0-9_.:\\/~-]+\.(?:py|mjs|js|ts|ps1|sh|rb))\b/g;
// A leading `cd <dir> &&` sets the directory the rest of the command runs in.
const CD_PREFIX = /(?:^|[;&|]\s*)cd\s+(?:"([^"]+)"|'([^']+)'|([^\s;&|]+))/g;
const PY_IMPORT = /^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import\b|import\s+([A-Za-z_][\w.]*))/gm;

// Tools that print, search, hash, compare or list a file and cannot run it. A command made only of these is never
// judged: the operator must be able to read and hash the very files this gate protects. sed (its `e` command) and
// awk (system()) can run code, so they are not here.
const READ_ONLY = new Set(["cat", "head", "tail", "less", "more", "wc", "grep", "egrep", "fgrep", "rg", "ugrep",
  "sha256sum", "sha1sum", "sha512sum", "md5sum", "b2sum", "cksum", "diff", "cmp", "comm", "file", "stat", "ls",
  "readlink", "realpath", "basename", "dirname", "du", "od", "xxd", "hexdump", "strings", "sort", "uniq", "cut",
  "nl", "echo", "printf", "true"]);
const GIT_READ_ONLY = new Set(["show", "diff", "log", "blame", "grep", "status", "cat-file", "ls-files", "rev-parse"]);
// Words that run the command after them: skipped, with their own option values.
const PREFIXES = { timeout: /^(?:-\S+|\d+(?:\.\d+)?[smhd]?)$/, nice: /^(?:-n|-?\d+)$/, env: /^-\S*$/ };
const ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/;
const WORD = /"[^"]*"|'[^']*'|\S+/g;

function readOnlySimple(segment) {
  // Redirections are not commands, and a substitution split inside quotes leaves the quote marks behind.
  const words = (segment.match(WORD) || []).filter((w) => !/^\d*[<>]/.test(w) && !/^["']+$/.test(w))
    .map((w) => w.replace(/^["']+|["']+$/g, ""));
  let i = 0;
  for (;;) {
    while (i < words.length && ASSIGNMENT.test(words[i])) i += 1;
    const skip = PREFIXES[words[i]];
    if (!skip) break;
    i += 1;
    while (i < words.length && skip.test(words[i])) i += 1;
  }
  if (i >= words.length) return true;
  const name = path.basename(words[i]);
  if (name === "git") {
    let j = i + 1;
    while (j < words.length && words[j].startsWith("-")) j += words[j] === "-C" ? 2 : 1;
    return GIT_READ_ONLY.has(words[j]);
  }
  return READ_ONLY.has(name);
}

/** Every simple command in the line is a read-only tool. Pipes, lists, newlines and command substitutions all split
 *  the line, so `cat s.py | python3` and `python3 -c "$(cat s.py)"` are not read-only. */
export function readOnlyCommand(command) {
  // The & of `2>&1`, `>&2` and `&>` is part of a redirection, not a list separator.
  return command.replace(/\d*[<>]&(?:\d+|-)|&>>?/g, (m) => (m[0] === "&" ? ">" : " "))
    .split(/\$\(|[;&|\n`()]+/).every(readOnlySimple);
}

/** The nearest `.send-chokepoint.json` at or above `from`, validated.
 *  Returns null when none exists, which means this hook does nothing. */
export function loadConfig(from) {
  let dir = path.resolve(from || process.cwd());
  const problems = [];
  for (;;) {
    const candidate = path.join(dir, CONFIG_BASENAME);
    if (fs.existsSync(candidate)) {
      let doc;
      try {
        doc = JSON.parse(fs.readFileSync(candidate, "utf8"));
      } catch {
        problems.push(`${candidate}: not valid JSON — ignored`);
        return { config: null, problems };
      }
      const config = { sender: "", exempt: [], providers: DEFAULT_PROVIDERS };
      for (const k of Object.keys(doc)) {
        if (!CONFIG_KEYS.has(k)) problems.push(`${candidate}: unknown key "${k}" — ignored`);
      }
      if (typeof doc.sender === "string" && doc.sender.trim()) config.sender = doc.sender.trim();
      if (Array.isArray(doc.exempt)) config.exempt = doc.exempt.filter((x) => typeof x === "string");
      if (Array.isArray(doc.providers) && doc.providers.length) {
        config.providers = doc.providers.filter((x) => typeof x === "string");
      }
      if (!config.sender) {
        problems.push(`${candidate}: no "sender" declared — the chokepoint stays off`);
        return { config: null, problems };
      }
      config.root = dir;
      return { config, problems, at: candidate };
    }
    const parent = path.dirname(dir);
    if (parent === dir) return { config: null, problems };
    dir = parent;
  }
}

const norm = (p) => p.replace(/\\/g, "/").toLowerCase();

/** Is this file the declared sender, or inside a declared exempt directory?
 *
 * ⚠ ANCHORED TO THE CONFIG'S OWN DIRECTORY, deliberately. Matching on basename
 * would let any readable file called `outbound_mail.py` — including one written
 * to a temp directory — impersonate the sanctioned sender and reach a send
 * endpoint. `sender` and every `exempt` entry are resolved against the folder
 * holding `.send-chokepoint.json`, and compared as exact normalised paths.
 */
function isAllowedPath(fp, config) {
  const n = norm(path.resolve(fp));
  if (n === norm(path.resolve(config.root, config.sender))) return true;
  return config.exempt.some((frag) => {
    if (!frag) return false;
    const base = norm(path.resolve(config.root, frag));
    return n === base || n.startsWith(base + "/");
  });
}

function looksLikeSender(text, config) {
  return config.providers.some((h) => text.includes(h)) && SEND_VERB.test(text);
}

function searchDirs(command, cwd) {
  const dirs = [];
  for (const m of command.matchAll(CD_PREFIX)) {
    const d = m[1] || m[2] || m[3];
    if (d) dirs.push(d);
  }
  if (cwd) dirs.push(cwd);
  dirs.push(process.cwd());
  return dirs;
}

/** Every existing script path named in the command.
 *
 * ⚠ A bare token must be resolved against the COMMAND's directory, not this
 * process's. `cd somewhere && python sender.py` is the shape people actually
 * type, and resolving it here would find nothing and allow the send. */
function referencedFiles(command, cwd) {
  const dirs = searchDirs(command, cwd);
  const out = [];
  for (const m of command.matchAll(PATH_TOKEN)) {
    const token = m[1] || m[2] || m[3] || "";
    if (!token) continue;
    const candidates = path.isAbsolute(token)
      ? [token]
      : [token, ...dirs.map((d) => path.resolve(d, token))];
    for (const fp of candidates) {
      try {
        const st = fs.statSync(fp);
        if (st.isFile() && st.size <= MAX_READ_BYTES) { out.push(fp); break; }
      } catch { /* keep looking */ }
    }
  }
  return out;
}

/** Local modules a python file imports, resolved beside it.
 *
 * ⚠ The scripts that bypass a sender are usually THIN: they import a mail
 * helper and call it, so the provider host never appears in the file the
 * command names. Checking only that file lets every one of them through. */
function localImports(fp) {
  let body = "";
  try { body = fs.readFileSync(fp, "utf8"); } catch { return []; }
  const dir = path.dirname(fp);
  const out = [];
  for (const m of body.matchAll(PY_IMPORT)) {
    const mod = (m[1] || m[2] || "").split(".")[0];
    if (!mod) continue;
    const candidate = path.join(dir, mod + ".py");
    try {
      if (fs.statSync(candidate).isFile()) out.push(candidate);
    } catch { /* stdlib or a dependency, not ours to judge */ }
  }
  return out;
}

function senderVerdict(fp, config, via) {
  if (isAllowedPath(fp, config)) return null;
  let body = "";
  try { body = fs.readFileSync(fp, "utf8"); } catch { return null; }
  if (!looksLikeSender(body, config)) return null;
  return {
    reason: via
      ? `${path.basename(via)} imports ${path.basename(fp)}, which reaches a mail-send endpoint`
      : "this script reaches a mail-send endpoint",
    where: fp,
  };
}

export function judge(command, cwd, config) {
  if (!config || typeof command !== "string" || !command.trim()) return null;
  if (readOnlyCommand(command)) return null;
  if (looksLikeSender(command, config)) {
    return { reason: "this command reaches a mail-send endpoint directly",
             where: "the command line" };
  }
  for (const fp of referencedFiles(command, cwd)) {
    const direct = senderVerdict(fp, config);
    if (direct) return direct;
    // An allowlisted file does not launder its imports, but the sanctioned
    // sender may import whatever it needs.
    if (isAllowedPath(fp, config)) continue;
    for (const dep of localImports(fp)) {
      const indirect = senderVerdict(dep, config, fp);
      if (indirect) return indirect;
    }
  }
  return null;
}

const advice = (config) => [
  `Mail leaves through ${config.sender} alone, so its checks cannot be walked past.`,
  "",
  "If this command does not send mail, it named a file that does — move that",
  `send into ${config.sender}, or add its directory to "exempt" in`,
  `${CONFIG_BASENAME}.`,
].join("\n");

function main() {
  let raw = "";
  try { raw = fs.readFileSync(0, "utf8"); } catch { process.exit(0); }
  let payload;
  try { payload = JSON.parse(raw); } catch { process.exit(0); }
  const command = (payload?.tool_input || {}).command;
  const cwd = payload?.cwd;
  // ⚠ Search the directories the command will actually RUN in before the
  // session's own. `cd repo && python blast.py` from a parent is governed by
  // repo's config; looking only at the payload cwd finds none and exits before
  // the `cd` is ever considered — the same bypass shape as resolving a bare
  // path against the wrong directory, one layer up.
  let loaded = { config: null, problems: [] };
  try {
    for (const dir of searchDirs(typeof command === "string" ? command : "", cwd)) {
      const attempt = loadConfig(dir);
      loaded.problems.push(...(attempt.problems || []));
      if (attempt.config) { loaded = { ...attempt, problems: loaded.problems }; break; }
    }
  } catch { process.exit(0); }
  for (const p of [...new Set(loaded.problems || [])]) {
    process.stdout.write(`send-chokepoint: ${p}\n`);
  }
  if (!loaded.config) process.exit(0);
  let verdict = null;
  try { verdict = judge(command, cwd, loaded.config); } catch { process.exit(0); }
  if (!verdict) process.exit(0);
  process.stderr.write(
    `BLOCKED: ${verdict.reason} (${verdict.where}).\n\n${advice(loaded.config)}\n`);
  process.exit(2);
}

if (process.argv[1]
    && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  main();
}
