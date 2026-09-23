// The deliverables ledger: deliverables.json beside (or without) substrate.json.
// Task boards and scratchpads that hold "built but not yet sent" state do not
// survive context compaction; this file does, and --check surfaces every open
// obligation at session start.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { makeWorkspace, writeManifest, run, prim } from "./helpers.mjs";

const HOUR = 3600 * 1000;

function has(text, needle) {
  assert.ok(text.includes(needle), `expected output to contain:\n${needle}\n--- got ---\n${text}`);
}

function writeLedger(root, dir, ledger) {
  const d = path.join(root, dir);
  fs.mkdirSync(d, { recursive: true });
  const f = path.join(d, "deliverables.json");
  fs.writeFileSync(f, JSON.stringify(ledger, null, 2) + "\n");
  return f;
}

test("an unsent deliverable alarms at --check, sent ones stay silent", () => {
  const ws = makeWorkspace();
  writeManifest(ws, "repoA", { repo: "repoA", primitives: [prim("a-core")] });
  writeLedger(ws, "repoA", {
    deliverables: [
      { id: "vendor-bundle", recipient: "Derek", artifact: "bundle-2026-08-10.zip",
        builtAt: new Date(Date.now() - 26 * HOUR).toISOString() },
      { id: "sarah-reply", recipient: "Sarah",
        builtAt: new Date(Date.now() - 30 * HOUR).toISOString(),
        sentAt: new Date(Date.now() - 29 * HOUR).toISOString() },
    ],
  });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  has(res.stdout, "ALARM: UNSENT deliverable: repoA/vendor-bundle for Derek — built 26h ago (bundle-2026-08-10.zip)");
  assert.ok(!res.stdout.includes("sarah-reply"), `sent deliverables are not obligations:\n${res.stdout}`);
});

test("a ledger alarms even in a repo with no substrate manifest", () => {
  const ws = makeWorkspace();
  writeLedger(ws, "notes-only", {
    deliverables: [{ id: "exec-brief", recipient: "Andrew",
      builtAt: new Date(Date.now() - 72 * HOUR).toISOString() }],
  });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  has(res.stdout, "ALARM: UNSENT deliverable: notes-only/exec-brief for Andrew — built 3d ago");
});

test("excluded dirs are never scanned for ledgers", () => {
  const ws = makeWorkspace();
  writeLedger(ws, "node_modules", {
    deliverables: [{ id: "phantom", recipient: "nobody", builtAt: new Date().toISOString() }],
  });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  assert.ok(!res.stdout.includes("phantom"), `excludeDirs must apply to ledgers too:\n${res.stdout}`);
});

test("malformed ledgers and undated entries are problems, never crashes or alarms", () => {
  const ws = makeWorkspace();
  const d = path.join(ws, "badrepo");
  fs.mkdirSync(d);
  fs.writeFileSync(path.join(d, "deliverables.json"), "not json at all\n");
  writeLedger(ws, "shaperepo", { deliverables: [{ id: "undated", recipient: "X" }] });
  writeLedger(ws, "arrayrepo", [{ id: "bare-array" }]);
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  has(res.stdout, "PROBLEM: unreadable ledger: badrepo/deliverables.json");
  has(res.stdout, 'PROBLEM: invalid deliverable undated in shaperepo: "builtAt" must be an ISO-8601 instant');
  has(res.stdout, 'PROBLEM: invalid ledger: arrayrepo/deliverables.json needs a "deliverables" array');
  assert.ok(!res.stdout.includes("ALARM:"), `malformed entries must not alarm:\n${res.stdout}`);
});

test("duplicate top-level deliverables keys cannot hide an obligation", () => {
  const ws = makeWorkspace();
  const dir = path.join(ws, "duplicate");
  fs.mkdirSync(dir);
  fs.writeFileSync(
    path.join(dir, "deliverables.json"),
    '{"deliverables":[{"id":"hidden","recipient":"Joe","builtAt":"2026-08-28T00:00:00Z"}],"deliverables":[]}\n'
  );

  const res = run(["--check", "--root", ws]);

  assert.equal(res.status, 0);
  has(res.stdout, "duplicate JSON key deliverables");
});

