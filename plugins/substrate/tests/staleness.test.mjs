import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { makeWorkspace, makeGitRepo, writeManifest, writeConfig, run, prim, gitInit, gitCommit, setMtime } from "./helpers.mjs";

const HOUR = 3600 * 1000;

function has(text, needle) {
  assert.ok(text.includes(needle), `expected output to contain:\n${needle}\n--- got ---\n${text}`);
}

test("alarm fires on real drift in a git repo", () => {
  const ws = makeWorkspace();
  const mf = makeGitRepo(ws, "driftrepo", { repo: "driftrepo", primitives: [prim("d-core")] });
  // manifest predates the commit by 24h; the commit touched code.txt, so this is drift
  setMtime(mf, Date.now() - 24 * HOUR);
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  has(res.stdout, "ALARM: manifest may be stale: driftrepo changed 1 files since manifest");
  assert.ok(!res.stdout.match(/ALARM:.*refresh|re-run/), "alarm text stays factual, no imperatives");
});

test("no alarm on a lone manifest re-commit", () => {
  const ws = makeWorkspace();
  const d = path.join(ws, "calm");
  fs.mkdirSync(d);
  fs.writeFileSync(path.join(d, "code.txt"), "code\n");
  gitInit(d);
  const mf = writeManifest(ws, "calm", { repo: "calm", primitives: [prim("c-core")] });
  gitCommit(d, "base", Math.floor(Date.now() / 1000) - 48 * 3600);
  // re-commit touching ONLY substrate.json, dated now
  writeManifest(ws, "calm", { repo: "calm", primitives: [prim("c-core", { status: "core-only" })] });
  gitCommit(d, "manifest refresh");
  setMtime(mf, Date.now() - 24 * HOUR);
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  assert.ok(!res.stdout.includes("ALARM:"), `lone manifest re-commit is not drift:\n${res.stdout}`);
  has(res.stdout, "staleness: all manifests current");
});

test("git missing yields exactly one note, never per-repo spam", () => {
  const ws = makeWorkspace();
  makeGitRepo(ws, "g1", { repo: "g1", primitives: [prim("g1-core")] });
  makeGitRepo(ws, "g2", { repo: "g2", primitives: [prim("g2-core")] });
  const emptyBin = fs.mkdtempSync(path.join(os.tmpdir(), "substrate-nobin-"));
  const env = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (!/^path$/i.test(k)) env[k] = v;
  }
  env.PATH = emptyBin;
  const res = run(["--check", "--root", ws], { envReplace: env });
  assert.equal(res.status, 0, "--check exits 0 even without git");
  const note = "git not found — staleness skipped for git repos";
  assert.equal(res.stdout.split(note).length - 1, 1, `expected the note exactly once:\n${res.stdout}`);
  assert.ok(!res.stdout.includes("staleness check failed"), `no per-repo failure spam:\n${res.stdout}`);
});

test("undeclared non-git repo is skipped with a one-line note", () => {
  const ws = makeWorkspace();
  writeManifest(ws, "plain", { repo: "plain", primitives: [prim("p-core")] });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  has(res.stdout, 'NOTE: staleness skipped: plain is not a git repo; add "plain" to nonGitRepos in substrate.config.json');
  assert.ok(!res.stdout.includes("ALARM:"), `skip, not alarm:\n${res.stdout}`);
});

test("declared non-git repo alarms via mtime", () => {
  const ws = makeWorkspace();
  const mf = writeManifest(ws, "plain2", { repo: "plain2", primitives: [prim("p2-core")] });
  fs.mkdirSync(path.join(ws, "plain2", "src"));
  fs.writeFileSync(path.join(ws, "plain2", "src", "new.txt"), "fresh\n");
  writeConfig(ws, { nonGitRepos: { plain2: ["src"] } });
  setMtime(mf, Date.now() - 24 * HOUR);
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  has(res.stdout, "ALARM: manifest may be stale: plain2 changed 1 files since manifest");
});

