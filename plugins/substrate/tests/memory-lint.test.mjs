// memory-lint checks the workspace's memory files against ground truth it can
// verify deterministically: cited paths that no longer exist, index entries
// pointing at deleted memories, and "unsent" claims that deliverables.json
// says were sent. It flags; it never rewrites — the agent owns the repair.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT = fileURLToPath(new URL("../scripts/memory-lint.mjs", import.meta.url));

function makeFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "memlint-ws-"));
  const memDir = fs.mkdtempSync(path.join(os.tmpdir(), "memlint-mem-"));
  fs.mkdirSync(path.join(root, "sca-kpi", "etl"), { recursive: true });
  fs.mkdirSync(path.join(root, "sca-kpi", ".git"));
  fs.writeFileSync(path.join(root, "sca-kpi", "etl", "real.py"), "x = 1\n");
  // A colon is legal in a POSIX filename. Without one here, `split(":")[0]` and an
  // anchored numeric strip behave identically on every other case, so nothing
  // would pin the difference and the imprecise version would pass review.
  fs.writeFileSync(path.join(root, "sca-kpi", "etl", "od:d.py"), "y = 2\n");
  return { root, memDir };
}

function writeMemory(memDir, name, body) {
  const file = path.join(memDir, `${name}.md`);
  fs.writeFileSync(
    file,
    `---\nname: ${name}\ndescription: fixture\nmetadata:\n  type: project\n---\n\n${body}\n`
  );
  return file;
}

function run({ root, memDir }) {
  const res = spawnSync(process.execPath, [SCRIPT, "--root", root, "--dir", memDir], {
    encoding: "utf8",
  });
  return { code: res.status, out: res.stdout ?? "", err: res.stderr ?? "" };
}

test("an existing cited path stays silent", () => {
  const fx = makeFixture();
  writeMemory(fx.memDir, "good", "The module lives at `sca-kpi/etl/real.py` today.");
  const { code, out } = run(fx);
  assert.equal(code, 0);
  assert.ok(!out.includes("ALARM"), out);
  assert.match(out, /memory-lint: 1 memor/);
});

test("an existing filename containing spaces is not mistaken for a command", () => {
  const fx = makeFixture();
  fs.writeFileSync(path.join(fx.root, "sca-kpi", "etl", "my file.txt"), "exists");
  writeMemory(fx.memDir, "spaces", "See `sca-kpi/etl/my file.txt` and `sca-kpi/etl/my file.txt:12`.");
  const { code, out } = run(fx);
  assert.equal(code, 0);
  assert.ok(!out.includes("ALARM"), out);
});

test("a missing path under an existing anchor raises an ALARM naming memory and path", () => {
  const fx = makeFixture();
  writeMemory(fx.memDir, "stale", "See `sca-kpi/etl/gone.py` for the loader.");
  const { code, out } = run(fx);
  assert.equal(code, 0, "lint must never block session start");
  assert.match(out, /ALARM: memory-lint: stale cites missing path: sca-kpi\/etl\/gone\.py/);
});

test("a repo-relative citation colliding with an unrelated root directory stays silent", () => {
  const fx = makeFixture();
  fs.mkdirSync(path.join(fx.root, "bin"));
  fs.mkdirSync(path.join(fx.root, "ops"));
  fs.mkdirSync(path.join(fx.root, "sca-kpi", "bin"));
  fs.writeFileSync(path.join(fx.root, "sca-kpi", "bin", "bench.py"), "print(1)\n");
  writeMemory(fx.memDir, "collide", "The bench repo is `sca-kpi/`. Run `bin/bench.py`; see `ops/runbook.md`.");
  const { code, out } = run(fx);
  assert.equal(code, 0);
  assert.ok(!out.includes("ALARM"), out);
});

