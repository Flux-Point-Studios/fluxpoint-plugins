#!/usr/bin/env bash
# Sourced by hook launchers. Cache a working absolute interpreter against PATH,
# so each Bash tool call starts Python once rather than probing twice first.
# A cache miss still RUNS candidates: Windows App Execution Aliases exist but
# are not interpreters. WindowsApps aliases can be rejected without spawning.
if [ -z "${FPL_PY:-}" ]; then
  _fpl_cache_root="${XDG_CACHE_HOME:-${HOME:-${TMPDIR:-/tmp}}/.cache}/fluxpoint"
  _fpl_cache="$_fpl_cache_root/python"
  _fpl_saved_path= _fpl_saved_py=
  if [ -f "$_fpl_cache" ] && [ -O "$_fpl_cache" ]; then
    { IFS= read -r _fpl_saved_path; IFS= read -r _fpl_saved_py; } <"$_fpl_cache"
    if [ "$_fpl_saved_path" = "$PATH" ] && [ -x "$_fpl_saved_py" ]; then
      FPL_PY="$_fpl_saved_py"
    fi
  fi
  if [ -z "${FPL_PY:-}" ]; then
    for _fpl_cand in python3 python; do
      _fpl_path="$(command -v "$_fpl_cand" 2>/dev/null)" || continue
      case "$_fpl_path" in *WindowsApps*|*windowsapps*) continue ;; esac
      if "$_fpl_path" -c "import sys" >/dev/null 2>&1; then
        FPL_PY="$_fpl_path"
        # Cache writes are best effort and atomic. A private user directory
        # avoids trusting an interpreter path in a shared temporary file.
        (umask 077
         mkdir -p "$_fpl_cache_root" 2>/dev/null &&
         printf '%s\n%s\n' "$PATH" "$FPL_PY" >"$_fpl_cache.$$" &&
         mv -f "$_fpl_cache.$$" "$_fpl_cache") 2>/dev/null || true
        break
      fi
    done
  fi
  unset _fpl_cache_root _fpl_cache _fpl_saved_path _fpl_saved_py _fpl_cand _fpl_path
  if [ -z "${FPL_PY:-}" ]; then
    echo "fluxpoint: no working python interpreter on PATH" >&2
    exit 127
  fi
fi
export FPL_PY PYTHONIOENCODING=utf-8
