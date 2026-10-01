#!/usr/bin/env node
// substrate-graph.mjs — generates SUBSTRATE.md from per-repo substrate.json manifests.
// Zero dependencies, Node >= 18, cross-platform. Deterministic output (sorted; no
// timestamps in the body).
//
//   node substrate-graph.mjs --emit  [--root <dir>]   write <root>/SUBSTRATE.md; exit 1 on manifest problems
//   node substrate-graph.mjs --json  [--root <dir>]   the graph as JSON on stdout; exit 1 on manifest problems
//   node substrate-graph.mjs --check [--root <dir>]   compact summary + staleness; ALWAYS exits 0
//
// Root resolution: --root arg, else CLAUDE_PROJECT_DIR, else cwd.
// --check stdout is injected as session context by the SessionStart hook, so every
// manifest-derived string is validated + control-char-stripped + length-capped
// before it can reach stdout or the committed SUBSTRATE.md: a manifest a third
// party drops into the workspace root (a vendored dir, an unpacked tarball, a
// clone) must not be able to forge output lines, forge a reassuring "all current"
// line, forge a <system-reminder> block, or flood the context window.
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { TextDecoder } from "node:util";
import { parseJsonNoDuplicateKeys, parseTimestamp } from "./lifecycle.mjs";

const DEFAULT_EXCLUDE_DIRS = ["node_modules", ".git", "dist", "build", "out", "coverage", "__pycache__"];
const WALK_MAX_DEPTH = 6;
const WALK_MAX_ENTRIES = 5000;
const IMPORT_EXTS = new Set([".js", ".mjs", ".cjs", ".ts", ".py"]);
const JS_RESOLVE_SUFFIXES = ["", ".js", ".mjs", ".cjs", ".ts", "/index.js", "/index.mjs", "/index.cjs", "/index.ts"];
const GIT_MAX_BUFFER = 64 * 1024 * 1024;
const STRING_CAP = 2000; // one manifest string is one line/cell; long is fine, unbounded is not
const LIST_CAP = 50; // printed problem/alarm/note lines before "N more suppressed"
const MAX_DELIVERABLE_LEDGER_BYTES = 1024 * 1024;
const MAX_DELIVERABLE_ENTRIES = 5000;

// control chars (C0 + DEL) and the Unicode line/paragraph separators, so a
// manifest string can never break out of its line into forged output
const CONTROL_CHARS = new RegExp("[\u0000-\u001f\u007f-\u009f\u2028\u2029]", "g");

function clean(value, max = STRING_CAP) {
  const s = String(value).replace(CONTROL_CHARS, " ");
  return s.length > max ? s.slice(0, max) + "…" : s;
}

function capList(list, max = LIST_CAP) {
  if (list.length <= max) return list;
  return [...list.slice(0, max), `…and ${list.length - max} more suppressed`];
}

function readBoundedUtf8File(file, label, maxBytes) {
  const lexical = fs.lstatSync(file);
  if (!lexical.isFile() || lexical.isSymbolicLink()) throw new Error(`${label} is not a regular file`);
  if (lexical.size > maxBytes) throw new Error(`${label} exceeds ${maxBytes} bytes`);
  const flags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0);
  const handle = fs.openSync(file, flags);
  try {
    const before = fs.fstatSync(handle);
    if (
      !before.isFile() || before.dev !== lexical.dev || before.ino !== lexical.ino
      || before.size !== lexical.size
    ) {
      throw new Error(`${label} changed while it was being opened`);
    }
    const chunks = [];
    const buffer = Buffer.allocUnsafe(Math.min(64 * 1024, maxBytes + 1));
    let total = 0;
    while (total <= maxBytes) {
      const count = fs.readSync(handle, buffer, 0, Math.min(buffer.length, maxBytes + 1 - total), null);
      if (count === 0) break;
      total += count;
      chunks.push(Buffer.from(buffer.subarray(0, count)));
    }
    const after = fs.fstatSync(handle);
    const lexicalAfter = fs.lstatSync(file);
    if (total > maxBytes) throw new Error(`${label} exceeds ${maxBytes} bytes`);
    if (
      total !== before.size || after.size !== before.size || after.mtimeMs !== before.mtimeMs
      || lexicalAfter.isSymbolicLink() || lexicalAfter.dev !== before.dev || lexicalAfter.ino !== before.ino
    ) {
      throw new Error(`${label} changed while it was being read`);
    }
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks, total));
    } catch {
      throw new Error(`${label} is not valid UTF-8`);
    }
  } finally {
    fs.closeSync(handle);
  }
}

function resolveRoot(argv) {
  const i = argv.indexOf("--root");
  if (i !== -1) {
    if (!argv[i + 1]) throw new Error("--root requires a directory argument");
    return path.resolve(argv[i + 1]);
  }
  if (process.env.CLAUDE_PROJECT_DIR) return path.resolve(process.env.CLAUDE_PROJECT_DIR);
  return process.cwd();
}

