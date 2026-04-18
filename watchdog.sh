#!/bin/bash
# ============================================================
# 守门人脚本：监听 GitHub 更新 → 自动拉取 → 自动重启系统
# 用法：nohup bash watchdog.sh >> data/logs/watchdog.log 2>&1 &
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
RUN_SCRIPT="run_strategy.py"
CHECK_INTERVAL=300   # 每 5 分钟检查一次 GitHub
LOG_FILE="$PROJECT_DIR/data/logs/watchdog.log"

mkdir -p "$PROJECT_DIR/data/logs"

# 防止重复启动（同时只允许一个守门人进程）
LOCK_FILE="$PROJECT_DIR/data/logs/watchdog.lock"
if [ -f "$LOCK_FILE" ]; then
    OLD_PID=$(cat "$LOCK_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 守门人已在运行 (PID=$OLD_PID)，退出"
        exit 0
    fi
fi
echo $$ > "$LOCK_FILE"
trap "rm -f $LOCK_FILE" EXIT

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

restart_system() {
    log ">>> 停止旧进程..."
    pkill -f "$RUN_SCRIPT" 2>/dev/null
    sleep 2

    log ">>> 启动系统..."
    cd "$PROJECT_DIR"
    nohup "$VENV_PYTHON" "$RUN_SCRIPT" >> "$LOG_FILE" 2>&1 &
    PID=$!
    log ">>> 系统已启动 PID=$PID"
}

# 确保系统初始运行
cd "$PROJECT_DIR"
if ! pgrep -f "$RUN_SCRIPT" > /dev/null; then
    log "系统未运行，初始启动..."
    restart_system
fi

log "=== 守门人启动，每 ${CHECK_INTERVAL}s 检查 GitHub 更新 ==="

LAST_COMMIT=$(git rev-parse HEAD 2>/dev/null)

while true; do
    sleep "$CHECK_INTERVAL"

    cd "$PROJECT_DIR"

    # 拉取远程最新提交哈希（不修改本地文件）
    git fetch origin main --quiet 2>/dev/null
    REMOTE_COMMIT=$(git rev-parse origin/main 2>/dev/null)

    if [ "$REMOTE_COMMIT" != "$LAST_COMMIT" ]; then
        log "=== 检测到 GitHub 新提交：$LAST_COMMIT → $REMOTE_COMMIT ==="

        # 拉取新代码
        git pull origin main --quiet 2>/dev/null
        log "代码已更新"

        # 重启系统
        restart_system

        # 发送升级通知邮件
        "$VENV_PYTHON" "$PROJECT_DIR/notify.py" upgrade >> "$LOG_FILE" 2>&1 &

        LAST_COMMIT="$REMOTE_COMMIT"
    else
        # 检查系统是否还活着，不活着就重启
        if ! pgrep -f "$RUN_SCRIPT" > /dev/null; then
            log "⚠️  系统意外退出，自动重启..."
            restart_system
            # 发送崩溃告警邮件
            "$VENV_PYTHON" "$PROJECT_DIR/notify.py" crash >> "$LOG_FILE" 2>&1 &
        fi
    fi
done