test("a deliverables ledger symlink cannot read outside the workspace", () => {
  const ws = makeWorkspace();
  const repo = path.join(ws, "linked");
  const outside = path.join(path.dirname(ws), `${path.basename(ws)}-outside-ledger.json`);
  fs.mkdirSync(repo);
  fs.writeFileSync(outside, JSON.stringify({ deliverables: [{
    id: "outside-secret",
    recipient: "nobody",
    builtAt: new Date(Date.now() - 2 * HOUR).toISOString(),
  }] }, null, 2) + "\n");
  let env = {};
  try {
    fs.symlinkSync(outside, path.join(repo, "deliverables.json"), "file");
  } catch {
    const ledger = path.join(repo, "deliverables.json");
    fs.copyFileSync(outside, ledger);
    const preload = path.join(ws, "symlink-shape.cjs");
    fs.writeFileSync(preload, `
const fs = require("node:fs");
const path = require("node:path");
const original = fs.lstatSync;
fs.lstatSync = function (file, ...args) {
  const stat = original.call(this, file, ...args);
  if (path.resolve(file) !== path.resolve(process.env.SUBSTRATE_TEST_SYMLINK)) return stat;
  return new Proxy(stat, { get(target, key) {
    if (key === "isSymbolicLink") return () => true;
    const value = Reflect.get(target, key, target);
    return typeof value === "function" ? value.bind(target) : value;
  } });
};
`);
    env = { NODE_OPTIONS: `--require=${preload}`, SUBSTRATE_TEST_SYMLINK: ledger };
  }

  const res = run(["--check", "--root", ws], { env });

  assert.equal(res.status, 0);
  has(res.stdout, "PROBLEM: unreadable ledger: linked/deliverables.json");
  assert.ok(!res.stdout.includes("outside-secret"), `external ledger content escaped:\n${res.stdout}`);
});

test("deliverables ledgers are byte and entry bounded before indexing", () => {
  const oversized = makeWorkspace();
  writeLedger(oversized, "huge", {
    deliverables: [{
      id: "huge",
      builtAt: new Date().toISOString(),
      warning: "x".repeat(1024 * 1024),
    }],
  });
  const bytes = run(["--check", "--root", oversized]);
  assert.equal(bytes.status, 0);
  has(bytes.stdout, "deliverables.json exceeds 1048576 bytes");

  const crowded = makeWorkspace();
  writeLedger(crowded, "crowded", {
    deliverables: Array.from({ length: 5001 }, (_, index) => ({
      id: `d-${index}`,
      builtAt: new Date().toISOString(),
    })),
  });
  const entries = run(["--check", "--root", crowded]);
  assert.equal(entries.status, 0);
  has(entries.stdout, "deliverables.json exceeds 5000 entries");
});

test("ledger strings are sanitized and capped — the output is injected session context", () => {
  const ws = makeWorkspace();
  writeLedger(ws, "hostile", {
    deliverables: [{
      id: "evil\u001b[31mred\u0007" + "x".repeat(500),
      recipient: "Bob\u0000\u001b[2J",
      builtAt: new Date(Date.now() - 2 * HOUR).toISOString(),
    }],
  });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  assert.ok(!res.stdout.includes("\u001b"), "no escape bytes reach the session context");
  assert.ok(!res.stdout.includes("\u0007"), "no bell bytes either");
  has(res.stdout, "…"); // the 500-char id was capped
});

test("an obligation that ended without a send closes on its reason, not on a fake sentAt", () => {
  const ws = makeWorkspace();
  writeLedger(ws, "repoC", {
    deliverables: [
      { id: "reshaped-preview", recipient: "Jordan", artifact: "preview.html",
        builtAt: "2026-08-15T15:06:19Z",
        sentAt: null,
        closedAt: "2026-08-20T00:00:00Z",
        closedReason: "superseded",
        closedBecause: "superseded by a self-serve page the recipient uses directly; the emailed send no longer exists" },
    ],
  });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  assert.ok(!res.stdout.includes("reshaped-preview"), `a reasoned closure is quiet:\n${res.stdout}`);
  assert.ok(!res.stdout.includes("PROBLEM"), `a well-formed closure is not a problem:\n${res.stdout}`);
});