function loadConfig(root, problems) {
  const config = { graceHours: 6, excludeDirs: new Set(DEFAULT_EXCLUDE_DIRS), nonGitRepos: Object.create(null) };
  const cf = path.join(root, "substrate.config.json");
  if (!fs.existsSync(cf)) return config;
  try {
    const parsed = JSON.parse(fs.readFileSync(cf, "utf8"));
    if (typeof parsed.graceHours === "number") config.graceHours = parsed.graceHours;
    if (Array.isArray(parsed.excludeDirs)) for (const d of parsed.excludeDirs) config.excludeDirs.add(String(d));
    if (parsed.nonGitRepos && typeof parsed.nonGitRepos === "object") {
      for (const [dir, rels] of Object.entries(parsed.nonGitRepos)) {
        if (dir === "__proto__" || dir === "constructor" || dir === "prototype") continue;
        if (Array.isArray(rels)) config.nonGitRepos[dir] = rels.map(String);
      }
    }
  } catch (e) {
    problems.push(`unreadable config: substrate.config.json (${clean(e.message)})`);
  }
  return config;
}

function isStr(x) {
  return typeof x === "string";
}

function strArray(x) {
  return Array.isArray(x) && x.every(isStr);
}

function validateManifest(parsed, dir, problems) {
  // Reject a malformed/hostile manifest as a named problem rather than letting
  // it crash the run or override internal fields — the returned object carries
  // ONLY validated, sanitized data; the caller supplies dir/manifest/mtimeMs.
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    problems.push(`invalid manifest: ${clean(dir)}/substrate.json is not a JSON object`);
    return null;
  }
  if (!isStr(parsed.repo)) {
    problems.push(`invalid manifest: ${clean(dir)}/substrate.json needs a string "repo"`);
    return null;
  }
  if (parsed.primitives !== undefined && !Array.isArray(parsed.primitives)) {
    problems.push(`invalid manifest: ${clean(parsed.repo)} field "primitives" is not an array`);
    return null;
  }
  const repo = clean(parsed.repo);
  const primitives = [];
  for (const p of parsed.primitives || []) {
    if (!p || typeof p !== "object" || !isStr(p.id)) {
      problems.push(`invalid primitive in ${repo}: each needs a string "id"`);
      continue;
    }
    if (![p.kind, p.status, p.desc].every(isStr) || !strArray(p.paths) || !strArray(p.consumes || [])) {
      problems.push(`invalid primitive ${clean(p.id)} in ${repo}: kind/status/desc must be strings, paths/consumes string arrays`);
      continue;
    }
    if (p.terminal !== undefined && typeof p.terminal !== "boolean") {
      problems.push(`invalid primitive ${clean(p.id)} in ${repo}: "terminal" must be a boolean`);
      continue;
    }
    primitives.push({
      id: clean(p.id),
      kind: clean(p.kind),
      status: clean(p.status),
      desc: clean(p.desc),
      paths: p.paths.map((x) => clean(x)),
      consumes: (p.consumes || []).map((x) => clean(x)),
      terminal: p.terminal === true,
    });
  }
  return { repo, primitives };
}

function loadManifests(root, excludeDirs, problems) {
  const repos = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    if (!entry.isDirectory()) continue;
    if (excludeDirs.has(entry.name)) continue; // never ingest manifests from node_modules/.git/vendored build dirs
    const mf = path.join(root, entry.name, "substrate.json");
    if (!fs.existsSync(mf)) continue; // manifest-less repos are demos/synthetic by doctrine — never alarmed on
    let parsed;
    try {
      parsed = JSON.parse(fs.readFileSync(mf, "utf8"));
    } catch (e) {
      problems.push(`unreadable manifest: ${clean(entry.name)}/substrate.json (${clean(e.message)})`);
      continue;
    }
    const v = validateManifest(parsed, entry.name, problems);
    if (!v) continue;
    // internal fields are set here, never from the manifest — a manifest cannot
    // override dir/mtimeMs to steer git -C outside the repo or backdate staleness
    repos.push({ repo: v.repo, primitives: v.primitives, dir: entry.name, manifest: mf, mtimeMs: fs.statSync(mf).mtimeMs });
  }
  return repos;
}

// ---- deliverables ledger (deliverables.json beside — or without — substrate.json) ----
// Task boards and scratchpads that hold "built but not yet sent" state do not
// survive an assistant's context compaction; a file in the repo does. Any entry
// with a builtAt and no recorded ending is an open obligation, surfaced at every
// session start until someone records how it ended.
//
// An obligation ends in more than one way. When sentAt was the only exit, the
// only way to quiet a deliverable that was superseded, withdrawn, or delivered
// in a meeting was to write a send that never happened — a register that had to
// lie to stay quiet, and whose alarms then read as noise. So a non-send ending
// closes on closedAt plus a reason from a fixed vocabulary plus a sentence
// saying why, and sentAt keeps meaning exactly one thing: an email left.
const CLOSE_REASONS = ["superseded", "withdrawn", "answered-elsewhere"];

function parseCalendarDay(value) {
  if (typeof value !== "string") return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (year < 1 || month < 1 || month > 12) return null;
  if (day < 1 || day > new Date(Date.UTC(year, month, 0)).getUTCDate()) return null;
  return match[0];
}

function fmtAge(ms) {
  const h = Math.round(ms / 3600000);
  if (h < 1) return "under an hour";
  if (h < 48) return `${h}h`;
  return `${Math.round(h / 24)}d`;
}

