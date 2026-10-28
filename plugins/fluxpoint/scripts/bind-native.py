#!/usr/bin/env python3
"""Corroborate a completed fixed-command Workflow agent from its native capture."""
import argparse
import json
from pathlib import Path
import sys

import native_binding


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--agent-id')
    mode.add_argument('--audit', help='graph-audit result requiring native probe binding')
    parser.add_argument('--projects')
    parser.add_argument('--report', help='optional exact copy to compare with native StructuredOutput')
    args = parser.parse_args()
    try:
        report = json.loads(Path(args.report).read_text(encoding='utf-8')) if args.report else None
        if args.audit:
            if report is not None:
                raise ValueError('--report applies to a fixed-reading agent, not an audit')
            result = native_binding.bind_audit(args.run_id, json.loads(Path(args.audit).read_text(encoding='utf-8')), args.projects)
        else:
            result = native_binding.bind(args.run_id, args.agent_id, args.projects, report)
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps(dict(bound=False, error=str(exc))))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