test("explicit day precision closes legacy obligations without inventing a clock time", () => {
  const ws = makeWorkspace();
  writeLedger(ws, "repoDay", {
    deliverables: [
      { id: "sent-on-known-day", recipient: "Nick",
        builtAt: "2026-08-20T13:00:00Z", sentOn: "2026-08-20" },
      { id: "closed-on-known-day", recipient: "Jordan",
        builtAt: "2026-08-15T15:06:19Z", closedOn: "2026-08-20",
        closedReason: "superseded",
        closedBecause: "superseded by a self-serve page the recipient uses directly" },
    ],
  });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  assert.ok(!res.stdout.includes("sent-on-known-day"), `a day-precision send is quiet:\n${res.stdout}`);
  assert.ok(!res.stdout.includes("closed-on-known-day"), `a day-precision closure is quiet:\n${res.stdout}`);
  assert.ok(!res.stdout.includes("PROBLEM"), `explicit day precision is valid:\n${res.stdout}`);
});

test("a closure with no reason or no explanation keeps alarming and says what it needs", () => {
  const ws = makeWorkspace();
  writeLedger(ws, "repoD", {
    deliverables: [
      { id: "no-reason", recipient: "Andrew", builtAt: "2026-08-15T15:06:19Z",
        closedAt: "2026-08-20T00:00:00Z" },
      { id: "no-because", recipient: "Andrew", builtAt: "2026-08-15T15:06:19Z",
        closedAt: "2026-08-20T00:00:00Z", closedReason: "withdrawn" },
      { id: "made-up-reason", recipient: "Andrew", builtAt: "2026-08-15T15:06:19Z",
        closedAt: "2026-08-20T00:00:00Z", closedReason: "vibes", closedBecause: "felt done" },
    ],
  });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  for (const id of ["no-reason", "no-because", "made-up-reason"]) {
    has(res.stdout, `PROBLEM: deliverable ${id} in repoD: "closedAt" needs a "closedReason" of superseded/withdrawn/answered-elsewhere and a "closedBecause" saying why`);
    has(res.stdout, `ALARM: UNSENT deliverable: repoD/${id}`);
  }
});

test("an entry cannot be both sent and closed-without-a-send", () => {
  const ws = makeWorkspace();
  writeLedger(ws, "repoE", {
    deliverables: [
      { id: "both", recipient: "Joe", builtAt: "2026-08-15T15:06:19Z",
        sentAt: "2026-08-18T15:00:00Z", closedAt: "2026-08-18T15:00:00Z",
        closedReason: "answered-elsewhere", closedBecause: "delivered in the 1:1" },
    ],
  });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  has(res.stdout, 'PROBLEM: deliverable both in repoE: has both "sentAt" and "closedAt" — a send and a non-send closure cannot both be true');
});

test("only real ISO instants close a delivery obligation", () => {
  const ws = makeWorkspace();
  writeLedger(ws, "repoIso", {
    deliverables: [
      { id: "word-date", recipient: "Joe", builtAt: "2026-08-18T14:00:00Z", sentAt: "yesterday" },
      { id: "impossible-date", recipient: "Joe", builtAt: "2026-08-18T14:00:00Z", sentAt: "2026-02-30T10:00:00Z" },
      { id: "date-in-instant-field", recipient: "Joe", builtAt: "2026-08-18T14:00:00Z", sentAt: "2026-08-20" },
    ],
  });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  has(res.stdout, 'PROBLEM: deliverable word-date in repoIso: "sentAt" must be an ISO-8601 instant');
  has(res.stdout, 'PROBLEM: deliverable impossible-date in repoIso: "sentAt" must be an ISO-8601 instant');
  has(res.stdout, 'PROBLEM: deliverable date-in-instant-field in repoIso: "sentAt" must be an ISO-8601 instant');
  has(res.stdout, "ALARM: UNSENT deliverable: repoIso/word-date");
  has(res.stdout, "ALARM: UNSENT deliverable: repoIso/impossible-date");
  has(res.stdout, "ALARM: UNSENT deliverable: repoIso/date-in-instant-field");
});

test("day precision is exclusive and cannot reverse or future-date the lifecycle", () => {
  const ws = makeWorkspace();
  writeLedger(ws, "repoDayInvalid", {
    deliverables: [
      { id: "two-send-clocks", recipient: "Joe", builtAt: "2026-08-18T14:00:00Z",
        sentAt: "2026-08-20T14:00:00Z", sentOn: "2026-08-20" },
      { id: "early-day", recipient: "Joe", builtAt: "2026-08-18T14:00:00Z",
        sentOn: "2026-08-17" },
      { id: "future-day", recipient: "Joe", builtAt: "2026-08-18T14:00:00Z",
        closedOn: "2099-01-01", closedReason: "withdrawn", closedBecause: "fixture" },
    ],
  });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  has(res.stdout, 'PROBLEM: deliverable two-send-clocks in repoDayInvalid: has both "sentAt" and "sentOn"');
  for (const id of ["two-send-clocks", "early-day", "future-day"]) {
    has(res.stdout, `ALARM: UNSENT deliverable: repoDayInvalid/${id}`);
  }
  has(res.stdout, "PROBLEM: deliverable early-day in repoDayInvalid: ending day must be on or after builtAt and not in the future");
  has(res.stdout, "PROBLEM: deliverable future-day in repoDayInvalid: ending day must be on or after builtAt and not in the future");
});

