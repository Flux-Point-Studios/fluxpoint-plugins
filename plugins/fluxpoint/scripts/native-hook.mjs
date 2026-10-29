// Claude exec-form entry point. Native children never inherit Claude's pipes.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const MAX_INPUT = 4 * 1024 * 1024, MAX_OUTPUT = 1024 * 1024;
export function supportedClaude(version) {
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(version);
  return !!match && (Number(match[1]) > 2 || Number(match[1]) === 2 && (Number(match[2]) > 1 || Number(match[2]) === 1 && Number(match[3]) >= 312));
}
function executable(name) {
  const names = process.platform === 'win32' && !path.extname(name) ? [name + '.exe', name] : [name];
  for (const directory of path.isAbsolute(name) || /[/\\]/.test(name) ? [''] : (process.env.PATH || '').split(path.delimiter)) {
    for (const value of names) {
      const candidate = path.resolve(directory, value);
      try {
        if (candidate.toLowerCase().includes('windowsapps')) continue;
        fs.accessSync(candidate, fs.constants.X_OK);
        if (fs.statSync(candidate).isFile() && !/\.(?:cmd|bat|sh)$/i.test(candidate)) return fs.realpathSync(candidate);
      } catch {}
    }
  }
  throw new Error('native executable unavailable: ' + name);
}
function stamp(filename) {
  const value = fs.statSync(filename);
  return [filename, value.size, value.mtimeMs, value.ino];
}
function cacheDirectory() {
  const directory = path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), '.cache'), 'fluxpoint', 'native-hooks');
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const stat = fs.lstatSync(directory);
  if (stat.isSymbolicLink() || !stat.isDirectory() || process.platform !== 'win32' && (stat.uid !== process.getuid() || stat.mode & 0o077)) throw new Error('hook cache directory must be private');
  return directory;
}
function cached(key, get) {
  const filename = path.join(cacheDirectory(), crypto.createHash('sha256').update(JSON.stringify(key)).digest('hex') + '.json');
  try {
    const stat = fs.lstatSync(filename);
    if (stat.isFile() && !stat.isSymbolicLink() && stat.size < 8192 && (process.platform === 'win32' || stat.uid === process.getuid() && !(stat.mode & 0o077))) {
      const value = JSON.parse(fs.readFileSync(filename, 'utf8'));
      if (JSON.stringify(value.stamp) === JSON.stringify(stamp(value.file))) return Promise.resolve(value.file);
    }
  } catch {}
  return get().then(file => {
    const temporary = filename + '.' + crypto.randomBytes(8).toString('hex');
    try {
      fs.writeFileSync(temporary, JSON.stringify({ file, stamp: stamp(file) }), { mode: 0o600, flag: 'wx' });
      fs.renameSync(temporary, filename);
    } finally { try { fs.unlinkSync(temporary); } catch {} }
    return file;
  });
}
// After EXIT allow only a bounded drain: an orphan can keep private pipes open.
// The wrapper alone owns the runtime-facing pipes; only bounded bytes cross it.
export function run(file, args, input, milliseconds, env = process.env) {
  return new Promise((resolve, reject) => {
    let stdout = Buffer.alloc(0), stderr = Buffer.alloc(0), settled = false, exited = false, exitCode, drain;
    const child = spawn(file, args, { shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], env });
    const finish = (error, code) => {
      if (settled) return; settled = true; clearTimeout(timer); clearTimeout(drain);
      child.stdin.destroy(); child.stdout.destroy(); child.stderr.destroy();
      if (child.exitCode === null) child.kill();
      child.unref();
      error ? reject(error) : resolve({ code, stdout, stderr });
    };
    const timer = setTimeout(() => finish(new Error('native hook execution deadline exceeded')), milliseconds);
    child.on('error', error => finish(error));
    child.stdin.on('error', () => {});
    child.stdout.on('data', bytes => { stdout = Buffer.concat([stdout, bytes]); if (stdout.length > MAX_OUTPUT) finish(new Error('native hook output exceeds its limit')); });
    child.stderr.on('data', bytes => { stderr = Buffer.concat([stderr, bytes]); if (stderr.length > MAX_OUTPUT) finish(new Error('native hook stderr exceeds its limit')); });
    const drained = () => {
      if (exited && child.stdout.readableEnded && child.stderr.readableEnded) finish(null, exitCode);
    };
    child.stdout.on('end', drained); child.stderr.on('end', drained);
    child.on('exit', code => {
      exited = true; exitCode = code; drained();
      if (!settled) drain = setTimeout(() => finish(new Error('native hook stdio did not finish after process exit')), 250);
    });
    child.stdin.end(input);
  });
}
async function interpreter() {
  if (process.env.FPL_PY) {
    try { return executable(process.env.FPL_PY); } catch { throw new Error('configured Python interpreter is unavailable'); }
  }
  return cached(['python', process.env.PATH, process.platform], async () => {
    for (const name of ['python3', 'python']) {
      try {
        const file = executable(name);
        const observed = await run(file, ['-c', 'import sys; print(sys.executable)'], '', 2500, { ...process.env, PYTHONIOENCODING: 'utf-8' });
        if (observed.code === 0) return executable(observed.stdout.toString('utf8').trim());
      } catch {}
    }
    throw new Error('no working native Python interpreter');
  });
}
async function runtime() {
  if (Number(process.versions.node.split('.')[0]) < 18) throw new Error('native hooks require Node.js 18 or newer');
  let file;
  try { file = executable(process.env.FPL_CLAUDE_BINARY || 'claude'); }
  catch { throw new Error('native hooks require Claude Code 2.1.312+; install native claude or set FPL_CLAUDE_BINARY to claude.exe'); }
  await cached(['claude', stamp(file)], async () => {
    const result = await run(file, ['--version'], '', 4000);
    if (result.code !== 0 || !supportedClaude(result.stdout.toString('utf8').trim().split(/\s/)[0])) throw new Error('native hooks require tested Claude Code 2.1.312 or newer');
    return file;
  });
}
function input() {
  return new Promise((resolve, reject) => {
    let chunks = [], length = 0;
    const timer = setTimeout(() => { process.stdin.destroy(); reject(new Error('native hook stdin deadline exceeded')); }, 3000);
    process.stdin.on('data', chunk => {
      length += chunk.length;
      if (length > MAX_INPUT) { clearTimeout(timer); process.stdin.destroy(); reject(new Error('native hook input exceeds 4 MiB')); }
      else chunks.push(chunk);
    });
    process.stdin.on('error', error => { clearTimeout(timer); reject(error); });
    process.stdin.on('end', () => { clearTimeout(timer); resolve(Buffer.concat(chunks)); });
  });
}
function warning(raw, error) {
  let payload = {};
  try { const parsed = JSON.parse(raw); if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) payload = parsed; } catch {}
  // Launcher-level failures have no Python jurisdiction. Key by cwd/session in
  // a private user cache; normal attest warnings use the repo state directory.
  const key = crypto.createHash('sha256').update(JSON.stringify([payload.cwd || process.cwd(), payload.session_id || 'nosession'])).digest('hex');
  try {
    const marker = path.join(cacheDirectory(), key + '.attest-warned');
    fs.closeSync(fs.openSync(marker, 'wx', 0o600));
    console.log(JSON.stringify({ systemMessage: 'fluxpoint attest: DISARMED — ' + error.message }));
  } catch (failure) { if (failure.code !== 'EEXIST') console.error('fluxpoint attest: DISARMED — ' + error.message); }
}
async function main() {
  const mode = process.argv[2];
  if (!['guard', 'attest'].includes(mode)) throw new Error('usage: native-hook.mjs guard|attest');
  if (process.env.FPL_DISABLE === '1') return;
  let raw = Buffer.alloc(0);
  const absoluteDeadline = setTimeout(() => {
    if (mode === 'guard') console.error('fluxpoint secret guard: native hook total deadline exceeded; command refused');
    else warning(raw, new Error('native hook total deadline exceeded'));
    process.exit(mode === 'guard' ? 2 : 0);
  }, mode === 'guard' ? 50000 : 15000);
  try {
    raw = await input(); await runtime(); const python = await interpreter();
    const target = mode === 'guard' ? ['secret-guard.py', '--hook'] : ['native-hook.py'];
    const result = await run(python, [path.join(here, target[0]), ...target.slice(1)], raw, mode === 'guard' ? 40000 : 8000, { ...process.env, PYTHONIOENCODING: 'utf-8' });
    if (mode === 'guard') {
      if (result.code !== 0 && result.code !== 2) throw new Error('secret interpreter failed (exit ' + result.code + ')');
      process.stdout.write(result.stdout); process.stderr.write(result.stderr); process.exitCode = result.code;
    } else {
      if (result.code !== 0) throw new Error(result.stderr.toString('utf8').trim() || 'attestation interpreter failed');
      process.stdout.write(result.stdout);
    }
  } catch (error) {
    if (mode === 'guard') { console.error('fluxpoint secret guard: ' + error.message + '; command refused'); process.exitCode = 2; }
    else warning(raw, error);
  } finally { clearTimeout(absoluteDeadline); process.stdin.destroy(); }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(error.message); process.exitCode = 2; });
}
