"""Own a command's descendants until they drain; never identify an old tree by PID alone."""
import ctypes
import os
import secrets
import shutil
import subprocess
import time


class CleanupPending(OSError):
    """Owned descendants could not be observed drained; never publish cancellation."""


def bash_path():
    """The bash to start, as an absolute path where one is known.

    On Windows a bare "bash" is resolved by CreateProcess, which searches
    System32 before PATH: on a machine with WSL that is WSL's bash.exe, a
    different toolchain from the Git Bash the hooks and the Bash tool use.
    py.sh exports the bash it runs under as FPL_BASH. Without it, PATH is
    searched past System32 and WindowsApps, where WSL's launchers live, and
    then beside git, which Git for Windows installs with its bash.
    """
    for cand in (os.environ.get('FPL_BASH'), os.environ.get('CLAUDE_CODE_GIT_BASH_PATH')):
        if cand and os.path.isfile(cand):
            return cand
    if os.name != 'nt':
        return shutil.which('bash') or 'bash'
    system = os.path.normcase(os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32'))
    dirs = [d for d in os.environ.get('PATH', '').split(os.pathsep)
            if os.path.normcase(d.rstrip('\\/')) != system and 'windowsapps' not in os.path.normcase(d)]
    found = shutil.which('bash', path=os.pathsep.join(dirs))
    if found:
        return found
    git = shutil.which('git')
    here = os.path.dirname(os.path.realpath(git)) if git else ''
    for _ in range(3 if git else 0):
        cand = os.path.join(here, 'usr', 'bin', 'bash.exe')
        if os.path.isfile(cand):
            return cand
        here = os.path.dirname(here)
    return 'bash'


def identity(pid):
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == 'nt':
        try:
            api = WindowsJob.api()
            api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            api.OpenProcess.restype = ctypes.c_void_p
            api.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
            api.GetProcessTimes.restype = ctypes.c_int
            handle = api.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                created, exited, kernel, user = [ctypes.c_uint64() for _ in range(4)]
                if not api.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
                    return None
                return 'windows:' + str(created.value)
            finally:
                api.CloseHandle(handle)
        except OSError:
            return None
    try:
        with open('/proc/%d/stat' % pid) as fh:
            fields = fh.read().rsplit(')', 1)[-1].split()
        with open('/proc/sys/kernel/random/boot_id') as fh:
            boot = fh.read().strip()
        return 'linux:%s:%s' % (boot, fields[19])
    except (OSError, IndexError):
        try:
            out = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True,
                                 text=True, timeout=2, check=True).stdout.strip()
            return 'posix:' + out if out else None
        except (OSError, subprocess.SubprocessError):
            return None


def matches(pid, expected):
    return bool(expected) and identity(pid) == expected


def group_members(group):
    """Live members only: zombies are not working descendants, even before init reaps them."""
    if os.path.isdir('/proc'):
        result = []
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            try:
                with open('/proc/%s/stat' % entry) as fh:
                    fields = fh.read().rsplit(')', 1)[-1].split()
                if int(fields[2]) == group and fields[0] not in ('Z', 'X'):
                    pid = int(entry)
                    result.append({'pid': pid, 'identity': identity(pid)})
            except (OSError, ValueError, IndexError):
                continue
        return result
    try:
        out = subprocess.run(['ps', '-eo', 'pid=,pgid=,stat='], capture_output=True,
                             text=True, timeout=2, check=True).stdout
        return [{'pid': int(parts[0]), 'identity': identity(int(parts[0]))}
                for parts in (line.split() for line in out.splitlines())
                if len(parts) >= 3 and int(parts[1]) == group and not parts[2].startswith('Z')]
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# Fixed-width Windows structure fields are also inspectable in non-Windows tests.
DWORD = ctypes.c_uint32
HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t


class BasicLimits(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', DWORD), ('MinimumWorkingSetSize', SIZE_T), ('MaximumWorkingSetSize', SIZE_T),
                ('ActiveProcessLimit', DWORD), ('Affinity', SIZE_T), ('PriorityClass', DWORD), ('SchedulingClass', DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount',
                'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                ('ProcessMemoryLimit', SIZE_T), ('JobMemoryLimit', SIZE_T),
                ('PeakProcessMemoryUsed', SIZE_T), ('PeakJobMemoryUsed', SIZE_T)]


class Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in ('TotalUserTime', 'TotalKernelTime',
                'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')] + [
                (name, DWORD) for name in ('TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses')]


class ThreadEntry(ctypes.Structure):
    _fields_ = [('dwSize', DWORD), ('cntUsage', DWORD), ('th32ThreadID', DWORD),
                ('th32OwnerProcessID', DWORD), ('tpBasePri', ctypes.c_int32),
                ('tpDeltaPri', ctypes.c_int32), ('dwFlags', DWORD)]


