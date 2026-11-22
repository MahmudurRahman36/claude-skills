#!/bin/sh
# Launcher for guard.py (harness-builder {{VERSION}}). Fails closed: unless guard.py runs and answers
# "allow", the tool call is blocked (exit 2). Tries python3, python, then py, so a harness built on
# one OS still works on another, and a broken or missing interpreter blocks instead of allowing.
input=$(cat)
dir=$(dirname "$0")
for py in python3 python py; do
  command -v "$py" >/dev/null 2>&1 || continue
  # skip stubs (e.g. the Windows Store "python3" alias) and Python 2 quietly
  "$py" -c "import sys; sys.exit(sys.version_info < (3, 8))" >/dev/null 2>&1 || continue
  out=$(printf '%s' "$input" | "$py" "$dir/guard.py")
  rc=$?
  if [ "$rc" -eq 0 ] && [ "$out" = "allow" ]; then exit 0; fi
  if [ "$rc" -eq 2 ]; then exit 2; fi
done
echo "harness guard: no working Python could run guard.py, so the call is blocked" >&2
exit 2
