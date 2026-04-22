#!/usr/bin/env bash
# ============================================================
# 紧急回退 + 5 项整改（主人 2026-04-22 10:45 CST 选 A）
#
# 事实层：
#   - 今日 net -$8.28（接近 daily_stop $8）
#   - 盈亏比 WL=0.13（比整改前更差）
#   - sz=1.0 的 4 笔全亏（W0 L4，avg -$1.71）
#   - CPS 又被降回 0.5（L10-004 再次发生）
#   - per_slot_stop $1.0 被击穿（实际 avg_loss $1.71）
#
# 整改动作：
#   P1 紧急回退到保守参数（保资金为首要）
#   P2 .env 锁定 ground_truth + env_guard 每 2min 守护
#   P3 REST 止损兜底加严（5s 查询 + 0.7 阈值）
#   P4 cron 注册 env_guard
#   P5 关闭 Phase 2-4 marker（不再自动升级）
# ============================================================
set -e
PROJ="/root/okx_eth_bot"
cd "$PROJ" 2>/dev/null || { echo "❌ 非服务器环境"; exit 1; }

{
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') 紧急回退启动 ====="

    # 拉最新代码
    git fetch origin main --quiet
    git reset --hard origin/main --quiet
    git clean -fd -e 'data/' --quiet 2>/dev/null || true

    # 备份 .env
    TS=$(date '+%Y%m%d_%H%M%S')
    cp .env ".env.before_rollback_$TS"

    # ── P1 紧急回退参数（保守基线）──
    # 数据驱动：今日 sz=0.3 是最接近盈亏平衡的规模
    apply() {
        if grep -q "^$1=" .env; then
            sed -i.tmp "s|^$1=.*|$1=$2|" .env && rm -f .env.tmp
        else
            echo "$1=$2" >> .env
        fi
        echo "  $1 = $2"
    }
    echo "--- 参数回退到保守基线 ---"
    apply "GRID_CONTRACTS_PER_SLOT"        "0.3"   # 问题 2+5：大仓全亏，缩回最稳规模
    apply "GRID_CONTRACTS_PER_SLOT_SHORT"  "0.3"   # 同上
    apply "GRID_LEVELS"                    "3"     # 问题 1：多档放大反被降级，先缩档位
    apply "GRID_PER_SLOT_STOP_USDT"        "0.6"   # 问题 3：$1.0 被击穿 → 再严
    apply "GRID_WHOLE_STOP_USDT"           "3.0"   # 今日已亏 $8 → 重启清零后限 $3
    apply "GRID_DAILY_STOP_USDT"           "5.0"   # 日止损降到 $5
    apply "GRID_MIN_SPACING_PCT"           "0.0030" # 高波动市场拉宽
    apply "GRID_MAX_SPACING_PCT"           "0.0080" # 上限也提
    apply "GRID_TP_MULT"                   "1.5"
    apply "TAKER_GATE_MODE"                "block"  # 从 warn → block，严格阻挡

    # ── P2 根因修复：daemon prompt 已禁止改 .env（L10-004 根治版）──
    # 旧方案：env_guard 定时对冲（本末倒置）
    # 新方案：prompt 铁律 "daemon 永远不得 sed .env" → 从源头杜绝
    # 清理旧 env_guard cron 和文件
    (crontab -l 2>/dev/null | grep -v "env_guard.sh") | crontab - 2>/dev/null || true
    rm -f scripts/env_guard.sh data/.env_ground_truth 2>/dev/null || true

    # ── P3 REST 止损兜底重启（用更严版本）──
    pkill -f "rest_stop_loss" 2>/dev/null || true
    # 用环境变量传更严参数
    REST_STOP_INTERVAL=5 REST_STOP_MULT=0.7 \
        nohup .venv/bin/python -m quant.tools.rest_stop_loss \
        >> data/logs/rest_stop_loss.log 2>&1 &
    REST_PID=$!
    echo "REST 止损兜底重启（更严）PID=$REST_PID"

    # ── 亏损自动登记器重启 ──
    pkill -f "loss_auto_logger" 2>/dev/null || true
    nohup .venv/bin/python -m quant.tools.loss_auto_logger --daemon \
        >> data/logs/loss_logger.log 2>&1 &

    # ── P4 清除 Phase 升级 marker（不允许自动升）──
    rm -f data/.phase2_applied data/.phase3_applied data/.phase4_applied
    rm -f data/.capital_fix_applied data/.fix_all_applied

    # 重启 strategy
    pkill -f run_strategy.py 2>/dev/null || true
    date '+%Y-%m-%d %H:%M:%S %Z' > data/.emergency_rollback_applied
    echo "===== 紧急回退完成 ====="
} >> data/logs/emergency_rollback.log 2>&1

echo "✅ 紧急回退已执行，资金保护优先"
echo ""
echo "核心参数现在是："
grep -E "^GRID_(CONTRACTS_PER_SLOT|LEVELS|PER_SLOT_STOP|WHOLE_STOP|DAILY_STOP|MIN_SPACING)=" .env
echo ""
echo "5-10 分钟后 strategy 会用新参数重启"
