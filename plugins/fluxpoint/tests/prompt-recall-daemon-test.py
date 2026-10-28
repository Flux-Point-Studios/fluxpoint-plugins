#!/usr/bin/env python3
"""Actual warm hook, authenticated transport, index replacement and idle exit."""
import contextlib
import hashlib
import hmac
import io
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import recall
from process_tree import bash_path


def mac(token, direction, nonce, value):
    return hmac.new(bytes.fromhex(token), (direction + '\n' + nonce + '\n' + value).encode(), hashlib.sha256).hexdigest()


class WarmRecall(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.state = Path(self.temp.name) / 'state'
        self.state.mkdir(mode=0o700)
        self.key = secrets.token_hex(32)
        self.endpoint = self.state / (self.key + '.json')
        self.env = dict(os.environ, FPL_RECALL_DAEMON_DIR=str(self.state), FPL_RECALL_DAEMON_IDLE='2',
                        FPL_MEM_PROMPT='1', FPL_PY=sys.executable, CLAUDE_PROJECT_DIR=str(self.root))
        self.source('original evidence')
        with contextlib.redirect_stdout(io.StringIO()):
            recall.build(str(self.root))
        self.payload = json.dumps({'prompt': 'beacon decoder', 'cwd': str(self.root)})

    def source(self, detail):
        (self.root / 'substrate.json').write_text(json.dumps({'repo': 'sample', 'primitives': [
            {'id': 'beacon-decoder', 'kind': 'tool', 'status': 'live', 'desc': 'beacon decoder ' + detail},
            {'id': 'payment', 'kind': 'tool', 'status': 'live', 'desc': 'payment scheduling'},
            {'id': 'mailbox', 'kind': 'tool', 'status': 'live', 'desc': 'mailbox routing'}]}))

    def server(self, idle=2):
        child = subprocess.Popen([sys.executable, str(SCRIPTS / 'prompt-recall-daemon.py'),
            '--state-dir', str(self.state), '--key', self.key, '--project', str(self.root),
            '--cwd', str(self.root), '--idle-seconds', str(idle)],
            env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.endpoint.is_file() and child.poll() is None:
            time.sleep(.02)
        self.assertTrue(self.endpoint.is_file(), 'daemon failed to publish a private endpoint')
        return child, json.loads(self.endpoint.read_text())

    def query(self, endpoint, payload=None, authenticated=True):
        payload = self.payload if payload is None else payload
        nonce = secrets.token_hex(16)
        request = {'nonce': nonce, 'payload': payload,
                   'mac': mac(endpoint['token'], 'request', nonce, payload) if authenticated else '0' * 64}
        with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=2) as connection:
            connection.settimeout(3)
            connection.sendall(json.dumps(request).encode() + b'\n')
            connection.shutdown(socket.SHUT_WR)
            received = b''
            while len(received) < 8192:
                part = connection.recv(8192)
                if not part:
                    break
                received += part
        if not authenticated:
            self.assertEqual(received, b'')
            return ''
        response = json.loads(received)
        self.assertEqual(response['mac'], mac(endpoint['token'], 'response', nonce, response['output']))
        return response['output']

    def test_authenticated_queries_reuse_daemon_and_detect_atomic_index_replacement(self):
        child, endpoint = self.server()
        self.assertIn('original evidence', self.query(endpoint))
        self.assertIn('original evidence', self.query(endpoint))
        self.source('replacement evidence')
        with contextlib.redirect_stdout(io.StringIO()):
            recall.build(str(self.root))
        self.assertIn('replacement evidence', self.query(endpoint))
        self.assertEqual(json.loads(self.endpoint.read_text())['pid'], child.pid)

    def test_authentication_and_payload_limits_leave_server_available(self):
        _, endpoint = self.server()
        self.query(endpoint, authenticated=False)
        with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=2) as connection:
            connection.sendall(b'x' * 65537)
        self.assertIn('original evidence', self.query(endpoint))
        self.assertEqual(self.query(endpoint, '{malformed'), '')
        self.assertEqual(self.query(endpoint, json.dumps({'prompt': 'unrelated question'})), '')

    def test_idle_exit_removes_only_its_endpoint(self):
        child, endpoint = self.server(idle=1)
        self.assertIn('original evidence', self.query(endpoint))
        child.wait(timeout=4)
        self.assertFalse(self.endpoint.exists())

    def test_missing_index_is_read_only(self):
        shutil.rmtree(recall.index_dir(str(self.root)))
        _, endpoint = self.server()
        self.assertIn('index', self.query(endpoint))
        self.assertFalse(Path(recall.index_dir(str(self.root))).exists())

    def test_index_cache_reads_once_and_invalidates_replaced_bytes(self):
        spec = importlib.util.spec_from_file_location('warm_cache_test', SCRIPTS / 'prompt-recall-daemon.py')
        daemon = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(daemon)
        cache, reads = daemon.IndexCache(), []
        filename = self.root / 'sample.json'
        filename.write_text('{"value":1}')
        def load():
            reads.append(1)
            return json.loads(filename.read_text())
        self.assertEqual(cache.read(filename, load), {'value': 1})
        self.assertEqual(cache.read(filename, load), {'value': 1})
        self.assertEqual(len(reads), 1)
        replacement = self.root / 'replacement.json'
        replacement.write_text('{"value":2}')
        os.replace(replacement, filename)
        self.assertEqual(cache.read(filename, load), {'value': 2})
        self.assertEqual(len(reads), 2)

    @unittest.skipUnless(shutil.which('node') and shutil.which('bash'), 'Node and Bash required')
    def test_warm_hook_needs_no_python_launch_and_does_not_hold_output_pipes(self):
        command = [bash_path(), (SCRIPTS / 'prompt-recall.sh').as_posix()]
        first = subprocess.run(command, input=self.payload, env=self.env, capture_output=True, text=True, timeout=8)
        self.assertIn('original evidence', first.stdout)
        endpoints = list(self.state.glob('*.json'))
        self.assertEqual(len(endpoints), 1)
        original = endpoints[0].read_bytes()
        second = subprocess.run(command, input=self.payload,
            env=dict(self.env, FPL_PY=str(self.root / 'does-not-exist')),
            capture_output=True, text=True, timeout=4)
        self.assertIn('original evidence', second.stdout)
        self.assertEqual(endpoints[0].read_bytes(), original)
        self.assertEqual(first.stderr + second.stderr, '')
        if os.name != 'nt':
            self.assertEqual(endpoints[0].stat().st_mode & 0o077, 0)

        # A stale port can be reused by another local process. It cannot
        # forge a reply: the bearer secret never travels over the socket.
        endpoint = json.loads(original)
        with socket.socket() as fake:
            fake.bind(('127.0.0.1', 0))
            fake.listen(1)
            fake.settimeout(3)
            def forge():
                connection, _ = fake.accept()
                with connection:
                    connection.recv(65536)
                    connection.sendall(json.dumps({'output': 'FORGED CONTEXT', 'mac': '0' * 64}).encode() + b'\n')
            thread = threading.Thread(target=forge, daemon=True)
            thread.start()
            endpoint['port'] = fake.getsockname()[1]
            endpoints[0].write_text(json.dumps(endpoint))
            forged = subprocess.run(command, input=self.payload,
                env=dict(self.env, FPL_PY=str(self.root / 'does-not-exist')),
                capture_output=True, text=True, timeout=6)
            thread.join(timeout=3)
            self.assertEqual(forged.stdout, '')
            self.assertEqual(forged.returncode, 0)


if __name__ == '__main__':
    unittest.main()