test("future or pre-build endings cannot silence a delivery obligation", () => {
  const ws = makeWorkspace();
  const builtAt = new Date(Date.now() - 2 * HOUR).toISOString();
  const future = new Date(Date.now() + 24 * HOUR).toISOString();
  const beforeBuild = new Date(Date.now() - 3 * HOUR).toISOString();
  writeLedger(ws, "repoChronology", {
    deliverables: [
      { id: "future-send", recipient: "Joe", builtAt, sentAt: future },
      { id: "early-send", recipient: "Joe", builtAt, sentAt: beforeBuild },
      { id: "future-close", recipient: "Joe", builtAt, closedAt: future,
        closedReason: "withdrawn", closedBecause: "fixture" },
      { id: "early-close", recipient: "Joe", builtAt, closedAt: beforeBuild,
        closedReason: "withdrawn", closedBecause: "fixture" },
    ],
  });

  const res = run(["--check", "--root", ws]);

  assert.equal(res.status, 0);
  for (const id of ["future-send", "early-send", "future-close", "early-close"]) {
    has(res.stdout, `PROBLEM: deliverable ${id} in repoChronology: ending must be on or after builtAt and not in the future`);
    has(res.stdout, `ALARM: UNSENT deliverable: repoChronology/${id}`);
  }
});

test("a warning rides the alarm so a stale artifact is never sent cold", () => {
  const ws = makeWorkspace();
  writeLedger(ws, "repoF", {
    deliverables: [
      { id: "stale-memo", recipient: "Andrew", artifact: "memo.md",
        builtAt: new Date(Date.now() - 30 * HOUR).toISOString(),
        warning: "the carrier paragraph was refuted 8/15 — rewrite before sending" },
    ],
  });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  has(res.stdout, "ALARM: UNSENT deliverable: repoF/stale-memo for Andrew — built 30h ago (memo.md) ⚠ DO NOT SEND COLD: the carrier paragraph was refuted 8/15 — rewrite before sending");
});

test("emit writes unsent deliverables into SUBSTRATE.md's alarm section", () => {
  const ws = makeWorkspace();
  writeManifest(ws, "repoB", { repo: "repoB", primitives: [prim("b-core")] });
  writeLedger(ws, "repoB", {
    deliverables: [{ id: "weekly-report", recipient: "Nick",
      builtAt: new Date(Date.now() - 5 * HOUR).toISOString() }],
  });
  const res = run(["--emit", "--root", ws]);
  assert.equal(res.status, 0);
  const md = fs.readFileSync(path.join(ws, "SUBSTRATE.md"), "utf8");
  has(md, "UNSENT deliverable: repoB/weekly-report for Nick");
});

test("one malformed or duplicate entry cannot silence the rest of the register", () => {
  // A single bad id must report ITS entry and nothing more: indexing the whole
  // ledger through one throw dropped every other obligation, and a register that
  // goes quiet on a typo is the failure this ledger exists to prevent.
  const ws = makeWorkspace();
  writeLedger(ws, "mixed", { deliverables: [
    { id: "real-open", recipient: "Nick", builtAt: "2026-08-15T15:06:19Z" },
    { id: "", recipient: "X", builtAt: "2026-08-15T15:06:19Z" },
    { id: "dup", recipient: "Joe", builtAt: "2026-08-15T15:06:19Z" },
    { id: "dup", recipient: "Joe", builtAt: "2026-08-15T15:06:19Z" },
  ] });
  const res = run(["--check", "--root", ws]);
  assert.equal(res.status, 0);
  has(res.stdout, 'invalid deliverable in mixed: each deliverable needs a non-empty string "id"');
  has(res.stdout, "repeats deliverable id dup");
  // the good obligation still alarms despite the two bad siblings
  has(res.stdout, "UNSENT deliverable: mixed/real-open for Nick");
});
