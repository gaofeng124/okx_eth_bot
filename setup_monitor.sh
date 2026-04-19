#!/bin/bash
# ============================================================
# 一键安装 monitor.py 为 systemd 守护进程
# 在服务器上运行: bash setup_monitor.sh
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_FILE="$PROJECT_DIR/monitor.service"
SYSTEMD_PATH="/etc/systemd/system/eth-monitor.service"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 安装 ETH Monitor 守护进程..."

# 生成 service 文件（动态路径）
cat > "$SYSTEMD_PATH" << EOF
[Unit]
Description=ETH Bot Real-time Monitor Daemon
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/monitor.py
Restart=always
RestartSec=10
StandardOutput=append:$PROJECT_DIR/data/logs/monitor.log
StandardError=append:$PROJECT_DIR/data/logs/monitor.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "$PROJECT_DIR/data/logs"
systemctl daemon-reload
systemctl enable eth-monitor
systemctl restart eth-monitor
sleep 2
systemctl status eth-monitor --no-pager

echo ""
echo "=== 安装完成 ==="
echo "查看日志: tail -f $PROJECT_DIR/data/logs/monitor.log"
echo "检查状态: systemctl status eth-monitor"
