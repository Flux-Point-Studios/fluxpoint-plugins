#!/usr/bin/env python3
"""Run arbitrary long commands without attesting them; await in foreground slices."""
import argparse
import os
import secrets
import sys
import background as bg

BACKGROUND = os.path.join('.claude', 'fluxpoint', 'longrun')


def worker(store, token):
    rec = store.read(token)
    if rec is None:
        return 2
    store.write({'token': token, 'pid': os.getpid()})
    try:
        code = bg.run_command(store, token, rec['command'], rec['cwd'], rec['log'])
        bg.finish(store, token, code)
        return 0
    except (OSError, ValueError) as exc:
        bg.fail(store, token, str(rec['command'][0]) + ': ' + str(exc))
        return 4


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', default='.')
    commands = ap.add_subparsers(dest='action', required=True)
    start = commands.add_parser('start')
    start.add_argument('--log', required=True)
    start.add_argument('command', nargs=argparse.REMAINDER)
    wait = commands.add_parser('await')
    wait.add_argument('token')
    wait.add_argument('--until')
    wait.add_argument('--timeout', type=float, default=bg.AWAIT_DEFAULT)
    cancel = commands.add_parser('cancel')
    cancel.add_argument('token')
    internal = commands.add_parser('_worker', help=argparse.SUPPRESS)
    internal.add_argument('token')
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    store = bg.Store(root, BACKGROUND)
    try:
        if args.action == '_worker':
            return worker(store, args.token)
        if args.action == 'start':
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
            if not command:
                raise ValueError('start requires -- CMD [ARG ...]')
            token = 'lr_' + secrets.token_hex(8)
            log = os.path.abspath(args.log)
            store.write({'token': token, 'status': 'RUNNING', 'started': bg.now(), 'pid': None,
                         'command': command, 'cwd': os.getcwd(), 'log': log})
            try:
                proc = bg.spawn([sys.executable, os.path.abspath(__file__), '--root', root,
                                 '_worker', token], os.getcwd())
                store.write({'token': token, 'pid': proc.pid})
            except OSError as exc:
                bg.fail(store, token, exc)
                raise
            print(token, flush=True)
            return 0
        if args.action == 'cancel':
            rec = bg.cancel(store, args.token)
            print(f"longrun: {args.token} {rec['status']}; exit {rec.get('exit')}")
            return 0
        rec = store.read(args.token)
        if rec is None:
            raise ValueError('unknown or unreadable background run ' + args.token)
        status, rec, reason = bg.wait(store.read, rec, args.timeout, args.until)
        print(f"longrun: {args.token} {rec.get('status')}; exit {rec.get('exit')}; {reason}")
        log_tail = bg.tail(rec.get('log'))
        if log_tail:
            print(log_tail)
        return status
    except bg.CancellationPending as exc:
        print(f'longrun: cancellation pending: {exc}', file=sys.stderr)
        return 4
    except (OSError, ValueError) as exc:
        print(f'longrun: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