test("graceHours override suppresses the mtime alarm", () => {
  const ws = makeWorkspace();
  const mf = writeManifest(ws, "plain2", { repo: "plain2", primitives: [prim("p2-core")] });
  fs.mkdirSync(path.join(ws, "plain2", "src"));
  fs.writeFileSync(path.join(ws, "plain2", "src", "new.txt"), "fresh\n");
  writeConfig(ws, { graceHours: 1000, nonGitRepos: { plain2: ["src"] } });
  setMtime(mf, Date.now() - 24 * HOUR);
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  assert.ok(!res.stdout.includes("ALARM:"), `24h drift inside a 1000h grace window:\n${res.stdout}`);
  has(res.stdout, "staleness: all manifests current");
});

test("excludeDirs override prunes the walk", () => {
  const ws = makeWorkspace();
  const mf = writeManifest(ws, "plain3", { repo: "plain3", primitives: [prim("p3-core")] });
  fs.mkdirSync(path.join(ws, "plain3", "generated"));
  fs.writeFileSync(path.join(ws, "plain3", "generated", "new.txt"), "fresh\n");
  writeConfig(ws, { nonGitRepos: { plain3: ["."] } });
  setMtime(mf, Date.now() - 24 * HOUR);
  let res = run(["--check", "--root", ws]);
  has(res.stdout, "ALARM: manifest may be stale: plain3 changed 1 files since manifest");

  writeConfig(ws, { excludeDirs: ["generated"], nonGitRepos: { plain3: ["."] } });
  res = run(["--check", "--root", ws]);
  assert.ok(!res.stdout.includes("ALARM:"), `excluded dir must not count as drift:\n${res.stdout}`);
});

test("deep walk is capped and says so", () => {
  const ws = makeWorkspace();
  const mf = writeManifest(ws, "plain4", { repo: "plain4", primitives: [prim("p4-core")] });
  const deep = path.join(ws, "plain4", "deep", "a", "b", "c", "d", "e", "f", "g");
  fs.mkdirSync(deep, { recursive: true });
  fs.writeFileSync(path.join(deep, "new.txt"), "unreachably fresh\n");
  writeConfig(ws, { nonGitRepos: { plain4: ["deep"] } });
  setMtime(mf, Date.now() - 24 * HOUR);
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  has(res.stdout, "NOTE: walk capped for plain4");
  assert.ok(!res.stdout.includes("ALARM:"), `files beyond the cap are honestly unseen, not guessed:\n${res.stdout}`);
});

test("non-git diagnostics name the directory key when the manifest uses a different repo label", () => {
  const ws = makeWorkspace();
  const mf = writeManifest(ws, "svc-dir", { repo: "svc", primitives: [prim("svc-core")] });
  fs.mkdirSync(path.join(ws, "svc-dir", "src"));
  fs.writeFileSync(path.join(ws, "svc-dir", "src", "code.py"), "x = 1");
  setMtime(mf, Date.now() - 24 * HOUR);
  writeConfig(ws, { nonGitRepos: { svc: ["src"] } });
  const skipped = run(["--check", "--root", ws]);
  has(skipped.stdout, 'NOTE: staleness skipped: svc (dir svc-dir) is not a git repo; add "svc-dir" to nonGitRepos in substrate.config.json');
  writeConfig(ws, { nonGitRepos: { "svc-dir": ["src", "../outside"] } });
  const stale = run(["--check", "--root", ws]);
  has(stale.stdout, "ALARM: manifest may be stale: svc (dir svc-dir) changed 1 files since manifest");
  has(stale.stdout, "NOTE: config path escaped svc (dir svc-dir)'s directory");
  assert.ok(!stale.stdout.includes("staleness skipped:"), stale.stdout);
});

test("git failures and walk limits identify a directory different from the manifest name", () => {
  const ws = makeWorkspace();
  writeManifest(ws, "unborn-dir", { repo: "unborn", primitives: [] });
  gitInit(path.join(ws, "unborn-dir"));
  writeManifest(ws, "deep-dir", { repo: "deep", primitives: [] });
  fs.mkdirSync(path.join(ws, "deep-dir", "src", "a", "b", "c", "d", "e", "f", "g"), { recursive: true });
  writeConfig(ws, { nonGitRepos: { "deep-dir": ["src"] } });
  const res = run(["--check", "--root", ws]);
  has(res.stdout, "ALARM: staleness check failed: unborn (dir unborn-dir)");
  has(res.stdout, "NOTE: walk capped for deep (dir deep-dir)");
});