function deliverableAlarms(root, excludeDirs, problems) {
  const alarms = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    if (!entry.isDirectory() || excludeDirs.has(entry.name)) continue;
    const df = path.join(root, entry.name, "deliverables.json");
    if (!fs.existsSync(df)) continue;
    let parsed;
    try {
      const label = `${entry.name}/deliverables.json`;
      parsed = parseJsonNoDuplicateKeys(
        readBoundedUtf8File(df, label, MAX_DELIVERABLE_LEDGER_BYTES),
        label
      );
    } catch (e) {
      problems.push(`unreadable ledger: ${clean(entry.name)}/deliverables.json (${clean(e.message)})`);
      continue;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || !Array.isArray(parsed.deliverables)) {
      problems.push(`invalid ledger: ${clean(entry.name)}/deliverables.json needs a "deliverables" array`);
      continue;
    }
    if (parsed.deliverables.length > MAX_DELIVERABLE_ENTRIES) {
      problems.push(
        `invalid ledger: ${clean(entry.name)}/deliverables.json exceeds ${MAX_DELIVERABLE_ENTRIES} entries`
      );
      continue;
    }
    // Per-entry validity. A single malformed or duplicate id reports its own
    // entry and nothing more — indexing the whole ledger through one throw would
    // drop every OTHER obligation in the repo, and a register that goes quiet on
    // one typo is the exact failure the closedReason vocabulary exists to end.
    const indexed = new Map();
    for (const item of parsed.deliverables) {
      if (!item || typeof item !== "object" || Array.isArray(item) || typeof item.id !== "string" || !item.id.trim()) {
        problems.push(`invalid deliverable in ${clean(entry.name)}: each deliverable needs a non-empty string "id"`);
        continue;
      }
      if (indexed.has(item.id)) {
        problems.push(`invalid ledger: ${clean(entry.name)}/deliverables.json repeats deliverable id ${clean(item.id)}`);
        continue;
      }
      indexed.set(item.id, item);
    }
    for (const d of indexed.values()) {
      if (!d || typeof d !== "object" || !isStr(d.id)) {
        problems.push(`invalid deliverable in ${clean(entry.name)}: each needs a string "id"`);
        continue;
      }
      const built = parseTimestamp(d.builtAt);
      if (built == null) {
        problems.push(`invalid deliverable ${clean(d.id)} in ${clean(entry.name)}: "builtAt" must be an ISO-8601 instant`);
        continue;
      }
      const rawSent = isStr(d.sentAt) ? d.sentAt.trim() : "";
      const rawSentOn = isStr(d.sentOn) ? d.sentOn.trim() : "";
      const rawClosed = isStr(d.closedAt) ? d.closedAt.trim() : "";
      const rawClosedOn = isStr(d.closedOn) ? d.closedOn.trim() : "";
      const sentAt = rawSent ? parseTimestamp(rawSent) : null;
      const sentOn = rawSentOn ? parseCalendarDay(rawSentOn) : null;
      const closedAt = rawClosed ? parseTimestamp(rawClosed) : null;
      const closedOn = rawClosedOn ? parseCalendarDay(rawClosedOn) : null;
      const now = Date.now();
      const today = new Date(now).toISOString().slice(0, 10);
      const builtDay = d.builtAt.trim().slice(0, 10);
      const sentChronology = sentAt != null && sentAt >= built && sentAt <= now;
      const sentDayChronology = sentOn != null && sentOn >= builtDay && sentOn <= today;
      const closedChronology = closedAt != null && closedAt >= built && closedAt <= now;
      const closedDayChronology = closedOn != null && closedOn >= builtDay && closedOn <= today;
      if (rawSent && sentAt == null) {
        problems.push(`deliverable ${clean(d.id)} in ${clean(entry.name)}: "sentAt" must be an ISO-8601 instant`);
      }
      if (rawSentOn && sentOn == null) {
        problems.push(`deliverable ${clean(d.id)} in ${clean(entry.name)}: "sentOn" must be an ISO-8601 calendar date`);
      }
      if (rawClosed && closedAt == null) {
        problems.push(`deliverable ${clean(d.id)} in ${clean(entry.name)}: "closedAt" must be an ISO-8601 instant`);
      }
      if (rawClosedOn && closedOn == null) {
        problems.push(`deliverable ${clean(d.id)} in ${clean(entry.name)}: "closedOn" must be an ISO-8601 calendar date`);
      }
      if ((sentAt != null && !sentChronology) || (closedAt != null && !closedChronology)) {
        problems.push(`deliverable ${clean(d.id)} in ${clean(entry.name)}: ending must be on or after builtAt and not in the future`);
      }
      if ((sentOn != null && !sentDayChronology) || (closedOn != null && !closedDayChronology)) {
        problems.push(`deliverable ${clean(d.id)} in ${clean(entry.name)}: ending day must be on or after builtAt and not in the future`);
      }
      const duplicateSendPrecision = Boolean(rawSent && rawSentOn);
      const duplicateClosePrecision = Boolean(rawClosed && rawClosedOn);
      if (duplicateSendPrecision) {
        problems.push(`deliverable ${clean(d.id)} in ${clean(entry.name)}: has both "sentAt" and "sentOn" — choose the precision the evidence supports`);
      }
      if (duplicateClosePrecision) {
        problems.push(`deliverable ${clean(d.id)} in ${clean(entry.name)}: has both "closedAt" and "closedOn" — choose the precision the evidence supports`);
      }
      const rawSend = rawSent || rawSentOn;
      const rawClose = rawClosed || rawClosedOn;
      if (rawSend && rawClose) {
        const sendField = rawSent ? "sentAt" : "sentOn";
        const closeField = rawClosed ? "closedAt" : "closedOn";
        problems.push(`deliverable ${clean(d.id)} in ${clean(entry.name)}: has both "${sendField}" and "${closeField}" — a send and a non-send closure cannot both be true`);
      }
      const ambiguousEnding = duplicateSendPrecision || duplicateClosePrecision || Boolean(rawSend && rawClose);
      if (!ambiguousEnding && (sentChronology || sentDayChronology)) continue;
      if (closedAt != null || closedOn != null) {
        const reason = isStr(d.closedReason) ? d.closedReason.trim() : "";
        const because = isStr(d.closedBecause) ? d.closedBecause.trim() : "";
        const closedField = closedAt != null ? "closedAt" : "closedOn";
        // A half-written closure keeps alarming: going quiet on a typo is the
        // failure this field exists to end.
        if (!CLOSE_REASONS.includes(reason) || !because) {
          problems.push(`deliverable ${clean(d.id)} in ${clean(entry.name)}: "${closedField}" needs a "closedReason" of ${CLOSE_REASONS.join("/")} and a "closedBecause" saying why`);
        } else if (!ambiguousEnding && (closedChronology || closedDayChronology)) {
          continue;
        }
      }
      // One-line alarms are injected session context — cap every field hard.
      const who = isStr(d.recipient) && d.recipient.trim() ? clean(d.recipient, 80) : "unnamed recipient";
      const what = isStr(d.artifact) && d.artifact.trim() ? ` (${clean(d.artifact, 120)})` : "";
      // A built artifact whose claims went stale is worse than an unbuilt one:
      // it reads as ready. The warning rides the alarm so it reaches the reader.
      const warn = isStr(d.warning) && d.warning.trim() ? ` ⚠ DO NOT SEND COLD: ${clean(d.warning, 160)}` : "";
      alarms.push(`UNSENT deliverable: ${clean(entry.name, 80)}/${clean(d.id, 80)} for ${who} — built ${fmtAge(Date.now() - built)} ago${what}${warn}`);
    }
  }
  return alarms;
}

