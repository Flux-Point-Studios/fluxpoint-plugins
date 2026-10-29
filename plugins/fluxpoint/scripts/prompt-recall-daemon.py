#!/usr/bin/env python3
"""Private, offline prompt queries. Never builds an index or contacts an embedder."""
import argparse
from collections import OrderedDict
import contextlib
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
import threading

import recall

MAX_REQUEST = 65536
MAX_OUTPUT = 4096
MAX_CACHE_BYTES = 32 * 1024 * 1024


def signature(path):
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            return None
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    except OSError:
        return None


class IndexCache:
    """Atomic replacements invalidate cached JSON on the next query."""
    def __init__(self):
        self.files = OrderedDict()
        self.bytes = 0

    def read(self, path, load):
        path = os.path.abspath(path)
        stamp = signature(path)
        old = self.files.get(path)
        if old and old[0] == stamp:
            self.files.move_to_end(path)
            return old[1]
        if old:
            self.bytes -= old[0][2]
            del self.files[path]
        if stamp is None:
            return None
        if stamp[2] > MAX_CACHE_BYTES:
            raise SystemExit('recall index exceeds the warm query cache limit')
        value = load()
        if signature(path) != stamp:
            return None  # A mixed generation never enters the cache.
        self.files[path] = (stamp, value)
        self.bytes += stamp[2]
        while self.bytes > MAX_CACHE_BYTES or len(self.files) > 32:
            _, discarded = self.files.popitem(last=False)
            self.bytes -= discarded[0][2]
        return value

    def install(self):
        if hasattr(recall, '_read_json_path'):
            path_loader = recall._read_json_path
            recall._read_json_path = lambda path: self.read(path, lambda: path_loader(path))
        else:
            raw = recall._load_json
            recall._load_json = lambda root, filename: self.read(
                os.path.join(root, recall.INDEX_DIR, filename), lambda: raw(root, filename))
        # Source freshness is advisory; checking every note's content again
        # on each prompt defeats a warm query. Bound that diagnostic lag to
        # five seconds. Concrete index file replacements always invalidate now.
        def ttl(function):
            snapshots = OrderedDict()
            def cached(root):
                key, now = os.path.realpath(root), time.monotonic()
                old = snapshots.get(key)
                if old and now - old[0] < 5:
                    return old[1]
                value = function(root)
                snapshots[key] = (now, value)
                snapshots.move_to_end(key)
                while len(snapshots) > 8:
                    snapshots.popitem(last=False)
                return value
            return cached
        recall._source_fingerprints = ttl(recall._source_fingerprints)
        if hasattr(recall, '_corpus_fingerprints'):
            recall._corpus_fingerprints = ttl(recall._corpus_fingerprints)
        else:
            recall.recall_sources.fingerprints = ttl(recall.recall_sources.fingerprints)


def git(directory, option):
    try:
        result = subprocess.run(['git', '-C', directory, 'rev-parse', option],
            capture_output=True, text=True, timeout=1)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def contained(base, candidate):
    base, candidate = os.path.realpath(base), os.path.realpath(candidate)
    try:
        return os.path.normcase(os.path.commonpath((base, candidate))) == os.path.normcase(base)
    except ValueError:
        return False


def root_for_payload(payload, project, cwd):
    """The same project jurisdiction and workspace containment as fpl_cd_project."""
    if project and git(project, '--is-inside-work-tree') == 'true':
        return os.path.realpath(project)
    raw = payload.get('cwd') if isinstance(payload.get('cwd'), str) else ''
    if project:
        if not os.path.isdir(project):
            return None
        candidate = raw or project
        if not os.path.isabs(candidate):
            candidate = os.path.join(project, candidate)
        if not contained(project, candidate) or not os.path.isdir(candidate):
            return None
    else:
        candidate = raw if raw and os.path.isdir(raw) else cwd
    candidate = os.path.realpath(candidate)
    top = git(candidate, '--show-toplevel')
    if top:
        if project and not contained(project, top):
            return None
        candidate = os.path.realpath(top)
    return candidate if os.path.isdir(candidate) else None


