#!/usr/bin/env python3
"""Actual CLI Workflow worktree acceptance using a dummy localhost provider.

Run explicitly with FPL_CLAUDE_BINARY pointing at Claude Code 2.1.312. No real
model, provider credential, network deployment or permission bypass is used.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from claude_local import LocalProvider, run_cli, text_content, text_reply, tool_reply
from worktree_base_fixture import cg, CONTRACTS, commands, graph
import native_binding


def git(root, *args, check=True):
    value = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, encoding='utf-8', check=check)
    return value.stdout.strip() if check else value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', default=os.environ.get('FPL_CLAUDE_BINARY') or shutil.which('claude'))
    parser.add_argument('--output', type=Path, help='retain the private native captures here')
    args = parser.parse_args()
    try:
        if not args.binary: raise ValueError('set FPL_CLAUDE_BINARY to the installed Claude CLI')
        binary = Path(args.binary).resolve()
        version = subprocess.check_output([str(binary), '--version'], text=True, encoding='utf-8', timeout=10).strip()
        if not version.startswith('2.1.312 '): raise ValueError('native acceptance profile requires CLI2.1.312: ' + version)
        directory = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix='fpl-worktree-native-'))
        directory.mkdir(parents=True, exist_ok=True)
        repo, origin, config, artifacts = [directory / name for name in ('repo', 'origin.git', 'config', 'artifacts')]
        repo.mkdir()
        git(repo, 'init', '-q', '-b', 'main'); git(repo, 'config', 'user.name', 'Local runtime fixture'); git(repo, 'config', 'user.email', 'fixture@example.invalid')
        (repo / '.gitignore').write_text('.claude/settings.local.json\n', encoding='utf-8')
        (repo / 'README.md').write_text('Private runtime isolation fixture.\n', encoding='utf-8')
        git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'Default branch fixture')
        subprocess.run(['git', 'init', '--bare', '-q', '-b', 'main', str(origin)], check=True)
        git(repo, 'remote', 'add', 'origin', str(origin)); git(repo, 'push', '-q', '-u', 'origin', 'main')
        git(repo, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
        fresh = git(repo, 'rev-parse', 'HEAD')
        (repo / 'tools').mkdir(); (repo / 'tools/run.sh').write_bytes(b'#!/usr/bin/env bash\nprintf "campaign tool present\\n"\n')
        git(repo, 'add', 'tools/run.sh'); git(repo, 'commit', '-qm', 'Campaign base adds tool')
        base = {'sha': git(repo, 'rev-parse', 'HEAD'), 'root': repo.as_posix(), 'branch': 'main', 'porcelain': ''}
        settings = repo / '.claude/settings.local.json'; settings.parent.mkdir()
        settings.write_text('{"worktree":{"baseRef":"fresh"}}\n', encoding='utf-8')
        emitted = commands(base)
        negative = 'git -C ' + shlex.quote(repo.as_posix()) + ' update-ref refs/heads/issue188-negative-control ' + base['sha']
        worker_commands = emitted[:4] + [negative] + emitted[4:] + [emitted[3], emitted[2]]
        node = {'id': 'base-probe', 'contract': 'NativeReadingsV1', 'isolation': 'worktree',
                'prompt': 'Runtime control: execute the emitted base-check commands in order, but immediately before the merge attempt this outside-write negative: ' + negative + '. It must be refused specifically by the worktree-isolation guard. A generic permission denial or a successful write invalidates the probe: stop. After the final HEAD reading, read your branch and clean status again. Never bypass permissions or disable sandboxing.'}
        source = cg.emit(graph([node]), CONTRACTS)
        main_turn, worker_turn = [0], [0]
        observed = []
        def respond(body, _index):
            names = {tool.get('name') for tool in body.get('tools', [])}
            if 'StructuredOutput' not in names:
                turn = main_turn[0]; main_turn[0] += 1
                if turn == 0:
                    if 'Workflow' not in names: return text_reply('BLOCKED: Workflow is unavailable in this actual runtime profile.')
                    return tool_reply('Skill', {'skill': 'workflow-authoring'}, 'main-0')
                if turn == 1: return tool_reply('Workflow', {'script': source, 'args': {'_base': base}}, 'main-1')
                return text_reply('The native captures determine acceptance; no model judgment is asserted.')
            results = [block for message in body['messages'] if message['role'] == 'user' and isinstance(message.get('content'), list)
                       for block in message['content'] if block.get('type') == 'tool_result' and str(block.get('tool_use_id', '')).startswith('worker-')]
            observed[:] = results
            turn = worker_turn[0]; worker_turn[0] += 1
            refusal = next((row for row in results if row['tool_use_id'] == 'worker-4'), None)
            isolation_refused = refusal and refusal.get('is_error') is True and 'isolated in the worktree' in text_content(refusal.get('content')) and 'Refusing to run' in text_content(refusal.get('content'))
            if turn < len(worker_commands) and (turn < 5 or isolation_refused):
                return tool_reply('Bash', {'command': worker_commands[turn], 'timeout': 10000, 'description': 'Run the private native worktree control'}, 'worker-' + str(turn))
            readings = [{'exit': native_binding._normalize(text_content(row.get('content')), row.get('is_error'))[0],
                         'output': native_binding._normalize(text_content(row.get('content')), row.get('is_error'))[1]} for row in results]
            final = next((text_content(row.get('content')).strip() for row in results if row['tool_use_id'] == 'worker-6'), fresh)
            return tool_reply('StructuredOutput', {'commands': worker_commands[:len(readings)], 'readings': readings, 'baseSeen': final}, 'worker-final')
        prompt = 'Please run this one-worker Workflow runtime-isolation acceptance probe in our private fixture. Use the actual worktree isolation guard; you are authorized to attempt the harmless shared-ref negative and plain own-branch ff-only command. All model replies are scripted localhost fixture responses. No permission bypass, sandbox disabling, provider calls or external changes.'
        with LocalProvider(respond) as provider:
            invocation = run_cli(binary, repo, prompt, provider, config, artifacts, ['Skill(workflow-authoring)', 'Workflow', 'Bash(git *)'], timeout=90)
        if invocation['exit'] != 0: raise ValueError('native CLI failed; inspect ' + str(artifacts / 'cli.jsonl'))
        initial = [json.loads(line) for line in (artifacts / 'cli.jsonl').read_text(encoding='utf-8').splitlines() if line.startswith('{')]
        init = next(row for row in initial if row.get('type') == 'system' and row.get('subtype') == 'init')
        if init['permissionMode'] != 'default': raise ValueError('native probe did not preserve default permissions')
        completion = None
        for request in provider.requests:
            for message in request['body']['messages']:
                if message['role'] != 'user': continue
                content = text_content(message.get('content'))
                match = re.search(r'<task-notification>.*?<tool-use-id>main-1</tool-use-id>.*?<status>completed</status>.*?<result>([\s\S]*?)</result>', content, re.S)
                if match: completion = json.loads(match[1])
        if (not isinstance(completion, dict) or completion.get('outcome') != 'COMPLETE'
                or (completion.get('worktreeBase') or {}).get('sha') != base['sha']):
            raise ValueError('the actual compiled Workflow did not complete on the expected base')
        (directory / 'compiled-summary.json').write_text(json.dumps(completion, indent=2), encoding='utf-8')
        runs = list((config / 'projects').glob('*/*/subagents/workflows/wf_*'))
        if len(runs) != 1: raise ValueError('actual Workflow did not produce exactly one native run')
        run = runs[0]; journal = [json.loads(line) for line in (run / 'journal.jsonl').read_text(encoding='utf-8').splitlines()]
        started = [row for row in journal if row.get('type') == 'started' and row.get('label') == 'base-probe']
        if len(started) != 1: raise ValueError('native Workflow worker was not actually started')
        agent = started[0]['agentId']; meta = json.loads((run / ('agent-' + agent + '.meta.json')).read_text(encoding='utf-8'))
        if meta.get('spawnedWithWorktree') is not True: raise ValueError('worker lacks native worktree isolation metadata')
        certificate = native_binding.bind(run.name, agent, config / 'projects', report=completion['results']['base-probe'], expected_base=base['sha'])
        by_id = {row['tool_use_id']: row for row in observed}
        raw = lambda index: text_content(by_id['worker-' + str(index)].get('content')).strip()
        if len(observed) != len(worker_commands) or raw(0) != fresh or raw(6) != base['sha']: raise ValueError('native before/after HEAD does not establish behind-to-exact-base transition')
        for index in (0, 1, 2, 3, 5, 6, 7, 8):
            row = by_id['worker-' + str(index)]
            if row.get('is_error', False) is not False or native_binding._normalize(text_content(row.get('content')), row.get('is_error'))[0] != 0:
                raise ValueError('a positive native control did not exit successfully: ' + str(index))
        if raw(3) != raw(7) or not raw(3): raise ValueError('native worker branch changed or detached')
        if by_id['worker-4'].get('is_error') is not True or 'isolated in the worktree' not in raw(4) or 'Refusing to run' not in raw(4): raise ValueError('outside write was not refused by the actual isolation guard')
        if git(repo, 'show-ref', '--verify', '--quiet', 'refs/heads/issue188-negative-control', check=False).returncode != 1: raise ValueError('negative write changed the shared repository')
        if git(repo, 'rev-parse', 'HEAD') != base['sha']: raise ValueError('shared HEAD moved')
        worker_root = Path(meta['worktreePath'])
        if (worker_root.resolve() == repo.resolve() or git(worker_root, 'rev-parse', 'HEAD') != base['sha']
                or git(worker_root, 'symbolic-ref', '--short', 'HEAD') != raw(3) or git(worker_root, 'status', '--porcelain')):
            raise ValueError('native worktree lacks final clean exact-base state on its own attached branch')
        for index in (2, 8):
            if native_binding._normalize(raw(index), by_id['worker-' + str(index)].get('is_error'))[1]: raise ValueError('native worker was not clean')
        report = {'version': version, 'cliSha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'base': base,
                  'fresh': fresh, 'runId': run.name, 'agentId': agent, 'branch': raw(3), 'certificate': certificate,
                  'outsideWrite': 'refused by native worktree isolation', 'merge': 'plain ff-only accepted; exact base and branch retained',
                  'requests': len(provider.requests), 'dummyKeyOnly': all(row['dummyKeyOnly'] for row in provider.requests),
                  'artifacts': str(directory), 'modelQualityOrProviderCostClaim': False}
        (directory / 'acceptance.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, sort_keys=True))
        return 0
    except (OSError, ValueError, StopIteration, KeyError, subprocess.SubprocessError) as exc:
        print('worktree-base-live: ' + str(exc)); return 1


if __name__ == '__main__': raise SystemExit(main())
