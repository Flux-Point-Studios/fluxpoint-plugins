import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { makeWorkspace, makeGitRepo, writeManifest, run, prim } from "./helpers.mjs";

function has(text, needle) {
  assert.ok(text.includes(needle), `expected output to contain:\n${needle}\n--- got ---\n${text}`);
}

test("discovery + emit renders every pinned section", () => {
  const ws = makeWorkspace();
  makeGitRepo(ws, "alpha", {
    repo: "alpha",
    primitives: [prim("a-core"), prim("a-tool", { kind: "toolkit", consumes: ["a-core"] })],
  });
  makeGitRepo(ws, "beta", {
    repo: "beta",
    primitives: [prim("b-api", { kind: "api", paths: ["api", "docs/contract.md"], consumes: ["a-core"] }), prim("b-demo", { status: "core-only" })],
  });
  makeGitRepo(ws, "gamma", { repo: "gamma", primitives: [] });

  const res = run(["--emit", "--root", ws]);
  assert.equal(res.status, 0, res.stderr);
  const outPath = path.join(ws, "SUBSTRATE.md");
  has(res.stdout, `wrote ${outPath}: 3 repos, 4 primitives, 2 edges, 3 orphans, 1 hubs, 0 alarms`);

  const md = fs.readFileSync(outPath, "utf8");
  assert.ok(md.startsWith("# SUBSTRATE — generated primitive registry\n"), "header");
  has(md, "from 3 `substrate.json` manifests");
  has(md, "Shipping a primitive means updating that repo's `substrate.json` in the same commit.");
  has(md, "## Nodes (4 primitives, by repo)");
  has(md, "### alpha\n- **a-core** (engine, live) — a-core primitive\n  - paths: `src`");
  has(md, "- **a-tool** (toolkit, live) — a-tool primitive\n  - paths: `src`\n  - consumes: a-core");
  has(md, "- **b-api** (api, live) — b-api primitive\n  - paths: `api`, `docs/contract.md`\n  - consumes: a-core");
  has(md, "- **b-demo** (engine, core-only) — b-demo primitive");
  has(md, "### gamma"); // zero-primitive repos still get their header
  has(md, '## Edges (2 — "A consumes B")');
  has(md, "- a-tool → a-core");
  has(md, "- b-api → a-core");
  has(md, "### Orphans (3 — no in-edges; dormant-value candidates)");
  has(md, "- a-tool (alpha)");
  has(md, "- b-api (beta)");
  has(md, "- b-demo (beta)");
  has(md, "### Hubs (in-degree ≥ 2 — harden first)");
  has(md, "- a-core (alpha) ← 2: a-tool, b-api");
  has(md, "### Staleness alarms (as of last generation)\n\n- none");
  assert.ok(!md.includes("### Manifest problems"), "no problems section on a clean workspace");
  assert.ok(md.endsWith("\n") && !md.endsWith("\n\n"), "exactly one trailing newline");
});

test("duplicate ids, dangling consumes, unreadable manifests -> problems and emit exit 1", () => {
  const ws = makeWorkspace();
  fs.mkdirSync(path.join(ws, "bad"));
  fs.writeFileSync(path.join(ws, "bad", "substrate.json"), "{nope");
  writeManifest(ws, "r1", { repo: "r1", primitives: [prim("dup")] });
  writeManifest(ws, "r2", { repo: "r2", primitives: [prim("dup"), prim("x", { consumes: ["ghost"] })] });

  const res = run(["--emit", "--root", ws]);
  assert.equal(res.status, 1, "emit exits 1 on manifest problems");
  has(res.stderr, "problem: unreadable manifest: bad/substrate.json");
  has(res.stderr, "problem: duplicate primitive id: dup (r1/substrate.json and r2/substrate.json)");
  has(res.stderr, "problem: unknown consumes target: x -> ghost");
  // the file is still written even when problems exist
  const md = fs.readFileSync(path.join(ws, "SUBSTRATE.md"), "utf8");
  has(md, "### Manifest problems");
  has(md, "- duplicate primitive id: dup (r1/substrate.json and r2/substrate.json)");

  const check = run(["--check", "--root", ws]);
  assert.equal(check.status, 0, "--check always exits 0, even with problems");
  has(check.stdout, "PROBLEM: duplicate primitive id: dup (r1/substrate.json and r2/substrate.json)");
  has(check.stdout, "PROBLEM: unknown consumes target: x -> ghost");
});

