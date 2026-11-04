// The prose-smell gate: deterministic detection of the AI-writing frames that
// actually mark generated text (contrastive negation, whether-you're triples,
// assistant artifacts, stock LLM vocabulary) while leaving normal writing —
// metaphors, qualifiers, a reasonable em dash — alone. The harness decides
// whether a draft smells, never the author's self-report.
import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { linkedPluginRoot } from "./helpers.mjs";

const SCRIPT = fileURLToPath(new URL("../scripts/prose-smell.mjs", import.meta.url));

function runOnText(text, args = []) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "smell-"));
  const file = path.join(dir, "draft.md");
  fs.writeFileSync(file, text);
  const r = spawnSync("node", [SCRIPT, ...args, file], { encoding: "utf8" });
  return { ...r, file, dir };
}

function runHook(toolInput, toolName = "Write", script = SCRIPT) {
  const payload = JSON.stringify({ tool_name: toolName, tool_input: toolInput });
  return spawnSync("node", [script, "--hook"], { input: payload, encoding: "utf8" });
}

// ---------------------------------------------------------------- high rules
test("contrastive negation frames are high findings", () => {
  for (const line of [
    "Reconciliation isn't just about matching numbers — it's about trust.",
    "This work is not just about speed; it's about honesty.",
    "The pilot is more than just a proof of concept.",
    "Success is not merely about output, it's about outcomes.",
  ]) {
    const r = runOnText(line);
    assert.equal(r.status, 2, `expected high finding for: ${line}\n${r.stdout}${r.stderr}`);
    assert.match(r.stderr, /negation-contrast|more-than-just/);
  }
});

test("whether-you're inclusive triples are high findings", () => {
  const r = runOnText(
    "Whether you're an agent, a TM, or a state director, the wizard adapts.");
  assert.equal(r.status, 2);
  assert.match(r.stderr, /whether-your/);
});

test("assistant artifacts are high findings", () => {
  for (const line of [
    "Would you like me to add a summary table?",
    "I hope this helps! Reach out anytime.",
    "Let me know if you'd like a deeper breakdown.",
  ]) {
    const r = runOnText(line);
    assert.equal(r.status, 2, `expected artifact hit for: ${line}`);
    assert.match(r.stderr, /assistant-artifact/);
  }
});

test("stock LLM vocabulary is a high finding", () => {
  for (const line of [
    "Let's delve into the numbers.",
    "A rich tapestry of carrier relationships.",
    "In today's fast-paced insurance landscape, speed wins.",
    "This is a testament to the team's work.",
    "The broker plays a pivotal role in the flow.",
  ]) {
    const r = runOnText(line);
    assert.equal(r.status, 2, `expected stock-vocab hit for: ${line}`);
  }
});

// ------------------------------------------- the frame family beyond one spelling
// Issue #63: a 1,300-word document carried nine contrastive-negation frames
// and the gate returned exit 0, because the rule matched exactly one
// spelling. These are the sentences it passed, verbatim.
const NINE = [
  "Settlement is a computation, not a decision: the trigger reads three feeds.",
  "The product is not the gap.",
  "Reserves are paid by premiums rather than emissions, and the pool is funded first.",
  "Capital sits in the pool instead of sitting idle.",
  "Claims are paid as a waterfall, never into the asset being insured.",
  "Anyone can submit; there is no adjuster and no vote.",
  "Aegis itself has no token and no TGE planned; the treasury holds stablecoins only.",
  "Every figure is on chain, and we would rather you check the chain than take our word.",
];

test("the nine live sentences from #63 no longer pass as a document", () => {
  const r = runOnText(NINE.join("\n\n"));
  assert.equal(r.status, 2, `expected exit 2\n${r.stdout}${r.stderr}`);
  for (const rule of ["contrast-comma", "contrast-slogan", "rather-than", "instead-of",
                      "contrast-never", "no-and-no", "would-rather-than"]) {
    assert.match(r.stderr, new RegExp(rule), `expected a HIGH ${rule} finding`);
  }
});

test("each slogan form is HIGH on a single hit", () => {
  for (const [line, rule] of [
    ["Settlement is a computation, not a decision: the trigger reads three feeds.", "contrast-comma"],
    ["The gate reads the tree. The product is not the gap.", "contrast-slogan"],
    ["Fees are paid to the pool, not to the insured.", "contrast-comma"],
  ]) {
    const r = runOnText(line);
    assert.equal(r.status, 2, `expected exit 2 for: ${line}\n${r.stdout}${r.stderr}`);
    assert.match(r.stderr, new RegExp(rule));
  }
});