class WindowsJob:
    @staticmethod
    def api():
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        specs = {
            'CreateJobObjectW': ([ctypes.c_void_p, ctypes.c_wchar_p], HANDLE),
            'OpenJobObjectW': ([DWORD, ctypes.c_int, ctypes.c_wchar_p], HANDLE),
            'SetInformationJobObject': ([HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD], ctypes.c_int),
            'QueryInformationJobObject': ([HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.c_void_p], ctypes.c_int),
            'AssignProcessToJobObject': ([HANDLE, HANDLE], ctypes.c_int),
            'TerminateJobObject': ([HANDLE, DWORD], ctypes.c_int),
            'CloseHandle': ([HANDLE], ctypes.c_int),
            'CreateToolhelp32Snapshot': ([DWORD, DWORD], HANDLE),
            'Thread32First': ([HANDLE, ctypes.POINTER(ThreadEntry)], ctypes.c_int),
            'Thread32Next': ([HANDLE, ctypes.POINTER(ThreadEntry)], ctypes.c_int),
            'OpenThread': ([DWORD, ctypes.c_int, DWORD], HANDLE),
            'ResumeThread': ([HANDLE], DWORD),
        }
        for name, (args, result) in specs.items():
            function = getattr(kernel, name)
            function.argtypes, function.restype = args, result
        return kernel

    def __init__(self, name=None, create=False):
        self.kernel = self.api()
        self.name = name or 'Local\\fluxpoint-' + secrets.token_hex(16)
        self.handle = (self.kernel.CreateJobObjectW(None, self.name) if create else
                       self.kernel.OpenJobObjectW(0x0004 | 0x0008, False, self.name))
        if not self.handle:
            if not create and ctypes.get_last_error() == 2:
                return
            raise ctypes.WinError(ctypes.get_last_error())
        if create:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                error = ctypes.get_last_error()
                self.close()
                raise ctypes.WinError(error)

    def assign(self, proc):
        if not self.kernel.AssignProcessToJobObject(self.handle, HANDLE(int(proc._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def active(self):
        if not self.handle:
            return 0
        info = Accounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.ActiveProcesses

    def terminate(self):
        if self.handle and self.active() and not self.kernel.TerminateJobObject(self.handle, 130):
            raise ctypes.WinError(ctypes.get_last_error())

    def resume(self, pid):
        # Popen closes CreateProcess's primary-thread handle. Enumerate while
        # the process is still suspended, then resume only those initial threads.
        snapshot = self.kernel.CreateToolhelp32Snapshot(0x4, 0)
        if not snapshot or snapshot == HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        threads = []
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            more = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    threads.append(entry.th32ThreadID)
                more = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.kernel.CloseHandle(snapshot)
        if not threads:
            raise OSError('cannot find suspended command thread')
        for tid in threads:
            handle = self.kernel.OpenThread(0x2, False, tid)  # THREAD_SUSPEND_RESUME
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if self.kernel.ResumeThread(handle) == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.kernel.CloseHandle(handle)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class ProcessTree:
    def __init__(self, command, cwd, out, platform=None, shell=False):
        self.job = WindowsJob(create=True) if (platform or os.name) == 'nt' else None
        self.proc = None
        self.members = []
        if self.job and not shell and isinstance(command, list) and str(command[0]).lower() in ('bash', 'bash.exe'):
            command = [bash_path()] + command[1:]
        try:
            options = {'creationflags': 0x200 | 0x4} if self.job else {'start_new_session': True}
            self.proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                         stdout=out, stderr=subprocess.STDOUT, shell=shell, **options)
            if self.job:
                self.job.assign(self.proc)
            else:
                self.members = group_members(self.proc.pid) or []
            self.command_identity = identity(self.proc.pid)
        except BaseException:
            if self.proc:
                self.proc.kill()
                self.proc.wait()
            self.close()
            raise

    def metadata(self):
        return {'commandPid': self.proc.pid, 'commandIdentity': self.command_identity,
                'processGroup': self.proc.pid, 'groupMembers': self.members,
                'treeType': 'windows-job' if self.job else 'posix-group',
                'jobName': self.job.name if self.job else None}

    def resume(self):
        if self.job:
            self.job.resume(self.proc.pid)

    def running(self):
        if self.job:
            return self.job.active() > 0
        members = group_members(self.proc.pid)
        if members is not None:
            self.members = members
            return bool(members)
        try:
            os.killpg(self.proc.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def terminate(self):
        if self.job:
            self.job.terminate()
        else:
            from background import terminate_tree
            terminate_tree(self.proc.pid)

    def close(self):
        if self.job:
            self.job.close()


def owned_members(rec):
    group = rec.get('processGroup')
    if type(group) is not int or group <= 1:
        return []
    members = group_members(group)
    if members is None:
        raise CleanupPending('cannot establish current process-group ownership')
    known = list(rec.get('groupMembers') or [])
    if rec.get('commandIdentity'):
        known.append({'pid': rec.get('commandPid'), 'identity': rec['commandIdentity']})
    identities = {(item.get('pid'), item.get('identity')) for item in known if item.get('identity')}
    if members and not any((item['pid'], item['identity']) in identities for item in members):
        raise CleanupPending('process group no longer matches its recorded creation identities; refusing PID reuse')
    return members


def has_owned_tree(rec):
    if rec.get('treeType') == 'windows-job' and rec.get('jobName'):
        job = WindowsJob(rec['jobName'])
        try:
            return job.active() > 0
        finally:
            job.close()
    return bool(owned_members(rec))


def terminate_owned(rec, timeout=5.0):
    deadline = time.monotonic() + timeout
    try:
        if rec.get('treeType') == 'windows-job' and rec.get('jobName'):
            job = WindowsJob(rec['jobName'])
            try:
                job.terminate()
                while job.active():
                    if time.monotonic() >= deadline:
                        raise CleanupPending('Windows job still has active descendants; cancellation is pending')
                    time.sleep(0.05)
            finally:
                job.close()
        elif owned_members(rec):
            from background import terminate_tree
            terminate_tree(rec['processGroup'])
            while True:
                members = group_members(rec['processGroup'])
                if members is None:
                    raise CleanupPending('cannot observe whether the POSIX group drained')
                if not members:
                    break
                if time.monotonic() >= deadline:
                    raise CleanupPending('POSIX group still has live descendants; cancellation is pending')
                time.sleep(0.05)
    except CleanupPending:
        raise
    except OSError as exc:
        raise CleanupPending('cannot verify process-tree cleanup: ' + str(exc)) from exc
