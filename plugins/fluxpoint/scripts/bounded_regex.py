"""Evaluate readiness regexes in an isolated, deadline-bound Python worker.

The parent never compiles or searches a regex. Startup, input transfer,
matching and output transfer share one finite deadline. A timed-out or
interrupted worker is killed and reaped; it cannot retain the awaiter's pipes.
"""
import json
import math
from pathlib import Path
import subprocess
import sys
import time

MAX_PATTERN = 65536
MAX_LINE = 1024 * 1024
MAX_BATCH = 4 * 1024 * 1024


class RegexTimeout(ValueError):
    """The isolated regex evaluation exhausted its readiness-wait budget."""


def _reap(proc):
    if proc.poll() is None:
        proc.kill()
    # The worker cannot spawn children: isolated startup skips site/custom
    # imports, and its only executable operation is standard-library re.search.
    # Always drain and wait after kill, including cancellation of the caller.
    proc.communicate()


def search_lines(pattern, lines, timeout):
    """Return the first matching line (including ''), or None for no match.

    Invalid input/regex or a failed worker raises ValueError. Deadline
    exhaustion raises RegexTimeout, never a nonmatch or readiness result.
    """
    began = time.monotonic()
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise RegexTimeout('readiness regex has no finite positive matching budget')
    deadline = began + timeout
    if not isinstance(pattern, str) or len(pattern) > MAX_PATTERN:
        raise ValueError('readiness regex must be a string of at most 65536 characters')
    if not isinstance(lines, (list, tuple)) or any(not isinstance(line, str) for line in lines):
        raise ValueError('readiness lines must be a list of strings')
    if any(len(line) > MAX_LINE for line in lines):
        raise ValueError('log line exceeds 1 MiB; await without --until')
    encoded = json.dumps(dict(pattern=pattern, lines=lines), ensure_ascii=True).encode('ascii')
    if len(encoded) > MAX_BATCH:
        raise ValueError('readiness batch exceeds 4 MiB; read a smaller log batch')
    if time.monotonic() >= deadline:
        raise RegexTimeout('readiness regex exhausted its matching budget before worker launch')
    proc = subprocess.Popen([sys.executable, '-I', '-S', str(Path(__file__).resolve()), '--worker'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        output, error = proc.communicate(encoded, timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        _reap(proc)
        raise RegexTimeout(f'readiness regex exceeded its {timeout:g}s matching budget') from exc
    except BaseException:
        _reap(proc)
        raise
    if time.monotonic() >= deadline:
        raise RegexTimeout(f'readiness regex exceeded its {timeout:g}s matching budget')
    try:
        result = json.loads(output.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        detail = error.decode('utf-8', 'replace').strip()[-500:]
        raise ValueError(f'readiness regex worker returned no valid result: {detail or proc.returncode}') from exc
    if proc.returncode != 0 or not isinstance(result, dict) or 'error' in result:
        detail = result.get('error', f'exit {proc.returncode}') if isinstance(result, dict) else 'invalid result'
        raise ValueError('readiness regex worker: ' + str(detail))
    match = result.get('matched')
    if match is not None and (not isinstance(match, str) or match not in lines):
        raise ValueError('readiness regex worker returned an invalid matching line')
    if time.monotonic() >= deadline:
        raise RegexTimeout(f'readiness regex exceeded its {timeout:g}s matching budget')
    return match


def _worker():
    # re is deliberately imported only in the subprocess. Even regex
    # compilation and catastrophic backtracking stay outside the awaiter.
    import re
    try:
        raw = sys.stdin.buffer.read(MAX_BATCH+1)
        if len(raw) > MAX_BATCH:
            raise ValueError('readiness input exceeds its bounded batch size')
        data = json.loads(raw.decode('ascii'))
        expression = re.compile(data['pattern'])
        matched = next((line for line in data['lines'] if expression.search(line)), None)
        result, code = dict(matched=matched), 0
    except Exception as exc:
        result, code = dict(error=f'{type(exc).__name__}: {exc}'), 2
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode('ascii'))
    sys.stdout.buffer.flush()
    return code


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        print('bounded_regex is an internal readiness matcher', file=sys.stderr)
        sys.exit(64)
    sys.exit(_worker())
