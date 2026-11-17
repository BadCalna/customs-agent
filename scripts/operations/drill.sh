#!/usr/bin/env bash
# Backup -> isolated restore -> verify. Failure retains isolation and evidence.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
operations_main drill "$@"
