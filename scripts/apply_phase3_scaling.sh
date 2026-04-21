#!/usr/bin/env bash
# ============================================================
# Phase 3 规模放大（主人批准 B 激进版，自动/手动均可）
# 触发条件：Phase 1+2 稳定 ≥ 90min + EV ≥ +0.06 + WL ≥ 0.55
#
# CPS 1.0 → 1.2（+20% 单格规模）
# 预期峰值利用率：50% → 65%
# whole_stop 保持 $5 / daily_stop 保持 $8
# ============================================================
set -e
PROJ="/root/okx_eth_bot"
cd "$PROJ" 2>/dev/null || exit 1

{
    git fetch origin main --quiet
    git reset --hard origin/main --quiet
    TS=$(date '+%Y%m%d_%H%M%S')
    cp .env ".env.before_phase3_$TS"
    sed -i.tmp 's/^GRID_CONTRACTS_PER_SLOT_SHORT=.*/GRID_CONTRACTS_PER_SLOT_SHORT=1.2/' .env
    sed -i.tmp 's/^GRID_CONTRACTS_PER_SLOT=.*/GRID_CONTRACTS_PER_SLOT=1.2/' .env
    rm -f .env.tmp
    mkdir -p data
    date '+%Y-%m-%d %H:%M:%S %Z' > data/.phase3_applied
    pkill -f run_strategy.py 2>/dev/null || true
} >> data/logs/phase3.log 2>&1

echo "✅ Phase 3 已激活（CPS 1.2，预期利用率 65%）"