test("a factual qualifier after a comma is not a contrast", () => {
  // The last sentence from the issue, kept in the set on purpose: a naive
  // ", not X" rule fires on it and a gate that cries wolf gets skimmed.
  for (const line of [
    "Chris Borders, counsel, formerly of a large firm, not full-time.",
    "The audit is scheduled, not yet started.",
    "Retries are billed per attempt, not applicable to the free tier.",
    "The row was dropped, not because it failed, and the log says so.",
  ]) {
    const r = runOnText(line);
    assert.equal(r.status, 0, `expected exit 0 for: ${line}\n${r.stderr}`);
    assert.doesNotMatch(r.stderr, /contrast-comma/);
  }
});

test("a comparand that keeps going is a sentence, not a slogan", () => {
  // The comma form needs the comparand to close its clause within a few
  // words; a clause that carries on is ordinary prose.
  const r = runOnText(
    "The figure is net, not the gross number the vendor quoted in the original estimate " +
    "before the rebate was applied to the second invoice.");
  assert.equal(r.status, 0, r.stderr);
});

test("connective contrasts fail by density, with the denominator floored", () => {
  // One "rather than" in a short note is English.
  const one = runOnText("Reserves are paid by premiums rather than emissions.");
  assert.equal(one.status, 0, one.stderr);
  assert.equal(one.stdout.trim(), "");
  // Two in under 500 words is advice (density 2 per 500 at the floor).
  const two = runOnText(
    "Reserves are paid by premiums rather than emissions.\n\n" +
    "Capital sits in the pool instead of sitting idle.");
  assert.equal(two.status, 0, two.stderr);
  assert.match(two.stdout, /\[medium\] (?:rather-than|instead-of)/);
  // Four in under 500 words fails; and the count is named on every line.
  const four = runOnText(
    "Reserves are paid by premiums rather than emissions.\n\n" +
    "Capital sits in the pool instead of sitting idle.\n\n" +
    "Claims are paid as a waterfall, never into the asset.\n\n" +
    "There is no adjuster and no vote.");
  assert.equal(four.status, 2, four.stdout);
  assert.match(four.stderr, /4 connective contrasts/);
  // The same four spread across 1,200 words is a density under 2 and clean.
  const filler = Array(300).fill("plain words that carry no frame here").join(" ");
  const diluted = runOnText(
    "Reserves are paid by premiums rather than emissions. " + filler + "\n\n" +
    "Capital sits in the pool instead of sitting idle. " + filler + "\n\n" +
    "Claims are paid as a waterfall, never into the asset. " + filler + "\n\n" +
    "There is no adjuster and no vote. " + filler);
  assert.equal(diluted.status, 0, diluted.stderr);
  assert.doesNotMatch(diluted.stdout, /rather-than/);
});

test("inline code spans are never scanned, so a report can quote the tell", () => {
  const r = runOnText(
    "The rule misses `Settlement is a computation, not a decision.` and " +
    "`The product is not the gap.` alike; both should fail.");
  assert.equal(r.status, 0, r.stderr);
  assert.doesNotMatch(r.stdout + r.stderr, /contrast-/);
  // And line numbers survive the blanking: the span is replaced in place.
  const r2 = runOnText("first line has `code`\n\nThe product is not the gap.");
  assert.equal(r2.status, 2);
  assert.match(r2.stderr, /draft\.md:3 \[HIGH\] contrast-slogan/);
});

test("the aphoristic restatement after a long sentence is advice", () => {
  const long = "The settlement engine reads three independent price feeds, waits for " +
    "two of them to agree within the configured tolerance window, and only then " +
    "releases the payout to the policyholder's address on chain.";
  const r = runOnText(`${long} Settlement is a computation.`);
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /aphoristic-restatement/);
  // A short sentence with no copula, or with no long sentence before it, is rhythm.
  const r2 = runOnText(`${long} Then it stops.`);
  assert.doesNotMatch(r2.stdout, /aphoristic-restatement/);
  const r3 = runOnText("Fees are low. Settlement is a computation.");
  assert.doesNotMatch(r3.stdout, /aphoristic-restatement/);
});

