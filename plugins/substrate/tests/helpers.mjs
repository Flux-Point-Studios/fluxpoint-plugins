// Shared fixture builders for the substrate test suites. Every workspace is a
// fresh temp dir; git repos get committer identity inline so no global config
// is required (same precedent as fluxpoint's hooks-test.sh).
import { execFileSync, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const SCRIPT = fileURLToPath(new URL("../scripts/substrate-graph.mjs", import.meta.url));

// Neutral cwd for spawned runs: cwd is the last-resort root, so it must never
// be a directory that contains stray substrate.json fixtures (like os.tmpdir()).
const DEFAULT_CWD = fs.mkdtempSync(path.join(os.tmpdir(), "substrate-cwd-"));

export function makeWorkspace() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "substrate-ws-"));
}

export function prim(id, extra = {}) {
  return {
    id,
    name: id,
    desc: `${id} primitive`,
    kind: "engine",
    paths: ["src"],
    consumes: [],
    status: "live",
    ...extra,
  };
}

export function writeManifest(root, dir, manifest) {
  const d = path.join(root, dir);
  fs.mkdirSync(d, { recursive: true });
  const mf = path.join(d, "substrate.json");
  fs.writeFileSync(mf, JSON.stringify(manifest, null, 2) + "\n");
  return mf;
}

export function writeConfig(root, config) {
  fs.writeFileSync(path.join(root, "substrate.config.json"), JSON.stringify(config, null, 2) + "\n");
}

export function gitInit(dir) {
  execFileSync("git", ["-C", dir, "init", "-q", "-b", "main"], { stdio: "ignore" });
}

export function gitCommit(dir, message, epochSeconds) {
  const env = { ...process.env };
  if (epochSeconds !== undefined) {
    env.GIT_AUTHOR_DATE = `@${epochSeconds} +0000`;
    env.GIT_COMMITTER_DATE = `@${epochSeconds} +0000`;
  }
  execFileSync("git", ["-C", dir, "add", "-A"], { stdio: "ignore" });
  execFileSync(
    "git",
    ["-C", dir, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message],
    { stdio: "ignore", env },
  );
}

export function makeGitRepo(root, dir, manifest, files = { "code.txt": "code\n" }) {
  const d = path.join(root, dir);
  fs.mkdirSync(d, { recursive: true });
  for (const [rel, content] of Object.entries(files)) {
    const f = path.join(d, rel);
    fs.mkdirSync(path.dirname(f), { recursive: true });
    fs.writeFileSync(f, content);
  }
  gitInit(d);
  const mf = writeManifest(root, dir, manifest);
  gitCommit(d, "base");
  return mf;
}

/** The plugin directory reached through a symlink (a junction on Windows), as Claude Code reaches it when
 *  CLAUDE_PLUGIN_ROOT runs through one; the link is removed when test `t` ends. */
export function linkedPluginRoot(t) {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "substrate-link-"));
  const root = path.join(parent, "substrate");
  fs.symlinkSync(path.dirname(path.dirname(SCRIPT)), root, "junction");
  t.after(() => {
    fs.unlinkSync(root);
    fs.rmdirSync(parent);
  });
  return root;
}

export function setMtime(file, ms) {
  fs.utimesSync(file, new Date(ms), new Date(ms));
}

// opts.env merges over the inherited environment; opts.envReplace replaces it
// wholesale (used to simulate a PATH without git). CLAUDE_PROJECT_DIR is always
// stripped first so a run inside Claude Code cannot hijack root resolution.
export function run(args, opts = {}) {
  const env = opts.envReplace ? { ...opts.envReplace } : { ...process.env };
  delete env.CLAUDE_PROJECT_DIR;
  Object.assign(env, opts.env || {});
  return spawnSync(process.execPath, [SCRIPT, ...args], {
    encoding: "utf8",
    cwd: opts.cwd || DEFAULT_CWD,
    env,
  });
}