def query(payload, project, cwd):
    try:
        value = json.loads(payload)
        if not isinstance(value, dict) or not str(value.get('prompt') or '').strip():
            return ''
        root = root_for_payload(value, project, cwd)
        if root is None:
            return ''
        output = io.StringIO()
        previous = sys.stdin
        try:
            sys.stdin = io.StringIO(payload)
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()), recall.recall_sources.project_views():
                recall.for_prompt(root)
        finally:
            sys.stdin = previous
        return output.getvalue().encode('utf-8')[:MAX_OUTPUT].decode('utf-8', errors='ignore')
    except (OSError, ValueError, TypeError, KeyError, SystemExit):
        return ''


def mac(token, direction, nonce, value):
    return hmac.new(bytes.fromhex(token), (direction + '\n' + nonce + '\n' + value).encode(), hashlib.sha256).hexdigest()


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    stat = path.lstat()
    if path.is_symlink() or not path.is_dir():
        raise ValueError('query state must be a private directory')
    if os.name != 'nt' and (stat.st_uid != os.getuid() or stat.st_mode & 0o077):
        raise ValueError('query state directory is not private to this user')
    return path


def serve(directory, key, project, cwd, idle):
    directory = private_directory(directory)
    endpoint = directory / (key + '.json')
    token = secrets.token_hex(32)
    IndexCache().install()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        listener.settimeout(.25)
        state = {'version': 1, 'pid': os.getpid(), 'port': listener.getsockname()[1], 'token': token}
        temporary = directory / (key + '.' + token + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(state, out)
        os.replace(temporary, endpoint)
        last = time.monotonic()
        try:
            while time.monotonic() - last < idle:
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(.5)
                    try:
                        incoming = bytearray()
                        while b'\n' not in incoming and len(incoming) <= MAX_REQUEST:
                            part = connection.recv(min(8192, MAX_REQUEST + 1 - len(incoming)))
                            if not part:
                                break
                            incoming.extend(part)
                        if len(incoming) > MAX_REQUEST or b'\n' not in incoming:
                            continue
                        request = json.loads(incoming.decode('utf-8'))
                        nonce, payload = request.get('nonce'), request.get('payload')
                        if (not isinstance(nonce, str) or not re.fullmatch('[0-9a-f]{32}', nonce)
                                or not isinstance(payload, str) or len(payload.encode()) > 32768
                                or not isinstance(request.get('mac'), str)
                                or not hmac.compare_digest(request['mac'], mac(token, 'request', nonce, payload))):
                            continue
                        last = time.monotonic()
                        # A blocked source read or excessive graph cannot
                        # leave a query process alive indefinitely on Windows,
                        # where interrupting a worker thread is not supported.
                        deadline = threading.Timer(5, lambda: os._exit(0))
                        deadline.daemon = True
                        deadline.start()
                        try:
                            output = query(payload, project, cwd)
                        finally:
                            deadline.cancel()
                        response = {'output': output, 'mac': mac(token, 'response', nonce, output)}
                        connection.sendall(json.dumps(response).encode() + b'\n')
                    except (OSError, ValueError, TypeError, AttributeError):
                        continue
        finally:
            try:
                if json.loads(endpoint.read_text())['token'] == token:
                    endpoint.unlink()
            except (OSError, ValueError, KeyError):
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir')
    parser.add_argument('--key')
    parser.add_argument('--project', default='')
    parser.add_argument('--cwd', default=os.getcwd())
    parser.add_argument('--idle-seconds', type=float, default=300)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if args.once:
        deadline = threading.Timer(5, lambda: os._exit(0))
        deadline.daemon = True
        deadline.start()
        payload = sys.stdin.buffer.read(32769)
        if len(payload) <= 32768:
            sys.stdout.write(query(payload.decode('utf-8'), args.project, args.cwd))
        return 0
    if not args.state_dir or not re.fullmatch('[0-9a-f]{64}', args.key or ''):
        parser.error('private state directory and 64-character key required')
    serve(args.state_dir, args.key, args.project, args.cwd, min(3600, max(1, args.idle_seconds)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
