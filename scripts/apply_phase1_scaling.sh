#!/usr/bin/env bash
# ============================================================
# Phase 1 仓位放大脚本（186U 账户全量利用）
# 项目负责人 AI 审批 + 主人同意（2026-04-21 17:30 CST）
#
# 改动：
#   GRID_CONTRACTS_PER_SLOT_SHORT  0.4 → 1.0 张/slot（+150%）
#   GRID_WHOLE_STOP_USDT          3.0 → 5.0 USDT（2.7% 权益 hard stop）
#   GRID_DAILY_STOP_USDT          5.0 → 8.0 USDT（4.3% 权益日止损）
#   GRID_MIN_SPACING_PCT          0.0025 → 0.0032（25bps → 32bps，盈亏比救治）
#   GRID_MAX_SPACING_PCT          0.0055 → 0.0060（保留高波动余量）
#   TAKER_GATE_MODE              (未设置) → warn（启用新 alpha 因子观察）
#
# 幂等：执行前检查 data/.phase1_applied 标记文件，已应用则跳过
# 可回退：备份 .env 到 .env.pre_phase1_<timestamp>
#
# 用法：
#   bash scripts/apply_phase1_scaling.sh           # 正式执行
#   bash scripts/apply_phase1_scaling.sh --dry-run # 仅显示会改什么，不执行
# ============================================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

ENV_FILE="$PROJECT_DIR/.env"
MARKER="$PROJECT_DIR/data/.phase1_applied"
LOG="$PROJECT_DIR/data/logs/phase1_scaling.log"
mkdir -p "$(dirname "$LOG")"

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=1
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $1" | tee -a "$LOG"
}

log "=== Phase 1 放大脚本启动 ==="

if [ ! -f "$ENV_FILE" ]; then
    log "ERROR: .env not found at $ENV_FILE"
    exit 1
fi

if [ -f "$MARKER" ]; then
    log "标记文件存在：$MARKER"
    log "Phase 1 已应用过。如需重新应用，手动删除该文件。"
    exit 0
fi

# 备份 .env
TS=$(date '+%Y%m%d_%H%M%S')
BACKUP="$ENV_FILE.pre_phase1_$TS"
if [ "$DRY_RUN" -eq 0 ]; then
    cp "$ENV_FILE" "$BACKUP"
    log "已备份 .env → $BACKUP"
else
    log "[DRY RUN] 会备份 .env → $BACKUP"
fi

# 旧值快照
log "=== 修改前 .env 相关参数 ==="
grep -E "^(GRID_CONTRACTS_PER_SLOT_SHORT|GRID_CONTRACTS_PER_SLOT|GRID_WHOLE_STOP_USDT|GRID_DAILY_STOP_USDT|GRID_MIN_SPACING_PCT|GRID_MAX_SPACING_PCT|TAKER_GATE_MODE)=" "$ENV_FILE" | tee -a "$LOG" || true

apply_env_change() {
    local key="$1"
    local val="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        if [ "$DRY_RUN" -eq 0 ]; then
            sed -i.tmp "s|^${key}=.*|${key}=${val}|" "$ENV_FILE" && rm -f "$ENV_FILE.tmp"
        fi
        log "update: ${key}=${val}"
    else
        if [ "$DRY_RUN" -eq 0 ]; then
            echo "${key}=${val}" >> "$ENV_FILE"
        fi
        log "append: ${key}=${val}"
    fi
}

# ── 核心改动 ────────────────────────────────────────────────
apply_env_change "GRID_CONTRACTS_PER_SLOT_SHORT" "1.0"
apply_env_change "GRID_CONTRACTS_PER_SLOT" "1.0"
apply_env_change "GRID_WHOLE_STOP_USDT" "5.0"
apply_env_change "GRID_DAILY_STOP_USDT" "8.0"
apply_env_change "GRID_MIN_SPACING_PCT" "0.0032"
apply_env_change "GRID_MAX_SPACING_PCT" "0.0060"
apply_env_change "TAKER_GATE_MODE" "warn"

# 新值快照
log "=== 修改后 .env 相关参数 ==="
grep -E "^(GRID_CONTRACTS_PER_SLOT_SHORT|GRID_CONTRACTS_PER_SLOT|GRID_WHOLE_STOP_USDT|GRID_DAILY_STOP_USDT|GRID_MIN_SPACING_PCT|GRID_MAX_SPACING_PCT|TAKER_GATE_MODE)=" "$ENV_FILE" | tee -a "$LOG" || true

# 写标记
if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$(dirname "$MARKER")"
    date '+%Y-%m-%d %H:%M:%S %Z' > "$MARKER"
    log "已写标记文件：$MARKER"
fi

# 重启 run_strategy（watchdog 会在 5 分钟内重启，但我们 pkill 立即触发）
if [ "$DRY_RUN" -eq 0 ]; then
    if pgrep -f "run_strategy.py" > /dev/null; then
        log "pkill run_strategy.py（watchdog 将 5 分钟内拉起）"
        pkill -f "run_strategy.py" || true
    else
        log "run_strategy.py 未运行，跳过 kill"
    fi
else
    log "[DRY RUN] 会执行：pkill -f run_strategy.py"
fi

log "=== Phase 1 放大完成 ==="
log ""
log "⏱️  预期效果（2h 观察窗口）："
log "   - 峰值保证金利用率 20% → 50-60%"
log "   - 单笔毛利 ~\$0.13 → ~\$0.33"
log "   - 盈亏比 0.43 → 0.60-0.70（结合 aging 与 taker gate）"
log "   - 日 PnL 外推 \$0.8-1.0 → \$1.8-2.5"
log ""
log "🛡️  硬风控（L1-001 铁律）："
log "   - whole_stop \$5 = 2.7% 权益"
log "   - daily_stop \$8 = 4.3% 权益"
log "   - 若 2h 内 EV 转负 → 手动回退：cp $BACKUP $ENV_FILE && pkill -f run_strategy.py"
log ""
log "📊 验证命令："
log "   grep -E '(taker-warn|慢出血|Taker.*订阅)' data/logs/*.log"
log "   .venv/bin/python -m quant.tools.daily_health"
