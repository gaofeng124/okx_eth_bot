#!/usr/bin/env bash
# ============================================================
# Phase 2 资金激活脚本（L10-001 根因修复 + 挂单档位放开）
# 主人 2026-04-21 21:42 CST 批准 A+B+C 三合一
#
# 改动：
#   GRID_LEVELS              4 → 5（挂单档位 +25%，Phase 2 正式启动）
#   GRID_MIN_SPACING_PCT     0.0032 → 0.0020（从 32bps 放到 20bps，贴合静市）
#                             配合 TP_MULT=1.5 → TP=30bps 仍 > fee 5bps，净 25bps 利
#   （代码层已改 US session cap 1→2 + CALM/ELEVATED 2→3 档，watchdog 拉取生效）
#
# 不动：
#   GRID_CONTRACTS_PER_SLOT 保持 1.0（Phase 1 的仓位规模）
#   whole_stop / daily_stop 保持（Phase 1 已放大）
#   TAKER_GATE_MODE=warn（继续观察触发率）
#
# 预期效果：
#   - 挂单 1 档 → 5 档（US session 至少 2 档 + NORMAL vol 最多 5 档）
#   - 资金利用率常态 7.5% → 30-45%
#   - 成交频率 1-2 笔/h → 3-5 笔/h
#   - 日 PnL 外推 $1-2 → $3-5
#
# 幂等：data/.phase2_applied 标记
# 可回退：cp .env.pre_phase2_<ts> .env
# ============================================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

ENV_FILE="$PROJECT_DIR/.env"
MARKER="$PROJECT_DIR/data/.phase2_applied"
LOG="$PROJECT_DIR/data/logs/phase2_scaling.log"
mkdir -p "$(dirname "$LOG")"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $1" | tee -a "$LOG"; }

log "=== Phase 2 + L10-001 修复 启动 ==="

[ -f "$ENV_FILE" ] || { log "ERROR: .env not found at $ENV_FILE"; exit 1; }
[ -f "$MARKER" ] && { log "已应用过（$MARKER 存在）。如需重做：rm $MARKER"; exit 0; }

TS=$(date '+%Y%m%d_%H%M%S')
BACKUP="$ENV_FILE.pre_phase2_$TS"
if [ "$DRY_RUN" -eq 0 ]; then
    cp "$ENV_FILE" "$BACKUP"
    log "已备份 .env → $BACKUP"
else
    log "[DRY RUN] 会备份 .env → $BACKUP"
fi

log "=== 修改前 ==="
grep -E "^(GRID_LEVELS|GRID_MIN_SPACING_PCT|GRID_MAX_SPACING_PCT|GRID_CONTRACTS_PER_SLOT)" "$ENV_FILE" | tee -a "$LOG" || true

apply() {
    local k="$1" v="$2"
    if grep -q "^${k}=" "$ENV_FILE"; then
        [ "$DRY_RUN" -eq 0 ] && { sed -i.tmp "s|^${k}=.*|${k}=${v}|" "$ENV_FILE" && rm -f "$ENV_FILE.tmp"; }
        log "update: ${k}=${v}"
    else
        [ "$DRY_RUN" -eq 0 ] && echo "${k}=${v}" >> "$ENV_FILE"
        log "append: ${k}=${v}"
    fi
}

apply "GRID_LEVELS"           "5"
apply "GRID_MIN_SPACING_PCT"  "0.0020"
apply "GRID_MAX_SPACING_PCT"  "0.0060"

log "=== 修改后 ==="
grep -E "^(GRID_LEVELS|GRID_MIN_SPACING_PCT|GRID_MAX_SPACING_PCT|GRID_CONTRACTS_PER_SLOT)" "$ENV_FILE" | tee -a "$LOG" || true

if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$(dirname "$MARKER")"
    date '+%Y-%m-%d %H:%M:%S %Z' > "$MARKER"
    log "已写标记 → $MARKER"
fi

if [ "$DRY_RUN" -eq 0 ]; then
    pgrep -f "run_strategy.py" > /dev/null && {
        log "pkill run_strategy.py（watchdog 将 5 分钟内拉起）"
        pkill -f "run_strategy.py" || true
    } || log "run_strategy.py 未运行，跳过 kill"
fi

log ""
log "🎯 Phase 2 上线 + L10-001 防护激活"
log ""
log "📊 预期："
log "   - 挂单档位 1 → 3-5 档"
log "   - 资金利用率 7.5% → 30-45%"
log "   - 成交 1-2 笔/h → 3-5 笔/h"
log "   - 日 PnL \$1-2 → \$3-5"
log ""
log "🔧 如 1h 内成交仍 < 2 笔："
log "   python -m quant.tools.system_health  # 看异常信号"
log "   tail -200 data/logs/*.log | grep -E 'grid_active|active_levels'"
log ""
log "🛑 回退（如 2h EV 反向恶化）："
log "   cp $BACKUP $ENV_FILE && rm $MARKER && pkill -f run_strategy.py"