// ---------------------------------------------------------- in-house config
test("a .prose-smell.json above the file downgrades the family to advice", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "smell-cfg-"));
  fs.writeFileSync(path.join(dir, ".prose-smell.json"), JSON.stringify({ contrastive: "advise" }));
  const nested = path.join(dir, "docs", "deep");
  fs.mkdirSync(nested, { recursive: true });
  const file = path.join(nested, "README.md");
  fs.writeFileSync(file, NINE.join("\n\n"));
  const r = spawnSync("node", [SCRIPT, file], { encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /\[medium\] contrast-comma .*in-house: advised/);
  assert.match(r.stdout, /\[medium\] contrast-slogan/);
  assert.equal(r.stderr.trim(), "");
  // The canonical rules keep their severity under the same config.
  fs.writeFileSync(file, "This isn't just about OCR — it's about capacity.");
  const r2 = spawnSync("node", [SCRIPT, file], { encoding: "utf8" });
  assert.equal(r2.status, 2);
  assert.match(r2.stderr, /negation-contrast/);
  // And the hook path honors it too.
  fs.writeFileSync(file, NINE.join("\n\n"));
  assert.equal(runHook({ file_path: file }).status, 0);
});

test("a malformed or unknown config is reported and ignored, never obeyed", () => {
  for (const body of ["{not json", JSON.stringify({ contrastive: "off" }),
                      JSON.stringify({ contrasitve: "advise" }), "[]"]) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "smell-badcfg-"));
    fs.writeFileSync(path.join(dir, ".prose-smell.json"), body);
    const file = path.join(dir, "draft.md");
    fs.writeFileSync(file, "The product is not the gap.");
    const r = spawnSync("node", [SCRIPT, file], { encoding: "utf8" });
    assert.equal(r.status, 2, `config ${body} must not switch the gate off`);
    assert.match(r.stderr, /prose-smell: .*\.prose-smell\.json/);
    assert.doesNotMatch(r.stderr, /at .*\(node:/);
  }
});

test("the shipped WRITING snippet passes its own gate", () => {
  // The snippet quotes every banned frame as an example. Quoted in code
  // spans, it is a list of tells rather than a document carrying them —
  // the same rule this repo's harness applies to itself.
  const snippet = fileURLToPath(new URL("../templates/WRITING.snippet.md", import.meta.url));
  const r = spawnSync("node", [SCRIPT, snippet], { encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.stderr.trim(), "");
});

test("a bold-lead bullet slogan is still the slogan", () => {
  const r = runOnText("- **The ledger is not a receipt.** It records what fired.");
  assert.equal(r.status, 2, r.stdout);
  assert.match(r.stderr, /contrast-slogan/);
});

test("the repo's own docs are treated as in-house prose", () => {
  // This repository writes dense engineering prose and carries the config;
  // its README must scan clean on exit code, with the frames reported as
  // advice rather than blocking every doc edit through the hook.
  const readme = fileURLToPath(new URL("../../../README.md", import.meta.url));
  const r = spawnSync("node", [SCRIPT, readme], { encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
});

// -------------------------------------------------------------- medium rules
test("sentence-initial from-to coverage parallelism is medium, not failing", () => {
  const r = runOnText(
    "From daily budgeting apps to sophisticated portfolios, money management is easier.");
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /from-to-coverage/);
});

test("em-dash density is reported as medium only when genuinely heavy", () => {
  const heavy = Array(6).fill(
    "The run — measured yesterday — proved it — twice — cleanly.").join("\n");
  const r = runOnText(heavy);
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /em-dash-density/);
  const light = "The run — measured yesterday — proved it across two hundred and " +
    "forty statements yesterday, and the cache made the second pass free. " +
    "Nothing in the report needed a caveat beyond the snapshot date itself.";
  const r2 = runOnText(light);
  assert.doesNotMatch(r2.stdout, /em-dash-density/);
});

// ------------------------------------------------------- normal writing safe
test("normal writing with metaphors, qualifiers and a dash passes clean", () => {
  const r = runOnText(
    "The queue drained like a bathtub with the plug half out — slowly, then all " +
    "at once. It was remarkably difficult to verify the older scans, and the " +
    "results were slightly confusing until we checked the snapshot date. " +
    "Whether the carrier resends the file is their call, and we will wait.");
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.stdout.trim(), "");
});

test("code fences are never scanned", () => {
  const r = runOnText(
    "Here is the command:\n\n```js\n// isn't just about X — it's about Y\n" +
    "const s = \"Would you like me to\";\n```\n\nRun it once.");
  assert.equal(r.status, 0, r.stderr);
});

// ------------------------------------------------------------------ hook mode
test("hook mode scans prose writes and feeds findings back on exit 2", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "smell-hook-"));
  const file = path.join(dir, "reply-draft.md");
  fs.writeFileSync(file, "This isn't just about OCR — it's about capacity.");
  const r = runHook({ file_path: file });
  assert.equal(r.status, 2);
  assert.match(r.stderr, /negation-contrast/);
});

