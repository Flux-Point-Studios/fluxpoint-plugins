"""Shared detached command records, bounded waits and process-tree cancellation.

This module attests nothing. Callers decide what completion means and persist
any evidence before publishing DONE. Python 3.9+, standard library only.
"""
from contextlib import contextmanager
import datetime
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import time

AWAIT_DEFAULT = 540
AWAIT_MAX = 570
TOKEN = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
TERMINAL = {'DONE', 'CANCELLED', 'ERROR'}


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Store:
    def __init__(self, root, directory):
        self.directory = os.path.join(os.path.abspath(root), directory)

    def path(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ValueError('invalid background token')
        return os.path.join(self.directory, token + '.json')

    def read(self, token):
        try:
            with open(self.path(token), encoding='utf-8') as fh:
                rec = json.load(fh)
            if not isinstance(rec, dict) or rec.get('token') != token:
                return None
            return rec
        except (OSError, ValueError):
            return None

    @contextmanager
    def locked(self, token):
        path = self.path(token)
        os.makedirs(self.directory, exist_ok=True)
        with open(path + '.lock', 'a+b') as lock:
            lock.seek(0)
            if os.name == 'nt':
                import msvcrt
                if os.fstat(lock.fileno()).st_size == 0:
                    lock.write(b'\0')
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == 'nt':
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _write(self, rec):
        path = self.path(rec['token'])
        fd, tmp = tempfile.mkstemp(prefix='.' + rec['token'], suffix='.tmp', dir=self.directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fh:
                json.dump(rec, fh)
                fh.write('\n')
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def write(self, rec):
        with self.locked(rec['token']):
            old = self.read(rec['token']) or {}
            merged = dict(old, **rec)
            if old.get('cancelRequested'):
                merged['cancelRequested'] = True
            self._write(merged)
        return merged


def detached_options(platform=None):
    if (platform or os.name) == 'nt':
        return {'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200)}
    return {'start_new_session': True}


def spawn(argv, cwd):
    return subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            **detached_options())


def alive_nt(pid):
    """Query the process with pointer-sized HANDLEs, including on 64-bit Windows."""
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    except (AttributeError, OSError):
        # Unknown is bounded by await's deadline, never read as a finished run.
        return True


def alive(pid, started=''):
    if pid is None:
        try:
            stamp = datetime.datetime.strptime(started, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
            return (datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds() < 60
        except (TypeError, ValueError):
            return False
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == 'nt':
        return alive_nt(pid)
    try:
        os.kill(pid, 0)
        # An unreaped detached runner is no longer working.
        try:
            with open('/proc/%d/stat' % pid) as fh:
                if fh.read().rsplit(')', 1)[-1].split()[0] == 'Z':
                    return False
        except (OSError, IndexError):
            pass
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_tree(pid, platform=None, grace=2.0):
    if type(pid) is not int or pid <= 1 or pid == os.getpid():
        raise ValueError('refusing invalid command process group')
    if (platform or os.name) == 'nt':
        result = subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)],
                                capture_output=True, text=True, timeout=30)
        if result.returncode and alive(pid):
            raise OSError('taskkill failed: ' + (result.stderr or result.stdout).strip())
        return
    # run_command creates this session/group before publishing its PID.
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_command(store, token, command, cwd, log):
    """Start and wait for a command; serialize launch with cancellation requests."""
    os.makedirs(os.path.dirname(os.path.abspath(log)), exist_ok=True)
    with open(log, 'wb') as out:
        with store.locked(token):
            rec = store.read(token)
            if rec is None:
                raise ValueError('background record disappeared before command launch')
            if rec.get('cancelRequested'):
                return None
            proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, **detached_options())
            rec.update(commandPid=proc.pid, processGroup=proc.pid)
            store._write(rec)
        try:
            return proc.wait()
        except BaseException:
            terminate_tree(proc.pid)
            proc.wait()
            raise


