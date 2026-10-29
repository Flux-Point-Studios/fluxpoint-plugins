#!/usr/bin/env python3
"""Report project/local worktree.baseRef without passing or refusing a launch."""
import argparse
import json
from worktree_settings import describe, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    value = report(args.root)
    print(json.dumps(value, sort_keys=True) if args.json else describe(value))
    return 0


if __name__ == '__main__': raise SystemExit(main())
