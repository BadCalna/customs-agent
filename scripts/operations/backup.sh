#!/usr/bin/env bash
# Backup production into a private COMPLETE-marked directory; no destructive action.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
operations_main backup "$@"
