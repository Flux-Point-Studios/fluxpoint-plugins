"""Read-only Windows health sampling, using only Python's standard library.

Each sample runs one PowerShell process. Win32_Process CPU counters are
cumulative 100 ns values; creation timestamps keep reused PIDs distinct.
GetSystemTimes supplies machine-wide CPU deltas around that invocation.
"""

import ctypes
import datetime
import json
import math
import re
import subprocess
import time


_POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
function Read-Processes {
    @(Get-CimInstance Win32_Process -ErrorAction Stop | ForEach-Object {
        $created = $null
        if ($null -ne $_.CreationDate) {
            $created = $_.CreationDate.ToUniversalTime().ToString('o')
        }
        [pscustomobject]@{
            pid = $_.ProcessId; ppid = $_.ParentProcessId; created = $created
            kernel = [string]$_.KernelModeTime; user = [string]$_.UserModeTime
            command = $_.CommandLine; name = $_.Name
        }
    })
}
try {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $before = @(Read-Processes)
    $beforeAt = [DateTime]::UtcNow.ToString('o')
    $started = $clock.Elapsed.TotalSeconds
    Start-Sleep -Milliseconds __MILLISECONDS__
    $after = @(Read-Processes)
    $afterAt = [DateTime]::UtcNow.ToString('o')
    $elapsed = $clock.Elapsed.TotalSeconds - $started
} catch {
    [Console]::Error.WriteLine('Windows process table unreadable: ' + $_.Exception.Message)
    exit 1
}
$ports = @{status = 'unsupported'; detail = 'TCP dynamic range command unavailable'}
if (Get-Command netsh.exe -ErrorAction SilentlyContinue) {
    try {
        $ranges = @()
        foreach ($family in @('ipv4', 'ipv6')) {
            $range = @(netsh.exe int $family show dynamicport tcp 2>&1 | ForEach-Object { [string]$_ })
            if ($LASTEXITCODE -ne 0) { throw ('netsh ' + $family + ' dynamic range query failed') }
            $ranges += @{family = $family; range_output = $range}
        }
        if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
            $connections = @(Get-NetTCPConnection -ErrorAction Stop | Select-Object LocalAddress, LocalPort)
            $ports = @{status = 'ok'; ranges = $ranges; connections = $connections; detail = ''}
        } elseif (Get-Command netstat.exe -ErrorAction SilentlyContinue) {
            $connections = @(netstat.exe -ano -p tcp 2>&1 | ForEach-Object { [string]$_ })
            if ($LASTEXITCODE -ne 0) { throw 'netstat TCP query failed' }
            $ports = @{status = 'ok'; ranges = $ranges; netstat_output = $connections; detail = ''}
        } else {
            $ports = @{status = 'unsupported'; detail = 'TCP connection commands unavailable'}
        }
    } catch {
        $ports = @{status = 'unreadable'; detail = $_.Exception.Message}
    }
}
@{before = $before; after = $after; before_at = $beforeAt; after_at = $afterAt;
  elapsed = $elapsed; ports = $ports} | ConvertTo-Json -Depth 5 -Compress
