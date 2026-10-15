"""Read-only machine health probes shared by box-health.py and watchdog.py.

CPU counters are sampled, never inferred from lifetime CPU time. Unix init
adoption alone does not establish an orphan: legitimate services also have
PPID 1. An observed parent exit/PID reuse is strong evidence; an init-adopted
bounded plugin hook or literal busy loop is reported as a suspected orphan.
"""
import datetime
import errno
import json
import math
import os
from pathlib import Path
import re
import socket
import statistics
import sys
import time

DEFAULT_PLUGIN = Path(__file__).resolve().parents[1]


def positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be finite and positive')
    return value


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


def linux_snapshot(proc=Path('/proc'), clock=None):
    """Read stable Linux process identities and aggregate CPU ticks from procfs."""
    proc = Path(proc)
    clock = time.monotonic if clock is None else clock
    ticks = os.sysconf('SC_CLK_TCK')
    stat_text = (proc / 'stat').read_text(encoding='ascii')
    cpu = next((line.split()[1:] for line in stat_text.splitlines() if line.startswith('cpu ')), None)
    if cpu is None or len(cpu) < 4:
        raise ValueError('/proc/stat: aggregate CPU counters unavailable')
    counters = [int(value) for value in cpu]
    # guest/guest_nice are already included in user/nice and must not be added twice.
    total = sum(counters[:8]) / ticks
    idle = (counters[3] + (counters[4] if len(counters) > 4 else 0)) / ticks
    uptime = float((proc / 'uptime').read_text(encoding='ascii').split()[0])
    boot = next((int(line.split()[1]) for line in stat_text.splitlines() if line.startswith('btime ')), None)
    if boot is None:
        raise ValueError('/proc/stat: boot time unavailable')
    rows, unreadable = [], []
    for directory in sorted(proc.iterdir(), key=lambda p: p.name):
        if not directory.name.isdigit():
            continue
        try:
            raw = (directory / 'stat').read_text(encoding='utf-8', errors='replace')
            left, right = raw.find('('), raw.rfind(')')
            if left < 0 or right < left:
                raise ValueError('malformed process stat')
            fields = raw[right + 1:].split()
            start = int(fields[19]) / ticks
            command = (directory / 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace').strip()
            name = raw[left + 1:right]
            rows.append(dict(pid=int(directory.name), ppid=int(fields[1]), name=name,
                             command=command or f'[{name}]', created=iso(boot + start),
                             created_epoch=boot + start, age_seconds=max(0., uptime-start),
                             cpu_seconds=(int(fields[11])+int(fields[12]))/ticks))
        except FileNotFoundError:
            continue  # a process exiting during enumeration is normal
        except (OSError, ValueError, IndexError) as exc:
            unreadable.append(f'PID {directory.name}: {exc}')
    if not rows:
        raise ValueError('Linux process table is empty or unreadable')
    return dict(processes=rows, cpu=dict(total_seconds=total, idle_seconds=idle),
                at=clock(), unreadable=unreadable)


def linux_ports(proc=Path('/proc')):
    """Distinct locally occupied TCP ports in the configured dynamic range."""
    proc = Path(proc)
    try:
        start, end = map(int, (proc / 'sys/net/ipv4/ip_local_port_range').read_text().split())
        if not 0 < start <= end <= 65535:
            raise ValueError('invalid dynamic port range')
        ports, tables = set(), 0
        for filename in ('tcp', 'tcp6'):
            path = proc / 'net' / filename
            if not path.exists() and filename == 'tcp6':
                continue  # IPv6 can be disabled; the IPv4 table still covers IPv4.
            lines = path.read_text(encoding='ascii').splitlines()
            if not lines or 'local_address' not in lines[0]:
                raise ValueError(f'{path}: invalid TCP table header')
            tables += 1
            for line in lines[1:]:
                if not line.strip():
                    continue
                local = line.split()[1]
                port = int(local.rsplit(':', 1)[1], 16)
                if start <= port <= end:
                    ports.add(port)
        if not tables:
            raise ValueError('no TCP socket table available')
        return dict(status='ok', ranges=[dict(family='IPv4/IPv6', used=len(ports),
                    range_start=start, range_size=end-start+1)],
                    detail='distinct local TCP ports, including TIME_WAIT and listeners; pressure estimate, not a free-tuple guarantee')
    except (OSError, ValueError, IndexError) as exc:
        return dict(status='unreadable', detail=str(exc))


def linux_sample(seconds):
    before = linux_snapshot()
    time.sleep(seconds)
    after = linux_snapshot()
    total = after['cpu']['total_seconds']-before['cpu']['total_seconds']
    idle = after['cpu']['idle_seconds']-before['cpu']['idle_seconds']
    return dict(platform='linux', before=before['processes'], after=after['processes'],
                elapsed=after['at']-before['at'], cpu=dict(total_seconds=total, busy_seconds=total-idle),
                unreadable=before['unreadable']+after['unreadable'], ports=linux_ports())


def sample_processes(seconds):
    if sys.platform.startswith('linux'):
        return linux_sample(seconds)
    if sys.platform == 'win32':
        from box_health_windows import sample
        return sample(seconds)
    raise NotImplementedError(f'process sampling is unsupported on {sys.platform}; supported platforms: Linux, Windows')


def loopback_probe(limit_ms=50., attempts=5):
    """Use only an ephemeral 127.0.0.1 listener, then close every socket."""
    sockets = []
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sockets.append(listener)
        listener.settimeout(max(2., limit_ms/1000*2))
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sockets.append(client)
        client.settimeout(max(2., limit_ms/1000*2))
        client.connect(listener.getsockname())
        server, _ = listener.accept()
        sockets.append(server)
        server.settimeout(max(2., limit_ms/1000*2))
        values = []
        for _ in range(attempts+1):  # warm the connection before timing it
            began = time.perf_counter()
            client.sendall(b'x')
            if server.recv(1) != b'x':
                raise OSError('loopback request was truncated')
            server.sendall(b'x')
            if client.recv(1) != b'x':
                raise OSError('loopback response was truncated')
            values.append((time.perf_counter()-began)*1000)
        values = values[1:]
        median = statistics.median(values)
        return dict(status='unhealthy' if median >= limit_ms else 'ok', medianMs=median,
                    maxMs=max(values), attempts=attempts, thresholdMs=limit_ms)
    except (socket.timeout, TimeoutError) as exc:
        return dict(status='unhealthy', detail=f'127.0.0.1 round trip timed out: {exc}')
    except OSError as exc:
        status = ('unsupported' if exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT)
                  else 'unreadable' if exc.errno in (errno.EACCES, errno.EPERM) else 'unhealthy')
        return dict(status=status, detail=f'127.0.0.1 probe failed: {exc}')
    finally:
        for sock in reversed(sockets):
            sock.close()


