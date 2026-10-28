import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { makeWorkspace, makeGitRepo, writeManifest, writeConfig, run, prim, gitCommit, setMtime } from "./helpers.mjs";

function git(dir, ...args) {
  return execFileSync("git", ["-C", dir, ...args], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
}

function addWorktree(ws, from, to, branch) {
  git(path.join(ws, from), "worktree", "add", "-q", "-b", branch, path.join(ws, to));
}

function ledger(dir) {
  fs.writeFileSync(path.join(dir, "deliverables.json"), JSON.stringify({ deliverables: [
    { id: "report-1", recipient: "Avery", builtAt: "2026-01-01T00:00:00Z", sentAt: null },
  ] }));
}

test("main checkout, changed worktree, and local clone form one canonical graph and alarm set", () => {
  const ws = makeWorkspace();
  const main = path.join(ws, "z-main");
  const mf = makeGitRepo(ws, "z-main", { repo: "alpha", primitives: [
    prim("alpha-core", { desc: "canonical main", paths: ["src/core.mjs"] }),
    prim("alpha-tool", { consumes: ["alpha-core"], paths: ["src/tool.mjs"] }),
  ] }, { "src/core.mjs": "export const core = 1;", "src/tool.mjs": 'import { core } from "./core.mjs";' });
  ledger(main);
  gitCommit(main, "ledger");
  addWorktree(ws, "z-main", "a-feature", "feature");
  // An ignored branch has a different manifest and a real lint failure. Neither
  // can replace or contaminate the canonical checkout's graph.
  writeManifest(ws, "a-feature", { repo: "alpha", primitives: [
    prim("alpha-core", { desc: "feature copy", paths: ["src/core.mjs"] }),
    prim("alpha-tool", { paths: ["src/tool.mjs"] }),
  ] });
  gitCommit(path.join(ws, "a-feature"), "feature manifest");
  git(ws, "clone", "-q", main, path.join(ws, "b-clone"));
  setMtime(mf, Date.now() - 48 * 3600 * 1000);
  const check = run(["--check", "--root", ws]);
  assert.equal(check.status, 0, check.stderr);
  assert.match(check.stdout, /substrate: 1 repos \/ 2 primitives \/ 1 edges/);
  assert.doesNotMatch(check.stdout, /PROBLEM:/);
  assert.equal((check.stdout.match(/UNSENT deliverable:/g) || []).length, 1, check.stdout);
  assert.match(check.stdout, /UNSENT deliverable: z-main\/report-1/);
  assert.equal((check.stdout.match(/manifest may be stale:/g) || []).length, 1, check.stdout);
  assert.match(check.stdout, /manifest may be stale: alpha \(dir z-main\)/);
  assert.equal((check.stdout.match(/other checkouts ignored/g) || []).length, 1, check.stdout);
  assert.match(check.stdout, /NOTE: z-main: 2 other checkouts ignored \(a-feature, b-clone\)/);

  const json = run(["--json", "--root", ws]);
  assert.equal(json.status, 0, json.stderr);
  const graph = JSON.parse(json.stdout);
  assert.deepEqual(graph.repos, ["alpha"]);
  assert.equal(graph.primitives.length, 2);
  assert.equal(graph.primitives.find((p) => p.id === "alpha-core").desc, "canonical main");
  assert.equal(graph.notes.filter((n) => n.includes("other checkouts ignored")).length, 1);
  const emit = run(["--emit", "--root", ws]);
  assert.equal(emit.status, 0, emit.stderr);
  const md = fs.readFileSync(path.join(ws, "SUBSTRATE.md"), "utf8");
  assert.equal((md.match(/^### alpha \(dir z-main\)$/gm) || []).length, 1, md);
  assert.doesNotMatch(md, /feature copy/);
});

test("worktrees select the main checkout, then first name when main is outside the root", () => {
  const outer = makeWorkspace(), ws = makeWorkspace();
  makeGitRepo(outer, "main", { repo: "alpha", primitives: [prim("core")] });
  git(path.join(outer, "main"), "worktree", "add", "-q", "-b", "zeta", path.join(ws, "zeta"));
  git(path.join(outer, "main"), "worktree", "add", "-q", "-b", "beta", path.join(ws, "beta"));
  writeManifest(ws, "beta", { repo: "alpha", primitives: [prim("core", { desc: "first by name" })] });
  const graph = JSON.parse(run(["--json", "--root", ws]).stdout);
  assert.deepEqual(graph.repos, ["alpha"]);
  assert.equal(graph.primitives[0].desc, "first by name");
  assert.ok(graph.notes.includes("beta: 1 other worktree ignored (zeta)"));
});

test("independent clones sharing a normalized remote are collapsed without a network request", () => {
  const ws = makeWorkspace();
  makeGitRepo(ws, "a", { repo: "alpha", primitives: [prim("core", { desc: "first" })] });
  makeGitRepo(ws, "b", { repo: "alpha", primitives: [prim("core", { desc: "second" })] });
  git(path.join(ws, "a"), "remote", "add", "origin", "git@EXAMPLE.com:team/alpha.git");
  git(path.join(ws, "b"), "remote", "add", "origin", "https://example.com/team/alpha/");
  const res = run(["--json", "--root", ws]);
  assert.equal(res.status, 0, res.stderr);
  const graph = JSON.parse(res.stdout);
  assert.deepEqual(graph.repos, ["alpha"]);
  assert.equal(graph.primitives[0].desc, "first");
  assert.ok(graph.notes.includes("a: 1 other checkout ignored (b)"));
});

test("origins on different nondefault ports retain separate repositories", () => {
  const ws = makeWorkspace();
  makeGitRepo(ws, "a", { repo: "alpha", primitives: [prim("alpha-core")] });
  makeGitRepo(ws, "b", { repo: "beta", primitives: [prim("beta-core")] });
  git(path.join(ws, "a"), "remote", "add", "origin", "ssh://git@example.com:443/team/alpha.git");
  git(path.join(ws, "b"), "remote", "add", "origin", "https://example.com/team/alpha.git");
  const res = run(["--json", "--root", ws]);
  assert.equal(res.status, 0, res.stderr);
  const graph = JSON.parse(res.stdout);
  assert.deepEqual(graph.repos, ["alpha", "beta"]);
  assert.deepEqual(graph.primitives.map((p) => p.id), ["alpha-core", "beta-core"]);
});

test("ledger-only worktrees use the same canonical discovery", () => {
  const ws = makeWorkspace();
  makeGitRepo(ws, "main", { repo: "alpha", primitives: [] });
  fs.unlinkSync(path.join(ws, "main", "substrate.json"));
  ledger(path.join(ws, "main"));
  gitCommit(path.join(ws, "main"), "ledger only");
  addWorktree(ws, "main", "feature", "feature");
  const check = run(["--check", "--root", ws]);
  assert.match(check.stdout, /substrate: 0 repos/);
  assert.equal((check.stdout.match(/UNSENT deliverable:/g) || []).length, 1, check.stdout);
  assert.match(check.stdout, /main: 1 other worktree ignored \(feature\)/);
});

test("unverified same-name copies produce one actionable collision and preserve dangling edges", () => {
  const ws = makeWorkspace();
  const primitives = Array.from({ length: 100 }, (_, i) => prim(`id-${i}`));
  writeManifest(ws, "alpha", { repo: "shared", primitives });
  writeManifest(ws, "alpha-copy", { repo: "shared", primitives: [
    ...primitives.slice(0, -1), prim("id-99", { consumes: ["ghost"] }),
  ] });
  const check = run(["--check", "--root", ws]);
  assert.equal((check.stdout.match(/PROBLEM:/g) || []).length, 2, check.stdout);
  assert.match(check.stdout, /unknown consumes target: id-99 -> ghost \(alpha-copy\/substrate.json\)/);
  assert.match(check.stdout, /repo shared is declared in 2 directories \(alpha\/substrate.json, alpha-copy\/substrate.json\); exclude a directory with excludeRepos/);
  assert.doesNotMatch(check.stdout, /duplicate primitive id:|more suppressed/);
  assert.equal(run(["--emit", "--root", ws]).status, 1);
  writeConfig(ws, { excludeRepos: ["*-copy"] });
  assert.equal(run(["--emit", "--root", ws]).status, 0);
});

test("a duplicate inside a conflicting same-name manifest is still a schema problem", () => {
  const ws = makeWorkspace();
  writeManifest(ws, "alpha", { repo: "shared", primitives: [prim("dup")] });
  writeManifest(ws, "beta", { repo: "shared", primitives: [prim("dup"), prim("dup")] });
  const res = run(["--emit", "--root", ws]);
  assert.equal(res.status, 1);
  assert.match(res.stderr, /duplicate primitive id: dup \(beta\/substrate.json and beta\/substrate.json\)/);
});

test("actionable consumes and import errors precede a flood of unrelated duplicate ids", () => {
  const ws = makeWorkspace();
  const primitives = Array.from({ length: 60 }, (_, i) => prim(`id-${i}`, { paths: [] }));
  writeManifest(ws, "a", { repo: "first", primitives });
  writeManifest(ws, "b", { repo: "second", primitives: [
    ...primitives, prim("tool", { consumes: ["ghost"], paths: ["src/tool.mjs"] }),
    prim("core", { paths: ["src/core.mjs"] }),
  ] });
  fs.mkdirSync(path.join(ws, "b", "src"));
  fs.writeFileSync(path.join(ws, "b", "src", "core.mjs"), "export const core = 1;");
  fs.writeFileSync(path.join(ws, "b", "src", "tool.mjs"), 'import { core } from "./core.mjs";');
  const check = run(["--check", "--root", ws]);
  assert.match(check.stdout, /PROBLEM: unknown consumes target: tool -> ghost/);
  assert.match(check.stdout, /PROBLEM: import lint: tool -> core undeclared/);
  assert.match(check.stdout, /duplicate primitive id: id-0 \(a\/substrate.json and b\/substrate.json\)/);
  assert.match(check.stdout, /more suppressed/);
});

test("discovery-only excludes remove manifests and ledgers without pruning nested source paths", () => {
  const ws = makeWorkspace();
  writeManifest(ws, "keep", { repo: "keep", primitives: [
    prim("tool", { paths: ["wt-old/tool.mjs"] }), prim("core", { paths: ["wt-old/core.mjs"] }),
  ] });
  fs.mkdirSync(path.join(ws, "keep", "wt-old"));
  fs.writeFileSync(path.join(ws, "keep", "wt-old", "core.mjs"), "export const core = 1;");
  fs.writeFileSync(path.join(ws, "keep", "wt-old", "tool.mjs"), 'import { core } from "./core.mjs";');
  writeManifest(ws, "wt-old", { repo: "ignore", primitives: [prim("ignored")] });
  ledger(path.join(ws, "wt-old"));
  writeConfig(ws, { excludeRepos: ["wt-*"] });
  const check = run(["--check", "--root", ws]);
  assert.match(check.stdout, /substrate: 1 repos \/ 2 primitives/);
  assert.match(check.stdout, /import lint: tool -> core undeclared \(keep\/wt-old\/tool.mjs/);
  assert.doesNotMatch(check.stdout, /UNSENT|ignored/);
});