test("hook mode reached through a symlinked plugin root still feeds findings back", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "smell-hook-"));
  const file = path.join(dir, "reply-draft.md");
  fs.writeFileSync(file, "This isn't just about OCR — it's about capacity.");
  const r = runHook({ file_path: file }, "Write", path.join(linkedPluginRoot(t), "scripts", "prose-smell.mjs"));
  assert.equal(r.status, 2, r.stderr);
  assert.match(r.stderr, /negation-contrast/);
});

test("hook mode ignores non-prose files and generated registries", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "smell-hook-"));
  const code = path.join(dir, "engine.mjs");
  fs.writeFileSync(code, "// isn't just about X — it's about Y");
  assert.equal(runHook({ file_path: code }).status, 0);
  const reg = path.join(dir, "SUBSTRATE.md");
  fs.writeFileSync(reg, "This isn't just about nodes — it's about edges.");
  assert.equal(runHook({ file_path: reg }).status, 0);
  assert.equal(runHook({}).status, 0);              // no file_path at all
  assert.equal(runHook({ file_path: path.join(dir, "gone.md") }).status, 0);
});

test("match-dense files stay inside the hook budget (the quadratic is dead)", () => {
  // Review bait: ~40k matches once took 20s via per-match slice-and-split
  // line counting. With O(n) offsets + binary search this finishes in well
  // under the 15s hook budget; the bound here is generous only for CI noise.
  const dense = Array(40_000).fill("This is more than just a line.").join("\n");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "smell-perf-"));
  const file = path.join(dir, "dense.md");
  fs.writeFileSync(file, dense);
  const t0 = Date.now();
  const r = spawnSync("node", [SCRIPT, file], { encoding: "utf8", timeout: 14_000 });
  const elapsed = Date.now() - t0;
  assert.equal(r.status, 2, `expected HIGH findings, got ${r.status}: ${r.stderr.slice(0, 200)}`);
  assert.ok(elapsed < 10_000, `scan took ${elapsed}ms — the quadratic is back`);
});

test("finding floods are capped, not injected wholesale", () => {
  const flood = Array(500).fill("This is more than just noise.").join("\n");
  const r = runOnText(flood);
  assert.equal(r.status, 2);
  const lines = r.stderr.trim().split("\n");
  assert.ok(lines.length < 40, `expected capped output, got ${lines.length} lines`);
  assert.match(r.stderr, /more HIGH suppressed/);
});

test("a non-string file_path in hook JSON exits 0 silently", () => {
  for (const fp of [123, { a: 1 }, ["a.md"], null, true]) {
    const r = runHook({ file_path: fp });
    assert.equal(r.status, 0, `file_path=${JSON.stringify(fp)} must exit 0`);
    assert.equal(r.stderr.trim(), "", `no stack trace for ${JSON.stringify(fp)}`);
  }
});

test("the CLI path reports bad arguments in one line, never a stack trace", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "smell-cli-"));
  const missing = path.join(dir, "missing.md");
  const r = spawnSync("node", [SCRIPT, missing, dir], { encoding: "utf8" });
  assert.equal(r.status, 0);
  assert.match(r.stderr, /not a readable file/);
  assert.doesNotMatch(r.stderr, /at .*\(node:/);
});

test("oversized files are skipped, not scanned", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "smell-big-"));
  const file = path.join(dir, "huge.md");
  fs.writeFileSync(file,
    "This isn't just about size — it's about budget.\n".padEnd(2_100_000, "x"));
  const r = runHook({ file_path: file });
  assert.equal(r.status, 0);
  assert.equal(r.stderr.trim(), "");
});

test("clean prose through the hook is silent", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "smell-hook-"));
  const file = path.join(dir, "note.md");
  fs.writeFileSync(file, "The deploy finished at 4pm and the gate held.");
  const r = runHook({ file_path: file });
  assert.equal(r.status, 0);
  assert.equal(r.stderr.trim(), "");
});

// ------------------------------------------------ hook mode, Codex payloads
// Codex fires the Write|Edit matcher for apply_patch and sends the patch text
// in tool_input.command with no file_path; the files come from its headers.
function runPatchHook(patch, cwd) {
  const payload = JSON.stringify({ tool_name: "apply_patch", cwd, tool_input: { command: patch } });
  return spawnSync("node", [SCRIPT, "--hook"], { input: payload, encoding: "utf8" });
}

