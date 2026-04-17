#!/usr/bin/env python3
"""
系统状态日志查看器
用法：python status.py
每次运行自动生成最新的 status.log
"""
import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT = Path(__file__).parent
LOG_DIR = PROJECT / "data/logs/daily"
WATCHDOG_LOG = PROJECT / "data/logs/watchdog.log"
OUTPUT = PROJECT / "data/logs/status.log"

CST = timezone(timedelta(hours=8))

def ts_cst(ts_str: str) -> str:
    """UTC 时间字符串转北京时间"""
    try:
        t = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return t.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts_str

def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

lines = []
def add(text=""):
    lines.append(text)

# ═══════════════════════════════════════════
add("=" * 60)
add(f"  ETH 量化系统状态报告  |  {now_cst()} (北京时间)")
add("=" * 60)

# ─── 1. Git 推送历史（升级记录）───────────────
add("")
add("【升级记录】GitHub 最近10次推送")
add("-" * 60)
try:
    result = subprocess.run(
        ["git", "log", "--oneline", "-10",
         "--format=%ai | %s",
         "--date=format:%Y-%m-%d %H:%M:%S"],
        capture_output=True, text=True, cwd=PROJECT
    )
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            # 转换为北京时间
            parts = line.split(" | ", 1)
            if len(parts) == 2:
                try:
                    t = datetime.fromisoformat(parts[0].strip())
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone(timedelta(hours=8)))
                    t_cst = t.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
                    add(f"  {t_cst}  {parts[1].strip()}")
                except Exception:
                    add(f"  {line}")
            else:
                add(f"  {line}")
except Exception as e:
    add(f"  [读取失败] {e}")

# ─── 2. 守门人日志（启动/停止）─────────────────
add("")
add("【系统运行记录】守门人日志（最近20条）")
add("-" * 60)
if WATCHDOG_LOG.exists():
    all_lines = WATCHDOG_LOG.read_text(encoding="utf-8", errors="ignore").strip().split("\n")
    recent = [l for l in all_lines if any(kw in l for kw in
              ["启动", "停止", "PID", "已启动", "意外退出", "检测到", "初始", "=== 守门人"])]
    for l in recent[-20:]:
        add(f"  {l.strip()}")
else:
    add("  [守门人日志不存在，可能尚未运行]")

# ─── 3. 交易系统状态（最新）──────────────────
add("")
add("【实时交易状态】最新快照")
add("-" * 60)
today = datetime.now(CST).strftime("%Y-%m-%d")
analysis_file = LOG_DIR / today / "analysis.jsonl"

if analysis_file.exists():
    all_records = []
    with open(analysis_file, encoding="utf-8") as f:
        for line in f:
            try:
                all_records.append(json.loads(line))
            except Exception:
                pass

    if all_records:
        latest = all_records[-1]
        ss = latest.get("status_summary", {})
        session = ss.get("session", {})

        add(f"  时间          : {ts_cst(latest.get('ts_wall', ''))}")
        add(f"  ETH 价格      : {latest.get('mid', 0):.2f} USDT")
        add(f"  市场状态      : {ss.get('regime', '-')}")
        add(f"  波动率状态    : {ss.get('vol_regime', '-')}")
        add(f"  ATR(短期)     : {ss.get('atr_short_bps', 0):.2f} bps")
        add(f"  网格激活      : {'✅ 是' if ss.get('grid_active') else '❌ 否'}")
        add(f"  网格中心价    : {ss.get('grid_center', 0):.2f}")
        add(f"  格宽          : {ss.get('grid_spacing_bps', 0):.1f} bps")
        add(f"  挂单档位      : {ss.get('slots_live', [])}")
        add(f"  持仓档位      : {list(ss.get('slots_holding', {}).keys())}")
        add(f"  今日已实现盈亏: {ss.get('daily_pnl', 0):+.4f} USDT")
        add(f"  资金费率      : {ss.get('funding_rate', 0):.5f}")
        add("")
        add(f"  本次会话统计：")
        add(f"  ├ 运行时长    : {session.get('elapsed_hours', 0):.2f} 小时")
        add(f"  ├ 成交笔数    : {session.get('trades', 0)} 笔")
        add(f"  ├ 胜率        : {session.get('win_rate', 0)*100:.1f}%")
        add(f"  ├ 净盈亏      : {session.get('net_pnl_usdt', 0):+.4f} USDT")
        add(f"  └ 每小时盈亏  : {session.get('pnl_per_hour', 0):+.4f} USDT/h")
else:
    add(f"  [今日日志不存在：{analysis_file}]")

# ─── 4. 近期收益汇总 ──────────────────────────
add("")
add("【近期收益汇总】各日表现")
add("-" * 60)
add(f"  {'日期':<12} {'成交笔数':>8} {'净盈亏':>12} {'胜率':>8} {'运行时长':>10}")
add(f"  {'-'*12} {'-'*8} {'-'*12} {'-'*8} {'-'*10}")

if LOG_DIR.exists():
    day_dirs = sorted(LOG_DIR.iterdir(), reverse=True)[:7]
    for day_dir in day_dirs:
        if not day_dir.is_dir():
            continue
        af = day_dir / "analysis.jsonl"
        if not af.exists():
            continue
        try:
            records = []
            with open(af, encoding="utf-8") as f:
                for line in f:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass
            if records:
                last = records[-1].get("status_summary", {}).get("session", {})
                trades = last.get("trades", 0)
                pnl = last.get("net_pnl_usdt", 0)
                wr = last.get("win_rate", 0) * 100
                hrs = last.get("elapsed_hours", 0)
                pnl_str = f"{pnl:+.4f} USDT"
                add(f"  {day_dir.name:<12} {trades:>8} {pnl_str:>12} {wr:>7.1f}% {hrs:>9.1f}h")
        except Exception:
            pass

# ─── 5. 进程状态 ──────────────────────────────
add("")
add("【进程状态】")
add("-" * 60)
try:
    r1 = subprocess.run(["pgrep", "-f", "watchdog.sh"], capture_output=True, text=True)
    r2 = subprocess.run(["pgrep", "-f", "run_strategy.py"], capture_output=True, text=True)
    wd_pid = r1.stdout.strip()
    st_pid = r2.stdout.strip()
    add(f"  守门人进程    : {'✅ 运行中 PID=' + wd_pid if wd_pid else '❌ 未运行'}")
    add(f"  交易系统进程  : {'✅ 运行中 PID=' + st_pid if st_pid else '❌ 未运行'}")
except Exception:
    add("  [进程状态读取失败]")

add("")
add("=" * 60)
add(f"  报告生成时间: {now_cst()}")
add("=" * 60)

# 输出到文件和终端
report = "\n".join(lines)
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
OUTPUT.write_text(report, encoding="utf-8")
print(report)
print(f"\n✅ 报告已保存到：{OUTPUT}")
