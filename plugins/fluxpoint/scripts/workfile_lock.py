"""Cross-process lock shared by every read/modify/write of a work file."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import time


@contextmanager
def workfile_lock(path):
    path = Path(path).resolve()
    state = (path.parent if path.parent.name == 'fluxpoint' and path.parent.parent.name == '.claude'
             else path.parent / '.claude' / 'fluxpoint')
    state.mkdir(parents=True, exist_ok=True)
    (state / '.gitignore').write_text('*\n', encoding='utf-8')
    key = hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()[:24]
    with (state / (key + '.lock')).open('a+b') as fh:
        if os.name == 'nt':
            import msvcrt
            if fh.tell() == 0:
                fh.write(b'\0')
                fh.flush()
            deadline = time.monotonic() + 30
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f'timed out locking {path}')
                    time.sleep(.05)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
