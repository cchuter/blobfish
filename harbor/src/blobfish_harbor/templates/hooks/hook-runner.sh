#!/bin/sh
script="$0.py"

for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1; then
    "$py" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info[0] >= 3 else 1)
PY
    if [ "$?" -eq 0 ]; then
      exec "$py" "$script"
    fi
  fi
done

exit 0
