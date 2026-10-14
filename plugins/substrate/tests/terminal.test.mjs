// Terminal products: a delivered end-user surface with zero in-edges is at its
// correct end state, not a dormant-value candidate.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { makeWorkspace, writeManifest, run, prim } from "./helpers.mjs";

function has(text, needle) {
  assert.ok(text.includes(needle), `expected output to contain:\n${needle}\n--- got ---\n${text}`);
}

test("terminal primitives leave the orphan list and get their own section", () => {
  const ws = makeWorkspace();
  writeManifest(ws, "t", {
    repo: "t",
    primitives: [
      prim("t-core"),
      prim("t-tool", { consumes: ["t-core"] }),
      prim("t-product", { kind: "toolkit", terminal: true }),
    ],
  });
  const res = run(["--emit", "--root", ws]);
  assert.equal(res.status, 0, res.stderr);
  has(res.stdout, "1 orphans");

  const md = fs.readFileSync(path.join(ws, "SUBSTRATE.md"), "utf8");
  has(md, "- **t-product** (toolkit, live, terminal) — t-product primitive");
  has(md, "### Orphans (1 — no in-edges; dormant-value candidates)");
  has(md, "- t-tool (t)");
  assert.ok(!md.includes("- t-product (t)\n"), "a terminal product is not an orphan");
  has(md, "### Terminal products (1 — delivered surfaces; zero in-edges is the correct end state)");
  has(md, "- t-product (t) — t-product primitive");

  const check = run(["--check", "--root", ws]);
  has(check.stdout, "orphans: 1");
});

test("a terminal primitive still consumes, still counts as a consumer, and is never a hub artifact", () => {
  const ws = makeWorkspace();
  writeManifest(ws, "t", {
    repo: "t",
    primitives: [prim("t-core"), prim("t-other"), prim("t-product", { terminal: true, consumes: ["t-core", "t-other"] })],
  });
  const res = run(["--emit", "--root", ws]);
  assert.equal(res.status, 0, res.stderr);
  const md = fs.readFileSync(path.join(ws, "SUBSTRATE.md"), "utf8");
  has(md, "- t-product → t-core");
  has(md, "- t-product → t-other");
  has(md, "### Orphans (0 — no in-edges; dormant-value candidates)");
});

test("a non-boolean terminal is a manifest problem", () => {
  const ws = makeWorkspace();
  writeManifest(ws, "t", { repo: "t", primitives: [prim("t-bad", { terminal: "yes" })] });
  const res = run(["--emit", "--root", ws]);
  assert.equal(res.status, 1);
  has(res.stderr, 'problem: invalid primitive t-bad in t/substrate.json: "terminal" must be a boolean');
});

test("a workspace with no terminal products still renders the section", () => {
  const ws = makeWorkspace();
  writeManifest(ws, "t", { repo: "t", primitives: [prim("t-core")] });
  assert.equal(run(["--emit", "--root", ws]).status, 0);
  const md = fs.readFileSync(path.join(ws, "SUBSTRATE.md"), "utf8");
  has(md, "### Terminal products (0 — delivered surfaces; zero in-edges is the correct end state)");
  has(md, "- none");
});