// ---- import-scan lint (the registry verifies itself) ----
// A manifest nobody validates is a map that lies, and the sweep-before-build
// doctrine makes that map the mandatory first step. So: walk each primitive's
// declared source files, resolve their intra-repo imports, map each resolved
// file back to its owning primitive, and fail the emit when a real import
// crosses a primitive boundary that no `consumes` edge declares. Resolution is
// regex-level over comment-blanked source — it finds edges that exist, and it
// cannot find edges that are not imports (HTTP seams, Apex, cross-repo), which
// stay hand-declared.

function skipStringLiteral(src, i, quote) {
  let j = i + 1;
  while (j < src.length) {
    if (src[j] === "\\") {
      j += 2;
      continue;
    }
    if (src[j] === quote) return j + 1;
    if (quote !== "`" && src[j] === "\n") return j; // unterminated: give up at EOL rather than eat the file
    j++;
  }
  return src.length;
}

function blankComments(src, isPy) {
  // Comment bodies become spaces and newlines survive, so line numbers in a
  // miss line point at the real import site. Prose that quotes an import — and
  // this codebase's headers are full of it — must never forge an edge.
  const out = src.split("");
  const blank = (a, b) => {
    for (let k = a; k < b; k++) if (out[k] !== "\n") out[k] = " ";
  };
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (isPy) {
      if (c === "#") {
        let j = i;
        while (j < src.length && src[j] !== "\n") j++;
        blank(i, j);
        i = j;
        continue;
      }
      const fence = src.slice(i, i + 3);
      if (fence === '"""' || fence === "'''") {
        const end = src.indexOf(fence, i + 3);
        const j = end === -1 ? src.length : end + 3;
        blank(i, j);
        i = j;
        continue;
      }
    } else {
      if (c === "/" && src[i + 1] === "/") {
        let j = i;
        while (j < src.length && src[j] !== "\n") j++;
        blank(i, j);
        i = j;
        continue;
      }
      if (c === "/" && src[i + 1] === "*") {
        const end = src.indexOf("*/", i + 2);
        const j = end === -1 ? src.length : end + 2;
        blank(i, j);
        i = j;
        continue;
      }
    }
    if (c === '"' || c === "'" || (!isPy && c === "`")) {
      i = skipStringLiteral(src, i, c);
      continue;
    }
    i++;
  }
  return out.join("");
}

function lineIndex(src) {
  const starts = [0];
  for (let i = 0; i < src.length; i++) if (src[i] === "\n") starts.push(i + 1);
  return (offset) => {
    let lo = 0, hi = starts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (starts[mid] <= offset) lo = mid;
      else hi = mid - 1;
    }
    return lo + 1;
  };
}

function firstFile(candidates) {
  for (const c of candidates) {
    const st = fs.statSync(c, { throwIfNoEntry: false });
    if (st && st.isFile()) return c;
  }
  return null;
}

function resolvePyModule(roots, parts) {
  if (!parts.length) return null;
  for (const r of roots) {
    const p = path.join(r, ...parts);
    const hit = firstFile([p + ".py", path.join(p, "__init__.py")]);
    if (hit) return hit;
  }
  return null;
}