test("a stale citation through a repo nested under a plain directory still alarms", () => {
  const fx = makeFixture();
  fs.mkdirSync(path.join(fx.root, "work", "app", ".git"), { recursive: true });
  writeMemory(fx.memDir, "nested", "See `work/app/src/gone.ts`.");
  const { out } = run(fx);
  assert.match(out, /ALARM: memory-lint: nested cites missing path: work\/app\/src\/gone\.ts/);
});

test("gitfiles and substrate.json both establish repository anchors", () => {
  const fx = makeFixture();
  fs.mkdirSync(path.join(fx.root, "work", "checkout"), { recursive: true });
  fs.writeFileSync(path.join(fx.root, "work", "checkout", ".git"), "gitdir: /elsewhere/worktrees/checkout\n");
  fs.mkdirSync(path.join(fx.root, "projects", "managed"), { recursive: true });
  fs.writeFileSync(path.join(fx.root, "projects", "managed", "substrate.json"), "{}\n");
  writeMemory(fx.memDir, "markers", "See `work/checkout/src/gone.ts` and `projects/managed/src/gone.ts`.");
  const { out } = run(fx);
  assert.match(out, /cites missing path: work\/checkout\/src\/gone\.ts/);
  assert.match(out, /cites missing path: projects\/managed\/src\/gone\.ts/);
  assert.match(out, /2 alarm\(s\)/);
});

test("a repository root establishes an anchor for its own relative paths", () => {
  const fx = makeFixture();
  fs.mkdirSync(path.join(fx.root, ".git"));
  fs.mkdirSync(path.join(fx.root, "src"));
  writeMemory(fx.memDir, "rootrepo", "See `src/gone.ts`.");
  const { out } = run(fx);
  assert.match(out, /ALARM: memory-lint: rootrepo cites missing path: src\/gone\.ts/);
});

test("a missing directory before any repository marker stays unanchored", () => {
  const fx = makeFixture();
  fs.mkdirSync(path.join(fx.root, "work"));
  fs.writeFileSync(path.join(fx.root, "work", "plain-file"), "not a directory\n");
  writeMemory(fx.memDir, "unknown", "See `work/unknown/src/gone.ts` and `work/plain-file/gone.ts`.");
  const { code, out } = run(fx);
  assert.equal(code, 0);
  assert.ok(!out.includes("ALARM"), out);
});

test("a file:line citation is checked as the FILE, not as a path ending in a number", () => {
  // `path:line` is the house citation convention — it is what makes a reference
  // clickable in the terminal — so flagging every one of them as missing makes
  // the lint wrong more often than right, and an alerter that cries wolf is one
  // people stop reading. Both spellings appear in real memories.
  const fx = makeFixture();
  writeMemory(fx.memDir, "cited", "See `sca-kpi/etl/real.py:44` and `sca-kpi/etl/real.py:44-58`.");
  const { code, out } = run(fx);
  assert.equal(code, 0);
  assert.ok(!out.includes("ALARM"), out);
});

test("only a trailing LINE reference is stripped, never any colon", () => {
  // `split(":")[0]` passes the other cases and is wrong: a colon is legal in a
  // POSIX filename, and file:line:col is a real spelling that tools emit. The
  // strip has to be anchored and numeric or it silently checks the wrong file.
  const fx = makeFixture();
  writeMemory(
    fx.memDir,
    "precise",
    "Both `sca-kpi/etl/real.py:44:12` and `sca-kpi/etl/od:d.py` resolve."
  );
  const { out } = run(fx);
  assert.ok(!out.includes("ALARM"), out);
});

test("a file:line citation whose FILE is gone still raises", () => {
  // The suffix is stripped, not the check: a stale citation must still be caught.
  const fx = makeFixture();
  writeMemory(fx.memDir, "stalecite", "See `sca-kpi/etl/gone.py:44` for the loader.");
  const { code, out } = run(fx);
  assert.match(out, /ALARM: memory-lint: stalecite cites missing path/);
});

