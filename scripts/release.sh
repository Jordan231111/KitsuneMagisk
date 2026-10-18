#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
case "${1:-}" in
  verify|metadata)
    shift
    exec scripts/env.py python3 tools/release.py verify "$@"
    ;;
  inventory)
    shift
    exec scripts/env.py python3 tools/release.py inventory "$@"
    ;;
  tag-name)
    scripts/env.py python3 tools/build_info.py --release-tag
    ;;
  *)
    echo "Usage: $0 verify --acceptance FILE [--artifacts DIR] [--output FILE]" >&2
    echo "       $0 inventory [--output FILE] | tag-name" >&2
    exit 1
    ;;
esac
