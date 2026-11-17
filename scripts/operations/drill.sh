#!/usr/bin/env bash
# P22 备份恢复演练：备份 → 恢复到隔离位置 → 逐项比对 → 清理。
#
# 这条链路是“备份真的能恢复”的唯一证明；只做备份不做恢复演练，等于没有备份。
#
# 用法：
#   scripts/operations/drill.sh                 # 完整演练，结束后清理隔离目标
#   scripts/operations/drill.sh --keep          # 保留恢复出来的库与目录，便于手工检查
#   scripts/operations/drill.sh --no-quiesce    # 演练时不停写（演示库可用）
#
# 演练的安全边界：
#   - 恢复目标是新库 + 新目录，不覆盖业务库，也不覆盖原件目录；
#   - 演练不删除任何备份；
#   - 清理只删本次演练新建的库与目录。
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

KEEP=0
QUIESCE_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP=1; shift ;;
    --no-quiesce) QUIESCE_ARGS+=(--no-quiesce); shift ;;
    *) die "未知参数：$1" ;;
  esac
done

log "===== 1/4 备份 ====="
BACKUP_DIR="$(BACKUP_MODE="$BACKUP_MODE" "$OPS_DIR/backup.sh" --label drill ${QUIESCE_ARGS[@]+"${QUIESCE_ARGS[@]}"} | tail -1)"
[ -d "$BACKUP_DIR" ] || die "备份目录未生成"
log "备份目录：$BACKUP_DIR"

log "===== 2/4 恢复到隔离目标 ====="
RESTORE_LOG="$("$OPS_DIR/restore.sh" "$BACKUP_DIR" 2>&1)"
printf '%s\n' "$RESTORE_LOG"
RESTORE_OUT="$(printf '%s\n' "$RESTORE_LOG" | tail -2)"
TARGET_DB="$(printf '%s\n' "$RESTORE_OUT" | head -1)"
TARGET_FILES="$(printf '%s\n' "$RESTORE_OUT" | tail -1)"
[ -n "$TARGET_DB" ] && [ -n "$TARGET_FILES" ] || die "没能从恢复脚本拿到目标库/目录"
log "隔离目标：库=$TARGET_DB 目录=$TARGET_FILES"

log "===== 3/4 比对 ====="
VERIFY_STATUS=0
if ! "$OPS_DIR/verify_restore.sh" "$BACKUP_DIR" \
      --target-db "$TARGET_DB" --target-files "$TARGET_FILES"; then
  VERIFY_STATUS=1
fi

log "===== 4/4 清理 ====="
if [ "$KEEP" = 1 ]; then
  log "按要求保留：库=${TARGET_DB} 目录=${TARGET_FILES}（检查完请手工删除）"
elif [ "$BACKUP_MODE" = "compose" ]; then
  dc exec -T db dropdb -U "$(env_get CUSTOMS_PG_USER customs_agent)" --if-exists "$TARGET_DB" || warn "删库失败，请手工处理：$TARGET_DB"
  dc run --rm --no-deps -T app sh -c "rm -rf '$TARGET_FILES'" || warn "删目录失败，请手工处理：$TARGET_FILES"
  log "已清理隔离目标"
else
  if [ -n "${CUSTOMS_PG_PASSWORD:-}" ]; then export PGPASSWORD="$CUSTOMS_PG_PASSWORD"; fi
  dropdb $(pg_native_admin_args) --if-exists "$TARGET_DB" || warn "删库失败，请手工处理：$TARGET_DB"
  rm -rf "$TARGET_FILES" || warn "删目录失败，请手工处理：$TARGET_FILES"
  log "已清理隔离目标"
fi

echo
if [ "$VERIFY_STATUS" = 0 ]; then
  log "演练结果：通过（备份可恢复，行数与文件哈希一致）"
else
  die "演练结果：失败（见上面的差异明细；备份目录保留在 $BACKUP_DIR 供排查）"
fi
