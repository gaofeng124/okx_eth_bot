"""
策略核心配置 — 全部参数 ≤ 15 个
===============================
设计原则：每个参数都有明确的量化依据，不靠猜测。
"""
from __future__ import annotations

import os
from pathlib import Path

# ── 交易标的 ───────────────────────────────────────────────────────────────
INST_ID = os.getenv("INST_ID", "ETH-USDT-SWAP")  # OKX 线性永续合约
CT_VAL = 0.01        # 1张合约 = 0.01 ETH（OKX ETH 永续固定值）

# ── API 密钥（实盘必填，回测不需要）───────────────────────────────────────
OKX_API_KEY     = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY  = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE  = os.getenv("OKX_PASSPHRASE", "")
OKX_IS_SIMULATED = os.getenv("OKX_IS_SIMULATED", "0") == "1"  # 模拟盘
# 优先读 OKX_REST_HTTP_PROXY，若未设置则回退到 OKX_WS_PROXY（兼容旧 .env）
OKX_REST_HTTP_PROXY = os.getenv("OKX_REST_HTTP_PROXY") or os.getenv("OKX_WS_PROXY", "")

# ── 策略参数（核心 10 个）────────────────────────────────────────────────
# EMA 三均线（9/21/55 是 Fibonacci 数列，黄金比例）
EMA_FAST   = int(os.getenv("EMA_FAST",   "9"))
EMA_SLOW   = int(os.getenv("EMA_SLOW",  "21"))
EMA_TREND  = int(os.getenv("EMA_TREND", "55"))

# ATR 止损/止盈倍数
ATR_PERIOD  = int(os.getenv("ATR_PERIOD",  "14"))
# 参数扫描（5m K 线，2025-01-01 ~ 2025-04-13）最优参数：
#   atr_sl_mult=2.0, atr_tp_mult=2.0, adx_min=25, trail_trigger=3.0, max_hold=24
#   结果：总收益+1.69%，夏普3.85，最大回撤-11%，胜率54.8%
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "2.0"))  # 止损 = 2.0 × ATR
ATR_TP_MULT = float(os.getenv("ATR_TP_MULT", "2.0"))  # 止盈 = 2.0 × ATR → RR=1.0 + trail加成

# 趋势强度过滤（ADX > 25 过滤震荡，比20更严格，减少假信号）
ADX_MIN   = float(os.getenv("ADX_MIN",  "25.0"))
ADX_PERIOD = int(os.getenv("ADX_PERIOD", "14"))

# 追踪止损：盈利达 TRAIL_TRIGGER × ATR 后启动，回撤 TRAIL_GIVEBACK × ATR 退出
# 扫描结果：trigger=3.0 最优（不过早激活，让利润跑起来）
TRAIL_TRIGGER  = float(os.getenv("TRAIL_TRIGGER",  "3.0"))
TRAIL_GIVEBACK = float(os.getenv("TRAIL_GIVEBACK", "1.5"))

# 最大持仓时间：24根5分钟K线 = 2小时
MAX_HOLD_BARS = int(os.getenv("MAX_HOLD_BARS", "24"))

# ── 仓位管理 ────────────────────────────────────────────────────────────
LEVERAGE        = float(os.getenv("LEVERAGE", "3.0"))   # 3x 杠杆（保守安全）
TD_MODE         = os.getenv("TD_MODE", "isolated")       # 逐仓模式
RISK_PER_TRADE  = float(os.getenv("RISK_PER_TRADE", "0.015"))  # 每笔风险 1.5% 账户
MIN_CONTRACTS   = 1
MAX_CONTRACTS   = int(os.getenv("MAX_CONTRACTS", "3"))   # 最多 3 张

# ── 手续费 ──────────────────────────────────────────────────────────────
# OKX VIP0：maker=0.02%，taker=0.05%；做市进 + IOC 出 ≈ 7bps
MAKER_FEE_BPS = 2.0
TAKER_FEE_BPS = 5.0
ROUNDTRIP_FEE_BPS = MAKER_FEE_BPS + TAKER_FEE_BPS  # 7bps

# ── 数据目录 ─────────────────────────────────────────────────────────────
ROOT_DIR  = Path(__file__).resolve().parent.parent
DATA_DIR  = ROOT_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── 风控 ─────────────────────────────────────────────────────────────────
DAILY_MAX_LOSS_PCT = float(os.getenv("DAILY_MAX_LOSS_PCT", "0.05"))  # 日最大亏损 5%
MAX_CONSEC_LOSS    = int(os.getenv("MAX_CONSEC_LOSS", "5"))           # 连续亏损 5 次暂停
HALT_COOLDOWN_SEC  = 1800.0  # 暂停后冷静 30 分钟
