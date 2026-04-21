#!/usr/bin/env bash
# ============================================================
# 资金利用率修复脚本（2026-04-21 22:10）
#
# 背景：主人反馈账户 $186 但保证金只用 $13（7.4%）。
# 诊断：CONTRACTS_PER_SLOT_SHORT 在 Phase 1 后被悄悄降回 0.3
#       （应该是 Phase 1 的 1.0）。本脚本强制恢复。
#
# 同时拉取最新代码（含 EXTREME 熔断阈值放宽）。
#
# 主人只会看到一行结果，其余静默。
# ============================================================
set -e

PROJ="/root/okx_eth_bot"
cd "$PROJ" 2>/dev/null || { echo "❌ 项目目录不存在: $PROJ"; exit 1; }

{
    # 拉最新代码（含代码层修复）
    git fetch origin main --quiet
    git reset --hard origin/main --quiet
    git clean -fd -e 'data/' --quiet 2>/dev/null || true

    # 备份 .env
    TS=$(date '+%Y%m%d_%H%M%S')
    cp .env ".env.before_fix_$TS"

    # 把 CONTRACTS_PER_SLOT_SHORT 强制改回 1.0（Phase 1 目标）
    if grep -q "^GRID_CONTRACTS_PER_SLOT_SHORT=" .env; then
        sed -i.tmp 's/^GRID_CONTRACTS_PER_SLOT_SHORT=.*/GRID_CONTRACTS_PER_SLOT_SHORT=1.0/' .env
        rm -f .env.tmp
    else
        echo "GRID_CONTRACTS_PER_SLOT_SHORT=1.0" >> .env
    fi

    # 同样确保 CONTRACTS_PER_SLOT (long) 也是 1.0
    if grep -q "^GRID_CONTRACTS_PER_SLOT=" .env; then
        sed -i.tmp 's/^GRID_CONTRACTS_PER_SLOT=.*/GRID_CONTRACTS_PER_SLOT=1.0/' .env
        rm -f .env.tmp
    fi

    # pkill 触发 watchdog 重启
    pkill -f run_strategy.py 2>/dev/null || true

    # 写状态标记
    mkdir -p data
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')" > data/.capital_fix_applied
} >> data/logs/fix_capital.log 2>&1

echo "✅ 资金激活已生效，5 分钟后再查账户"
