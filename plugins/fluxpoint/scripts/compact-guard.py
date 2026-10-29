#!/usr/bin/env python3
"""Compile pinned Compact circuits, check native proof controls and ratchet artifacts/costs.

--baseline records the first measurement. Replacing an existing baseline needs
--accept --reason. --check executes the complete native adapter, never a cached
or simulated proof. Toolchains and proving parameters are installed separately.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import compact_native as compact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    parser.add_argument('--config', default=compact.CONFIG)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--baseline', action='store_true')
    action.add_argument('--check', action='store_true')
    action.add_argument('--report', action='store_true')
    action.add_argument('--tighten', action='store_true')
    parser.add_argument('--accept', action='store_true')
    parser.add_argument('--reason', default='')
    parser.add_argument('--require-armed', action='store_true', help='refuse a missing native manifest even when no tracked stateful/private source is detected')
    args = parser.parse_args()
    try:
        root = Path(args.root).resolve()
        path = root / compact.BASELINE
        if args.require_armed and not (root / args.config).exists():
            raise ValueError('native Compact proof check is NOT ARMED; configure ' + args.config)
        if args.report:
            print(path.read_text(encoding='utf-8') if path.exists() else 'compact-guard: NOT ARMED')
            return 0
        if not (root / args.config).exists():
            if args.check:
                if compact.tracked_native_sources(root):
                    raise ValueError('stateful/private Compact source is tracked but the native adapter is NOT ARMED; configure ' + args.config)
                return 0
            raise ValueError('no Compact manifest; configure the real native adapter first')
        cfg = compact.load(root, args.config)
        checked = None
        if args.tighten:
            from ratchet import green
            checked = green(str(root))
        measured = compact.measure(root, cfg)
        prior = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        if args.check:
            if prior is None: raise ValueError('Compact manifest is present but the native ratchet is NOT ARMED')
            findings = compact.compare(prior, measured)
            if findings: raise ValueError('; '.join(findings))
            print('compact-guard: native controls, relations, artifact pins and cost ceilings pass')
            return 0
        if prior is not None and not args.tighten:
            if not args.accept or len(args.reason.strip()) < 20:
                raise ValueError('replacing a native baseline needs --accept --reason with at least 20 characters')
            measured['acceptedChange'] = {'reason': args.reason.strip(), 'priorSha256': compact.relation(prior)}
        if args.tighten:
            if prior is None: raise ValueError('--tighten requires an existing native baseline')
            findings = compact.compare(prior, measured)
            if findings: raise ValueError('--tighten refused: ' + '; '.join(findings))
            from ratchet import clean
            clean(str(root), checked)
        measured['toolchain'] = {'compiler': cfg['compilerVersion'], 'compilerLedger': cfg['compilerLedger'], 'runtime': cfg['runtimeVersion']}
        path.write_text(json.dumps(measured, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        print('compact-guard: native baseline recorded: ' + str(path))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print('compact-guard: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