test("a whitespace span is checked by its leading path, never a later slash-bearing word", () => {
  const fx = makeFixture();
  writeMemory(
    fx.memDir,
    "args",
    [
      "Run `sca-kpi/etl/real.py check`; see `sca-kpi/etl/real.py:1 SOME_VAR`.",
      "Also `sca-kpi/etl/real.py:1:3 SYMBOL` and `sca-kpi/etl/real.py:1-4 RANGE`.",
      "The probe sends `User-Agent: sca-kpi/1.0`.",
      "Stale: `sca-kpi/etl/gone.py check`.",
    ].join("\n")
  );
  const { code, out } = run(fx);
  assert.equal(code, 0);
  assert.equal((out.match(/^ALARM:/gm) || []).length, 1, out);
  assert.match(out, /ALARM: memory-lint: args cites missing path: sca-kpi\/etl\/gone\.py$/m);
});

test("annotated citations for a missing file retain their leading line reference", () => {
  const fx = makeFixture();
  writeMemory(fx.memDir, "symbol", "See `sca-kpi/etl/gone.py:12 SOME_VAR`.");
  const { out } = run(fx);
  assert.match(out, /ALARM: memory-lint: symbol cites missing path: sca-kpi\/etl\/gone\.py:12$/m);
  assert.ok(!out.includes("SOME_VAR"), out);
});

test("repeated references to a missing path in one memory count as one finding", () => {
  const fx = makeFixture();
  writeMemory(fx.memDir, "dup", "The loader is `sca-kpi/etl/gone.py`.\n\nFiles:\n- `sca-kpi/etl/gone.py` - loader");
  const { out } = run(fx);
  assert.equal((out.match(/^ALARM:/gm) || []).length, 1, out);
  assert.match(out, /1 alarm\(s\)/);
});

test("slash spellings and line references for one file share a finding", () => {
  const fx = makeFixture();
  writeMemory(
    fx.memDir,
    "normalized",
    [
      "See `sca-kpi/etl/gone.py:10` and `sca-kpi/etl/gone.py:20:3`.",
      "Again `sca-kpi/etl/gone.py:30-40 SYMBOL`, `sca-kpi/etl/gone.py check`,",
      "and `sca-kpi\\etl\\gone.py:50`.",
    ].join("\n")
  );
  const { out } = run(fx);
  assert.equal((out.match(/^ALARM:/gm) || []).length, 1, out);
  assert.match(out, /cites missing path: sca-kpi\/etl\/gone\.py:10$/m);
  assert.match(out, /1 alarm\(s\)/);
});

test("the same missing path in different memories remains a finding for each memory", () => {
  const fx = makeFixture();
  writeMemory(fx.memDir, "first", "See `sca-kpi/etl/gone.py` twice: `sca-kpi/etl/gone.py`.");
  writeMemory(fx.memDir, "second", "See `sca-kpi/etl/gone.py`.");
  const { out } = run(fx);
  assert.match(out, /ALARM: memory-lint: first cites missing path/);
  assert.match(out, /ALARM: memory-lint: second cites missing path/);
  assert.equal((out.match(/^ALARM:/gm) || []).length, 2, out);
  assert.match(out, /2 memories scanned, 2 alarm\(s\)/);
});

test("duplicates do not consume the output cap or inflate suppressed findings", () => {
  const fx = makeFixture();
  const repeated = Array(30).fill("`sca-kpi/etl/repeated.py`");
  const distinct = Array.from({ length: 21 }, (_, i) => `\`sca-kpi/etl/gone-${i}.py\``);
  writeMemory(fx.memDir, "capped", [...repeated, ...distinct].join("\n"));
  const { out } = run(fx);
  assert.equal((out.match(/^ALARM:/gm) || []).length, 20, out);
  assert.equal((out.match(/cites missing path: sca-kpi\/etl\/repeated\.py/g) || []).length, 1, out);
  assert.match(out, /cites missing path: sca-kpi\/etl\/gone-18\.py/);
  assert.match(out, /2 more finding\(s\) suppressed/);
  assert.match(out, /22 alarm\(s\)/);
});