"""


def _system_times():
    """Return (idle, kernel, user) seconds; Windows kernel includes idle."""
    from ctypes import wintypes

    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    try:
        function = ctypes.windll.kernel32.GetSystemTimes
        function.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
        function.restype = wintypes.BOOL
        if not function(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise OSError("GetSystemTimes failed")
    except (AttributeError, OSError) as exc:
        raise ValueError("Windows CPU counters unreadable: {}".format(exc)) from exc
    return tuple(((value.dwHighDateTime << 32) | value.dwLowDateTime) / 10000000.0
                 for value in (idle, kernel, user))


def _timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("missing creation timestamp")
    value = value.replace("Z", "+00:00")
    # .NET's round-trip date has seven fractional digits; Python 3.9's
    # fromisoformat accepts the microsecond precision emitted by Python.
    value = re.sub(r"(\.\d{6})\d+(?=[+-]\d{2}:\d{2}$)", r"\1", value)
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(datetime.timezone.utc)


def _integer(value):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError("invalid nonnegative integer")
    return int(value)


def _processes(rows, observed_at):
    if not isinstance(rows, list) or not rows:
        raise ValueError("Windows process table unreadable: empty or invalid table")
    try:
        observed = _timestamp(observed_at)
        result = []
        seen = set()
        for row in rows:
            pid, ppid = _integer(row["pid"]), _integer(row["ppid"])
            # Windows Idle has no creation date and is not a real process.
            if pid == 0 and not row.get("created"):
                continue
            created = _timestamp(row["created"])
            if pid in seen or created > observed:
                raise ValueError("duplicate PID or future creation timestamp")
            seen.add(pid)
            name = row.get("name") or ""
            command_available = isinstance(row.get("command"), str) and bool(row['command'])
            command = row["command"] if command_available else f'<command line unavailable; process name {name}>'
            if not isinstance(command, str) or not isinstance(name, str):
                raise ValueError("invalid process command or name")
            result.append({
                "pid": pid, "ppid": ppid,
                "created": created.isoformat(),
                "age_seconds": (observed - created).total_seconds(),
                "cpu_seconds": (_integer(row["kernel"]) + _integer(row["user"])) / 10000000.0,
                "command": command, "name": name, "command_available": command_available,
            })
        if not result:
            raise ValueError("no identifiable processes")
        return result
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Windows process table unreadable: {}".format(exc)) from exc


def _lines(value):
    if isinstance(value, str):
        return value.splitlines()
    if isinstance(value, list) and all(isinstance(line, str) for line in value):
        return value
    raise ValueError("invalid command output")


def _ports(raw):
    """Normalize command output; absence never masquerades as zero usage."""
    result = {"status": "unreadable", "used": 0, "range_start": 0,
              "range_size": 0, "detail": "invalid TCP port sample"}
    if not isinstance(raw, dict):
        return result
    status = raw.get("status")
    if status in ("unsupported", "unreadable"):
        result.update(status=status, detail=str(raw.get("detail") or status))
        return result
    try:
        if status != "ok":
            raise ValueError("invalid TCP port sample status")
        if "ranges" in raw:
            ranges = raw["ranges"]
            if not isinstance(ranges, list) or len(ranges) != 2:
                raise ValueError("invalid TCP address-family ranges")
            ports_by_family = {"ipv4": set(), "ipv6": set()}
            if "connections" in raw:
                if not isinstance(raw["connections"], list):
                    raise ValueError("invalid TCP connection table")
                for row in raw["connections"]:
                    address = row["LocalAddress"]
                    if not isinstance(address, str) or not address:
                        raise ValueError("invalid TCP local address")
                    family = "ipv6" if ":" in address else "ipv4"
                    ports_by_family[family].add(_integer(row["LocalPort"]))
            else:
                for line in _lines(raw["netstat_output"]):
                    fields = line.split()
                    if not fields or fields[0].upper() != "TCP":
                        continue
                    if len(fields) < 4:
                        raise ValueError("invalid netstat TCP row")
                    address, port = fields[1].rsplit(":", 1)
                    family = "ipv6" if ":" in address else "ipv4"
                    ports_by_family[family].add(_integer(port))
            normalized = []
            seen = set()
            for item in ranges:
                family = item["family"].lower()
                if family not in ports_by_family or family in seen:
                    raise ValueError("invalid TCP address family")
                seen.add(family)
                parsed = _ports({"status": "ok", "range_output": item["range_output"],
                                 "local_ports": list(ports_by_family[family])})
                if parsed["status"] != "ok":
                    raise ValueError(parsed["detail"])
                normalized.append({"family": "IPv4" if family == "ipv4" else "IPv6",
                                   "used": parsed["used"], "range_start": parsed["range_start"],
                                   "range_size": parsed["range_size"]})
            return {"status": "ok", "ranges": normalized, "detail": ""}
        # netsh labels are localized; the two numeric values retain this form.
        values = [int(match.group(1)) for line in _lines(raw["range_output"])
                  for match in [re.search(r":\s*(\d+)\s*$", line)] if match]
        if len(values) != 2:
            raise ValueError("unrecognized TCP dynamic range output")
        start, size = values
        if not (1 <= start <= 65535 and 1 <= size <= 65536 - start):
            raise ValueError("invalid TCP dynamic range")
        if "local_ports" in raw:
            if not isinstance(raw["local_ports"], list):
                raise ValueError("invalid TCP local port table")
            ports = {_integer(value) for value in raw["local_ports"]}
        else:
            ports = set()
            for line in _lines(raw["netstat_output"]):
                fields = line.split()
                if not fields or fields[0].upper() != "TCP":
                    continue
                if len(fields) < 4:
                    raise ValueError("invalid netstat TCP row")
                ports.add(_integer(fields[1].rsplit(":", 1)[1]))
        if any(port > 65535 for port in ports):
            raise ValueError("invalid TCP local port")
        result.update(status="ok", used=sum(start <= port < start + size for port in ports),
                      range_start=start, range_size=size, detail="")
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        result["detail"] = str(exc)
    return result


def sample(seconds=5.0):
    """Sample Windows process, CPU and TCP data without changing the system.

    An unreadable process table or CPU counter raises ValueError. Optional TCP
    data instead carries an explicit unsupported/unreadable status.
    """
    try:
        seconds = float(seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("sample seconds must be finite and positive") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("sample seconds must be finite and positive")
    milliseconds = max(1, math.ceil(seconds * 1000))
    if milliseconds > 2147483647:
        raise ValueError("sample seconds exceeds the Windows sleep limit")
    cpu_before = _system_times()
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             _POWERSHELL.replace("__MILLISECONDS__", str(milliseconds))],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=seconds + 60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Windows process table unreadable: {}".format(exc)) from exc
    cpu_after = _system_times()
    if completed.returncode:
        detail = (completed.stderr or "PowerShell sample failed").strip()
        raise ValueError("Windows process table unreadable: {}".format(detail))
    try:
        payload = json.loads(completed.stdout.lstrip("\ufeff"))
        elapsed = float(payload["elapsed"])
        if not math.isfinite(elapsed) or elapsed <= 0 or elapsed > time.monotonic() - started + 1:
            raise ValueError("invalid sample duration")
        before = _processes(payload["before"], payload["before_at"])
        after = _processes(payload["after"], payload["after_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Windows process table unreadable: {}".format(exc)) from exc
    idle, kernel, user = (after - before for before, after in zip(cpu_before, cpu_after))
    total = kernel + user
    if min(idle, kernel, user) < 0 or total <= 0 or idle > total:
        raise ValueError("Windows CPU counters unreadable: invalid counter delta")
    return {"platform": "win32", "before": before, "after": after, "elapsed": elapsed,
            "cpu": {"busy_seconds": total - idle, "total_seconds": total},
            "ports": _ports(payload.get("ports"))}
