"""Bounded real-CLI fixtures with a scripted, localhost-only Anthropic provider.

This tests runtime/tool behavior. Scripted replies are no model-quality or
provider-cost evidence. Keep the CLI's normal permission and isolation checks.
"""
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from process_tree import ProcessTree

DUMMY_KEY = 'sk-ant-test-local-runtime-fixture-dummy-only'


def text_reply(text):
    return {'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn'}


def tool_reply(name, inputs, ident):
    return {'content': [{'type': 'tool_use', 'id': ident, 'name': name, 'input': inputs}], 'stop_reason': 'tool_use'}


def text_content(value):
    if isinstance(value, str): return value
    if isinstance(value, list): return '\n'.join(row.get('text', '') for row in value if isinstance(row, dict))
    return str(value)


class LocalProvider:
    """responder(body, one_based_request_index) returns content/stop_reason."""
    def __init__(self, responder):
        self.responder = responder
        self.requests, self.responses, self.errors = [], [], []
        self.lock = threading.RLock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_POST(self):
                try:
                    size = int(self.headers.get('Content-Length', 0))
                    if not 0 < size <= 16 * 1024 * 1024: raise ValueError('invalid local API request size')
                    body = json.loads(self.rfile.read(size))
                    if self.headers.get('x-api-key') != DUMMY_KEY: raise ValueError('local provider received a non-fixture credential')
                    if self.path.split('?')[0] == '/v1/messages/count_tokens':
                        output = b'{"input_tokens":100}'
                        self.send_response(200); self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(output))); self.end_headers(); self.wfile.write(output)
                        return
                    if self.path.split('?')[0] != '/v1/messages': raise ValueError('unsupported local API path: ' + self.path)
                    with owner.lock:
                        index = len(owner.requests) + 1
                        owner.requests.append({'path': self.path, 'dummyKeyOnly': True, 'body': body})
                        reply = owner.responder(body, index)
                        message = {'id': 'msg_local_' + str(index), 'type': 'message', 'role': 'assistant',
                                   'model': body['model'], 'content': reply['content'],
                                   'stop_reason': reply['stop_reason'], 'stop_sequence': None,
                                   'usage': {'input_tokens': 100, 'output_tokens': 8}}
                        owner.responses.append(message)
                    if body.get('stream'):
                        events = [('message_start', {'type': 'message_start', 'message': {**message, 'content': [], 'stop_reason': None}})]
                        for position, block in enumerate(message['content']):
                            start = {'type': 'text', 'text': ''} if block['type'] == 'text' else {**block, 'input': {}}
                            delta = {'type': 'text_delta', 'text': block['text']} if block['type'] == 'text' else {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}
                            events += [('content_block_start', {'type': 'content_block_start', 'index': position, 'content_block': start}),
                                       ('content_block_delta', {'type': 'content_block_delta', 'index': position, 'delta': delta}),
                                       ('content_block_stop', {'type': 'content_block_stop', 'index': position})]
                        events += [('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': message['stop_reason'], 'stop_sequence': None}, 'usage': {'output_tokens': 8}}), ('message_stop', {'type': 'message_stop'})]
                        output = ''.join('event: ' + name + '\ndata: ' + json.dumps(value) + '\n\n' for name, value in events).encode('utf-8')
                        kind = 'text/event-stream'
                    else:
                        output = json.dumps(message).encode('utf-8'); kind = 'application/json'
                    self.send_response(200); self.send_header('Content-Type', kind)
                    self.send_header('Content-Length', str(len(output))); self.end_headers(); self.wfile.write(output)
                except Exception as exc:
                    owner.errors.append(str(exc))
                    self.send_error(400, 'local fixture protocol refused')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)

    def save(self, directory):
        directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
        for name, value in [('requests', self.requests), ('responses', self.responses), ('errors', self.errors)]:
            (directory / (name + '.json')).write_text(json.dumps(value, indent=2), encoding='utf-8')


def run_cli(binary, repo, prompt, provider, config, artifacts, allowed_tools=(), timeout=90, extra=()):
    """Normal permissions; stdin and child descendants belong to a bounded job."""
    forbidden = ('--dangerously-skip-permissions', '--allow-dangerously-skip-permissions', '--bare', '--safe-mode', '--restricted', '--permission-mode', '--permission-prompts', '--settings')
    if any(any(flag == prefix or flag.startswith(prefix + '=') for prefix in forbidden) for flag in extra):
        raise ValueError('runtime acceptance cannot disable permission/customization checks')
    repo, config, artifacts = Path(repo).resolve(), Path(config).resolve(), Path(artifacts).resolve()
    config.mkdir(parents=True, exist_ok=True); artifacts.mkdir(parents=True, exist_ok=True)
    keep = {'PATH', 'SHELL', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE', 'PATHEXT',
            'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL', 'TERM', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA', 'APPDATA',
            'FPL_BASH', 'CLAUDE_CODE_GIT_BASH_PATH'}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_API_KEY=DUMMY_KEY, ANTHROPIC_BASE_URL=provider.url,
               DISABLE_TELEMETRY='1', DISABLE_ERROR_REPORTING='1', DISABLE_AUTOUPDATER='1',
               CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL='1', PYTHONIOENCODING='utf-8',
               NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
    argv = [str(binary), '--print', '--verbose', '--output-format', 'stream-json', '--model', 'sonnet',
            '--setting-sources', 'project,local', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--permission-mode', 'manual', '--permission-prompts', 'none', *extra]
    if allowed_tools: argv += ['--allowedTools', *allowed_tools]
    prompt_path = artifacts / 'prompt.txt'; prompt_path.write_bytes(prompt.encode('utf-8'))
    invocation = {'argv': argv, 'cwd': str(repo), 'provider': provider.url, 'dummyKeyOnly': True,
                  'permissionMode': 'manual', 'permissionPrompts': 'none', 'config': str(config), 'timeoutSeconds': timeout}
    (artifacts / 'invocation.json').write_text(json.dumps(invocation, indent=2), encoding='utf-8')
    # The owned launcher feeds stdin to the CLI without a Bash/cmd quoting hop.
    launch = [sys.executable, str(Path(__file__).resolve()), '--stdin-call', str(prompt_path), *argv]
    started = time.monotonic()
    prior = dict(os.environ)
    try:
        os.environ.clear(); os.environ.update(env)
        with (artifacts / 'cli.jsonl').open('wb') as stream:
            tree = ProcessTree(launch, str(repo), stream)
            try:
                tree.resume()
                status = tree.proc.wait(timeout=timeout)
            finally:
                try:
                    if tree.running(): tree.terminate()
                finally: tree.close()
    finally:
        os.environ.clear(); os.environ.update(prior)
        provider.save(artifacts)
    if provider.errors: raise ValueError('local provider protocol errors: ' + '; '.join(provider.errors))
    return {'exit': status, 'elapsedSeconds': round(time.monotonic() - started, 3), **invocation}


if __name__ == '__main__':
    if len(sys.argv) < 4 or sys.argv[1] != '--stdin-call': raise SystemExit('fixture launcher requires --stdin-call')
    with open(sys.argv[2], 'rb') as source:
        raise SystemExit(subprocess.run(sys.argv[3:], stdin=source).returncode)