def finish(store, token, code, before_finish=None, **fields):
    with store.locked(token):
        rec = store.read(token)
        if rec is None:
            raise ValueError('background record disappeared before completion')
        if before_finish is not None:
            before_finish(rec)
        rec.update(fields)
        rec.update(status='CANCELLED' if rec.get('cancelRequested') else 'DONE',
                   exit=code, finished=now())
        store._write(rec)
    return rec


def fail(store, token, exc):
    return store.write({'token': token, 'status': 'ERROR', 'error': str(exc), 'finished': now()})


def tail(path, lines=20):
    if not path:
        return ''
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            return '\n'.join(fh.read().decode('utf-8', 'replace').splitlines()[-lines:])
    except OSError:
        return ''


def wait(read, initial, timeout=AWAIT_DEFAULT, until=None, is_alive=alive):
    """Return (CLI status, record, reason); 0 known/matched, 3 running, 4 lost."""
    if not math.isfinite(timeout):
        raise ValueError('timeout must be finite')
    try:
        pattern = re.compile(until) if until is not None else None
    except re.error as exc:
        raise ValueError('invalid --until regex: ' + str(exc)) from exc
    deadline = time.monotonic() + min(AWAIT_MAX, max(0, timeout))
    rec = initial
    log_offset, pending = 0, ''
    while True:
        current = read(rec['token'])
        if current is None:
            return 4, rec, 'background record disappeared or is unreadable'
        rec = current
        if rec.get('status') in TERMINAL:
            if rec['status'] == 'ERROR':
                return 4, rec, 'runner error: ' + str(rec.get('error', 'unknown error'))
            if rec['status'] == 'DONE' and type(rec.get('exit')) is not int:
                return 4, rec, 'completion record has no command exit'
            return 0, rec, 'completed'
        if rec.get('status') != 'RUNNING':
            return 4, rec, 'unrecognized background status'
        if pattern is not None and rec.get('log'):
            try:
                with open(rec['log'], encoding='utf-8', errors='replace') as fh:
                    fh.seek(log_offset)
                    # A growing log must not keep one bounded await scanning forever.
                    chunk = fh.read(1024 * 1024)
                    log_offset = fh.tell()
                pending += chunk
                parts = pending.split('\n')
                pending = parts.pop()
                for line in parts:
                    if pattern.search(line):
                        return 0, rec, 'matched log line: ' + line[-500:]
                if len(pending) > 1024 * 1024:
                    raise ValueError('log line exceeds 1 MiB; await without --until')
            except FileNotFoundError:
                pass
        if not is_alive(rec.get('pid'), rec.get('started', '')):
            again = read(rec['token']) or rec
            if again.get('status') in TERMINAL:
                rec = again
                if rec['status'] != 'ERROR':
                    if rec['status'] == 'DONE' and type(rec.get('exit')) is not int:
                        return 4, rec, 'completion record has no command exit'
                    return 0, rec, 'completed'
            return 4, rec, 'runner is gone and left no verdict'
        if time.monotonic() >= deadline:
            return 3, rec, 'still running; repeat the foreground await call'
        time.sleep(min(0.2, max(0.01, deadline - time.monotonic())))


def cancel(store, token):
    rec = store.read(token)
    if rec is None:
        raise ValueError('unknown background run ' + token)
    if rec.get('status') in TERMINAL:
        return rec
    # run_command holds this same lock until commandPid is published. Either
    # cancel prevents launch or it obtains the full group's PID; never a gap.
    rec = store.write({'token': token, 'cancelRequested': True})
    if rec.get('status') in TERMINAL:
        return rec
    pid = rec.get('commandPid')
    if pid is not None:
        terminate_tree(pid)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        current = store.read(token) or rec
        if current.get('status') in TERMINAL:
            return current
        if not alive(current.get('pid'), current.get('started', '')):
            break
        time.sleep(0.05)
    # A runner stuck before/after launch cannot keep a cancellation pending.
    runner = rec.get('pid')
    if type(runner) is int and runner > 1 and runner != os.getpid() and alive(runner):
        if os.name == 'nt':
            terminate_tree(runner)
        else:
            os.kill(runner, signal.SIGTERM)
    return store.write({'token': token, 'status': 'CANCELLED', 'finished': now()})
