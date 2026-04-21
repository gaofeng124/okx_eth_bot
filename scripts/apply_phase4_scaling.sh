#!/usr/bin/env bash
# ============================================================
# Phase 4 终极放大（主人 2026-04-21 22:15 批准 B 激进版）
# 触发条件：Phase 3 稳定 ≥ 120min + EV ≥ +0.08 + WL ≥ 0.60
#            + 市场非趋势日（近 4h |delta| < 1.0%）
#
# 改动：
#   GRID_LEVELS             5 → 6（+1 档）
#   GRID_WHOLE_STOP_USDT    5 → 10（5.4% 权益，给 90% 利用率留空间）
#   GRID_DAILY_STOP_USDT    8 → 15（8% 权益）
#   GRID_PHASE4_TREND_GUARD 1（启用趋势日自动降级）
#
# 预期峰值利用率：65% → 85-90%
#
# 特殊防护：
#   - 趋势日（4h |delta| > 1.5%）→ 自动回 Phase 3
#   - whole_stop 触发 → 冷却 60min（原 5min）
#   - daily_stop 触发 → 自动回退到 Phase 1
# ============================================================
set -e
PROJ="/root/okx_eth_bot"
cd "$PROJ" 2>/dev/null || exit 1

{
    git fetch origin main --quiet
    git reset --hard origin/main --quiet
    TS=$(date '+%Y%m%d_%H%M%S')
    cp .env ".env.before_phase4_$TS"

    sed -i.tmp 's/^GRID_LEVELS=.*/GRID_LEVELS=6/' .env
    sed -i.tmp 's/^GRID_WHOLE_STOP_USDT=.*/GRID_WHOLE_STOP_USDT=10.0/' .env
    sed -i.tmp 's/^GRID_DAILY_STOP_USDT=.*/GRID_DAILY_STOP_USDT=15.0/' .env

    # 启用 Phase 4 趋势日守卫
    if grep -q "^GRID_PHASE4_TREND_GUARD=" .env; then
        sed -i.tmp 's/^GRID_PHASE4_TREND_GUARD=.*/GRID_PHASE4_TREND_GUARD=1/' .env
    else
        echo "GRID_PHASE4_TREND_GUARD=1" >> .env
    fi
    rm -f .env.tmp

    mkdir -p data
    date '+%Y-%m-%d %H:%M:%S %Z' > data/.phase4_applied
    pkill -f run_strategy.py 2>/dev/null || true
} >> data/logs/phase4.log 2>&1

echo "✅ Phase 4 已激活（LEVELS=6，whole_stop=\$10，预期峰值利用率 85-90%）"
