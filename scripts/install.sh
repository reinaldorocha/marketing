#!/usr/bin/env bash
# Can also be piped from the repository's raw main URL on a prepared Linux VPS.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]:-/dev/stdin}")/.." && pwd)"
if [[ ! -f "$root/deploy/vps.py" ]]; then
  root="${MARKETING_INSTALL_DIR:-/opt/marketing}"
  if [[ ! -d "$root/.git" ]]; then
    git clone --branch main --single-branch https://github.com/reinaldorocha/marketing.git "$root"
  fi
fi
exec python3 "$root/deploy/vps.py" install "$@"
