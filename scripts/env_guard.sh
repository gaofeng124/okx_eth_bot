#!/usr/bin/env bash
# ============================================================
# .env 参数守护进程（L10-004 续）
#
# 问题：daemon 多次悄悄降级 CPS（1.0 → 0.5 → 0.3），resource 浪费
# 方案：关键参数写在 data/.env_ground_truth，每 2 分钟对比 .env
#       若 .env 偏离 ground_truth → 强制恢复 + 写异常日志 + 触发邮件
#
# 用法（cron）:
#   */2 * * * * /root/okx_eth_bot/scripts/env_guard.sh
# ============================================================
set -e
PROJ="/root/okx_eth_bot"
cd "$PROJ" 2>/dev/null || exit 0

LOCK="$PROJ/data/.env_ground_truth"
[ -f "$LOCK" ] || exit 0  # 没锁定文件就不管

LOG="$PROJ/data/logs/env_guard.log"
mkdir -p "$(dirname "$LOG")"

CHANGED=0
while IFS='=' read -r key expected; do
    [ -z "$key" ] && continue
    [ "${key:0:1}" = "#" ] && continue

    actual=$(grep -E "^${key}=" "$PROJ/.env" 2>/dev/null | head -1 | cut -d= -f2-)
    if [ "$actual" != "$expected" ]; then
        echo "[$(date '+%H:%M:%S')] ⚠️ $key drifted: '$actual' → restoring '$expected'" >> "$LOG"
        sed -i.tmp "s|^${key}=.*|${key}=${expected}|" "$PROJ/.env"
        rm -f "$PROJ/.env.tmp"
        CHANGED=1
    fi
done < "$LOCK"

# 若有恢复 → 重启 strategy 让新参数生效
if [ $CHANGED -eq 1 ]; then
    echo "[$(date '+%H:%M:%S')] 触发 pkill run_strategy.py（参数已恢复）" >> "$LOG"
    pkill -f run_strategy.py 2>/dev/null || true
fi
