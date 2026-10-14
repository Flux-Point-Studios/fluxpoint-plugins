#!/usr/bin/env node
// memory-lint: deterministic staleness checks over the workspace's memory
// files, emitted on the substrate alarm channel at session start. It only
// flags what it can verify against ground truth — a cited path that is gone,
// an index entry pointing at a deleted memory, an "unsent" claim that
// deliverables.json contradicts. It never rewrites a memory: detection is
// mechanical, repair needs judgment, and the agent reading the alarm owns it.
//
//   node memory-lint.mjs [--root <workspace>] [--dir <memoryDir>]
//
// Defaults: root = cwd; memory dir = ~/.claude/projects/<key>/memory where
// <key> is the root path with every [:\/] replaced by '-' (the harness's own
// project-dir convention). Exit is 0 unconditionally — a lint that can wedge
// a session start is worse than the staleness it reports.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const MAX_LINES = 20;
const MAX_ECHO = 160;

function arg(name) {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

// Same boundary discipline as substrate-graph: nothing file-derived reaches
// stdout carrying control characters, and nothing unbounded.
const CTRL = new RegExp("[" + String.fromCharCode(0) + "-" + String.fromCharCode(31)
  + String.fromCharCode(127) + "-" + String.fromCharCode(159)
  + String.fromCharCode(0x2028) + String.fromCharCode(0x2029) + "]", "g");
function clean(s) {
  return String(s).replace(CTRL, "").slice(0, MAX_ECHO);
}

function defaultMemoryDir(root) {
  const key = path.resolve(root).replace(/[:\\/]/g, "-");
  return path.join(os.homedir(), ".claude", "projects", key, "memory");
}

function pathCandidates(body, root) {
  const out = [];
  for (const m of body.matchAll(/`([^`\n]+)`/g)) {
    const t = m[1].trim().replace(/[.,;:]+$/, "");
    if (!/[\\/]/.test(t)) continue;
    if (t.length > 200) continue;
    if (/^https?:/i.test(t)) continue;
    if (/[*?{}<>|"…]/.test(t)) continue;
    if (/(^|\s)-/.test(t)) continue; // command lines with flags
    // Preserve literal filenames with spaces when the whole citation exists;
    // interpreting that known file as a command would invent a missing prefix.
    if (/\s/.test(t) && fs.existsSync(path.join(root, stripLineRef(t.replace(/\\/g, "/"))))) {
      out.push(t);
      continue;
    }
    // A command or annotated citation may follow its path with other words.
    // Only its leading word can name the file; a later slash-bearing word in
    // `User-Agent: app/1.0` is not a citation.
    const head = t.split(/\s+/)[0];
    if (/[\\/]/.test(head)) out.push(head);
  }
  return out;
}

// Drive-letter paths only. A leading "/" token is an API endpoint or a URL
// fragment far more often than a file on this platform — those stay silent.
function isAbsolute(p) {
  return /^[A-Za-z]:[\\/]/.test(p);
}

// Alarm only where absence is provable: a relative path with an existing
// anchor that runs through a repository, including when root itself is one.
// A same-named directory under root is not enough to establish that a citation
// is root-relative. Unknown anchors and other machines stay silent.
// `path/to/file.ts:44` and `:44-58` are the house citation convention — they are
// what makes a reference clickable — so the line part must come off before the
// file is looked for. Left on, every cited line number reads as a missing file,
// and a lint that is wrong more often than right is one people stop reading.
function stripLineRef(p) {
  return p.replace(/:\d+(?::\d+)?(?:-\d+)?$/, "");
}

function isRepo(dir) {
  return fs.existsSync(path.join(dir, ".git")) ||
    fs.existsSync(path.join(dir, "substrate.json"));
}

function inRepo(root, norm) {
  if (isRepo(root)) return true;
  let dir = root;
  for (const segment of norm.split("/").slice(0, -1)) {
    dir = path.join(dir, segment);
    if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) return false;
    if (isRepo(dir)) return true;
  }
  return false;
}

function missingPath(root, norm) {
  // A drive-letter path names a filesystem this process cannot see: our cold
  // keys live on a Windows box by deliberate policy. Checking it here can only
  // ever produce a false alarm — and worse, POSIX path.dirname cannot parse a
  // backslashed path, so `C:\a\b\` collapses to "." which always exists and the
  // "parent exists, file does not" heuristic fires every single session.
  if (isAbsolute(norm)) return false;
  const first = norm.split("/")[0];
  if (first === "" || first === "." || first === "..") return false;
  const anchor = path.join(root, first);
  if (!fs.existsSync(anchor) || !fs.statSync(anchor).isDirectory()) return false;
  if (!inRepo(root, norm)) return false;
  return !fs.existsSync(path.join(root, norm));
}

function sentDeliverables(root) {
  const sent = [];
  let entries = [];
  try {
    entries = fs.readdirSync(root, { withFileTypes: true });
  } catch {
    return sent;
  }
  for (const e of entries) {
    if (!e.isDirectory()) continue;
    const f = path.join(root, e.name, "deliverables.json");
    if (!fs.existsSync(f)) continue;
    try {
      const d = JSON.parse(fs.readFileSync(f, "utf8"));
      for (const it of d.deliverables ?? []) {
        if (it.id && it.sentAt) sent.push({ id: it.id, sentAt: it.sentAt });
      }
    } catch {
      // a malformed deliverables file is substrate-graph's finding, not ours
    }
  }
  return sent;
}

function main() {
  const root = path.resolve(arg("--root") ?? process.cwd());
  const memDir = arg("--dir") ?? defaultMemoryDir(root);
  if (!fs.existsSync(memDir)) return;

  const lines = [];
  const files = fs
    .readdirSync(memDir)
    .filter((f) => f.endsWith(".md") && f !== "MEMORY.md");
  const sent = sentDeliverables(root);
  const unsentRe = /\b(not sent|unsent|never sent|pending send)\b/i;

  const index = path.join(memDir, "MEMORY.md");
  if (fs.existsSync(index)) {
    const body = fs.readFileSync(index, "utf8");
    for (const m of body.matchAll(/\]\(([^)\s]+\.md)\)/g)) {
      if (!fs.existsSync(path.join(memDir, m[1]))) {
        lines.push(`ALARM: memory-lint: MEMORY.md indexes missing file: ${clean(m[1])}`);
      }
    }
  }

  for (const f of files) {
    const name = f.replace(/\.md$/, "");
    const body = fs.readFileSync(path.join(memDir, f), "utf8");
    const seenPaths = new Set();
    for (const cand of pathCandidates(body, root)) {
      // Different line references and slash spellings still cite one file.
      // Keep the first spelling for the alarm, and count it once per memory.
      const norm = stripLineRef(cand.replace(/\\/g, "/"));
      if (seenPaths.has(norm)) continue;
      seenPaths.add(norm);
      if (missingPath(root, norm)) {
        lines.push(
          `ALARM: memory-lint: ${clean(name)} cites missing path: ${clean(cand.replace(/\\/g, "/"))}`
        );
      }
    }
    if (unsentRe.test(body)) {
      for (const d of sent) {
        if (!body.includes(d.id)) continue;
        const near = body
          .split("\n")
          .some((ln, i, all) =>
            ln.includes(d.id) &&
            all.slice(Math.max(0, i - 2), i + 3).some((l) => unsentRe.test(l))
          );
        if (near) {
          lines.push(
            `NOTE: memory-lint: ${clean(name)} says unsent, but '${clean(d.id)}' was sent ${clean(d.sentAt)}`
          );
        }
      }
    }
  }

  if (lines.length === 0 && files.length === 0) return;
  for (const l of lines.slice(0, MAX_LINES)) console.log(l);
  if (lines.length > MAX_LINES) {
    console.log(`NOTE: memory-lint: ${lines.length - MAX_LINES} more finding(s) suppressed`);
  }
  const n = files.length;
  console.log(
    `memory-lint: ${n} memor${n === 1 ? "y" : "ies"} scanned, ` +
      `${lines.filter((l) => l.startsWith("ALARM")).length} alarm(s), ` +
      `${lines.filter((l) => l.startsWith("NOTE")).length} note(s)`
  );
}

main();
process.exitCode = 0;