def hook_timeout(plugin_root=DEFAULT_PLUGIN):
    path = Path(plugin_root) / 'hooks/hooks.json'
    document = json.loads(path.read_text(encoding='utf-8'))
    values = []
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == 'timeout' and type(child) in (int, float) and math.isfinite(child) and child > 0:
                    values.append(child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(document)
    if not values:
        raise ValueError(f'{path}: no positive hook timeout found')
    return max(values)


def created_epoch(row):
    if type(row.get('created_epoch')) in (int, float):
        return row['created_epoch']
    try:
        return datetime.datetime.fromisoformat(row['created'].replace('Z', '+00:00')).timestamp()
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def plugin_process(row, plugin_root):
    name = row.get('name', '').lower()
    command = row.get('command', '')
    if not (name.startswith('python') or re.search(r'(?i)(?:^|[/\\\s"\'])python(?:\d+(?:\.\d+)*)?(?:\.exe)?(?:["\']?\s)', command)):
        return False
    normalized = command.replace('\\', '/').lower()
    root = str(plugin_root).replace('\\', '/').rstrip('/').lower()
    return ((root + '/scripts/') in normalized and '.py' in normalized
            or bool(re.search(r'/(?:fluxpoint|substrate)/(?:[^/\s"\']+/)?scripts/[^\s"\']+\.py\b', normalized)))


def parent_state(row, previous, current):
    """Identify a missing/reused parent without labeling ordinary init services."""
    ppid = row['ppid']
    if ppid <= 0:
        return None
    parent = current.get(ppid)
    child_birth = created_epoch(row)
    parent_birth = created_epoch(parent or {})
    if parent and child_birth is not None and parent_birth is not None and parent_birth > child_birth:
        return 'original parent PID was reused after this process started'
    if ppid > 1 and parent is None:
        return f'parent PID {ppid} is absent from the process table'
    before = previous.get(row['pid'])
    if ppid == 1 and before and before.get('created') == row.get('created') and before['ppid'] > 1:
        return f'parent PID {before["ppid"]} disappeared during the sample; process was adopted by init'
    return None


def analyze(sample, cpu_threshold=90., orphan_cpu_threshold=90., port_threshold=80.,
            plugin_root=DEFAULT_PLUGIN, plugin_timeout=600.):
    elapsed = positive(sample.get('elapsed'), 'sample elapsed time')
    def process_map(rows):
        if not isinstance(rows, list):
            raise ValueError('process table must be a list')
        mapped = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('process table row must be an object')
            for key in ('pid', 'ppid'):
                if type(row.get(key)) is not int or row[key] < 0:
                    raise ValueError(f'process row has invalid {key}')
            for key in ('cpu_seconds', 'age_seconds'):
                value = row.get(key)
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError(f'PID {row["pid"]}: {key} unavailable')
            if (not isinstance(row.get('command'), str) or not isinstance(row.get('created'), str)
                    or created_epoch(row) is None):
                raise ValueError('process row lacks a readable command or creation time')
            if row['pid'] in mapped:
                raise ValueError(f'duplicate process PID {row["pid"]}')
            mapped[row['pid']] = row
        return mapped
    before = process_map(sample['before'])
    after = process_map(sample['after'])
    if not before or not after:
        raise ValueError('process table is empty or unreadable')
    findings, checks, top = [], {}, []
    unreadable = list(sample.get('unreadable', []))
    for row in after.values():
        old = before.get(row['pid'])
        percent = None
        if old and old.get('created') == row['created']:
            delta = row['cpu_seconds']-old['cpu_seconds']
            if delta < 0:
                unreadable.append(f'PID {row["pid"]}: CPU counter moved backwards')
            else:
                percent = delta/elapsed*100
        evidence = {key: row.get(key) for key in ('pid', 'ppid', 'name', 'command', 'created', 'age_seconds')}
        evidence['cpuPercentOfCore'] = percent
        if percent is not None:
            top.append(evidence)
        orphan = parent_state(row, before, after)
        adopted = row['ppid'] == 1 and sample.get('platform', sys.platform).startswith('linux')
        literal_busy = bool(re.search(r'(?i)(?:-c\s+["\']?)?while\s+(?:True|1)\s*:\s*pass\b', row['command']))
        known_hook = bool(re.search(r"(?i)(?:secret-guard\.py[\"']?\s+[\"']?--hook|attest\.py[\"']?\s+[\"']?--record)", row['command']))
        suspected = adopted and (literal_busy or known_hook)
        reason = orphan or ('init-adopted bounded hook or literal busy loop; original parent identity is no longer available' if suspected else None)
        if reason and percent is not None and percent >= orphan_cpu_threshold:
            findings.append(dict(code='orphan-cpu', message=f'PID {row["pid"]} used {percent:.1f}% of a core; {reason}', processes=[evidence]))
        detached_gate = bool(re.search(r'(?i)attest\.py\b.*(?:--run|--ci)\b', row['command']))
        if reason and not detached_gate and plugin_process(row, plugin_root) and row.get('age_seconds', 0) > plugin_timeout:
            findings.append(dict(code='orphan-plugin', message=f'PID {row["pid"]} outlived the longest hook timeout ({plugin_timeout:g}s); {reason}', processes=[evidence]))
    top.sort(key=lambda p: (-(p['cpuPercentOfCore'] or 0), p['pid']))
    cpu = sample.get('cpu', {})
    total, busy = cpu.get('total_seconds'), cpu.get('busy_seconds')
    if (type(total) not in (int, float) or type(busy) not in (int, float)
            or not math.isfinite(total) or not math.isfinite(busy) or total <= 0 or busy < 0 or busy > total):
        checks['cpu'] = dict(status='unreadable', detail='aggregate CPU counters unavailable or inconsistent')
    else:
        percent = busy/total*100
        checks['cpu'] = dict(status='ok', percent=percent, threshold=cpu_threshold, topProcesses=top[:5])
        if percent >= cpu_threshold:
            findings.append(dict(code='cpu-saturated', message=f'total CPU {percent:.1f}% >= {cpu_threshold:g}% over {elapsed:.2f}s', processes=top[:5]))
    checks['processes'] = dict(status='unreadable' if unreadable else 'ok', count=len(after),
                              detail='; '.join(unreadable[:20]) if unreadable else 'stable PID/creation-time identities; ordinary init children are not presumed orphaned')
    ports = sample.get('ports', dict(status='unreadable', detail='TCP port occupancy unavailable'))
    checks['ports'] = ports
    if ports.get('status') not in ('ok', 'unreadable', 'unsupported'):
        checks['ports'] = dict(status='unreadable', detail='TCP port coverage has unknown status')
    if ports.get('status') == 'ok':
        ranges = ports.get('ranges', [ports])
        if not isinstance(ranges, list) or not ranges:
            checks['ports'] = dict(status='unreadable', detail='dynamic TCP range list unavailable')
            ranges = []
        for space in ranges:
            if not isinstance(space, dict):
                checks['ports'] = dict(status='unreadable', detail='invalid dynamic TCP range')
                break
            used, size = space.get('used'), space.get('range_size')
            if type(used) is not int or type(size) is not int or used < 0 or size <= 0 or used > size:
                checks['ports'] = dict(status='unreadable', detail='invalid dynamic TCP range or occupancy')
                break
            percent = used/size*100
            space['percent'] = percent
            space['threshold'] = port_threshold
            if percent >= port_threshold:
                findings.append(dict(code='ports-pressured', message=f'{space.get("family", "TCP")} dynamic TCP ports {used}/{size} ({percent:.1f}%) >= {port_threshold:g}%', processes=[]))
    return dict(sampleSeconds=elapsed, findings=findings, checks=checks)


def finish_report(report):
    unknown = [(name, check) for name, check in report['checks'].items()
               if check.get('status') in ('unreadable', 'unsupported')]
    # A process-table gap prevents reliable orphan diagnosis. Other unknown
    # checks also remain explicit, never a green result with missing coverage.
    code = 2 if unknown else 1 if report['findings'] else 0
    report.update(exitCode=code, status='unreadable' if any(c.get('status') == 'unreadable' for _, c in unknown)
                  else 'unsupported' if unknown else 'unhealthy' if code == 1 else 'healthy')
    return report


def check_health(sample_seconds=5., cpu_threshold=90., orphan_cpu_threshold=90., port_threshold=80.,
                 loopback_ms=50., plugin_root=None, plugin_timeout=None, sampler=None, loopback=None):
    for name, value in [('sample seconds', sample_seconds), ('CPU threshold', cpu_threshold),
                        ('orphan CPU threshold', orphan_cpu_threshold), ('port threshold', port_threshold), ('loopback milliseconds', loopback_ms)]:
        positive(value, name)
    if sample_seconds > 60 or cpu_threshold > 100 or port_threshold > 100:
        raise ValueError('sample must be at most 60 seconds; aggregate CPU and port thresholds at most 100%')
    root = DEFAULT_PLUGIN if plugin_root is None else Path(plugin_root)
    try:
        timeout = hook_timeout(root) if plugin_timeout is None else positive(plugin_timeout, 'plugin timeout')
        sample = (sample_processes if sampler is None else sampler)(sample_seconds)
        report = analyze(sample, cpu_threshold, orphan_cpu_threshold, port_threshold, root, timeout)
        report['pluginTimeoutSeconds'] = timeout
    except NotImplementedError as exc:
        report = dict(findings=[], checks=dict(processes=dict(status='unsupported', detail=str(exc))))
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError) as exc:
        report = dict(findings=[], checks=dict(processes=dict(status='unreadable', detail=str(exc))))
    probe = (loopback_probe if loopback is None else loopback)(loopback_ms)
    if not isinstance(probe, dict) or probe.get('status') not in ('ok', 'unhealthy', 'unreadable', 'unsupported'):
        probe = dict(status='unreadable', detail='loopback probe returned unknown coverage')
    report['checks']['loopback'] = probe
    if probe.get('status') == 'unhealthy':
        detail = probe.get('detail') or f'127.0.0.1 median round trip {probe["medianMs"]:.2f}ms >= {loopback_ms:g}ms'
        report['findings'].append(dict(code='loopback-slow', message=detail, processes=[]))
    report['requestedSampleSeconds'] = sample_seconds
    return finish_report(report)


def format_report(report):
    lines = [f'box-health: {report["status"]} (exit {report["exitCode"]}); sample {report.get("sampleSeconds", 0):.2f}s']
    for name, check in report['checks'].items():
        if check.get('status') in ('unreadable', 'unsupported'):
            lines.append(f'  {name}: {check["status"]}: {check.get("detail", "no detail")}')
    for finding in report['findings']:
        lines.append(f'  {finding["code"]}: {finding["message"]}')
        for process in finding.get('processes', []):
            percent = process.get('cpuPercentOfCore')
            cpu = f'{percent:.1f}%' if type(percent) in (int, float) else 'unavailable'
            lines.append(f'    PID {process["pid"]} PPID {process["ppid"]} created {process["created"]} '
                         f'CPU/core {cpu}; command {json.dumps(process["command"], ensure_ascii=False)}')
    if not report['findings'] and report['exitCode'] == 0:
        cpu = report['checks']['cpu']['percent']
        loop = report['checks']['loopback'].get('medianMs')
        lines.append(f'  total CPU {cpu:.1f}%; loopback median {loop:.3f}ms' if loop is not None else f'  total CPU {cpu:.1f}%')
    lines.append('  Read-only: no process was killed and no machine configuration was changed.')
    return '\n'.join(lines)