test("hook mode reads the files out of a Codex apply_patch payload", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "smell-hook-"));
  const file = path.join(dir, "note.md");
  fs.writeFileSync(file, "Would you like me to add a summary table?\n");
  const r = runPatchHook(`*** Begin Patch\n*** Update File: ${file}\n@@\n-a\n+b\n*** End Patch\n`, dir);
  assert.equal(r.status, 2, r.stdout + r.stderr);
  assert.match(r.stderr, /assistant-artifact/);
});

test("hook mode re-bases a relative apply_patch path from the payload's cwd", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "smell-hook-"));
  fs.writeFileSync(path.join(dir, "note.md"), "I hope this helps! Reach out anytime.\n");
  const r = runPatchHook("*** Begin Patch\n*** Add File: note.md\n+x\n*** End Patch\n", dir);
  assert.equal(r.status, 2, r.stdout + r.stderr);
});

test("hook mode ignores a Codex patch that only deletes prose or touches code", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "smell-hook-"));
  fs.writeFileSync(path.join(dir, "note.md"), "I hope this helps! Reach out anytime.\n");
  fs.writeFileSync(path.join(dir, "x.mjs"), "// I hope this helps\n");
  const r = runPatchHook(
    `*** Begin Patch\n*** Delete File: ${path.join(dir, "note.md")}\n*** Update File: ${path.join(dir, "x.mjs")}\n@@\n-a\n+b\n*** End Patch\n`,
    dir);
  assert.equal(r.status, 0, r.stdout + r.stderr);
});

// ------------------------------------------------------------ mail verbosity
// An ~800-word message whose every claim was accurate, and which passed every
// frame rule above, still came back from its reader asking for less; the
// ~130-word rewrite is what got acted on. Length is the defect the frame rules
// cannot see, and it only applies to prose that leaves as MAIL — a memo or a
// plan is legitimately long, so the trigger is structural: a file whose first
// non-blank line is a Subject: header is an email body.
function mail(words, subject = "Subject: RE: the quarterly figures") {
  const body = Array.from({ length: words }, (_, i) => `word${i}`).join(" ");
  return `${subject}\n\nHello,\n\n${body}\n\nRegards\n`;
}

test("an over-long mail body is a high finding", () => {
  const r = runOnText(mail(400), []);
  assert.equal(r.status, 2, `expected a high finding\n${r.stdout}${r.stderr}`);
  assert.match(r.stderr, /mail-too-long/);
});

test("a mail body at working length passes clean", () => {
  const r = runOnText(mail(120));
  assert.equal(r.status, 0, r.stderr);
  assert.doesNotMatch(r.stdout + r.stderr, /mail-too-long|mail-getting-long/);
});

test("a mail body approaching the ceiling is advice, not a failure", () => {
  const r = runOnText(mail(300));
  assert.equal(r.status, 0, `medium findings must not fail the gate\n${r.stderr}`);
  assert.match(r.stdout, /mail-getting-long/);
});

test("a long document that is NOT mail is left alone", () => {
  // The same 400 words with no Subject: header is a memo, and memos are
  // allowed to be long. Firing here would make the gate something to disable.
  const body = Array.from({ length: 400 }, (_, i) => `word${i}`).join(" ");
  const r = runOnText(`# Findings from the review\n\n${body}\n`);
  assert.equal(r.status, 0, r.stderr);
  assert.doesNotMatch(r.stdout + r.stderr, /mail-too-long|mail-getting-long/);
});

test("the Subject line does not count toward the body length", () => {
  // A long subject must not push a short mail over the ceiling, or the rule
  // would punish the one line that is supposed to be descriptive.
  const longSubject = "Subject: " + Array.from({ length: 80 }, (_, i) => `s${i}`).join(" ");
  const r = runOnText(mail(120, longSubject));
  assert.equal(r.status, 0, r.stderr);
});

test("mail length is measured on the author's own words, not a quoted chain", () => {
  // Replying in thread means the draft can carry the whole history below the
  // sign-off. Counting it would fail every reply on a long thread.
  const body = Array.from({ length: 120 }, (_, i) => `word${i}`).join(" ");
  const quoted = Array.from({ length: 600 }, (_, i) => `old${i}`).join(" ");
  const r = runOnText(
    `Subject: RE: something\n\nHello,\n\n${body}\n\nRegards\n\n`
    + `From: A Colleague\nSent: Thursday\nTo: team@example.com\n\n${quoted}\n`);
  assert.equal(r.status, 0, `a quoted chain must not fail the gate\n${r.stderr}`);
});
