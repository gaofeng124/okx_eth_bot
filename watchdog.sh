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

# 跟踪上次已知的 origin/main 提交
LAST_ORIGIN_COMMIT=$(git rev-parse origin/main 2>/dev/null || git rev-parse HEAD 2>/dev/null)
# 追踪本地 HEAD（检测 agent 通过 SSH 直接 commit 的本地改动）
LAST_LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null)

while true; do
    sleep "$CHECK_INTERVAL"

    cd "$PROJECT_DIR"

    # 拉取远程最新提交哈希（不修改本地文件）
    # 注意：必须用 "git fetch origin" 而非 "git fetch origin main"
    # 否则 origin/main 跟踪引用不会更新，watchdog 检测不到新提交
    git fetch origin --quiet 2>/dev/null
    REMOTE_COMMIT=$(git rev-parse origin/main 2>/dev/null)
    LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null)
    # 本地相对 origin/main 超前多少 commit（agent 改动时 > 0）
    LOCAL_AHEAD=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)

    # ── 分支 1：origin/main 有新提交 ────────────────────────────────
    if [ "$REMOTE_COMMIT" != "$LAST_ORIGIN_COMMIT" ]; then
        if [ "$LOCAL_AHEAD" -eq 0 ]; then
            # 本地没有 agent 改动 → 安全直接 reset
            log "=== 检测到 origin/main 新提交：$LAST_ORIGIN_COMMIT → $REMOTE_COMMIT（本地无改动，直接 reset）==="
            git reset --hard origin/main --quiet 2>/dev/null
            git clean -fd --quiet 2>/dev/null
            log "代码已强制同步到 $REMOTE_COMMIT"
            restart_system
            "$VENV_PYTHON" "$PROJECT_DIR/notify.py" upgrade >> "$LOG_FILE" 2>&1 &
        else
            # 本地有 agent 改动 → 尝试 rebase 保留 agent 工作；失败则放弃 pull 保留本地
            log "=== origin/main 有新提交 $REMOTE_COMMIT，但本地超前 ${LOCAL_AHEAD} 个 commit（agent 改动），尝试 rebase ==="
            if git rebase origin/main --quiet 2>>"$LOG_FILE"; then
                log "✓ rebase 成功，重启系统"
                restart_system
            else
                log "⚠️ rebase 冲突，已 abort；保留本地 agent 改动。人工合并时请在服务器上 git rebase/merge"
                git rebase --abort 2>/dev/null
            fi
        fi
        LAST_ORIGIN_COMMIT="$REMOTE_COMMIT"
        LAST_LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null)
        continue
    fi

    # ── 分支 2：本地 HEAD 变了（agent 刚在服务器上 commit 了东西）─────────
    if [ "$LOCAL_HEAD" != "$LAST_LOCAL_HEAD" ]; then
        log "=== 本地 HEAD 变化：$LAST_LOCAL_HEAD → $LOCAL_HEAD（agent 已在服务器 commit），重启加载新代码 ==="
        restart_system
        "$VENV_PYTHON" "$PROJECT_DIR/notify.py" upgrade >> "$LOG_FILE" 2>&1 &
        LAST_LOCAL_HEAD="$LOCAL_HEAD"
        continue
    fi

    # ── 分支 3：一切未变，只检查进程是否存活 ────────────────────────────
    if ! pgrep -f "$RUN_SCRIPT" > /dev/null; then
        log "⚠️  系统意外退出，自动重启..."
        restart_system
        "$VENV_PYTHON" "$PROJECT_DIR/notify.py" crash >> "$LOG_FILE" 2>&1 &
    fi
done
