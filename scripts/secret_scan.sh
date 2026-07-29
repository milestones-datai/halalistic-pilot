#!/usr/bin/env bash
# Local secret-scan wrapper. CI also runs gitleaks in the pr-test
# workflow; this is for the developer's pre-push sanity check.
#
# Requires: gitleaks (https://github.com/gitleaks/gitleaks). On macOS:
#   brew install gitleaks
# On Linux:
#   apt-get install gitleaks   # (or download from GitHub releases)
# On Windows:
#   scoop install gitleaks
#
# Usage:
#   ./scripts/secret_scan.sh                 # scan working tree
#   ./scripts/secret_scan.sh --staged        # only what's about to be committed
#   ./scripts/secret_scan.sh --history       # full git history (slower)
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:---working}"
ARGS=(detect --source . --config .gitleaks.toml --no-banner)

case "$MODE" in
  --staged)   ARGS+=(--staged --redact) ;;
  --history)  ARGS+=(--log-opts="--all") ;;
  --working)  ;;  # default
  *) echo "usage: $0 [--staged|--history|--working]"; exit 2 ;;
esac

gitleaks "${ARGS[@]}"
echo "OK: no secrets detected ($MODE)"
