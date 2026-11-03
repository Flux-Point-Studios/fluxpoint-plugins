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
import os from "node:os";
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

// Tools that print, search, hash, compare or list a file. A command made only of these, named bare, is never judged:
// the operator must be able to read and hash the very files this gate protects. Tools that can run code are not
// here: sed (its `e` command), awk (system()), rg (--pre), ugrep (--filter), sort (--compress-program), less and more
// (LESSOPEN, shell escapes), and git, whose every subcommand honours repository configuration an agent can write
// (core.fsmonitor, diff.external, textconv, core.pager).
const READ_ONLY = new Set(["cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "sha1sum", "sha256sum",
  "sha512sum", "md5sum", "b2sum", "cksum", "diff", "cmp", "comm", "file", "stat", "ls", "readlink", "realpath",
  "basename", "dirname", "du", "od", "xxd", "hexdump", "strings", "uniq", "cut", "nl", "echo", "printf", "true"]);
// The read-only tools with an option that runs a program, and those options' letters and long names. grep in the
// Claude Code shell runs ugrep, whose --filter, --pager and --view name a command and whose --config or --- loads a
// file that can; the host's wrapper hands those to the system grep, but the exemption does not lean on it. GNU diff
// --paginate runs pr; file -z and -Z decompress through helpers found on PATH.
const UGREP_RUNS = { letters: "", names: ["filter", "pager", "view", "config", "-"] };
const RUNS_PROGRAM = new Map([
  ["grep", UGREP_RUNS], ["egrep", UGREP_RUNS], ["fgrep", UGREP_RUNS],
  ["diff", { letters: "l", names: ["paginate"] }],
  ["file", { letters: "zZ", names: ["uncompress", "uncompress-noreport"] }],
]);
const TIMEOUT_FLAG = /^(?:-[sk]\S+|--signal=\S+|--kill-after=\S+|-v|--verbose|--foreground|--preserve-status|--)$/;
const DURATION = /^\d+(?:\.\d+)?[smhd]?$/;
// Words that run the command after them. Each returns the index of that command, or -1 for an option this reader
// does not know to be harmless. env takes no option at all: -S splits its value into a new command line.
const PREFIXES = new Map([
  ["timeout", (words, i) => {
    while (words[i] === "-s" || words[i] === "-k" || TIMEOUT_FLAG.test(words[i] ?? "")) {
      i += words[i] === "-s" || words[i] === "-k" ? 2 : 1;
    }
    return DURATION.test(words[i] ?? "") ? i + 1 : -1;
  }],
  ["nice", (words, i) => {
    if (words[i] === "-n") return i + 2;
    if (/^(?:-n|--adjustment=|-)-?\d+$/.test(words[i] ?? "")) return i + 1;
    return words[i]?.startsWith("-") ? -1 : i;
  }],
  ["env", (words, i) => (words[i]?.startsWith("-") ? -1 : i)],
]);
// An assignment can change what a later word runs (PATH, LD_PRELOAD, LESSOPEN, GIT_*), so any one is judged. `{name}>`
// assigns the new descriptor's number to name.
const ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*(?:\[|\+?=)/;
const NAMED_FD = /^\{[A-Za-z_][A-Za-z0-9_]*\}$/;
const ENDS_WORD = new Set([" ", "\t", "\n", ";", "&", "|", "(", ")", "<", ">"]);
const REDIRECT_OP = /&>>?|<<<|<<-?|<>|[<>]&|>>|>\||[<>]/y;
// bash expands the word after `<&` or `>&`, and when the result is not a descriptor number or `-`, expands it again as
// the file to open. Text made only of these characters comes out of that second pass unchanged.
const PLAIN_TEXT = /^[\w./,:=%+@-]+$/;
const PARAMETER = /\$(?:\{(?:[A-Za-z_][A-Za-z0-9_]*|\d+|[@*#?$!-])\}|[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])/y;
// What bash would run or rejoin in an unquoted heredoc body: a command substitution, arithmetic, a ${...} operator,
// or a trailing backslash, which joins the line to the next before the delimiter is looked for.
const BODY_EXPANSION = /`|\$[([]|\$\{(?!(?:[A-Za-z_][A-Za-z0-9_]*|\d+|[@*#?$!-])\})|\\$/;
// bash opens /dev/tcp/HOST/PORT and /dev/udp/HOST/PORT as sockets; the rest of /dev and /proc are devices and other
// processes' descriptors. Only these four streams there are ordinary.
const SPECIAL_ROOT = /^\/(?:dev|proc)(?:\/|$)/;
const STD_STREAMS = new Set(["/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"]);
const NOT_EXEMPT = Symbol("not exempt");

function notExempt() {
  throw NOT_EXEMPT;
}

/** A redirection target that opens an ordinary file: written out literally, outside /dev and /proc unless it is one
 *  of the standard streams, and a regular file if it exists already. */
function ordinaryTarget(target, cwd) {
  if (!target.literal) return false;
  let name = target.text;
  if (target.bare.startsWith("~")) {
    if (!/^~(?:\/|$)/.test(target.bare)) return false;
    name = os.homedir() + name.slice(1);
  }
  if (STD_STREAMS.has(path.posix.normalize(name))) return true;
  const full = path.resolve(cwd || process.cwd(), name);
  if (SPECIAL_ROOT.test(path.posix.normalize(name)) || SPECIAL_ROOT.test(full)) return false;
  try {
    return fs.statSync(full).isFile();
  } catch (err) {
    return err.code === "ENOENT";
  }
}

/** Whether `arg` sets one of `runs`' options: a letter anywhere in a short-option cluster, or a long name that
 *  abbreviates one of its names (getopt takes any unambiguous abbreviation) or begins with one (`---FILE`). */
function runsProgram(runs, arg) {
  if (/^-[^-]/.test(arg)) return [...arg.slice(1)].some((c) => runs.letters.includes(c));
  const name = arg.startsWith("--") ? arg.slice(2).split("=")[0] : "";
  return name !== "" && runs.names.some((n) => n.startsWith(name) || name.startsWith(n));
}

/** The command's words up to its name are literal, assign nothing, pass only harmless prefix options, and name a
 *  read-only tool; printf's first argument is not an option (printf -v NAME assigns); a tool in RUNS_PROGRAM takes
 *  only literal arguments, none of them an option that runs a program. */
function requireReadOnlyCommand(words) {
  const texts = words.map((w) => (w.literal ? w.text : null));
  let i = 0;
  for (;;) {
    if (ASSIGNMENT.test(words[i]?.bare ?? "")) notExempt();
    const after = PREFIXES.get(texts[i]);
    if (!after) break;
    i = after(texts, i + 1);
    if (i < 0) notExempt();
  }
  if (i >= words.length) return;
  if (words.slice(0, i + 1).some((w) => !w.literal) || !READ_ONLY.has(texts[i])) notExempt();
  const first = words[i + 1];
  if (texts[i] === "printf" && first && (!first.literal || /^-(?!-$)/.test(first.text))) notExempt();
  const runs = RUNS_PROGRAM.get(texts[i]);
  if (runs && words.slice(i + 1).some((w) => !w.literal || runsProgram(runs, w.text))) notExempt();
}

/** Reads `src` as bash would and throws NOT_EXEMPT unless every simple command in it, command substitutions
 *  included, is read-only and every redirection opens an ordinary file. A heredoc body is data unless bash would
 *  expand something in it. Syntax it does not follow — backquotes, arithmetic, subshells, process substitution, a
 *  heredoc inside a substitution, an unterminated quote or heredoc — throws too. */
function requireReadOnly(src, cwd) {
  let i = 0;
  const heredocs = [];
  const sticky = (re) => {
    re.lastIndex = i;
    const m = re.exec(src);
    if (m) i += m[0].length;
    return m?.[0];
  };
  // bash removes a backslash-newline before it reads a token. The reader follows one between words and inside a word,
  // and judges a line where one would join an operator or an expansion to what follows.
  const continued = (j) => src[j] === "\\" && src[j + 1] === "\n";
  const skipBlanks = () => {
    while (src[i] === " " || src[i] === "\t" || continued(i)) i += continued(i) ? 2 : 1;
  };

  // At a `$`: consumes an expansion and returns true, or returns false for a literal dollar sign.
  const dollar = (inQuotes) => {
    if (continued(i + 1)) notExempt();
    const next = src[i + 1];
    if (next === "(") {
      if (src[i + 2] === "(") notExempt();
      i += 2;
      list(true);
      return true;
    }
    if (next === "[" || next === "{") return sticky(PARAMETER) ? true : notExempt();
    if (!inQuotes && next === "'") {
      for (i += 2; src[i] !== "'"; i += src[i] === "\\" ? 2 : 1) if (i >= src.length) notExempt();
      i += 1;
      return true;
    }
    if (!inQuotes && next === '"') {
      i += 1;
      doubleQuoted();
      return true;
    }
    return Boolean(sticky(PARAMETER));
  };

  // At a double quote: the quoted text, and whether it holds no expansion.
  const doubleQuoted = () => {
    let text = "";
    let literal = true;
    for (i += 1; src[i] !== '"'; ) {
      const c = src[i];
      if (c === undefined || c === "`") notExempt();
      if (c === "\\" && src[i + 1] !== undefined && '$`"\\\n'.includes(src[i + 1])) {
        if (src[i + 1] !== "\n") text += src[i + 1];
        i += 2;
      } else if (c === "$" && dollar(true)) {
        literal = false;
      } else {
        text += c;
        i += 1;
      }
    }
    i += 1;
    return { text, literal };
  };

  // One word after quote removal: its text, whether it holds no expansion or pattern, whether any of it is quoted or
  // escaped, and its source without line continuations.
  const word = () => {
    let text = "";
    let bare = "";
    let literal = true;
    let quoted = false;
    while (i < src.length && !ENDS_WORD.has(src[i])) {
      const from = i;
      const c = src[i];
      if (continued(i)) {
        i += 2;
        continue;
      }
      if (c === "'") {
        const end = src.indexOf("'", i + 1);
        if (end < 0) notExempt();
        text += src.slice(i + 1, end);
        i = end + 1;
        quoted = true;
      } else if (c === '"') {
        const q = doubleQuoted();
        text += q.text;
        literal &&= q.literal;
        quoted = true;
      } else if (c === "\\") {
        if (i + 1 >= src.length) notExempt();
        text += src[i + 1];
        i += 2;
        quoted = true;
      } else if (c === "`") {
        notExempt();
      } else if (c === "$" && dollar(false)) {
        literal = false;
      } else {
        if ("*?[]{}".includes(c)) literal = false;
        text += c;
        i += 1;
      }
      bare += src.slice(from, i);
    }
    return { text, literal, quoted, bare };
  };

  const redirect = (nested) => {
    const op = sticky(REDIRECT_OP);
    if (continued(i)) notExempt();
    skipBlanks();
    const dup = op === "<&" || op === ">&";
    // bash reads a `-` after `<&` or `>&` as a token of its own that closes the descriptor; the text after it starts a
    // new word.
    if (dup && src[i] === "-") {
      i += 1;
      return;
    }
    const target = word();
    if (!target.bare || target.bare.startsWith("#")) notExempt();
    if (op === "<<" || op === "<<-") {
      // The delimiter is the quote-removed word; any quoting in it leaves the body unexpanded.
      if (nested || !target.literal || !target.text || target.text.includes("\n")) notExempt();
      heredocs.push({ delimiter: target.text, tabs: op === "<<-", quoted: target.quoted });
    } else if (dup && /^\d+-?$/.test(target.text)) {
      // Any descriptor but the standard three was inherited from whatever started the shell, and may be a socket.
      if (!target.literal || !/^[012]-?$/.test(target.text)) notExempt();
    } else if ((dup && !PLAIN_TEXT.test(target.text)) || (op !== "<<<" && !ordinaryTarget(target, cwd))) {
      notExempt();
    }
  };

  // After the newline that ends a line, the bodies of the heredocs it opened, in order.
  const bodies = () => {
    for (const doc of heredocs.splice(0)) {
      for (;;) {
        if (i >= src.length) notExempt();
        let end = src.indexOf("\n", i);
        if (end < 0) end = src.length;
        const line = src.slice(i, end);
        i = end + 1;
        if ((doc.tabs ? line.replace(/^\t+/, "") : line) === doc.delimiter) break;
        if (!doc.quoted && BODY_EXPANSION.test(line)) notExempt();
      }
    }
  };

  // A list of commands, up to the end of src or, inside `$(`, its closing parenthesis.
  const list = (nested) => {
    let words = [];
    for (;;) {
      skipBlanks();
      const c = src[i];
      if (c === undefined || c === ")") {
        if ((c === ")") !== nested || (c === undefined && heredocs.length)) notExempt();
        i += 1;
        requireReadOnlyCommand(words);
        return;
      }
      if (c === "(" || (c === "&" && continued(i + 1))) notExempt();
      if (c === "\n" || c === ";" || c === "|" || (c === "&" && src[i + 1] !== ">")) {
        i += 1;
        if ((c === "|" || c === "&") && (src[i] === c || (c === "|" && src[i] === "&"))) i += 1;
        requireReadOnlyCommand(words);
        words = [];
        if (c === "\n" && heredocs.length) {
          if (nested) notExempt();
          bodies();
        }
      } else if (c === "#") {
        while (i < src.length && src[i] !== "\n") i += 1;
      } else if (c === "<" || c === ">" || c === "&") {
        redirect(nested);
      } else {
        const w = word();
        if (src[i] === "<" || src[i] === ">") {
          if (NAMED_FD.test(w.bare)) notExempt();
          if (/^\d+$/.test(w.bare)) continue;
        }
        words.push(w);
      }
    }
  };

  list(false);
}

/** Every simple command in the line is a read-only tool and every redirection opens an ordinary file. Pipes, lists
 *  and command substitutions are followed, so `cat s.py | python3` and `python3 -c "$(cat s.py)"` are not
 *  read-only; quoting is read as bash reads it. */
export function readOnlyCommand(command, cwd) {
  try {
    requireReadOnly(command, cwd);
    return true;
  } catch {
    // Whatever this reader cannot follow, its own failure included, is judged rather than exempted.
    return false;
  }
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
    // bash reads `<&-sender.py` as a descriptor close and the word `sender.py`, so the path may start after the dashes.
    for (const name of new Set([token, token.replace(/^-+/, "")])) {
      if (!name) continue;
      const candidates = path.isAbsolute(name)
        ? [name]
        : [name, ...dirs.map((d) => path.resolve(d, name))];
      for (const fp of candidates) {
        try {
          const st = fs.statSync(fp);
          if (st.isFile() && st.size <= MAX_READ_BYTES) { out.push(fp); break; }
        } catch { /* keep looking */ }
      }
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

/** `tool` is the hook payload's tool_name. The read-only exemption reads bash grammar, so it applies to the Bash tool
 *  alone: PowerShell takes no backslash as an escape, and a separator bash reads as text can end its statement. */
export function judge(command, cwd, config, tool) {
  if (!config || typeof command !== "string" || !command.trim()) return null;
  // A heredoc body is data to the exemption unless bash would expand it. Every other judgment reads the full text,
  // so a body fed to an interpreter is still judged.
  if (tool === "Bash" && readOnlyCommand(command, cwd)) return null;
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
  try { verdict = judge(command, cwd, loaded.config, payload?.tool_name); } catch { process.exit(0); }
  if (!verdict) process.exit(0);
  process.stderr.write(
    `BLOCKED: ${verdict.reason} (${verdict.where}).\n\n${advice(loaded.config)}\n`);
  process.exit(2);
}

if (process.argv[1]
    && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  main();
}