test("every primitive schema diagnostic identifies the manifest file despite a different repo label", () => {
  const ws = makeWorkspace();
  writeManifest(ws, "bad-list-dir", { repo: "label", primitives: {} });
  writeManifest(ws, "bad-primitives-dir", { repo: "other-label", primitives: [
    {}, prim("bad-kind", { kind: false }), prim("bad-terminal", { terminal: "yes" }),
  ] });
  const res = run(["--emit", "--root", ws]);
  assert.equal(res.status, 1);
  has(res.stderr, 'invalid manifest: bad-list-dir/substrate.json field "primitives" is not an array');
  has(res.stderr, 'invalid primitive in bad-primitives-dir/substrate.json: each needs a string "id"');
  has(res.stderr, 'invalid primitive bad-kind in bad-primitives-dir/substrate.json: kind/status/desc');
  has(res.stderr, 'invalid primitive bad-terminal in bad-primitives-dir/substrate.json: "terminal" must be a boolean');
});

test("orphan and hub math: in-degree ordering in the compact summary", () => {
  const ws = makeWorkspace();
  writeManifest(ws, "m", {
    repo: "m",
    primitives: [
      prim("c1"),
      prim("c2"),
      prim("u1", { consumes: ["c1", "c2"] }),
      prim("u2", { consumes: ["c1", "c2"] }),
      prim("u3", { consumes: ["c1"] }),
    ],
  });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  has(res.stdout, "substrate: 1 repos / 5 primitives / 5 edges | orphans: 3 | hubs: c1(3), c2(2)");
});

test("optional version: 1 key is tolerated", () => {
  const ws = makeWorkspace();
  writeManifest(ws, "v", { repo: "v", version: 1, primitives: [prim("v-core")] });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  assert.ok(!res.stdout.includes("PROBLEM"), `unexpected problem:\n${res.stdout}`);
  has(res.stdout, "substrate: 1 repos / 1 primitives / 0 edges");
});

test("two emits are byte-identical", () => {
  const ws = makeWorkspace();
  makeGitRepo(ws, "det", { repo: "det", primitives: [prim("d-core"), prim("d-tool", { consumes: ["d-core"] })] });
  assert.equal(run(["--emit", "--root", ws]).status, 0);
  const first = fs.readFileSync(path.join(ws, "SUBSTRATE.md"));
  assert.equal(run(["--emit", "--root", ws]).status, 0);
  const second = fs.readFileSync(path.join(ws, "SUBSTRATE.md"));
  assert.ok(first.equals(second), "emit output must be deterministic");
});

test("root resolution: CLAUDE_PROJECT_DIR, --root precedence, cwd fallback, resolved path printed", () => {
  const ws1 = makeWorkspace();
  writeManifest(ws1, "r", { repo: "r", primitives: [prim("r-core")] });
  const ws2 = makeWorkspace();
  writeManifest(ws2, "r", { repo: "r", primitives: [prim("r-core")] });
  const ws3 = makeWorkspace();
  writeManifest(ws3, "r", { repo: "r", primitives: [prim("r-core")] });

  assert.equal(run(["--emit"], { env: { CLAUDE_PROJECT_DIR: ws1 } }).status, 0);
  assert.ok(fs.existsSync(path.join(ws1, "SUBSTRATE.md")), "CLAUDE_PROJECT_DIR is honored");

  assert.equal(run(["--emit", "--root", ws2], { env: { CLAUDE_PROJECT_DIR: ws1 } }).status, 0);
  assert.ok(fs.existsSync(path.join(ws2, "SUBSTRATE.md")), "--root beats CLAUDE_PROJECT_DIR");

  assert.equal(run(["--emit"], { cwd: ws3 }).status, 0);
  assert.ok(fs.existsSync(path.join(ws3, "SUBSTRATE.md")), "cwd is the fallback root");

  const check = run(["--check", "--root", ws3]);
  has(check.stdout, `full graph: ${path.join(ws3, "SUBSTRATE.md")}`);
});
