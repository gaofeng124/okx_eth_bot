#!/usr/bin/env bash
# ============================================================
# 规模回升 1.0 + 启动 Trend Follow（主人 13:00 CST 要求）
#
# 主人原话："下单量 0.03 太少了。最少也得有1。我是考虑 你一直没有大量下单
# 后期再加注还是一点经验都没有 做好止损就可以了"
#
# 调整：
#   GRID_CONTRACTS_PER_SLOT      0.3 → 1.0
#   GRID_CONTRACTS_PER_SLOT_SHORT 0.3 → 1.0
#   GRID_LEVELS                  保持 3（不贪多档）
#   GRID_PER_SLOT_STOP_USDT      0.6 → 0.8（sz=1 时更合理，严格但不致频触发）
#   GRID_WHOLE_STOP_USDT         3.0 → 5.0（对应更大仓位）
#   GRID_DAILY_STOP_USDT         5.0 → 8.0
#   GRID_MIN_SPACING_PCT         保持 0.0030
#
# 启动/重启后台服务：
#   - trend_follow_watcher.py（突破追势，独立于 grid）
#   - phase_monitor.py --daemon（数据驱动升级建议，不自动执行）
#   - rest_stop_loss.py（加严 5s/0.7 触发）
#   - loss_auto_logger.py（保留）
# ============================================================
set -e

PROJ="/root/okx_eth_bot"
cd "$PROJ" 2>/dev/null || { echo "❌ 非服务器环境"; exit 1; }

{
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') 规模回升 1.0 + Trend Follow 启动 ====="

    git fetch origin main --quiet
    git reset --hard origin/main --quiet
    git clean -fd -e 'data/' --quiet 2>/dev/null || true

    TS=$(date '+%Y%m%d_%H%M%S')
    cp .env ".env.before_scale_$TS"

    apply() {
        if grep -q "^$1=" .env; then
            sed -i.tmp "s|^$1=.*|$1=$2|" .env && rm -f .env.tmp
        else
            echo "$1=$2" >> .env
        fi
        echo "  $1 = $2"
    }

    echo "--- 参数调整 ---"
    apply "GRID_CONTRACTS_PER_SLOT"       "1.0"
    apply "GRID_CONTRACTS_PER_SLOT_SHORT" "1.0"
    apply "GRID_LEVELS"                   "3"
    apply "GRID_PER_SLOT_STOP_USDT"       "0.8"
    apply "GRID_WHOLE_STOP_USDT"          "5.0"
    apply "GRID_DAILY_STOP_USDT"          "8.0"
    apply "GRID_MIN_SPACING_PCT"          "0.0030"
    apply "GRID_MAX_SPACING_PCT"          "0.0080"
    apply "TAKER_GATE_MODE"               "block"

    # ── 启动 trend_follow_watcher ──
    pkill -f "trend_follow_watcher" 2>/dev/null || true
    nohup .venv/bin/python -m quant.tools.trend_follow_watcher \
        >> data/logs/trend_follow.log 2>&1 &
    TF_PID=$!
    echo "Trend Follow Watcher PID=$TF_PID"

    # ── 启动 phase_monitor ──
    pkill -f "phase_monitor" 2>/dev/null || true
    nohup .venv/bin/python -m quant.tools.phase_monitor --daemon \
        >> data/logs/phase_monitor.log 2>&1 &
    PM_PID=$!
    echo "Phase Monitor PID=$PM_PID"

    # ── 重启 REST 止损兜底（更严）──
    pkill -f "rest_stop_loss" 2>/dev/null || true
    REST_STOP_INTERVAL=5 REST_STOP_MULT=0.7 \
        nohup .venv/bin/python -m quant.tools.rest_stop_loss \
        >> data/logs/rest_stop_loss.log 2>&1 &
    echo "REST 止损兜底 PID=$!"

    # ── 亏损自动登记器 ──
    pkill -f "loss_auto_logger" 2>/dev/null || true
    nohup .venv/bin/python -m quant.tools.loss_auto_logger --daemon \
        >> data/logs/loss_logger.log 2>&1 &
    echo "亏损自动登记 PID=$!"

    # 重启 strategy
    pkill -f run_strategy.py 2>/dev/null || true

    date '+%Y-%m-%d %H:%M:%S %Z' > data/.scale_trend_applied

    echo "===== 完成 ====="
    echo ""
    echo "核心参数："
    grep -E "^GRID_(CONTRACTS_PER_SLOT|LEVELS|PER_SLOT_STOP|WHOLE_STOP|DAILY_STOP|MIN_SPACING)=" .env
    echo ""
    echo "后台服务状态："
    echo "  trend_follow: $(pgrep -fc trend_follow_watcher) 进程"
    echo "  phase_monitor: $(pgrep -fc 'phase_monitor.*daemon') 进程"
    echo "  rest_stop_loss: $(pgrep -fc rest_stop_loss) 进程"
    echo "  loss_auto_logger: $(pgrep -fc loss_auto_logger) 进程"
} >> data/logs/scale_trend.log 2>&1

echo "✅ 规模回升 1.0 + Trend Follow 已启动"
echo "5-10 分钟后 strategy 用新参数重启"
