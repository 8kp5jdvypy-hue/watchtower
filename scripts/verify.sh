#!/usr/bin/env bash
# Run every deterministic local gate required for a repository-wide handoff.
# Override PYTHON_BIN when the system python is older than the required 3.11.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 11):
    version = ".".join(str(part) for part in sys.version_info[:3])
    raise SystemExit(
        f"Python 3.11+ is required; {version} is active. "
        "Set PYTHON_BIN to a supported virtual-environment interpreter."
    )
PY

cd "$REPO_ROOT"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" -m pytest -q -p no:cacheprovider

(
  cd "$REPO_ROOT/web-app"
  npm run test:unit
  npm run lint
  npm run build
)

(
  cd "$REPO_ROOT/web"
  npm run lint
  npm run build
)

git diff --check
echo "Verification complete."
