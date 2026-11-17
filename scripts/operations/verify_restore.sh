#!/usr/bin/env bash
# Verify --target-db / --target-files; validate actual identities, schema and hashes.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
operations_main verify_restore "$@"
