#!/usr/bin/env node
// prose-smell.mjs — deterministic AI-writing-smell gate for outward prose.
//
// WHAT IT CATCHES, and the evidence for each choice (Lillywhite 2026-05/07,
// Contawe 2026-02): the frames that actually mark generated text —
//   * contrastive negation, named by Contawe as the single most prevalent
//     tell. The canonical spelling ("isn't just about X — it's about Y") is
//     one rule; the SAME frame has six other spellings that passed nine live
//     instances in one 1,300-word document (issue #63), so the family is
//     matched as a family: "X, not Y" and "The X is not Y." slogans, "rather
//     than", "instead of", "X, never Y", "no X and no Y", "would rather X
//     than Y". Slogan forms fail on one hit; the connective forms fail by
//     density (see CONTRAST_* below), because one "rather than" in a page
//     is English and four are a tic;
//   * "whether you're A, B, or C" inclusive parallelism (runner-up);
//   * sentence-initial "From X to Y," coverage parallelism (runner-up;
//     MEDIUM here, because literal ranges are legitimate);
//   * leftover assistant artifacts ("Would you like me to…");
//   * stock LLM vocabulary (delve/tapestry/testament/pivotal role/…);
//   * the aphoristic restatement: a slogan of eight words or fewer restating
//     the twenty-five-word sentence before it (MEDIUM — a heuristic).
//
// WHAT IT DELIBERATELY LEAVES ALONE: metaphors, similes, qualifiers
// ("remarkably difficult"), clean structure, and em dashes in moderation —
// the same articles' own point is that normal writing techniques are not
// evidence. Em dashes get a density note only when genuinely heavy, never a
// failure. A factual qualifier that happens to share a comma with "not"
// ("counsel, not full-time") is excluded by the allowlist in CONTRAST_SKIP.
// The deeper tells (inhuman volume, claims the author cannot defend cold)
// are not regex-able; they live in the WRITING doctrine snippet.
//
// IN-HOUSE PROSE. Dense engineering prose uses "X, not Y" as a precision
// device, and a repo whose every README carries it would learn to route
// around a gate that fails each edit. So the contrastive family's severity
// is repo-configurable: a `.prose-smell.json` found by walking up from the
// scanned file's directory, carrying `{"contrastive": "advise"}`, caps the
// family at MEDIUM there. Every finding is still reported; only the exit
// code changes. Outward artifacts drafted outside such a tree get the
// default, which fails. The canonical rules keep their severity everywhere.
//
// Exit codes: 2 = at least one HIGH finding (stderr carries the findings, so
// a PostToolUse hook feeds them straight back to the author for a rewrite);
// 0 = clean or medium-only (mediums print to stdout as advice).
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PROSE_EXT = new Set([".md", ".markdown", ".txt", ".html", ".htm", ".eml"]);
// Generated/ledger files carry structured text, not authored prose.
const SKIP_BASENAMES = new Set(["substrate.md", "deliverables.json", "substrate.json"]);
const SKIP_SEGMENTS = new Set(["node_modules", ".git", "dist", "out"]);