function scanImports(absFile, raw) {
  // -> [{ line, spec, target }] where target is an absolute path that exists.
  const isPy = path.extname(absFile).toLowerCase() === ".py";
  const src = blankComments(raw, isPy);
  const lineOf = lineIndex(src);
  const dir = path.dirname(absFile);
  const found = [];
  if (isPy) {
    const rel = /^[ \t]*from[ \t]+(\.*)([A-Za-z_][\w.]*)?[ \t]+import[ \t]+([^\n]*)/gm;
    for (let m; (m = rel.exec(src)); ) {
      const [, dots, mod, names] = m;
      // An INDENTED import is a different claim than a module-level one:
      // deferring an import inside a function is the standard way to BREAK a
      // dependency cycle, so counting it as a hard edge asks the author to
      // declare the very cycle the deferral exists to avoid. It is still real
      // coupling worth surfacing — as a note, never as a fatal edge.
      const deferred = /^[ \t]/.test(m[0]);
      let base = dir;
      for (let k = 1; k < dots.length; k++) base = path.dirname(base);
      const searchRoots = dots ? [base] : [dir, path.dirname(dir)];
      const parts = mod ? mod.split(".") : [];
      const hit = resolvePyModule(searchRoots, parts);
      if (hit) found.push({ line: lineOf(m.index), spec: dots + (mod || ""), target: hit, deferred });
      // `from pkg import mod` and `from . import mod`: the imported names may
      // themselves be modules, and those are the real targets.
      const submoduleRoot = hit && path.basename(hit) === "__init__.py" ? path.dirname(hit) : parts.length ? null : dots ? base : null;
      if (submoduleRoot) {
        for (const n of names.split(",")) {
          const name = n.trim().replace(/[()]/g, "").split(/\s+as\s+/)[0].trim();
          if (!/^[A-Za-z_]\w*$/.test(name)) continue;
          const sub = resolvePyModule([submoduleRoot], [name]);
          if (sub) found.push({ line: lineOf(m.index), spec: dots + (mod ? mod + "." : "") + name, target: sub, deferred });
        }
      }
    }
    const abs = /^[ \t]*import[ \t]+([A-Za-z_][\w.]*(?:[ \t]*,[ \t]*[A-Za-z_][\w.]*)*)/gm;
    for (let m; (m = abs.exec(src)); ) {
      const deferred = /^[ \t]/.test(m[0]);
      for (const mod of m[1].split(",")) {
        const parts = mod.trim().split(".");
        const hit = resolvePyModule([dir, path.dirname(dir)], parts);
        if (hit) found.push({ line: lineOf(m.index), spec: mod.trim(), target: hit, deferred });
      }
    }
    // absolute dotted specs are also resolved from the repo root by the caller's
    // root candidate list below; `import pkg.core` from a repo-root script is
    // covered by the [dir, parent] roots above.
    return found;
  }
  const js = /(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(\s*|\bimport\s*)(['"])(\.[^'"\n]*)\1/g;
  for (let m; (m = js.exec(src)); ) {
    const spec = m[2];
    const base = path.resolve(dir, spec);
    const hit = firstFile(JS_RESOLVE_SUFFIXES.map((s) => base + s));
    if (hit) found.push({ line: lineOf(m.index), spec, target: hit });
  }
  return found;
}

function collectSourceFiles(repoDir, rel, excludeDirs) {
  // repo-relative code files under a declared path; the same escape refusal as
  // the staleness walk, so a manifest cannot aim the scanner outside its repo.
  const start = path.join(repoDir, rel);
  const within = path.relative(repoDir, start);
  if (within.startsWith("..") || path.isAbsolute(within)) return [];
  const st = fs.statSync(start, { throwIfNoEntry: false });
  if (!st) return [];
  const out = [];
  const stack = [{ p: start, depth: 0 }];
  let entries = 0;
  while (stack.length && entries < WALK_MAX_ENTRIES) {
    const { p: cur, depth } = stack.pop();
    entries++;
    const s = fs.statSync(cur, { throwIfNoEntry: false });
    if (!s) continue;
    if (s.isDirectory()) {
      if (excludeDirs.has(path.basename(cur)) || depth >= WALK_MAX_DEPTH) continue;
      for (const child of fs.readdirSync(cur)) stack.push({ p: path.join(cur, child), depth: depth + 1 });
    } else if (IMPORT_EXTS.has(path.extname(cur).toLowerCase())) {
      out.push(cur);
    }
  }
  return out;
}

function importLint(root, repos, excludeDirs) {
  const misses = new Map(); // "from->to" -> message, first site wins
  const deferredNotes = new Map(); // same key — coupling to know about, never fatal
  for (const r of repos) {
    const repoDir = path.join(root, r.dir);
    // resolve() normalizes the separators a suffix-joined resolution introduces,
    // and Windows path casing must not split one file into two owners
    const key = (abs) => {
      const r = path.resolve(abs);
      return process.platform === "win32" ? r.toLowerCase() : r;
    };
    const owners = new Map(); // file key -> [{ id, depth }]
    for (const p of r.primitives) {
      for (const rel of p.paths) {
        const depth = rel.split(/[\\/]+/).filter(Boolean).length;
        for (const f of collectSourceFiles(repoDir, rel, excludeDirs)) {
          const k = key(f);
          if (!owners.has(k)) owners.set(k, []);
          owners.get(k).push({ id: p.id, depth });
        }
      }
    }
    // The most specific declaration wins: a primitive that names one file owns
    // it even when a sibling primitive declares the whole directory.
    const ownersOf = (abs) => {
      const list = owners.get(key(abs));
      if (!list) return [];
      const deepest = Math.max(...list.map((o) => o.depth));
      return [...new Set(list.filter((o) => o.depth === deepest).map((o) => o.id))].sort();
    };
    const consumesOf = new Map(r.primitives.map((p) => [p.id, new Set(p.consumes)]));
    const files = [];
    for (const p of r.primitives) {
      for (const rel of p.paths) for (const f of collectSourceFiles(repoDir, rel, excludeDirs)) files.push(f);
    }
    for (const abs of [...new Set(files)].sort()) {
      const from = ownersOf(abs);
      if (!from.length) continue;
      const relFile = path.relative(repoDir, abs).split(path.sep).join("/");
      // an unreadable declared source is a hole in the lint, so it is left to
      // throw: main() reports it and --emit exits 1 rather than passing silently
      for (const imp of scanImports(abs, fs.readFileSync(abs, "utf8"))) {
        const to = ownersOf(imp.target);
        if (!to.length) continue;
        for (const owner of from) {
          if (to.includes(owner)) continue;
          if (to.some((t) => consumesOf.get(owner).has(t))) continue;
          const pair = `${owner}->${to.join("|")}`;
          if (imp.deferred) {
            // A function-local import is how a cycle is deliberately broken;
            // making it a fatal edge would demand the manifest declare the
            // cycle the code does not have. Surfaced, not enforced.
            if (!misses.has(pair) && !deferredNotes.has(pair))
              deferredNotes.set(
                pair,
                `deferred import: ${owner} uses ${to.join("|")} inside a function (${clean(r.dir, 120)}/${clean(relFile, 300)}:${imp.line} imports ${clean(imp.spec, 200)}) — real coupling, not declared and not required to be`,
              );
            continue;
          }
          if (misses.has(pair)) continue;
          deferredNotes.delete(pair); // a top-level site outranks a deferred one
          misses.set(
            pair,
            `import lint: ${owner} -> ${to.join("|")} undeclared (${clean(r.dir, 120)}/${clean(relFile, 300)}:${imp.line} imports ${clean(imp.spec, 200)})`,
          );
        }
      }
    }
  }
  return { problems: [...misses.values()].sort(), notes: [...deferredNotes.values()].sort() };
}

function buildGraph(repos, problems) {
  const nodes = new Map(); // id -> { ...primitive, repo }
  for (const r of repos) {
    for (const p of r.primitives || []) {
      if (nodes.has(p.id)) problems.push(`duplicate primitive id: ${p.id} (${nodes.get(p.id).repo} and ${r.repo})`);
      nodes.set(p.id, { ...p, repo: r.repo });
    }
  }
  const edges = []; // [from, to] = from consumes to
  for (const n of nodes.values()) {
    for (const target of n.consumes || []) {
      if (!nodes.has(target)) problems.push(`unknown consumes target: ${n.id} -> ${target}`);
      else edges.push([n.id, target]);
    }
  }
  edges.sort((a, b) => a[0].localeCompare(b[0]) || a[1].localeCompare(b[1]));
  const inDegree = new Map([...nodes.keys()].map((id) => [id, 0]));
  const consumers = new Map([...nodes.keys()].map((id) => [id, []]));
  for (const [from, to] of edges) {
    inDegree.set(to, inDegree.get(to) + 1);
    consumers.get(to).push(from);
  }
  const ids = [...nodes.keys()].sort();
  // A terminal product is a delivered surface a human uses directly: zero
  // in-edges is its correct end state, not dormant value waiting to be wired.
  const terminals = ids.filter((id) => nodes.get(id).terminal);
  const orphans = ids.filter((id) => inDegree.get(id) === 0 && !nodes.get(id).terminal);
  const hubs = ids
    .filter((id) => inDegree.get(id) >= 2)
    .sort((a, b) => inDegree.get(b) - inDegree.get(a) || a.localeCompare(b));
  return { nodes, edges, inDegree, consumers, orphans, hubs, terminals };
}

function newestUnder(base, rel, sinceMs, excludeDirs) {
  // { newestMs, newerCount, capped, escaped } for files under base/rel; capped
  // means the depth or entry limit was hit (counts are a lower bound); escaped
  // means rel resolved outside base and was refused.
  let newestMs = 0, newerCount = 0, entries = 0, capped = false;
  const start = path.join(base, rel);
  const within = path.relative(base, start);
  if (within.startsWith("..") || path.isAbsolute(within)) return { newestMs, newerCount, capped, escaped: true };
  if (!fs.existsSync(start)) return { newestMs, newerCount, capped, escaped: false };
  const stack = [{ p: start, depth: 0 }];
  while (stack.length) {
    if (entries >= WALK_MAX_ENTRIES) {
      capped = true;
      break;
    }
    const { p: cur, depth } = stack.pop();
    entries++;
    const st = fs.statSync(cur, { throwIfNoEntry: false }); // broken symlinks resolve to nothing
    if (!st) continue;
    if (st.isDirectory()) {
      if (excludeDirs.has(path.basename(cur))) continue;
      if (depth >= WALK_MAX_DEPTH) {
        capped = true;
        continue;
      }
      for (const child of fs.readdirSync(cur)) stack.push({ p: path.join(cur, child), depth: depth + 1 });
    } else {
      if (path.basename(cur) === "substrate.json") continue;
      if (st.mtimeMs > newestMs) newestMs = st.mtimeMs;
      if (st.mtimeMs > sinceMs) newerCount++;
    }
  }
  return { newestMs, newerCount, capped, escaped: false };
}

function git(repoDir, args) {
  return execFileSync("git", ["-C", repoDir, ...args], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    maxBuffer: GIT_MAX_BUFFER,
  }).trim();
}

function gitAvailable() {
  try {
    execFileSync("git", ["--version"], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function samePath(a, b) {
  // git prints forward slashes; realpath both sides so symlinked temp dirs
  // (macOS /var -> /private/var) and Windows casing cannot break the compare.
  const canon = (p) => {
    const r = fs.realpathSync(path.resolve(p));
    return process.platform === "win32" ? r.toLowerCase() : r;
  };
  return canon(a) === canon(b);
}

function isGitWorkTree(dir) {
  // rev-parse handles worktrees and gitfile setups; the toplevel compare stops
  // a plain subdir from inheriting an ANCESTOR repo's work tree.
  try {
    if (git(dir, ["rev-parse", "--is-inside-work-tree"]) !== "true") return false;
    return samePath(git(dir, ["rev-parse", "--show-toplevel"]), dir);
  } catch {
    return false;
  }
}

function stalenessAlarms(repos, root, config) {
  const alarms = [];
  const notes = [];
  const graceMs = config.graceHours * 60 * 60 * 1000;
  const gitOk = gitAvailable();
  let gitSkipped = false;
  for (const r of [...repos].sort((a, b) => a.repo.localeCompare(b.repo))) {
    try {
      const repoDir = path.join(root, r.dir);
      const declared = Object.prototype.hasOwnProperty.call(config.nonGitRepos, r.dir);
      if (gitOk && isGitWorkTree(repoDir)) {
        const lastCommitMs = parseInt(git(repoDir, ["log", "-1", "--format=%ct"]), 10) * 1000;
        if (lastCommitMs > r.mtimeMs + graceMs) {
          const files = new Set(
            git(repoDir, ["log", `--since=@${Math.floor(r.mtimeMs / 1000)}`, "--name-only", "--pretty=format:"])
              .split("\n").map((s) => s.trim()).filter((s) => s && s !== "substrate.json"),
          );
          // only alarm when a non-manifest file actually changed (a lone re-commit of substrate.json is not drift)
          if (files.size) alarms.push(`manifest may be stale: ${r.repo} changed ${files.size} files since manifest`);
        }
      } else if (declared) {
        let newestMs = 0, newerCount = 0, capped = false, escaped = false;
        for (const rel of config.nonGitRepos[r.dir]) {
          const res = newestUnder(repoDir, rel, r.mtimeMs, config.excludeDirs);
          if (res.newestMs > newestMs) newestMs = res.newestMs;
          newerCount += res.newerCount;
          capped = capped || res.capped;
          escaped = escaped || res.escaped;
        }
        if (escaped) notes.push(`config path escaped ${r.repo}'s directory and was ignored — nonGitRepos paths must stay inside the repo`);
        if (capped) notes.push(`walk capped for ${r.repo}: limits are depth ${WALK_MAX_DEPTH} / ${WALK_MAX_ENTRIES} entries, so newer-file counts are a lower bound`);
        if (newestMs > r.mtimeMs + graceMs) {
          alarms.push(`manifest may be stale: ${r.repo} changed ${newerCount} files since manifest`);
        }
      } else if (gitOk) {
        notes.push(`staleness skipped: ${r.repo} is not a git repo and has no nonGitRepos entry in substrate.config.json`);
      } else {
        gitSkipped = true; // cannot classify without git; the single note below covers it
      }
    } catch (e) {
      alarms.push(`staleness check failed: ${r.repo} (${clean(e.message.split("\n")[0])})`);
    }
  }
  if (gitSkipped) notes.unshift("git not found — staleness skipped for git repos");
  return { alarms, notes };
}

function emitMarkdown(repos, graph, alarms, notes, problems) {
  const { nodes, edges, inDegree, consumers, orphans, hubs, terminals } = graph;
  const L = [];
  L.push("# SUBSTRATE — generated primitive registry");
  L.push("");
  L.push(`Generated by \`substrate-graph.mjs\` from ${repos.length} \`substrate.json\` manifests. Do not edit by hand — regenerate with \`/substrate:emit\`. Shipping a primitive means updating that repo's \`substrate.json\` in the same commit.`);
  L.push("");
  L.push("**The emit gate.** A manifest edit and the emit ride the same commit. The import-scan lint reads every primitive's declared source, resolves its intra-repo imports, and fails the emit with a named `from -> to` miss list when a real import crosses a primitive boundary no `consumes` edge declares — so a stale graph blocks rather than regenerates quietly. The lint sees imports only: HTTP seams, Apex calls and cross-repo consumers stay hand-declared, and `\"terminal\": true` marks a delivered surface whose zero in-edges are the end state.");
  L.push("");
  L.push(`## Nodes (${nodes.size} primitives, by repo)`);
  for (const r of [...repos].sort((a, b) => a.repo.localeCompare(b.repo))) {
    L.push("");
    L.push(`### ${r.repo}`);
    for (const p of [...(r.primitives || [])].sort((a, b) => a.id.localeCompare(b.id))) {
      L.push(`- **${p.id}** (${p.kind}, ${p.status}${p.terminal ? ", terminal" : ""}) — ${p.desc}`);
      L.push(`  - paths: ${p.paths.map((x) => `\`${x}\``).join(", ")}`);
      if ((p.consumes || []).length) L.push(`  - consumes: ${p.consumes.join(", ")}`);
    }
  }
  L.push("");
  L.push(`## Edges (${edges.length} — "A consumes B")`);
  L.push("");
  for (const [from, to] of edges) L.push(`- ${from} → ${to}`);
  L.push("");
  L.push("## Analytics");
  L.push("");
  L.push(`### Orphans (${orphans.length} — no in-edges; dormant-value candidates)`);
  L.push("");
  for (const id of orphans) L.push(`- ${id} (${nodes.get(id).repo})`);
  L.push("");
  L.push(`### Terminal products (${terminals.length} — delivered surfaces; zero in-edges is the correct end state)`);
  L.push("");
  if (terminals.length) for (const id of terminals) L.push(`- ${id} (${nodes.get(id).repo}) — ${nodes.get(id).desc}`);
  else L.push("- none");
  L.push("");
  L.push(`### Hubs (in-degree ≥ 2 — harden first)`);
  L.push("");
  for (const id of hubs) L.push(`- ${id} (${nodes.get(id).repo}) ← ${inDegree.get(id)}: ${consumers.get(id).sort().join(", ")}`);
  L.push("");
  L.push("### Staleness alarms (as of last generation)");
  L.push("");
  const stale = [...alarms, ...notes];
  if (stale.length) for (const a of stale) L.push(`- ${a}`);
  else L.push("- none");
  if (problems.length) {
    L.push("");
    L.push("### Manifest problems");
    L.push("");
    for (const p of problems) L.push(`- ${p}`);
  }
  L.push("");
  return L.join("\n");
}

function writeSubstrateMd(out, text) {
  // never write THROUGH a symlink: the content is manifest-derived, so a symlink
  // at <root>/SUBSTRATE.md pointing at an auto-loaded file would be a write primitive
  if (fs.existsSync(out) && fs.lstatSync(out).isSymbolicLink()) fs.unlinkSync(out);
  fs.writeFileSync(out, text, "utf8");
}

function main() {
  const argv = process.argv.slice(2);
  const mode = argv.includes("--emit") ? "emit" : argv.includes("--json") ? "json" : "check";
  const root = resolveRoot(argv);
  const out = path.join(root, "SUBSTRATE.md");
  const problems = [];
  const config = loadConfig(root, problems);
  const repos = loadManifests(root, config.excludeDirs, problems);
  const graph = buildGraph(repos, problems);
  const lint = importLint(root, repos, config.excludeDirs);
  problems.push(...lint.problems);
  const { alarms, notes } = stalenessAlarms(repos, root, config);
  notes.push(...lint.notes);
  alarms.push(...deliverableAlarms(root, config.excludeDirs, problems));
  if (mode === "emit") {
    writeSubstrateMd(out, emitMarkdown(repos, graph, alarms, notes, problems));
    console.log(`wrote ${out}: ${repos.length} repos, ${graph.nodes.size} primitives, ${graph.edges.length} edges, ${graph.orphans.length} orphans, ${graph.hubs.length} hubs, ${alarms.length} alarms`);
    if (problems.length) {
      for (const p of problems) console.error(`problem: ${p}`);
      process.exitCode = 1;
    }
  } else if (mode === "json") {
    // The same graph SUBSTRATE.md renders, as machine-readable JSON, so other
    // tools (fluxpoint's recall layer among them) consume a projection instead
    // of parsing prose or raw manifests. Same rules as the markdown: sorted,
    // sanitized, capped, no timestamps — deterministic for identical manifests.
    const prims = [...graph.nodes.keys()].sort().map((id) => {
      const n = graph.nodes.get(id);
      return {
        id: clean(id, 200),
        repo: clean(n.repo || "", 200),
        kind: clean(n.kind || "", 80),
        status: clean(n.status || "", 80),
        desc: clean(n.desc || "", 500),
        paths: (Array.isArray(n.paths) ? n.paths : []).slice(0, 50).map((p) => clean(p, 500)),
        consumes: (Array.isArray(n.consumes) ? n.consumes : []).slice(0, 50).map((c) => clean(c, 200)),
      };
    });
    console.log(JSON.stringify({
      version: 1,
      repos: repos.map((r) => clean(r.repo, 200)).sort(),
      primitives: prims,
      edges: graph.edges.map(([a, b]) => [clean(a, 200), clean(b, 200)]),
      orphans: graph.orphans.map((id) => clean(id, 200)),
      hubs: graph.hubs.map((id) => ({ id: clean(id, 200), inDegree: graph.inDegree.get(id) })),
      problems: capList(problems).map((p) => clean(p)),
    }));
    if (problems.length) process.exitCode = 1;
  } else {
    const hubStr = graph.hubs.map((id) => `${id}(${graph.inDegree.get(id)})`).join(", ") || "none";
    console.log(`substrate: ${repos.length} repos / ${graph.nodes.size} primitives / ${graph.edges.length} edges | orphans: ${graph.orphans.length} | hubs: ${hubStr}`);
    console.log(`full graph: ${out} (regenerated by /substrate:emit)`);
    for (const p of capList(problems)) console.log(`PROBLEM: ${p}`);
    for (const a of capList(alarms)) console.log(`ALARM: ${a}`);
    for (const n of capList(notes)) console.log(`NOTE: ${n}`);
    if (!alarms.length && !notes.length) console.log("staleness: all manifests current");
  }
}

try {
  main();
} catch (e) {
  console.log(`substrate-graph error: ${e.message}`);
  // --check must stay exit-0 (it feeds a hook); the machine modes must not:
  // a consumer that got prose instead of JSON needs the exit code to say so.
  if (process.argv.includes("--emit") || process.argv.includes("--json")) process.exitCode = 1;
}
