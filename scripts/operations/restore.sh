#!/usr/bin/env bash
# Restore to isolated *_restore_* defaults; --target-db / --target-files override.
# 目标库已存在则拒绝；本脚本不覆盖。Result JSON; never writes backup media.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
operations_main restore "$@"
