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
// file it can read that is a sender outside the declared allowlist exits 2, and
// so does a line that runs shell code it cannot read, since that code could
// name any file.
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
// Quoted runs that may contain spaces, and bare tokens. Without the quoted forms, `python "space dir/blast.py"`
// matches only the tail after the space, which resolves to nothing and lets an unsanctioned sender run — and Windows
// user directories contain spaces as a matter of course. Bare tokens are searched for inside quoted runs too: a quoted
// run can be a whole command line (`sh -c 'python3 blast.py'`), which names no file itself.
const QUOTED_PATH = /"([^"\n]+\.(?:py|mjs|js|ts|ps1|sh|rb))"|'([^'\n]+\.(?:py|mjs|js|ts|ps1|sh|rb))'/g;
const BARE_PATH = /[A-Za-z0-9_.:\\/~-]+\.(?:py|mjs|js|ts|ps1|sh|rb)\b/g;
const SCRIPT_NAME = /\.(?:py|mjs|js|ts|ps1|sh|rb)$/;
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
// The read-only tools with an option that runs a program or writes a file, and those options' letters and long names.
// grep in the Claude Code shell runs ugrep, whose --filter, --pager and --view name a command, whose --config or ---
// loads a file that can, whose -Q starts a TUI that runs $PAGER or the --view command on a key press, and whose
// --save-config writes a file; the host's wrapper hands some of these to the system grep, but the exemption does not
// lean on it. GNU diff --paginate runs pr; file -z and -Z decompress through helpers found on PATH.
const UGREP_UNSAFE = { letters: "Q", names: ["filter", "pager", "view", "config", "-", "query", "save-config"] };
const UNSAFE_OPTIONS = new Map([
  ["grep", UGREP_UNSAFE], ["egrep", UGREP_UNSAFE], ["fgrep", UGREP_UNSAFE],
  ["diff", { letters: "l", names: ["paginate"] }],
  ["file", { letters: "zZ", names: ["uncompress", "uncompress-noreport"] }],
]);
// uniq and xxd write their second operand, so each of their arguments must name an ordinary file or none.
const WRITES_OPERAND = new Set(["uniq", "xxd"]);
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
// bash expands the word after `>&`, and when the result is not a descriptor number or `-`, expands it again as the
// file to open. Text made only of these characters comes out of that second pass unchanged.
const PLAIN_TEXT = /^[\w./,:=%+@-]+$/;
const PARAMETER = /\$(?:\{(?:[A-Za-z_][A-Za-z0-9_]*|\d+|[@*#?$!-])\}|[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])/y;
// What bash would run or rejoin in an unquoted heredoc body: a command substitution, arithmetic, a ${...} operator,
// or a trailing backslash, which joins the line to the next before the delimiter is looked for.
const BODY_EXPANSION = /`|\$[([]|\$\{(?!(?:[A-Za-z_][A-Za-z0-9_]*|\d+|[@*#?$!-])\})|\\$/;
// bash opens /dev/tcp/HOST/PORT and /dev/udp/HOST/PORT as sockets; the rest of /dev, /proc and /sys are devices, other
// processes' descriptors and kernel settings. Only these four streams there are ordinary.
const SPECIAL_ROOT = /^\/(?:dev|proc|sys)(?:\/|$)/;
const STD_STREAMS = new Set(["/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"]);
const STDIN_PATH = /^\/(?:dev\/stdin|dev\/fd\/0|proc\/(?:self|thread-self|\d+)\/fd\/0)$/;
// The shells whose -c string, heredoc and here-string the full judgment reads as code.
const SHELLS = new Set(["sh", "bash", "dash", "zsh", "ksh", "mksh", "ash", "yash", "posh"]);
const DECLARATIONS = new Set(["export", "declare", "typeset", "local", "readonly"]);
// A shell, or a builtin that runs a string or a file as code, named as a word of its own.
const RUNS_CODE = new RegExp(`(?:^|[^\\w.-])(?:${[...SHELLS, "env", "eval", "trap", "source"].join("|")})(?![\\w.-])`
  + "|(?:^|[\\s;&|({])\\.(?=\\s)");
// Reserved words that may come before a command's name. `time` is read with the runners below.
const RESERVED = new Set(["!", "{", "if", "then", "elif", "else", "do", "while", "until", "coproc"]);
// Programs that run the command after their options, each with the option letters whose argument is the next word.
// timeout also takes a duration; env's -S is read on its own.
const RUNNERS = new Map([["sudo", "CDghpRrtTUu"], ["doas", "Cu"], ["env", "aCSu"], ["exec", "a"], ["time", "fo"],
  ["nice", "n"], ["timeout", "ks"], ["nohup", ""], ["setsid", ""], ["stdbuf", "eio"], ["xargs", "adEILnPs"],
  ["command", ""], ["builtin", ""]]);
// A string with none of these characters reads back as itself.
const CODE_LIKE = /[\s;&|()<>$`'"\\]/;
const LENIENT_DEPTH = 8;
const EXPANSION_LIMIT = 4096;
const ENV_ESCAPES = { '"': '"', "#": "#", $: "$", "'": "'", "\\": "\\", _: " ", f: "\f", n: "\n", r: "\r", t: "\t",
  v: "\v" };
const C_ESCAPE = /\\(?:([0-7]{1,3})|x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})|c([\s\S])|([\s\S]))/g;
const C_ESCAPES = { a: "\x07", b: "\b", e: "\x1b", E: "\x1b", f: "\f", n: "\n", r: "\r", t: "\t", v: "\v", "\\": "\\",
  "'": "'", '"': '"', "?": "?" };
// A piece of a word whose text the line does not fix: an expansion, or a command's output.
const DYNAMIC = Object.freeze({ text: "", dynamic: true });
const NOT_EXEMPT = Symbol("not exempt");
const UNREADABLE = Symbol("unreadable");

function notExempt() {
  throw NOT_EXEMPT;
}

function cannotRead() {
  throw UNREADABLE;
}

/** The text of a `$'...'` string's contents. bash ends the string at a NUL. */
function ansiC(body) {
  return body.replace(C_ESCAPE, (all, oct, hex, u, U, ctl, ch) => {
    if (oct) return String.fromCharCode(parseInt(oct, 8) & 0xff);
    const code = parseInt(hex || u || U, 16);
    if (!Number.isNaN(code)) return code <= 0x10ffff ? String.fromCodePoint(code) : "";
    if (ctl !== undefined) return String.fromCharCode(ctl.charCodeAt(0) & 0x1f);
    return C_ESCAPES[ch] ?? all;
  }).split("\0")[0];
}

/** A variable's value in this process's environment, which the hook shares with the shell, or nothing. */
function variable(name) {
  return /^[A-Za-z_]/.test(name) && Object.hasOwn(process.env, name) ? process.env[name] : null;
}

/** A redirection target that opens an ordinary file: written out literally, outside /dev, /proc and /sys unless it is
 *  one of the standard streams, and a regular file if it exists already. */
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

/** Whether `arg` sets one of `options`: a letter anywhere in a short-option cluster, or a long name that abbreviates
 *  one of its names (getopt takes any unambiguous abbreviation) or begins with one (`---FILE`). */
function setsOption(options, arg) {
  if (/^-[^-]/.test(arg)) return [...arg.slice(1)].some((c) => options.letters.includes(c));
  const name = arg.startsWith("--") ? arg.slice(2).split("=")[0] : "";
  return name !== "" && options.names.some((n) => n.startsWith(name) || name.startsWith(n));
}

/** The command's words up to its name are literal, assign nothing, pass only harmless prefix options, and name a
 *  read-only tool; printf's first argument is not an option (printf -v NAME assigns); a tool in UNSAFE_OPTIONS takes
 *  only literal arguments, none of them one of those options; each argument of a tool in WRITES_OPERAND is an
 *  ordinary target. */
function requireReadOnlyCommand(words, cwd) {
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
  const unsafe = UNSAFE_OPTIONS.get(texts[i]);
  if (unsafe && words.slice(i + 1).some((w) => !w.literal || setsOption(unsafe, w.text))) notExempt();
  if (WRITES_OPERAND.has(texts[i]) && !words.slice(i + 1).every((w) => ordinaryTarget(w, cwd))) notExempt();
}

/** Reads `src` as bash reads it and calls `onCommand(words, redirects, piped)` at the end of every simple command,
 *  those in substitutions included; `piped` says the command reads a pipe. A word carries its text after quote removal
 *  (`text`), whether it holds no expansion or pattern (`literal`), whether any of it is quoted or escaped (`quoted`),
 *  its source without line continuations (`bare`), whether an expansion may supply its first character
 *  (`opensDynamic`), whether an unquoted expansion or pattern may split it into several words (`splits`), and its
 *  pieces, each with the name of the variable it came from, if any (`parts`).
 *
 *  The read-only exemption reads with `full` false. It follows a subset of the grammar and throws at the rest:
 *  backquotes, arithmetic, subshells, process substitution, `${...}` operators, a heredoc inside a substitution, a
 *  continuation that joins an operator or an expansion, and an unquoted heredoc body bash would expand. The full
 *  judgment reads with `full` true: it follows those too, decodes `$'...'`, gives a variable its value in this
 *  process's environment, and throws only at what bash cannot parse or where a heredoc's end cannot be known. It hands
 *  `onText` the inside of an arithmetic command `((...))`, which a POSIX shell reads as two subshells. With `text` it
 *  reads `src` as an unquoted heredoc body, following only its expansions. */
function readShell(src, { cwd, onCommand, onText, full = false, text = false }) {
  let i = 0;
  let piped = false;
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

  // A word's text built from its pieces, and whether a piece the line does not fix comes before any it does. `parts`
  // keeps each piece's text and, for a variable, its name.
  const builder = () => {
    const b = { text: "", literal: true, opensDynamic: false, fixed: false, parts: [] };
    b.add = (piece, dynamic, opens = dynamic, fixed = !dynamic && piece !== "", parts = [{ text: piece }]) => {
      if (opens && !b.fixed) b.opensDynamic = true;
      b.fixed ||= fixed;
      b.text += piece;
      b.parts.push(...parts);
      if (dynamic) b.literal = false;
    };
    b.addPiece = (piece) => b.add(piece.text, piece.dynamic, piece.opens ?? piece.dynamic, piece.fixed,
      piece.parts ?? [{ text: piece.text, name: piece.name }]);
    return b;
  };

  // At a `$`: consumes an expansion and returns the piece it adds to the word, or returns null for a literal dollar
  // sign.
  const dollar = (inQuotes) => {
    if (full) return dollarFull(inQuotes);
    if (continued(i + 1)) notExempt();
    const next = src[i + 1];
    if (next === "(") {
      if (src[i + 2] === "(") notExempt();
      i += 2;
      list(true);
      return DYNAMIC;
    }
    if (next === "[" || next === "{") return sticky(PARAMETER) ? DYNAMIC : notExempt();
    if (!inQuotes && next === "'") {
      for (i += 2; src[i] !== "'"; i += src[i] === "\\" ? 2 : 1) if (i >= src.length) notExempt();
      i += 1;
      return DYNAMIC;
    }
    if (!inQuotes && next === '"') {
      i += 1;
      doubleQuoted();
      return DYNAMIC;
    }
    return sticky(PARAMETER) ? DYNAMIC : null;
  };

  const dollarFull = (inQuotes) => {
    let j = i + 1;
    while (continued(j)) j += 2;
    const next = src[j];
    if (next === "(") {
      i = j;
      if (src[i + 1] === "(" && arithmetic()) return DYNAMIC;
      i += 1;
      list(true);
      return DYNAMIC;
    }
    if (next === "[") {
      i = j;
      enclosed("[", "]");
      return DYNAMIC;
    }
    if (next === "{") {
      i = j + 1;
      return braced(inQuotes);
    }
    if (next === "'" && !inQuotes) {
      const from = j + 1;
      for (i = from; src[i] !== "'"; i += src[i] === "\\" ? 2 : 1) if (i >= src.length) cannotRead();
      i += 1;
      return { text: ansiC(src.slice(from, i - 1)), dynamic: false, quoted: true };
    }
    if (next === '"' && !inQuotes) {
      i = j;
      const q = doubleQuoted();
      return { text: q.text, dynamic: !q.literal, opens: q.opensDynamic, fixed: q.fixed, parts: q.parts,
               quoted: true };
    }
    const name = /[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-]/y;
    name.lastIndex = j;
    const m = name.exec(src);
    if (!m) return null;
    i = j + m[0].length;
    return { text: variable(m[0]) ?? "", dynamic: true, name: m[0] };
  };

  // After `${`: the parameter and whatever follows it up to the closing brace. The piece is the variable's value or,
  // where an operator supplies one for an unset variable, that word. bash 5.3 runs the list in `${ list; }` and
  // `${| list; }` in the current shell.
  const braced = (inQuotes) => {
    if (src[i] !== undefined && " \t\n|".includes(src[i])) {
      if (src[i] === "|") i += 1;
      list(true, true);
      return DYNAMIC;
    }
    const name = sticky(/[#!]?(?:[A-Za-z_][A-Za-z0-9_]*|\d+|[@*#?$!-])?/y);
    let tail = "";
    for (;;) {
      const c = src[i];
      if (c === undefined) cannotRead();
      if (c === "}") break;
      if (continued(i)) {
        i += 2;
      } else if (c === "\\") {
        tail += src[i + 1] ?? "";
        i += 2;
      } else if (c === "'") {
        const end = src.indexOf("'", i + 1);
        if (end < 0) cannotRead();
        tail += inQuotes ? src.slice(i, end + 1) : src.slice(i + 1, end);
        i = end + 1;
      } else if (c === '"') {
        tail += doubleQuoted().text;
      } else if (c === "`") {
        backquote(inQuotes);
      } else {
        const piece = c === "$" && dollar(inQuotes);
        tail += piece ? piece.text : c;
        if (!piece) i += 1;
      }
    }
    i += 1;
    const value = variable(name);
    const op = /^:?[-=+?]/.exec(tail);
    if (!op) return { text: value ?? "", dynamic: true, name: !tail || tail.startsWith("[") ? name : undefined };
    const set = value !== null && (value !== "" || !op[0].startsWith(":"));
    const word = tail.slice(op[0].length);
    if (op[0].endsWith("+")) return { text: set ? word : "", dynamic: true };
    if (op[0].endsWith("?")) return { text: value ?? "", dynamic: true, name };
    return { text: set ? value : word, dynamic: true, name };
  };

  // At a backquote: the command it holds, read once `\$`, `` \` ``, `\\` and, inside double quotes, `\"` have lost
  // their backslash.
  const backquote = (inQuotes) => {
    let code = "";
    for (i += 1; src[i] !== "`"; i += 1) {
      if (i >= src.length) cannotRead();
      if (src[i] === "\\" && i + 1 < src.length) {
        i += 1;
        code += "$`\\".includes(src[i]) || (inQuotes && src[i] === '"') ? src[i] : `\\${src[i]}`;
      } else {
        code += src[i];
      }
    }
    i += 1;
    readShell(code, { cwd, onCommand, onText, full });
  };

  // Inside arithmetic or a `$[...]`: one quoted run, expansion or character.
  const arithmeticPiece = () => {
    const c = src[i];
    if (c === "\\") {
      i += 2;
    } else if (c === "'") {
      const end = src.indexOf("'", i + 1);
      if (end < 0) cannotRead();
      i = end + 1;
    } else if (c === '"') {
      doubleQuoted();
    } else if (c === "`") {
      backquote(false);
    } else if (!(c === "$" && dollar(true))) {
      i += 1;
    }
  };

  // At `((`: bash reads arithmetic up to the matching `))`. When the parenthesis that closes the first `(` is not
  // followed by another, the two were subshells, and this returns false having consumed nothing. Each start is tried
  // once, so a run of parentheses costs no more than its length times the text after it.
  const arithmeticEnds = new Map();
  const arithmetic = () => {
    const from = i;
    if (!arithmeticEnds.has(from)) {
      let depth = 0;
      let end = -1;
      for (i += 2; i < src.length && end < 0; ) {
        if (src[i] === "(") {
          depth += 1;
          i += 1;
        } else if (src[i] === ")" && depth > 0) {
          depth -= 1;
          i += 1;
        } else if (src[i] === ")") {
          if (src[i + 1] !== ")") break;
          end = i + 2;
        } else {
          arithmeticPiece();
        }
      }
      arithmeticEnds.set(from, end);
    }
    const end = arithmeticEnds.get(from);
    i = end < 0 ? from : end;
    return end >= 0;
  };

  const enclosed = (open, close) => {
    let depth = 0;
    for (i += 1; src[i] !== close || depth > 0; ) {
      if (i >= src.length) cannotRead();
      if (src[i] === open) depth += 1;
      if (src[i] === close) depth -= 1;
      if (src[i] === open || src[i] === close) i += 1;
      else arithmeticPiece();
    }
    i += 1;
  };

  // At a double quote: the quoted text, whether it holds no expansion, whether an expansion comes before its first
  // fixed character, and whether it has one.
  const doubleQuoted = () => {
    const b = builder();
    for (i += 1; src[i] !== '"'; ) {
      const c = src[i];
      if (c === undefined) cannotRead();
      if (c === "`") {
        if (!full) notExempt();
        backquote(true);
        b.add("", true);
      } else if (c === "\\" && src[i + 1] !== undefined && '$`"\\\n'.includes(src[i + 1])) {
        if (src[i + 1] !== "\n") b.add(src[i + 1], false);
        i += 2;
      } else {
        const piece = c === "$" && dollar(true);
        if (piece) {
          b.addPiece(piece);
        } else {
          b.add(c, false);
          i += 1;
        }
      }
    }
    i += 1;
    return b;
  };

  // One word after quote removal: its text, whether it holds no expansion or pattern, whether any of it is quoted or
  // escaped, its source without line continuations, whether an expansion may supply its first character, and whether
  // it may split.
  const word = () => {
    const b = builder();
    let bare = "";
    let quoted = false;
    let splits = false;
    while (i < src.length && !ENDS_WORD.has(src[i])) {
      const from = i;
      const c = src[i];
      if (continued(i)) {
        i += 2;
        continue;
      }
      if (c === "'") {
        const end = src.indexOf("'", i + 1);
        if (end < 0) cannotRead();
        b.add(src.slice(i + 1, end), false);
        i = end + 1;
        quoted = true;
      } else if (c === '"') {
        const q = doubleQuoted();
        b.add(q.text, !q.literal, q.opensDynamic, q.fixed, q.parts);
        quoted = true;
      } else if (c === "\\") {
        if (i + 1 >= src.length) cannotRead();
        b.add(src[i + 1], false);
        i += 2;
        quoted = true;
      } else if (c === "`") {
        if (!full) notExempt();
        backquote(false);
        b.add("", true);
        splits = true;
      } else {
        const piece = c === "$" && dollar(false);
        if (piece) {
          b.addPiece(piece);
          quoted ||= Boolean(piece.quoted);
          splits ||= piece.dynamic && !piece.quoted;
        } else {
          b.add(c, false);
          i += 1;
          if ("*?[]{}".includes(c)) {
            b.literal = false;
            splits = true;
          }
        }
      }
      bare += src.slice(from, i);
    }
    return { text: b.text, literal: b.literal, quoted, bare, opensDynamic: b.opensDynamic, splits, parts: b.parts };
  };

  // At `<(` or `>(`: a word naming a pipe to the commands inside.
  const processSubstitution = () => {
    const from = i;
    i += 2;
    list(true);
    return { text: "", literal: false, quoted: false, bare: src.slice(from, i), opensDynamic: true, splits: false,
             process: true };
  };

  // A redirection, and the descriptor `fd` it applies to when a number or {name} came before it.
  const redirect = (nested, fd) => {
    const op = sticky(REDIRECT_OP);
    if (continued(i) && !full) notExempt();
    skipBlanks();
    const dup = op === "<&" || op === ">&";
    // bash reads a `-` after `<&` or `>&` as a token of its own that closes the descriptor; the text after it starts a
    // new word.
    if (dup && src[i] === "-") {
      i += 1;
      return { op, fd };
    }
    if (full && (src[i] === "<" || src[i] === ">") && src[i + 1] === "(") {
      return { op, fd, target: processSubstitution() };
    }
    const target = word();
    if (!target.bare || target.bare.startsWith("#")) cannotRead();
    if (op === "<<" || op === "<<-") {
      // The delimiter is the quote-removed word; any quoting in it leaves the body unexpanded.
      if (!target.literal || !target.text || target.text.includes("\n")) cannotRead();
      if (nested && !full) notExempt();
      const doc = { delimiter: target.text, tabs: op === "<<-", quoted: target.quoted };
      heredocs.push(doc);
      return { op, fd, target, doc };
    }
    if (full) return { op, fd, target };
    if (dup && /^\d+-?$/.test(target.text)) {
      // Any descriptor but the standard three was inherited from whatever started the shell, and may be a socket.
      if (!target.literal || !/^[012]-?$/.test(target.text)) notExempt();
    } else if ((dup && !PLAIN_TEXT.test(target.text)) || (op !== "<<<" && !ordinaryTarget(target, cwd))) {
      notExempt();
    }
    return { op, fd, target };
  };

  // After the newline that ends a line, the bodies of the heredocs it opened, in order. bash joins a line of an
  // unquoted body that ends in a backslash to the next before it looks for the delimiter, and ends a body the input
  // does not delimit at the end of the input.
  const bodies = () => {
    for (const doc of heredocs.splice(0)) {
      let body = "";
      for (;;) {
        if (i >= src.length) {
          if (!full) notExempt();
          break;
        }
        let end = src.indexOf("\n", i);
        if (end < 0) end = src.length;
        let line = src.slice(i, end);
        i = end + 1;
        while (full && !doc.quoted && /(?:^|[^\\])(?:\\\\)*\\$/.test(line) && i <= src.length) {
          end = src.indexOf("\n", i);
          if (end < 0) end = src.length;
          line = line.slice(0, -1) + src.slice(i, end);
          i = end + 1;
        }
        if (doc.tabs) line = line.replace(/^\t+/, "");
        if (line === doc.delimiter) break;
        if (!full && !doc.quoted && BODY_EXPANSION.test(line)) notExempt();
        body += `${line}\n`;
      }
      if (full && !doc.quoted) readShell(body, { cwd, onCommand, onText, full, text: true });
      doc.onBody?.(body);
    }
  };

  // A list of commands, up to the end of src or, inside `$(` or `(`, its closing parenthesis, or with `brace`, inside
  // `${ `, the `}` that ends it. A command reads a pipe when a `|` comes before it, or when the command a nested list
  // belongs to does.
  const list = (nested, brace = false) => {
    let words = [];
    let redirects = [];
    let fd = null;
    let cases = 0;
    // Inside a case command, whether a pattern comes next: after `in`, `;;`, `;&` or `;;&`.
    let patterns = false;
    let groups = 0;
    const inherited = piped;
    const command = () => {
      onCommand(words, redirects, piped);
      words = [];
      redirects = [];
    };
    for (;;) {
      skipBlanks();
      const c = src[i];
      if (full && c === ")" && cases > 0 && patterns) {
        // A case pattern ends here. bash matches it against the word; it names no file.
        i += 1;
        patterns = false;
        for (const w of words) w.pattern = true;
        command();
        continue;
      }
      if (c === undefined || c === ")") {
        if (brace || (c === ")") !== nested) cannotRead();
        if (c === undefined && heredocs.length && !full) notExempt();
        i += 1;
        command();
        if (c === undefined) for (const doc of heredocs.splice(0)) doc.onBody?.("");
        piped = inherited;
        return;
      }
      if (c === "(") {
        if (!full) notExempt();
        const from = i;
        if (patterns && (!words.length || words.at(-1).bare === "in")) {
          i += 1;
        } else if (src[i + 1] === "(" && arithmetic()) {
          // dash and other POSIX shells read `((` as two subshells.
          onText?.(src.slice(from + 2, i - 2));
        } else {
          i += 1;
          list(true);
        }
        continue;
      }
      if (c === "&" && continued(i + 1) && !full) notExempt();
      if (c === "\n" || c === ";" || c === "|" || (c === "&" && src[i + 1] !== ">")) {
        if (c === ";" && cases > 0 && (src[i + 1] === ";" || src[i + 1] === "&")) patterns = true;
        const pipe = c === "|" && src[i + 1] !== "|";
        i += 1;
        if ((c === "|" || c === "&") && (src[i] === c || (c === "|" && src[i] === "&"))) i += 1;
        command();
        piped = pipe || inherited;
        if (c === "\n" && heredocs.length) {
          if (nested && !full) notExempt();
          bodies();
        }
      } else if (c === "#") {
        while (i < src.length && src[i] !== "\n") i += 1;
      } else if (full && (c === "<" || c === ">") && src[i + 1] === "(") {
        words.push(processSubstitution());
      } else if (c === "<" || c === ">" || c === "&") {
        redirects.push(redirect(nested, fd));
        fd = null;
      } else {
        const w = word();
        if (src[i] === "<" || src[i] === ">") {
          if (NAMED_FD.test(w.bare)) {
            if (!full) notExempt();
            fd = w.bare;
            continue;
          }
          if (/^\d+$/.test(w.bare)) {
            fd = w.bare;
            continue;
          }
        }
        if (full && words.every((v) => RESERVED.has(v.bare))) {
          if (brace && w.bare === "}" && groups === 0) {
            command();
            piped = inherited;
            return;
          }
          if (w.bare === "{") groups += 1;
          if (w.bare === "}") groups -= 1;
          if (w.bare === "case") cases += 1;
          if (w.bare === "esac" && cases > 0) {
            cases -= 1;
            patterns = false;
          }
        }
        const opener = words.findIndex((v) => !RESERVED.has(v.bare));
        if (full && w.bare === "in" && opener >= 0 && words[opener].bare === "case" && words.length === opener + 2) {
          patterns = true;
        }
        words.push(w);
      }
    }
  };

  // An unquoted heredoc body: text in which only expansions do anything.
  const body = () => {
    while (i < src.length) {
      if (src[i] === "\\") i += 2;
      else if (src[i] === "`") backquote(true);
      else if (!(src[i] === "$" && dollar(true))) i += 1;
    }
  };

  if (text) body();
  else list(false);
}

/** Every simple command in the line is a read-only tool and every redirection opens an ordinary file. Pipes, lists
 *  and command substitutions are followed, so `cat s.py | python3` and `python3 -c "$(cat s.py)"` are not
 *  read-only; quoting is read as bash reads it. */
export function readOnlyCommand(command, cwd) {
  try {
    readShell(command, { cwd, onCommand: (words) => requireReadOnlyCommand(words, cwd) });
    return true;
  } catch {
    // Whatever this reader cannot follow, its own failure included, is judged rather than exempted.
    return false;
  }
}

/** The words env runs from a -S string, split as GNU env splits it, or null for a string env refuses or one that
 *  expands a variable, whose value is env's to know. */
function splitEnvString(s) {
  const out = [];
  let arg = null;
  let single = false;
  let double = false;
  const end = () => {
    if (arg !== null) out.push(arg);
    arg = null;
  };
  for (let k = 0; k < s.length; k += 1) {
    let c = s[k];
    if ((c === "'" && !double) || (c === '"' && !single)) {
      if (c === "'") single = !single;
      else double = !double;
      arg ??= "";
      continue;
    }
    if (" \t\n\v\f\r".includes(c) && !single && !double) {
      end();
      continue;
    }
    if (c === "#" && arg === null) break;
    if (c === "\\" && !(single && s[k + 1] !== "\\" && s[k + 1] !== "'")) {
      k += 1;
      c = s[k];
      if (c === "_" && !double) {
        end();
        continue;
      }
      if (c === "c") {
        if (double) return null;
        break;
      }
      c = ENV_ESCAPES[c];
      if (c === undefined) return null;
    } else if (c === "$" && !single) {
      return null;
    }
    arg = (arg ?? "") + c;
  }
  if (single || double) return null;
  end();
  return out;
}

const literalWord = (text) => ({ text, literal: true, quoted: true, bare: text, opensDynamic: false, splits: false });

/** The indexes of the words that run a simple command: each program in RUNNERS and, last, the command it runs, past
 *  assignments, reserved words and unquoted expansions that come out empty, which leave no word behind; `values` are
 *  those the line assigns. The chain ends early at an env -S, whose words env splits, and at command -v. */
function commandChain(words, values) {
  const chain = [];
  let k = 0;
  while (k < words.length) {
    const w = words[k];
    const empty = w.splits && !w.quoted && w.text === "" && !w.parts?.some((p) => values.has(p.name));
    if (RESERVED.has(w.bare) || ASSIGNMENT.test(w.bare) || empty) {
      k += 1;
      continue;
    }
    chain.push(k);
    const runner = w.literal ? path.posix.basename(w.text) : "";
    const takes = RUNNERS.get(runner);
    if (takes === undefined) break;
    for (k += 1; words[k]?.literal && /^-./.test(words[k].text); k += 1) {
      const option = words[k].text;
      if (option === "--") {
        k += 1;
        break;
      }
      const at = option.startsWith("--") ? -1 : [...option.slice(1)].findIndex((letter) => takes.includes(letter));
      if (runner === "env" && (option[at + 1] === "S" || /^--s/.test(option))) return chain;
      if (runner === "command" && /^-[^-]*[vV]/.test(option)) return chain;
      if (at >= 0 && at === option.length - 2) k += 1;
    }
    if (runner === "timeout") k += 1;
  }
  return chain;
}

/** What the full judgment reads from a line: the text of every word bash would build, in the line and in the shell
 *  code it carries, those of them bash would expand as globs, the words built from variables with the values the line
 *  assigns them, the directories it changes into, and, when it runs shell code the line does not show, where. */
function readLine(command, cwd) {
  const found = { texts: [], globs: [], templates: [], values: new Map(), dirs: [], unreadable: null };
  const assign = (name, value) => {
    const key = name.replace(/\+$|\[.*$/, "");
    if (!found.values.has(key)) found.values.set(key, new Set());
    found.values.get(key).add(value);
  };
  // Inside a string read only for the paths it names, nothing is refused.
  let lenient = 0;
  const unreadable = (where) => {
    if (!lenient) found.unreadable ??= where;
  };
  const read = (code) => readShell(code, { cwd, onCommand: inspect, onText: readLeniently, full: true });
  const readCode = (code, what) => {
    try {
      read(code);
    } catch {
      unreadable(`${what} cannot be read`);
    }
  };
  const readAsCode = new WeakSet();
  // A word that holds code must be in the line and written out.
  const readWord = (w, what) => {
    if (w) readAsCode.add(w);
    if (!w) unreadable(`${what} is not in the line`);
    else if (!w.literal) unreadable(`${what} is built from an expansion`);
    else readCode(w.text, what);
  };
  // Any other word or heredoc body may still run as code later: written to a file and run, handed to su, watch or ssh,
  // or kept for an alias or an arithmetic context. Each is read for the paths it names, to a bounded depth.
  const readLeniently = (text) => {
    if (lenient >= LENIENT_DEPTH || !CODE_LIKE.test(text)) return;
    lenient += 1;
    try {
      read(text);
    } catch {
      // The words read before the reader stopped are kept; a string need not be code at all.
    } finally {
      lenient -= 1;
    }
  };
  const inside = (re, name) => re.test(path.posix.normalize(name)) || re.test(path.resolve(cwd || process.cwd(), name));

  // What a shell or source reads from standard input. Only a heredoc or here-string written out in the line, or a file,
  // shows it; a pipe, a descriptor, a stream under /dev, /proc or /sys, or the input the command inherits does not.
  const input = (redirects, who) => {
    const what = `the input of ${who}`;
    const from = redirects.filter((r) => (r.fd === null || r.fd === "0") && r.op.startsWith("<"));
    if (!from.length) unreadable(`${what} is not in the line`);
    for (const r of from) {
      if (r.doc) {
        r.doc.onBody = (body) => (r.doc.quoted || !/[$`\\]/.test(body)
          ? readCode(body, what) : unreadable(`${what} is built from an expansion`));
      } else if (r.op === "<<<") {
        readWord(r.target, what);
      } else if (r.op === "<&" || !r.target.literal || inside(SPECIAL_ROOT, r.target.text)) {
        unreadable(`${what} is not in the line`);
      }
    }
  };

  // A script operand under /dev, /proc or /sys is a stream: standard input, or a descriptor the line does not show.
  // A script operand built from an expansion is resolved like any interpreter's, unless it may be a stream or the
  // command's input is a pipe or a redirection it could name.
  const script = (operand, redirects, who, piped) => {
    if (operand.process) unreadable(`${who} runs the output of a process substitution`);
    else if (operand.literal && inside(STDIN_PATH, operand.text)) input(redirects, who);
    else if (inside(SPECIAL_ROOT, operand.text)) {
      unreadable(operand.literal ? `${who} reads ${operand.text}` : `${who} reads a stream named by an expansion`);
    }
    else if (!operand.literal && (piped || redirects.some((r) => (r.fd === null || r.fd === "0") && r.op[0] === "<"))) {
      unreadable(`the script of ${who} is built from an expansion, and its input is a pipe or a redirection`);
    }
  };

  // The shell a word names, if any: literally, by its value in the environment, or by a value the line assigns.
  const shellNamed = (w) => {
    if (ASSIGNMENT.test(w.bare)) return null;
    const texts = w.literal ? [w.text] : substitutions(w.parts ?? [], found.values, { left: 64 });
    return texts.map((t) => path.posix.basename(t)).find((name) => SHELLS.has(name)) ?? null;
  };

  // A shell named at words[j]. Its -c string is code. At the head of the command so is its standard input, when it has
  // no script operand, and an expansion among its options may be -c.
  const shell = (words, j, redirects, head, piped) => {
    const who = shellNamed(words[j]);
    let c = false;
    let s = false;
    let k = j + 1;
    for (; k < words.length; k += 1) {
      const w = words[k];
      if (!w.literal) {
        if (c || w.process || !(/^[-+]/.test(w.text) || (head && w.opensDynamic))) break;
        if (w.splits) return unreadable(`the options of ${who} are built from an expansion`);
        // This may be -c, and the next word its string, or it may be the script.
        if (words[k + 1]) readWord(words[k + 1], `the -c string of ${who}`);
        break;
      }
      const option = w.text;
      if (option === "-" || option === "--") {
        k += 1;
        break;
      }
      if (!/^[-+]./.test(option)) break;
      if (option === "--version" || option === "--help") return;
      if (option === "--rcfile" || option === "--init-file" || option === "--emulate") k += 1;
      if (option.startsWith("--")) continue;
      if (option[0] === "-") {
        c ||= option.includes("c");
        s ||= option.includes("s");
      }
      k += (option.match(/[oO]/g) ?? []).length;
    }
    if (c) return readWord(words[k], `the -c string of ${who}`);
    if (!head) return;
    if (words[k] && !s) return script(words[k], redirects, who, piped);
    input(redirects, who);
  };

  // env at words[j]. A -S string is split into words, and env reads its options again from them on. At the head of the
  // command an expansion among its options may be -S.
  const env = (words, j, redirects, head, piped) => {
    for (let k = j + 1; k < words.length; k += 1) {
      const w = words[k];
      if (!w.literal) {
        if (w.text.startsWith("-") || (head && w.opensDynamic)) {
          unreadable("the options of env are built from an expansion");
        }
        return;
      }
      const option = w.text;
      if (!/^-./.test(option) || option === "--") return;
      let split = false;
      let attached = null;
      let takesNext = false;
      if (option.startsWith("--")) {
        const eq = option.indexOf("=");
        const name = option.slice(2, eq < 0 ? undefined : eq);
        split = name.startsWith("s") && "split-string".startsWith(name);
        if (eq >= 0) attached = option.slice(eq + 1);
        else takesNext = split || ["unset", "chdir", "argv0"].some((long) => long.startsWith(name));
      } else {
        const at = [...option.slice(1)].findIndex((letter) => "aCSu".includes(letter));
        if (at >= 0) {
          split = option[at + 1] === "S";
          attached = option.slice(at + 2) || null;
          takesNext = attached === null;
        }
      }
      if (takesNext) k += 1;
      if (!split) continue;
      const what = "the -S string of env";
      if (attached === null && !words[k]) return unreadable(`${what} is not in the line`);
      if (attached === null && !words[k].literal) return unreadable(`${what} is built from an expansion`);
      const made = splitEnvString(attached ?? words[k].text);
      if (!made) return unreadable(`${what} cannot be read`);
      return inspect([...words.slice(0, j + 1), ...made.map(literalWord), ...words.slice(k + 1)], redirects, piped);
    }
  };

  const inspect = (words, redirects, piped = false) => {
    const chain = commandChain(words, found.values);
    const head = chain.at(-1) ?? -1;
    // An unquoted pattern character or expansion makes a word a glob, except in a case pattern or a [[ test.
    const tested = words[head]?.bare === "[[";
    for (const w of [...words, ...redirects.map((r) => r.target).filter(Boolean)]) {
      const glob = w.splits && !w.pattern && !tested;
      found.texts.push(w.text);
      if (glob && /[*?[]/.test(w.text)) found.globs.push(w.text);
      if (w.parts?.some((p) => p.name)) found.templates.push({ parts: w.parts, glob });
    }
    const name = words[head]?.literal ? words[head].text : null;
    const args = words.slice(head + 1);
    // The values the line gives a variable: in a command made only of assignments, in a declaration's arguments, and
    // as a for loop's words.
    for (const w of head < 0 ? words : DECLARATIONS.has(name) ? args : []) {
      if (ASSIGNMENT.test(w.bare)) assign(w.text.slice(0, w.text.indexOf("=")), w.text.slice(w.text.indexOf("=") + 1));
    }
    if (name === "for" && args[1]?.bare === "in") for (const w of args.slice(2)) assign(args[0].text, w.text);
    if (name === "cd" || name === "pushd") {
      const dir = args.find((w) => !w.text.startsWith("-"));
      if (dir) found.dirs.push(dir.text);
    }
    if (name === "eval" && args.length) {
      for (const w of args) readAsCode.add(w);
      if (args.some((w) => !w.literal)) unreadable("the arguments of eval are built from an expansion");
      else readCode(args.map((w) => w.text).join(" "), "the arguments of eval");
    }
    if (name === "trap") {
      let n = 0;
      while (args[n]?.literal && /^-[lpP]+$/.test(args[n].text)) n += 1;
      if (args[n]?.text === "--") n += 1;
      if (args.length - n > 1) readWord(args[n], "the trap action");
    }
    const operand = args[0]?.text === "--" ? args[1] : args[0];
    if ((name === "source" || name === ".") && operand) script(operand, redirects, name, piped);
    // A command named by an expansion and given -c may be a shell.
    if (words[head] && !words[head].literal && args[0]?.literal && /^-[A-Za-z]*c[A-Za-z]*$/.test(args[0].text)) {
      readWord(args[1], "the -c string of a command named by an expansion");
    }
    for (let j = 0; j < words.length; j += 1) {
      const w = words[j];
      if (w.literal && !ASSIGNMENT.test(w.bare) && path.posix.basename(w.text) === "env") {
        env(words, j, redirects, chain.includes(j), piped);
      }
      if (shellNamed(w)) shell(words, j, redirects, j === head, piped);
    }
    for (const w of words) if (!readAsCode.has(w)) readLeniently(w.text);
    for (const r of redirects) {
      if (r.target && !readAsCode.has(r.target)) readLeniently(r.target.text);
      if (r.doc) r.doc.onBody ??= readLeniently;
    }
  };

  try {
    read(command);
  } catch {
    // The words read before the reader stopped are kept, and the raw text is still searched. A line it cannot follow
    // that may hand code to a shell is refused.
    if (RUNS_CODE.test(command)) {
      unreadable("the line cannot be read to its end, and it names a shell, env, eval, trap or source");
    }
  }
  return found;
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

// The expansions below share one budget of work for a whole line: each extra word a brace or a variable makes, each
// directory a glob reads and each path it visits spends one unit. A line that spends it all is refused rather than
// resolved in part.

/** The items of a `{a..e}` or `{1..9..2}` sequence, or null when `inner` is not one. */
function braceSequence(inner, budget) {
  const m = /^(-?\d+|[A-Za-z])\.\.(-?\d+|[A-Za-z])(?:\.\.(-?\d+))?$/.exec(inner);
  if (!m || /\d/.test(m[1]) !== /\d/.test(m[2])) return null;
  const numeric = /\d/.test(m[1]);
  const [from, to] = numeric ? [Number(m[1]), Number(m[2])] : [m[1].charCodeAt(0), m[2].charCodeAt(0)];
  const step = (Math.abs(Number(m[3] ?? 1)) || 1) * (from <= to ? 1 : -1);
  const width = numeric && /^-?0\d/.test(m[1] + m[2]) ? Math.max(m[1].length, m[2].length) : 0;
  const out = [];
  for (let n = from; (step > 0 ? n <= to : n >= to) && budget.left > out.length; n += step) {
    out.push(numeric ? String(n).padStart(width, "0") : String.fromCharCode(n));
  }
  if (step > 0 ? from + step * out.length <= to : from + step * out.length >= to) budget.left = 0;
  return out;
}

/** The words bash makes of `text` by brace expansion. Quoting is not known here, so a quoted brace is expanded too,
 *  which only adds paths to try. */
function braceExpand(text, budget, expanded = false) {
  if (budget.left <= 0) return [];
  for (let open = text.indexOf("{"); open >= 0; open = text.indexOf("{", open + 1)) {
    let depth = 0;
    const cuts = [open];
    for (let k = open; k < text.length; k += 1) {
      if (text[k] === "{") depth += 1;
      if (text[k] === "," && depth === 1) cuts.push(k);
      if (text[k] !== "}" || --depth > 0) continue;
      const items = cuts.length > 1
        ? [...cuts.slice(1), k].map((cut, n) => text.slice(cuts[n] + 1, cut))
        : braceSequence(text.slice(open + 1, k), budget);
      if (!items) break;
      return items.flatMap((item) => braceExpand(text.slice(0, open) + item + text.slice(k + 1), budget, true));
    }
  }
  if (expanded) budget.left -= 1;
  return [text];
}

/** The texts a word can take when each variable in it takes, besides the value it was read with, each value the line
 *  assigns it. */
function substitutions(parts, values, budget) {
  let out = [""];
  for (const part of parts) {
    const choices = new Set([part.text, ...(values.get(part.name) ?? [])]);
    if (choices.size > 1) budget.left -= out.length * (choices.size - 1);
    if (budget.left <= 0) return [];
    out = out.flatMap((head) => [...choices].map((choice) => head + choice));
  }
  return out;
}

/** A RegExp for one path segment of a glob: `*`, `?` and bracket sets. A POSIX class matches any character here. */
function globSegment(segment) {
  let re = "";
  for (let k = 0; k < segment.length; k += 1) {
    const c = segment[k];
    const end = c === "[" ? segment.indexOf("]", k + 2) : -1;
    if (c === "*") re += ".*";
    else if (c === "?") re += ".";
    else if (end < 0) re += c.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
    else {
      const set = segment.slice(k + 1, end);
      re += set.includes("[:") ? "." : `[${set.replace(/^[!^]/, "^").replace(/[\\\]]/g, "\\$&")}]`;
      k = end;
    }
  }
  try {
    return new RegExp(`^${re}$`, "s");
  } catch {
    // A set bash would read differently, such as a reversed range: any name may match.
    return /^/;
  }
}

/** The paths a glob names from each of `dirs`, as bash expands it: a name starting with `.` only for a segment that
 *  does. */
function globFiles(pattern, dirs, budget) {
  const out = [];
  const segments = pattern.split("/");
  const walk = (dir, k) => {
    const segment = segments[k];
    if (budget.left <= 0) return;
    budget.left -= 1;
    if (k === segments.length) {
      out.push(dir);
    } else if (!/[*?[]/.test(segment)) {
      walk(path.join(dir, segment), k + 1);
    } else {
      if (!budget.listings.has(dir)) {
        budget.left -= 1;
        let names = [];
        try {
          names = fs.readdirSync(dir);
        } catch {
          // Not a directory bash could read either: the segment matches nothing.
        }
        budget.listings.set(dir, names);
      }
      const re = globSegment(segment);
      for (const name of budget.listings.get(dir)) {
        if ((segment[0] === "." || name[0] !== ".") && re.test(name)) walk(path.join(dir, name), k + 1);
      }
    }
  };
  for (const dir of path.isAbsolute(pattern) ? ["/"] : new Set(dirs)) walk(path.resolve(dir), 0);
  return out;
}

/** `name` with a leading `~`, `~+` or `~-` replaced as bash replaces it, or null when it has none. */
function expandHome(name, cwd) {
  const m = /^~([+-]?)(?=\/|$)/.exec(name);
  if (!m) return null;
  const base = { "": os.homedir(), "+": cwd || process.cwd(), "-": process.env.OLDPWD }[m[1]];
  return base ? base + name.slice(m[0].length) : null;
}

/** The directories the command may run in. The reader gives each `cd` target as bash builds it; the raw text is
 *  searched too, for a line the reader could not follow to its end. */
function searchDirs(command, cwd, reading = readLine(command, cwd)) {
  const dirs = [];
  for (const m of command.matchAll(CD_PREFIX)) {
    const d = m[1] || m[2] || m[3];
    if (d) dirs.push(d);
  }
  for (const d of reading.dirs) dirs.push(path.resolve(cwd || process.cwd(), expandHome(d, cwd) ?? d));
  if (cwd) dirs.push(cwd);
  dirs.push(process.cwd());
  return dirs;
}

/** Every existing script path named in the command: each word's text as bash builds it, the paths inside those texts,
 *  and those in the raw text, which still holds what the reader could not follow.
 *
 * ⚠ A bare token must be resolved against the COMMAND's directory, not this
 * process's. `cd somewhere && python sender.py` is the shape people actually
 * type, and resolving it here would find nothing and allow the send. */
function referencedFiles(command, cwd, reading, budget) {
  const dirs = searchDirs(command, cwd, reading);
  const out = new Set();
  const tokens = new Set([...command.matchAll(QUOTED_PATH)].map((m) => m[1] || m[2]));
  for (const m of command.matchAll(BARE_PATH)) tokens.add(m[0]);
  const texts = new Set(reading.texts);
  const globs = new Set(reading.globs);
  for (const { parts, glob } of reading.templates) {
    for (const text of substitutions(parts, reading.values, budget)) {
      texts.add(text);
      if (glob && /[*?[]/.test(text)) globs.add(text);
    }
  }
  for (const text of texts) {
    for (const word of braceExpand(text, budget)) {
      for (const m of word.matchAll(BARE_PATH)) tokens.add(m[0]);
      for (const part of [word, word.slice(word.indexOf("=") + 1)]) if (SCRIPT_NAME.test(part)) tokens.add(part);
    }
  }
  for (const glob of globs) {
    for (const word of braceExpand(glob, budget)) {
      // An assigned value is not a glob yet, but an unquoted expansion of it later is.
      for (const part of new Set([word, word.slice(word.indexOf("=") + 1)])) {
        const matches = globFiles(expandHome(part, cwd) ?? part, dirs, budget);
        for (const fp of matches) if (SCRIPT_NAME.test(fp)) tokens.add(fp);
      }
    }
  }
  for (const token of tokens) {
    // bash reads `<&-sender.py` as a descriptor close and the word `sender.py`, so the path may start after the dashes.
    for (const name of new Set([token, token.replace(/^-+/, ""), expandHome(token, cwd)])) {
      if (!name) continue;
      const candidates = path.isAbsolute(name)
        ? [name]
        : [name, ...dirs.map((d) => path.resolve(d, name))];
      for (const fp of candidates) {
        try {
          const st = fs.statSync(fp);
          if (st.isFile() && st.size <= MAX_READ_BYTES) { out.add(fp); break; }
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
  const reading = readLine(command, cwd);
  if (looksLikeSender([command, ...reading.texts].join("\n"), config)) {
    return { reason: "this command reaches a mail-send endpoint directly",
             where: "the command line" };
  }
  const budget = { left: EXPANSION_LIMIT, listings: new Map() };
  for (const fp of referencedFiles(command, cwd, reading, budget)) {
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
  if (reading.unreadable) {
    return { reason: "this command runs shell code the gate cannot read", where: reading.unreadable, unreadable: true };
  }
  if (budget.left <= 0) {
    return { reason: "this command makes more paths than the gate resolves",
             where: "its braces, globs and variables", unreadable: true };
  }
  return null;
}

const advice = (config, verdict) => [
  `Mail leaves through ${config.sender} alone, so its checks cannot be walked past.`,
  "",
  ...(verdict.unreadable
    ? ["The gate reads the code a shell runs before it allows the line, so that code",
       "must be written out in the line itself, or put in a script file the line runs."]
    : ["If this command does not send mail, it named a file that does — move that",
       `send into ${config.sender}, or add its directory to "exempt" in`,
       `${CONFIG_BASENAME}.`]),
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
    `BLOCKED: ${verdict.reason} (${verdict.where}).\n\n${advice(loaded.config, verdict)}\n`);
  process.exit(2);
}

if (process.argv[1]
    && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  main();
}
