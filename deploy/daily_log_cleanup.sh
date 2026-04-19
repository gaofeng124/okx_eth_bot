#!/bin/bash
# 每日清理 data/logs/daily/ 下超过 14 天的目录
# 14 天以上的日志压缩成 tar.gz，30 天以上直接删
# 安装：
#   chmod +x deploy/daily_log_cleanup.sh
#   cp deploy/daily_log_cleanup.sh /etc/cron.daily/okx-bot-log-cleanup

set -e

DAILY_DIR="/root/okx_eth_bot/data/logs/daily"
ARCHIVE_DIR="/root/okx_eth_bot/data/logs/archive"

mkdir -p "$ARCHIVE_DIR"

# 压缩 14+ 天前的目录到 archive/
find "$DAILY_DIR" -maxdepth 1 -mindepth 1 -type d -mtime +14 | while read d; do
    name=$(basename "$d")
    if [ ! -f "$ARCHIVE_DIR/${name}.tar.gz" ]; then
        echo "压缩归档: $name"
        tar -czf "$ARCHIVE_DIR/${name}.tar.gz" -C "$DAILY_DIR" "$name" && rm -rf "$d"
    fi
done

# 删除 archive/ 中 60+ 天的 tar.gz
find "$ARCHIVE_DIR" -maxdepth 1 -name '*.tar.gz' -mtime +60 -delete

# 压缩并轮转 pnl_snapshots.jsonl 如果 > 300MB
SNAPSHOT_LOG="/root/okx_eth_bot/data/logs/pnl_snapshots.jsonl"
if [ -f "$SNAPSHOT_LOG" ]; then
    size=$(stat -c%s "$SNAPSHOT_LOG" 2>/dev/null || echo 0)
    if [ "$size" -gt 314572800 ]; then
        ts=$(date +%Y%m%d)
        gzip -c "$SNAPSHOT_LOG" > "$ARCHIVE_DIR/pnl_snapshots-${ts}.jsonl.gz"
        : > "$SNAPSHOT_LOG"  # truncate
        echo "pnl_snapshots.jsonl 已归档到 $ARCHIVE_DIR/pnl_snapshots-${ts}.jsonl.gz 并清空"
    fi
fi

echo "[$(date)] 日志清理完成"