test("a Windows drive-letter path is never checked against THIS filesystem", () => {
  // A `C:/...` path names a machine this process cannot see — our cold keys live
  // on one. Reporting it missing is guaranteed wrong, every session, forever.
  const fx = makeFixture();
  // BACKSLASHED, as Windows paths are actually written in our memories. POSIX
  // path.dirname cannot parse them, so the whole path collapses to "." — which
  // always exists, so the "parent exists, file does not" heuristic fires every
  // time. The forward-slash spelling took a different branch and hid this.
  writeMemory(fx.memDir, "otherbox", String.raw`The cold key is at \`C:\FPS_Development\cardano-node\spo-node\\\` on another box.`);
  const { code, out } = run(fx);
  assert.equal(code, 0);
  assert.ok(!out.includes("ALARM"), out);
});

test("unanchored paths, URLs, globs, and flagged commands stay silent", () => {
  const fx = makeFixture();
  writeMemory(
    fx.memDir,
    "noisy",
    [
      "Run `node broker/cli.js demo` then `uv run --with pytest python -m pytest tests -q`.",
      "Docs at `https://example.com/a/b` and pattern `data/*.local/*.json`.",
      "Repo-relative elsewhere: `etl/unknown_anchor.py`.",
      "API endpoints: `/transactions` and `/drives/x/items` are not files.",
      "Dot-relative in some repo: `./scripts/demo.sh` has no workspace anchor.",
    ].join("\n")
  );
  const { code, out } = run(fx);
  assert.equal(code, 0);
  assert.ok(!out.includes("ALARM"), out);
});

test("a MEMORY.md index entry pointing at a deleted memory file alarms", () => {
  const fx = makeFixture();
  fs.writeFileSync(
    path.join(fx.memDir, "MEMORY.md"),
    "# Memory Index\n\n- [Ghost](ghost-memory.md) — points nowhere\n"
  );
  const { out } = run(fx);
  assert.match(out, /ALARM: memory-lint: MEMORY\.md indexes missing file: ghost-memory\.md/);
});

test("an 'unsent' claim contradicted by deliverables.json is a NOTE, not an ALARM", () => {
  const fx = makeFixture();
  fs.mkdirSync(path.join(fx.root, "notes"));
  fs.writeFileSync(
    path.join(fx.root, "notes", "deliverables.json"),
    JSON.stringify({
      deliverables: [
        { id: "widget-note", sentAt: "2026-08-20T12:00:00Z" },
        { id: "other-note", sentAt: null },
      ],
    })
  );
  writeMemory(
    fx.memDir,
    "contra",
    "The widget-note reply is drafted but NOT sent yet.\nAlso other-note is unsent."
  );
  const { out } = run(fx);
  assert.match(out, /NOTE: memory-lint: contra says unsent, but 'widget-note' was sent 2026-08-20/);
  assert.ok(!out.includes("other-note"), "an actually-unsent deliverable is not a contradiction");
  assert.ok(!out.match(/ALARM.*widget-note/), "contradictions are notes, alarms are for hard facts");
});

test("control characters in memory content never reach stdout", () => {
  const fx = makeFixture();
  writeMemory(fx.memDir, "hostile", "See `sca-kpi/etl/" + String.fromCharCode(27) + "[31mgone" + String.fromCharCode(27) + "[0m.py` now.");
  const { out } = run(fx);
  assert.ok(out.includes("ALARM"), "the missing path is still reported");
  assert.ok(!out.includes(String.fromCharCode(27)), "escape bytes must be stripped from echoed content");
});

test("a missing memory dir is a silent success", () => {
  const fx = makeFixture();
  fs.rmSync(fx.memDir, { recursive: true, force: true });
  const { code, out } = run(fx);
  assert.equal(code, 0);
  assert.equal(out.trim(), "");
});