const HIGH_RULES = [
  {
    rule: "negation-contrast",
    // "isn't just about X — it's about Y" and its punctuation variants; the
    // resolving clause must appear in the SAME sentence for a hit.
    re: /\b(?:is|are|was|were)(?:n['’]t|\s+not)\s+(?:just|only|merely|simply)\b[^.?!\n]{0,90}[;,—–-]\s*(?:it|this|that|they)['’]?s?\b/gi,
  },
  { rule: "more-than-just", re: /\b(?:is|are)\s+more\s+than\s+just\b/gi },
  {
    rule: "whether-your",
    re: /\bwhether\s+you(?:['’]re|\s+are)\b[^.?!\n]{0,100}\bor\b/gi,
  },
  {
    rule: "assistant-artifact",
    re: /\b(?:would you like me to|just say the word|i hope this helps|let me know if you(?:['’]d| would) like|as an ai\b|great question[!,])/gi,
  },
  {
    rule: "stock-vocab",
    re: /\b(?:delv(?:e|es|ing)\b|tapestry\b|game-?changer|ever-evolving|in today['’]s fast-paced|unlock(?:ing)? the (?:power|potential|value)|elevate your\b|seamlessly integrat|a testament to\b|underscores? the\b|plays? a (?:pivotal|crucial|vital) role|it['’]s (?:important to note|worth noting)|in the (?:realm|landscape) of\b|navigating the (?:complexit|landscape))/gi,
  },
];

const MEDIUM_RULES = [
  // Sentence-initial coverage parallelism; literal ranges usually sit
  // mid-sentence or lack the trailing comma, so this stays narrow.
  { rule: "from-to-coverage", re: /^From\s+[^.?!\n]{3,60}\s+to\s+[^.?!\n]{3,60},/gim },
  { rule: "concluder", re: /^(?:in conclusion|in summary)\b/gim },
];

// ---------------------------------------------------------------------------
// The contrastive-negation family beyond its canonical spelling (#63).
//
// Two slogan forms, HIGH on a single hit:
//   contrast-comma   "Settlement is a computation, not a decision."
//   contrast-slogan  "The product is not the gap."
// Five connective forms, judged by density across the document:
//   rather-than, instead-of, contrast-never ("as a waterfall, never into"),
//   no-and-no ("no adjuster and no vote"), would-rather-than.
//
// The comma form requires the comparand to CLOSE its clause (a period,
// colon, semicolon, dash, closing paren, comma, or line end within five
// words), because that is what makes it a slogan rather than a sentence
// that goes on to say something. "not X, but Y" is a different frame and
// is left alone on purpose: the comparand there is not the point.
const CONTRAST_ARTICLE = /^(?:a|an|the|to|its|their|your|our|his|her|my|this|that|these|those)$/i;
// Words that make ", not <word>" a qualifier rather than a comparand:
// "counsel, not full-time", "shipped, not yet audited", "billed, not
// applicable here". Prepositions and conjunctions are here because
// ", not because it failed" contrasts a reason, which is ordinary writing.
const CONTRAST_SKIP = new Set([
  "yet", "applicable", "full-time", "part-time", "only", "necessarily",
  "always", "entirely", "quite", "really", "least", "less", "more", "much",
  "very", "so", "too", "nearly", "exactly", "merely", "simply", "otherwise",
  "now", "here", "there", "then", "than", "as", "like", "since", "while",
  "where", "whether", "that", "which", "who", "what", "how", "when", "if",
  "until", "unless", "because", "by", "for", "in", "on", "at", "with",
  "without", "from", "of", "before", "after", "into", "onto", "over",
  "under", "through", "against", "about", "per", "via", "all", "every",
  "everything", "anything", "nothing", "none", "one", "once", "twice",
  "again", "mind", "sure", "enough", "ready", "done", "available",
  "later", "earlier", "either", "neither", "any", "some", "both", "each",
  "same", "such", "long", "far", "often", "yet.", "ever", "never",
]);
const CONTRAST_COMMA = /,\s*not\s+(?:(?:just|even)\s+)?([A-Za-z][\w'’-]*)((?:\s+[\w'’-]+){0,4}?)(?=\s*(?:[.;:!?—–,)]|$))/gm;
// A standalone slogan sentence: subject, copula, "not", a short comparand,
// full stop. Anchored to the start of a sentence, so "Exports are not
// counted here and the figure is net." never matches — the comparand there
// is a clause, not a slogan.
// A markdown lead (bullet, number, bold, opening quote) is not the start of
// the sentence; the bold-lead bullet is how in-house docs write the slogan.
const CONTRAST_SLOGAN = /(?<=^|[.!?]\s+)(?:[-*>#]+\s+|\d+\.\s+)?(?:\*\*|__|["“])?(?:The|This|That|It)\s+[\w'’-]+\s+(?:is|are)\s+not\s+([A-Za-z][\w'’-]*)[^.!?\n]{0,40}[.!?](?:\*\*|__|["”])?(?=\s|$)/gm;
const CONTRAST_DENSITY_RULES = [
  { rule: "rather-than", re: /(?<!\bwould\s)\brather than\b/gi },
  { rule: "instead-of", re: /\binstead of\b/gi },
  { rule: "contrast-never", re: /,\s*never\s+(?!again\b|mind\b)[A-Za-z][\w'’-]*/gi },
  { rule: "no-and-no", re: /\bno\s+[\w-]+\s+(?:and|or)\s+no\s+[\w-]+/gi },
  { rule: "would-rather-than", re: /\bwould rather\b[^.?!\n]{0,60}\bthan\b/gi },
];
// Connective forms per 500 words. The denominator is floored at 500 so a
// two-line note with one "rather than" reads as one, never as a density of
// two hundred and fifty.
const CONTRAST_MEDIUM_PER_500 = 2;
const CONTRAST_HIGH_PER_500 = 4;
const CONTRAST_WORD_FLOOR = 500;
const CONTRAST_RULES = new Set([
  "contrast-comma", "contrast-slogan",
  ...CONTRAST_DENSITY_RULES.map((r) => r.rule),
]);

// The aphoristic restatement: a long sentence, then a slogan that says it
// again in eight words or fewer with a copula in it. MEDIUM, because a short
// sentence after a long one is also just rhythm.
const APHORISM_LONG_WORDS = 25;
const APHORISM_SHORT_WORDS = 8;

const EMDASH_PER_100_WORDS = 1.5;
const EMDASH_MIN_COUNT = 4;

// Mail length. Calibrated on what recipients did with real drafts rather than
// on a round number: an ~800-word message came back asking for less, and the
// ~130-word rewrite of the same content is what actually got acted on. Drafts
// around 100-200 words landed reliably. Length is the defect the frame rules
// above cannot see — every claim in that long draft was accurate and every
// frame rule was green, and it still failed as a message.
//
// Only MAIL is scored, and "is this mail?" is answered structurally: a file
// whose first non-blank line is a Subject: header is an email body. A memo, a
// plan or a findings doc is legitimately long, and firing on those is how a
// gate earns being switched off.
const MAIL_WORDS_HIGH = 350;
const MAIL_WORDS_MEDIUM = 250;
const SUBJECT_LINE = /^Subject:\s*\S/;
// A threaded reply carries the whole history under the sign-off. Those are not
// the author's words and counting them would fail every reply on a long thread.
const QUOTED_CHAIN = /^From:\s+\S[^\n]*\n(?:[^\n]*\n){0,3}?\s*(?:Sent|To|Date):\s+\S/m;
// Authored prose is small; anything bigger is a generated report or data
// masquerading as prose, and scanning it would cost memory and hook budget.
const MAX_BYTES = 2_000_000;

const CONFIG_BASENAME = ".prose-smell.json";
const CONFIG_KEYS = new Set(["contrastive"]);
const CONTRASTIVE_MODES = new Set(["fail", "advise"]);

/** Blank out fenced code blocks, preserving line numbers. */
function stripFences(text) {
  const lines = text.split("\n");
  let inFence = false;
  return lines
    .map((l) => {
      if (/^\s*(?:```|~~~)/.test(l)) {
        inFence = !inFence;
        return "";
      }
      return inFence ? "" : l;
    })
    .join("\n");
}

/** Blank out inline `code` spans, preserving offsets and line numbers.
 *
 * A bug report that quotes the tell it is about ("the rule misses `X, not
 * Y`") is not prose carrying the tell. The span is replaced by spaces of
 * the same length, so every index computed over the result still maps to
 * the original line. Spans do not cross lines: an unmatched backtick is a
 * backtick, not the start of a span that swallows the document. */
function stripInlineCode(text) {
  return text.replace(/`[^`\n]*`/g, (m) => " ".repeat(m.length));
}

// O(n) once, then O(log n) per match — the naive slice-and-split counter is
// O(index) per match, which goes quadratic on match-dense files and blew the
// hook budget in review (66s on a 1.9MB bait file).
function newlineOffsets(text) {
  const offsets = [];
  for (let i = text.indexOf("\n"); i !== -1; i = text.indexOf("\n", i + 1)) {
    offsets.push(i);
  }
  return offsets;
}

function lineOf(offsets, index) {
  let lo = 0, hi = offsets.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (offsets[mid] < index) lo = mid + 1;
    else hi = mid;
  }
  return lo + 1;
}

function contrastFindings(prose, offsets) {
  const out = [];
  CONTRAST_COMMA.lastIndex = 0;
  for (const m of prose.matchAll(CONTRAST_COMMA)) {
    const first = m[1];
    // An article is never the comparand; look past it to the noun. A
    // qualifier ("full-time", "yet", "because") is not a contrast at all.
    let head = first;
    if (CONTRAST_ARTICLE.test(first)) {
      const rest = (m[2] || "").trim().split(/\s+/).filter(Boolean);
      if (!rest.length) continue;
      head = rest[0];
    }
    if (CONTRAST_SKIP.has(head.toLowerCase())) continue;
    out.push({ severity: "high", rule: "contrast-comma", line: lineOf(offsets, m.index),
               excerpt: m[0].slice(0, 80).trim() });
  }
  CONTRAST_SLOGAN.lastIndex = 0;
  for (const m of prose.matchAll(CONTRAST_SLOGAN)) {
    if (CONTRAST_SKIP.has(m[1].toLowerCase())) continue;
    out.push({ severity: "high", rule: "contrast-slogan", line: lineOf(offsets, m.index),
               excerpt: m[0].slice(0, 80).trim() });
  }
  const connective = [];
  for (const { rule, re } of CONTRAST_DENSITY_RULES) {
    re.lastIndex = 0;
    for (const m of prose.matchAll(re)) {
      connective.push({ rule, line: lineOf(offsets, m.index), excerpt: m[0].slice(0, 80).trim() });
    }
  }
  if (connective.length) {
    const words = (prose.match(/\S+/g) || []).length;
    const per500 = (connective.length / Math.max(words, CONTRAST_WORD_FLOOR)) * 500;
    const severity = per500 >= CONTRAST_HIGH_PER_500 ? "high"
      : per500 >= CONTRAST_MEDIUM_PER_500 ? "medium" : null;
    if (severity) {
      for (const c of connective) {
        out.push({ severity, ...c,
                   excerpt: `${c.excerpt} (${connective.length} connective contrasts in ${words} words)` });
      }
    }
  }
  return out;
}

function aphorismFindings(prose, offsets) {
  const out = [];
  // Paragraph by paragraph, so a heading or a list item never counts as the
  // sentence before a slogan.
  const paraRe = /[^\n]+(?:\n[^\n]+)*/g;
  for (const para of prose.matchAll(paraRe)) {
    const text = para[0];
    if (/^\s*(?:[#>|*-]|\d+\.)/.test(text)) continue;
    const sentRe = /[^.!?]+[.!?]+/g;
    let prevWords = 0;
    for (const s of text.matchAll(sentRe)) {
      const words = (s[0].match(/\S+/g) || []).length;
      if (prevWords >= APHORISM_LONG_WORDS && words > 0 && words <= APHORISM_SHORT_WORDS
          && /\b(?:is|are)\b/i.test(s[0])) {
        out.push({ severity: "medium", rule: "aphoristic-restatement",
                   line: lineOf(offsets, para.index + s.index),
                   excerpt: s[0].slice(0, 80).trim() });
      }
      prevWords = words;
    }
  }
  return out;
}

/** Words the author wrote, when the file is an email body; null when it is not.
 *
 * The Subject header itself is excluded - a descriptive subject is a virtue and
 * must not count against the body - and so is any quoted chain below it.
 */
function authoredMailWords(prose) {
  const lines = prose.split("\n");
  const first = lines.findIndex((l) => l.trim() !== "");
  if (first === -1 || !SUBJECT_LINE.test(lines[first].trim())) return null;
  let body = lines.slice(first + 1).join("\n");
  const quoted = body.match(QUOTED_CHAIN);
  if (quoted) body = body.slice(0, quoted.index);
  return (body.match(/\S+/g) || []).length;
}

function mailLengthFindings(prose) {
  const words = authoredMailWords(prose);
  if (words === null) return [];
  if (words > MAIL_WORDS_HIGH) {
    return [{
      severity: "high", rule: "mail-too-long", line: 1,
      excerpt: `${words} words of body; mail that lands runs under `
        + `${MAIL_WORDS_MEDIUM}. Lead with what changes for the reader, keep the `
        + "few things that bite, move the rest to an attachment or a file",
    }];
  }
  if (words > MAIL_WORDS_MEDIUM) {
    return [{
      severity: "medium", rule: "mail-getting-long", line: 1,
      excerpt: `${words} words of body; the drafts that worked ran 83-199`,
    }];
  }
  return [];
}

export function scan(text, config = {}) {
  const prose = stripInlineCode(stripFences(text));
  const offsets = newlineOffsets(prose);
  const findings = [];
  for (const { rule, re } of HIGH_RULES) {
    re.lastIndex = 0;
    for (const m of prose.matchAll(re)) {
      findings.push({ severity: "high", rule, line: lineOf(offsets, m.index),
                      excerpt: m[0].slice(0, 80).trim() });
    }
  }
  for (const { rule, re } of MEDIUM_RULES) {
    re.lastIndex = 0;
    for (const m of prose.matchAll(re)) {
      findings.push({ severity: "medium", rule, line: lineOf(offsets, m.index),
                      excerpt: m[0].slice(0, 80).trim() });
    }
  }
  for (const f of contrastFindings(prose, offsets)) {
    if (config.contrastive === "advise" && f.severity === "high") {
      findings.push({ ...f, severity: "medium",
                      excerpt: `${f.excerpt} [in-house: advised, not failed]` });
    } else {
      findings.push(f);
    }
  }
  findings.push(...aphorismFindings(prose, offsets));
  findings.push(...mailLengthFindings(prose));
  const words = (prose.match(/\S+/g) || []).length;
  const dashes = (prose.match(/[—–]/g) || []).length;
  if (words > 0 && dashes >= EMDASH_MIN_COUNT
      && (dashes / words) * 100 > EMDASH_PER_100_WORDS) {
    findings.push({
      severity: "medium", rule: "em-dash-density", line: 1,
      excerpt: `${dashes} dashes in ${words} words — fine in-house style, `
        + "heavy for an external artifact; thin if this leaves the building",
    });
  }
  return findings;
}

/** The nearest `.prose-smell.json` above a file, validated.
 *
 * Returns {config, problems}. A malformed or unknown key is reported and
 * IGNORED — the default (fail) applies — because a config that could
 * silently switch the gate off would be the inverse of what a config is for,
 * and a config that could crash the hook would break unrelated writes. */
export function loadConfig(startDir) {
  let dir = path.resolve(startDir);
  for (;;) {
    const p = path.join(dir, CONFIG_BASENAME);
    if (fs.existsSync(p)) {
      let doc;
      try { doc = JSON.parse(fs.readFileSync(p, "utf8")); }
      catch (e) { return { config: {}, problems: [`${p}: not valid JSON (${e.message}) — ignored, default severities apply`] }; }
      if (!doc || typeof doc !== "object" || Array.isArray(doc)) {
        return { config: {}, problems: [`${p}: must be an object — ignored`] };
      }
      const problems = [];
      const config = {};
      for (const k of Object.keys(doc)) {
        if (!CONFIG_KEYS.has(k)) { problems.push(`${p}: unknown key "${k}" (known: ${[...CONFIG_KEYS].join(", ")}) — ignored`); continue; }
      }
      if ("contrastive" in doc) {
        if (CONTRASTIVE_MODES.has(doc.contrastive)) config.contrastive = doc.contrastive;
        else problems.push(`${p}: contrastive must be one of ${[...CONTRASTIVE_MODES].join(", ")} — ignored, default (fail) applies`);
      }
      return { config, problems, source: p };
    }
    const parent = path.dirname(dir);
    if (parent === dir) return { config: {}, problems: [] };
    dir = parent;
  }
}

// Flood cap, matching the registry's own convention: a wall of findings is
// suppressed, never injected wholesale into the transcript.
const MAX_REPORTED = 30;

function report(file, findings) {
  const highs = findings.filter((f) => f.severity === "high");
  const meds = findings.filter((f) => f.severity === "medium");
  for (const f of meds.slice(0, MAX_REPORTED)) {
    process.stdout.write(`${file}:${f.line} [medium] ${f.rule} — "${f.excerpt}"\n`);
  }
  if (meds.length > MAX_REPORTED) {
    process.stdout.write(`${file}: … ${meds.length - MAX_REPORTED} more medium suppressed\n`);
  }
  for (const f of highs.slice(0, MAX_REPORTED)) {
    process.stderr.write(`${file}:${f.line} [HIGH] ${f.rule} — "${f.excerpt}"\n`);
  }
  if (highs.length > MAX_REPORTED) {
    process.stderr.write(`${file}: … ${highs.length - MAX_REPORTED} more HIGH suppressed\n`);
  }
  return highs.length;
}

function isProseFile(fp) {
  if (!fp) return false;
  if (!PROSE_EXT.has(path.extname(fp).toLowerCase())) return false;
  if (SKIP_BASENAMES.has(path.basename(fp).toLowerCase())) return false;
  const segs = fp.split(/[\\/]/).map((s) => s.toLowerCase());
  return !segs.some((s) => SKIP_SEGMENTS.has(s));
}

function scanFile(fp) {
  if (fs.statSync(fp).size > MAX_BYTES) return 0;
  const { config, problems } = loadConfig(path.dirname(path.resolve(fp)));
  for (const p of problems) process.stderr.write(`prose-smell: ${p}\n`);
  return report(fp, scan(fs.readFileSync(fp, "utf8"), config));
}

// The files a PostToolUse payload wrote. Claude Code's Write and Edit carry
// tool_input.file_path. Codex's apply_patch, which its matcher aliases fire
// for Write|Edit, carries the patch text in tool_input.command, whose headers
// name the files; a relative path that does not resolve from here is re-based
// from the payload's cwd, the directory the patch was applied in. Deleted
// files are skipped: nothing to scan. A non-string file_path stays silent.
function hookFiles(payload) {
  const ti = payload && typeof payload.tool_input === "object" && payload.tool_input
    ? payload.tool_input : {};
  if (typeof ti.file_path === "string" && ti.file_path) return [ti.file_path];
  if (ti.file_path !== undefined) return [];
  const text = ["command", "patch", "input"].map((k) => ti[k])
    .find((v) => typeof v === "string" && v.includes("*** "));
  if (!text) return [];
  const cwd = typeof payload.cwd === "string" ? payload.cwd : "";
  const out = [];
  const re = /^\*\*\* (Add File|Update File|Move to): (.+?)\s*$/gm;
  let m;
  while ((m = re.exec(text)) !== null) {
    let p = m[2].trim();
    if (!path.isAbsolute(p) && cwd && !fs.existsSync(p) && fs.existsSync(path.join(cwd, p))) {
      p = path.join(cwd, p);
    }
    if (m[1] === "Move to" && out.length) out[out.length - 1] = p;
    else out.push(p);
  }
  return [...new Set(out)];
}

function main() {
  const args = process.argv.slice(2);
  if (args[0] === "--hook") {
    // PostToolUse contract: hook JSON on stdin; exit 2 feeds stderr back to
    // the author. Anything unparseable or out of scope exits 0 — a style
    // gate must never break an unrelated write.
    let raw = "";
    try { raw = fs.readFileSync(0, "utf8"); } catch { process.exit(0); }
    let files = [];
    try { files = hookFiles(JSON.parse(raw)); } catch { process.exit(0); }
    files = files.filter((fp) => typeof fp === "string" && isProseFile(fp) && fs.existsSync(fp));
    if (files.length === 0) process.exit(0);
    let highs = 0;
    for (const fp of files) {
      try { highs += scanFile(fp); } catch { /* unreadable is not a finding */ }
    }
    if (highs > 0) {
      process.stderr.write(
        "prose-smell: the flagged frames read as generated text — rewrite them "
        + "in plain declarative sentences before this leaves the building.\n");
      process.exit(2);
    }
    process.exit(0);
  }

  const files = args.filter((a) => !a.startsWith("--"));
  if (files.length === 0) {
    process.stderr.write("usage: prose-smell.mjs [--hook] <files…>\n");
    process.exit(0);
  }
  let highs = 0;
  for (const f of files) {
    // The CLI path takes user-typed arguments: a typo'd or directory path
    // gets one honest line, never a stack trace.
    try {
      if (!fs.existsSync(f) || !fs.statSync(f).isFile()) {
        process.stderr.write(`prose-smell: not a readable file: ${f}\n`);
        continue;
      }
      highs += scanFile(f);
    } catch (e) {
      process.stderr.write(`prose-smell: could not scan ${f}: ${e.code || e.message}\n`);
    }
  }
  process.exit(highs > 0 ? 2 : 0);
}

// Importable for tests; executed when run directly.
//
// ⚠ `new URL(import.meta.url).pathname` is NOT a filesystem path on Windows: it
// yields "/C:/Users/…", and path.resolve() then reads the leading slash as
// drive-root and returns "C:\C:\Users\…". The comparison was therefore ALWAYS
// false on Windows, main() never ran, and the gate exited 0 for every file it
// was ever handed - a hook that reported clean because it had not looked.
// Measured 2026-09-11 against the deployed 1.6.0: scan() returned the expected
// HIGH finding while the CLI exited 0, and 18 of this file's own 28 tests were
// red. fileURLToPath is the only correct URL-to-path conversion.
//
// The loader also names this module by its realpath, while argv[1] keeps any
// symlink or junction in CLAUDE_PLUGIN_ROOT, so both sides are resolved first.
if (process.argv[1]
    && fs.realpathSync(process.argv[1]) === fs.realpathSync(fileURLToPath(import.meta.url))) {
  main();
}
