#!/bin/bash
# ============================================================
# 云服务器一键部署脚本
# 在新服务器上执行：bash setup_server.sh
# ============================================================

set -e  # 任何命令失败则立即退出

echo ""
echo "=========================================="
echo "  OKX ETH Bot - 云服务器部署脚本"
echo "=========================================="
echo ""

# ── 0. 系统依赖 ──────────────────────────────
echo "[1/6] 安装系统依赖..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git curl wget screen htop \
    build-essential libssl-dev

echo "  ✅ 系统依赖安装完成"

# ── 1. 克隆代码 ──────────────────────────────
echo ""
echo "[2/6] 拉取代码..."
PROJECT_DIR="/root/okx_eth_bot"

if [ -d "$PROJECT_DIR" ]; then
    echo "  目录已存在，拉取最新代码..."
    cd "$PROJECT_DIR"
    git pull origin main
else
    git clone https://github.com/gaofeng124/okx_eth_bot.git "$PROJECT_DIR"
    cd "$PROJECT_DIR"
fi

echo "  ✅ 代码拉取完成（$(git rev-parse --short HEAD)）"

# ── 2. Python 虚拟环境 ────────────────────────
echo ""
echo "[3/6] 配置 Python 虚拟环境..."
cd "$PROJECT_DIR"

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "  ✅ Python 环境配置完成"
python --version

# ── 3. 创建日志目录 ───────────────────────────
echo ""
echo "[4/6] 创建目录结构..."
mkdir -p data/logs/daily
mkdir -p deploy

echo "  ✅ 目录创建完成"

# ── 4. 检查 .env ──────────────────────────────
echo ""
echo "[5/6] 检查配置文件..."

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo ""
    echo "  ⚠️  .env 文件不存在！"
    echo "  请执行：scp 本地路径/.env root@服务器IP:/root/okx_eth_bot/.env"
    echo "  或手动创建 .env 文件（参考 .env.example）"
    echo ""
    echo "  .env 最关键的内容（云服务器版）："
    echo "  OKX_API_KEY=你的API_KEY"
    echo "  OKX_SECRET_KEY=你的SECRET"
    echo "  OKX_PASSPHRASE=你的密码"
    echo "  OKX_WS_PROXY=          ← 留空！云服务器直连不需要代理"
    echo ""
    exit 1
else
    # 验证关键字段
    if grep -q "OKX_API_KEY=$" "$PROJECT_DIR/.env" || ! grep -q "OKX_API_KEY=" "$PROJECT_DIR/.env"; then
        echo "  ⚠️  .env 中 OKX_API_KEY 为空，请填写后重新运行"
        exit 1
    fi
    echo "  ✅ .env 文件已就绪"
    # 自动修复代理设置（云服务器不需要本地代理）
    sed -i 's|OKX_WS_PROXY=http://127.0.0.1:.*|OKX_WS_PROXY=|g' "$PROJECT_DIR/.env"
    echo "  ✅ 已清除本地代理设置（云服务器直连 OKX）"
fi

# ── 5. 设置守门人 systemd 服务 ────────────────
echo ""
echo "[6/6] 配置开机自启动（systemd）..."

cat > /etc/systemd/system/okx-watchdog.service << EOF
[Unit]
Description=OKX ETH Bot Watchdog
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=root
WorkingDirectory=/root/okx_eth_bot
ExecStart=/bin/bash /root/okx_eth_bot/watchdog.sh
Restart=always
RestartSec=10
StandardOutput=append:/root/okx_eth_bot/data/logs/watchdog.log
StandardError=append:/root/okx_eth_bot/data/logs/watchdog.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable okx-watchdog
systemctl start okx-watchdog

sleep 3

# 检查服务状态
if systemctl is-active --quiet okx-watchdog; then
    echo "  ✅ systemd 服务启动成功，开机自动运行"
else
    echo "  ⚠️  服务启动异常，查看详情："
    systemctl status okx-watchdog --no-pager
fi

# ── 完成 ──────────────────────────────────────
echo ""
echo "=========================================="
echo "  🎉 部署完成！"
echo "=========================================="
echo ""
echo "  查看实时日志："
echo "  tail -f /root/okx_eth_bot/data/logs/watchdog.log"
echo ""
echo "  查看系统状态："
echo "  cd /root/okx_eth_bot && .venv/bin/python status.py"
echo ""
echo "  手动控制服务："
echo "  systemctl stop    okx-watchdog  # 停止"
echo "  systemctl start   okx-watchdog  # 启动"
echo "  systemctl restart okx-watchdog  # 重启"
echo "  systemctl status  okx-watchdog  # 状态"
echo ""
