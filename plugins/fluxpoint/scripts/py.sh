#!/usr/bin/env bash
# Run one of this directory's Python scripts portably.
#
#   py.sh compile-graph.py WORK.md --check
#
# The commands and agents invoke these scripts as literal instructions rather
# than through lib.sh, so they cannot pick up a shell-resolved interpreter.
# Two things have to be settled before the interpreter starts, and this is the
# only shared point where both can be:
#
#   * the name — `python3` does not exist on a standard Windows install
#   * stdio encoding — Windows writes cp1252, so a non-ASCII character in any
#     output a caller parses comes back as a byte that no UTF-8 match sees
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: py.sh <script.py> [args...]" >&2
  exit 64
fi

if [ -z "${FPL_PY:-}" ]; then
  # Probe each candidate by RUNNING it, rather than asking whether the name
  # exists. Windows ships a `python3` App Execution Alias that is on PATH by
  # default on a machine with no python3 at all: it satisfies `command -v`,
  # then prints "Python was not found" and exits 49 for every argument.
  for _fpl_cand in python3 python; do
    if command -v "$_fpl_cand" >/dev/null 2>&1 &&
       "$_fpl_cand" -c "import sys" >/dev/null 2>&1; then
      FPL_PY="$_fpl_cand"
      break
    fi
  done
  unset _fpl_cand
  if [ -z "${FPL_PY:-}" ]; then
    echo "fluxpoint: no working python interpreter on PATH" >&2
    exit 127
  fi
fi
export PYTHONIOENCODING=utf-8
# The bash this runs under, for a script that has to start one (attest.py
# --run). On Windows a bare "bash" resolves through System32 first, which is
# WSL's where it is installed, not the Git Bash every hook runs in.
if [ -z "${FPL_BASH:-}" ] && [ -n "${BASH:-}" ]; then
  if command -v cygpath >/dev/null 2>&1; then
    FPL_BASH="$(cygpath -w "$BASH" 2>/dev/null || printf '%s' "$BASH")"
  else
    FPL_BASH="$BASH"
  fi
  export FPL_BASH
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="$here/$1"
if [ ! -f "$target" ]; then
  echo "fluxpoint: no such script: $1" >&2
  exit 66
fi
shift
exec "$FPL_PY" "$target" "$@"
