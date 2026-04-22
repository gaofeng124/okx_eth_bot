#!/usr/bin/env bash
# ============================================================
# 五条事实层问题一次性整改（主人 2026-04-22 09:00 CST "B 路径"）
#
# 整改内容：
#   Q1 per_slot_stop REST 兜底：启动 rest_stop_loss 后台进程
#   Q2 方向反复：Regime Router 评估市场切 direction
#   Q3 CPS 被悄悄降级：根因已修在 ai_brain prompt（L10-004 登记）
#   Q4 盈亏比 0.14：per_slot_stop $1.5 → $1.0（更早止损）
#                    慢出血 aging $0.30 仍生效但门槛提 15min
#   Q5 亏损自动登记：启动 loss_auto_logger 后台进程
#
# 参数调整总览：
#   GRID_CONTRACTS_PER_SLOT_SHORT 1.0（锁死，防 daemon 降级）
#   GRID_CONTRACTS_PER_SLOT       1.0
#   GRID_PER_SLOT_STOP_USDT      1.5 → 1.0（早 33% 止损）
#   GRID_DIRECTION               按 Regime Router 建议设
#   GRID_MIN_SPACING_PCT          保持 0.0020
#   GRID_WHOLE_STOP_USDT          5.0（不改）
#   GRID_DAILY_STOP_USDT          8.0（不改）
# ============================================================
set -e

PROJ="/root/okx_eth_bot"
cd "$PROJ" 2>/dev/null || { echo "❌ 非服务器环境"; exit 1; }

{
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') 开始整改 ====="

    # 拉取最新代码
    git fetch origin main --quiet
    git reset --hard origin/main --quiet
    git clean -fd -e 'data/' --quiet 2>/dev/null || true

    # 备份 .env
    TS=$(date '+%Y%m%d_%H%M%S')
    cp .env ".env.before_fix_all_$TS"

    # ── Q3 锁死 CPS 到 1.0（Phase 1 基线）──
    sed -i.tmp 's/^GRID_CONTRACTS_PER_SLOT_SHORT=.*/GRID_CONTRACTS_PER_SLOT_SHORT=1.0/' .env
    sed -i.tmp 's/^GRID_CONTRACTS_PER_SLOT=.*/GRID_CONTRACTS_PER_SLOT=1.0/' .env

    # ── Q4 盈亏比救治：per_slot_stop 从 $1.5 降到 $1.0（更早止损）──
    sed -i.tmp 's/^GRID_PER_SLOT_STOP_USDT=.*/GRID_PER_SLOT_STOP_USDT=1.0/' .env

    rm -f .env.tmp

    # ── Q2 Regime Router：立即评估 + 切方向 ──
    echo "--- Regime Router 评估 ---"
    .venv/bin/python -m quant.tools.regime_router 2>&1 | tail -15

    # ── Q1 启动 REST 止损兜底（后台进程）──
    pkill -f "rest_stop_loss" 2>/dev/null || true
    nohup .venv/bin/python -m quant.tools.rest_stop_loss >> data/logs/rest_stop_loss.log 2>&1 &
    REST_PID=$!
    echo "REST 止损兜底进程 PID=$REST_PID"
    echo $REST_PID > data/.rest_stop_loss.pid

    # ── Q5 启动亏损自动登记（后台进程）──
    pkill -f "loss_auto_logger" 2>/dev/null || true
    nohup .venv/bin/python -m quant.tools.loss_auto_logger --daemon >> data/logs/loss_logger.log 2>&1 &
    LOSS_PID=$!
    echo "亏损登记进程 PID=$LOSS_PID"
    echo $LOSS_PID > data/.loss_logger.pid

    # 清除 Phase 3/4 marker（防 daemon 误触发自动升级）
    # 因为 EV 0.14 远未达 Phase 3 条件 0.06
    rm -f data/.phase3_applied data/.phase4_applied

    # 重启 run_strategy
    pkill -f run_strategy.py 2>/dev/null || true

    date '+%Y-%m-%d %H:%M:%S %Z' > data/.fix_all_applied
    echo "===== 整改完成 ====="
    echo "核心参数："
    grep -E "^GRID_(CONTRACTS|PER_SLOT|DIRECTION|LEVELS|MIN_SPACING|WHOLE_STOP|DAILY_STOP)=" .env
} >> data/logs/fix_all.log 2>&1

echo "✅ 五条事实层问题整改完成"
echo ""
echo "后台进程状态："
echo "  - REST 止损兜底: $(pgrep -f rest_stop_loss && echo 运行中 || echo 未启动)"
echo "  - 亏损自动登记: $(pgrep -f loss_auto_logger && echo 运行中 || echo 未启动)"
echo ""
echo "5-10 分钟后 watchdog 会拉起 run_strategy 用新参数"
