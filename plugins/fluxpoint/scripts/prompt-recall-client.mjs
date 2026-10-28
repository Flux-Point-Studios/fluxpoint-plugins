// Warm UserPromptSubmit client. All failures are bounded and advisory.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import net from 'node:net';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
// Git Bash's shell dirname logic expects forward slashes in its script argv.
const launcher = path.join(here, 'py.sh').replaceAll('\\', '/');
const bash = process.argv[2] || process.env.SHELL || 'bash';
const project = process.env.CLAUDE_PROJECT_DIR ? path.resolve(process.env.CLAUDE_PROJECT_DIR) : '';
const cwd = process.cwd();
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const digest = text => crypto.createHash('sha256').update(text).digest('hex');
const authenticate = (token, direction, nonce, value) => crypto.createHmac('sha256', Buffer.from(token, 'hex'))
  .update(direction + '\n' + nonce + '\n' + value).digest('hex');

function privateDirectory(directory) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const stat = fs.lstatSync(directory);
  if (stat.isSymbolicLink() || !stat.isDirectory()
      || (process.platform !== 'win32' && (stat.uid !== process.getuid() || (stat.mode & 0o077)))) {
    throw new Error('query cache directory must be private');
  }
  return directory;
}

function endpointAt(filename) {
  try {
    const stat = fs.lstatSync(filename);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 4096
        || (process.platform !== 'win32' && (stat.uid !== process.getuid() || (stat.mode & 0o077)))) return null;
    const endpoint = JSON.parse(fs.readFileSync(filename, 'utf8'));
    return endpoint.version === 1 && Number.isInteger(endpoint.port) && endpoint.port > 0 && endpoint.port < 65536
      && /^[0-9a-f]{64}$/.test(endpoint.token) ? endpoint : null;
  } catch { return null; }
}

function query(endpoint, payload) {
  return new Promise(resolve => {
    const nonce = crypto.randomBytes(16).toString('hex');
    let received = Buffer.alloc(0), done = false;
    const socket = net.createConnection({ host: '127.0.0.1', port: endpoint.port });
    const timer = setTimeout(() => finish(null), 1800);
    const finish = result => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      socket.destroy();
      resolve(result);
    };
    socket.on('error', () => finish(null));
    socket.on('connect', () => socket.end(JSON.stringify({ nonce, payload,
      mac: authenticate(endpoint.token, 'request', nonce, payload) }) + '\n'));
    socket.on('data', data => {
      received = Buffer.concat([received, data]);
      if (received.length > 16384) return finish(null);
      if (!received.includes(10)) return;
      try {
        const response = JSON.parse(received.toString('utf8'));
        if (typeof response.output !== 'string' || Buffer.byteLength(response.output) > 4096
            || typeof response.mac !== 'string' || !/^[0-9a-f]{64}$/.test(response.mac)) return finish(null);
        const expected = authenticate(endpoint.token, 'response', nonce, response.output);
        finish(crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(response.mac)) ? response.output : null);
      } catch { finish(null); }
    });
    socket.on('end', () => finish(null));
  });
}

function cold(payload) {
  return new Promise(resolve => {
    let output = '', done = false;
    const child = spawn(bash, [launcher, 'prompt-recall-daemon.py', '--once',
      '--project', project, '--cwd', cwd], { stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true });
    const timer = setTimeout(() => finish(''), 3000);
    const finish = value => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      child.stdin?.destroy();
      child.stdout?.destroy();
      if (child.exitCode === null) child.kill();
      resolve(value);
    };
    child.on('error', () => finish(''));
    child.stdin.on('error', () => {});
    child.stdout.on('data', data => {
      output += data.toString('utf8');
      if (Buffer.byteLength(output) > 4096) finish('');
    });
    child.on('close', code => finish(code === 0 ? output : ''));
    child.stdin.end(payload);
  });
}

async function warm(payload) {
  const directory = privateDirectory(process.env.FPL_RECALL_DAEMON_DIR
    || path.join(os.homedir(), '.cache', 'fluxpoint', 'prompt-recall'));
  // Configuration affects corpus scope; interpreter selection does not. The
  // second warm request can succeed even if Python is no longer on PATH.
  const ignored = new Set(['FPL_PY', 'FPL_MEM_PROMPT', 'FPL_DISABLE', 'FPL_RECALL_DAEMON_DIR', 'FPL_RECALL_DAEMON_IDLE']);
  const context = Object.fromEntries(Object.entries(process.env).filter(([name]) =>
    !ignored.has(name) && /^(?:FPL_|GIT_|CODEX_|CLAUDE_)/.test(name)).sort());
  const files = fs.readdirSync(here).filter(name => name.endsWith('.py')).sort().map(name => {
    const stat = fs.statSync(path.join(here, name), { bigint: true });
    return [name, String(stat.size), String(stat.mtimeNs)];
  });
  const key = digest(JSON.stringify([here, project || cwd, context, files]));
  const filename = path.join(directory, key + '.json');
  const existing = endpointAt(filename);
  if (existing) {
    const result = await query(existing, payload);
    if (result !== null) return result;
  }
  const lock = path.join(directory, key + '.starting');
  const owner = crypto.randomBytes(16).toString('hex');
  let acquired = false;
  try {
    try {
      const stat = fs.lstatSync(lock);
      if (!stat.isSymbolicLink() && Date.now() - stat.mtimeMs > 15000) fs.unlinkSync(lock);
    } catch {}
    try { fs.writeFileSync(lock, owner, { mode: 0o600, flag: 'wx' }); acquired = true; } catch {}
    if (acquired) {
      const idle = Math.max(1, Math.min(3600, Number(process.env.FPL_RECALL_DAEMON_IDLE) || 300));
      const child = spawn(bash, [launcher, 'prompt-recall-daemon.py',
        '--state-dir', directory, '--key', key, '--project', project, '--cwd', cwd,
        '--idle-seconds', String(idle)], { detached: true, stdio: 'ignore', windowsHide: true });
      child.on('error', () => {});
      child.unref();
    }
    for (let i = 0; i < 40; i++) {
      await sleep(25);
      const endpoint = endpointAt(filename);
      if (!endpoint || endpoint.token === existing?.token) continue;
      return await query(endpoint, payload);
    }
  } finally {
    if (acquired) {
      try { if (fs.readFileSync(lock, 'utf8') === owner) fs.unlinkSync(lock); } catch {}
    }
  }
  return null;
}

async function input() {
  const chunks = [];
  let total = 0;
  const timer = setTimeout(() => process.stdin.destroy(), 1000);
  try {
    for await (const chunk of process.stdin) {
      total += chunk.length;
      if (total > 32768) return null;
      chunks.push(chunk);
    }
    return Buffer.concat(chunks).toString('utf8');
  } finally { clearTimeout(timer); }
}

try {
  const payload = await input();
  if (payload !== null) {
    const parsed = JSON.parse(payload);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed) && String(parsed.prompt || '').trim()) {
      let output = null;
      try { output = await warm(payload); } catch {}
      if (output === null) output = await cold(payload);
      process.stdout.write(output || '');
    }
  }
} catch {}
