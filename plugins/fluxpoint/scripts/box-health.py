#!/usr/bin/env python3
"""Read-only process, CPU, loopback and dynamic TCP port health check.

Exit 0: healthy; 1: unhealthy; 2: unreadable or unsupported coverage.
"""
import argparse
import json
import sys

from box_health import check_health, format_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--sample-seconds', type=float, default=5.)
    parser.add_argument('--cpu-threshold', type=float, default=90.)
    parser.add_argument('--orphan-cpu-threshold', type=float, default=90.)
    parser.add_argument('--port-threshold', type=float, default=80.)
    parser.add_argument('--loopback-ms', type=float, default=50.)
    parser.add_argument('--plugin-root')
    parser.add_argument('--plugin-timeout', type=float)
    args = parser.parse_args()
    try:
        report = check_health(sample_seconds=args.sample_seconds, cpu_threshold=args.cpu_threshold,
                              orphan_cpu_threshold=args.orphan_cpu_threshold, port_threshold=args.port_threshold,
                              loopback_ms=args.loopback_ms, plugin_root=args.plugin_root, plugin_timeout=args.plugin_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))
    return report['exitCode']


if __name__ == '__main__':
    sys.exit(main())
