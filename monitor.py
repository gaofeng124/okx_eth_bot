#!/usr/bin/env python3
"""
实时监控守护进程 — 每60秒检测交易系统健康状态，自动干预
职责：
  1. 检测系统是否在运行，崩溃则重启
  2. 检测网格是否激活，长期不激活则重启
  3. 检测浮亏是否超阈值，超则告警+冷却
  4. 检测今日盈亏，达到止损上限则暂停
  5. 发送实时状态邮件（异常时）
"""
from __future__ import annotations
import json, os, subprocess, sys, time, glob, smtplib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent
load_dotenv(PROJECT / ".env")

CST = timezone(timedelta(hours=8))
LOG  = PROJECT / "data" / "logs" / "monitor.log"
VENV = PROJECT / ".venv" / "bin" / "python"

# ── 配置 ────────────────────────────────────────────────
CHECK_INTERVAL_SEC   = 60       # 检查间隔
GRID_INACTIVE_LIMIT  = 600      # 网格超过10分钟不激活 → 重启
NO_TICK_LIMIT        = 300      # 超过5分钟无快照更新 → 重启
DRAWDOWN_ALERT_USDT  = 3.0      # 浮亏超3U → 告警邮件
DAILY_LOSS_LIMIT     = 8.0      # 今日亏损超8U → 停止系统
MAX_RESTARTS_PER_HOUR = 4       # 1小时内最多重启次数（防止循环崩溃）

SMTP_HOST = os.getenv("NOTIFY_SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("NOTIFY_SMTP_PORT", "587"))
SMTP_USER = os.getenv("NOTIFY_SMTP_USER", "")
SMTP_PASS = os.getenv("NOTIFY_SMTP_PASS", "")
NOTIFY_TO = os.getenv("NOTIFY_TO", "1240954013@qq.com")

# ── 状态 ────────────────────────────────────────────────
restart_times: list[float] = []
last_alert_ts: dict[str, float] = {}
grid_inactive_since: float = 0.0

def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str) -> None:
    line = f"[{now_cst()}] {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def send_alert(subject: str, body: str) -> None:
    if not SMTP_USER or not SMTP_PASS:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"ETH Monitor <{SMTP_USER}>"
        msg["To"]      = NOTIFY_TO
        msg.attach(MIMEText(f"<pre style='font-family:monospace'>{body}</pre>", "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.ehlo(); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [NOTIFY_TO], msg.as_string())
        log(f"[邮件] 已发送: {subject}")
    except Exception as e:
        log(f"[邮件] 发送失败: {e}")

def can_alert(key: str, cooldown: float = 1800) -> bool:
    """告警冷却（同一类型告警不要轰炸邮箱）"""
    now = time.time()
    if now - last_alert_ts.get(key, 0) > cooldown:
        last_alert_ts[key] = now
        return True
    return False

def is_strategy_running() -> bool:
    try:
        result = subprocess.run(["pgrep", "-f", "run_strategy.py"],
                                capture_output=True, text=True)
        return bool(result.stdout.strip())
    except Exception:
        return False

def restart_strategy(reason: str) -> bool:
    global restart_times
    now = time.time()
    # 清理1小时前的记录
    restart_times = [t for t in restart_times if now - t < 3600]
    if len(restart_times) >= MAX_RESTARTS_PER_HOUR:
        log(f"[重启] 拒绝：1小时内已重启{len(restart_times)}次，超过上限{MAX_RESTARTS_PER_HOUR}次")
        return False
    log(f"[重启] 原因：{reason}")
    subprocess.run(["pkill", "-f", "run_strategy.py"], capture_output=True)
    time.sleep(2)
    log_file = PROJECT / "data" / "logs" / "watchdog.log"
    proc = subprocess.Popen(
        [str(VENV), "run_strategy.py"],
        cwd=PROJECT,
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
    )
    restart_times.append(now)
    log(f"[重启] 完成 PID={proc.pid}")
    return True

def get_today_pnl() -> float:
    """从 pnl_snapshots.jsonl 精算今日盈亏"""
    today = datetime.now(CST).strftime("%Y-%m-%d")
    path  = PROJECT / "data" / "logs" / "pnl_snapshots.jsonl"
    if not path.exists():
        return 0.0
    run_pnl: dict[str, float] = {}
    try:
        for line in open(path, errors="ignore"):
            try:
                d = json.loads(line.strip())
                ts = datetime.fromisoformat(d["ts_utc"].replace("Z", "+00:00")).astimezone(CST)
                if ts.strftime("%Y-%m-%d") == today:
                    run_pnl[d.get("run_id", "?")] = float(d.get("net_realized_pnl_usdt") or 0)
            except Exception:
                pass
    except Exception:
        pass
    return sum(run_pnl.values())

def get_latest_snapshot() -> dict:
    """读取今日最新快照"""
    today = datetime.now(CST).strftime("%Y-%m-%d")
    files = glob.glob(str(PROJECT / "data" / "logs" / "daily" / today / "analysis.jsonl"))
    if not files:
        return {}
    try:
        lines = [l for l in open(files[0], errors="ignore") if l.strip()]
        if lines:
            return json.loads(lines[-1])
    except Exception:
        pass
    return {}

def check_and_act() -> None:
    global grid_inactive_since
    now = time.time()
    snap = get_latest_snapshot()
    pnl  = get_today_pnl()

    # ── 1. 检测系统是否在运行 ─────────────────────────────
    if not is_strategy_running():
        log("[检测] ⚠️  run_strategy.py 未运行")
        ok = restart_strategy("进程消失")
        if ok and can_alert("crash", 900):
            send_alert(
                f"⚠️ ETH Bot 崩溃重启 | 今日{pnl:+.1f}U | {now_cst()}",
                f"监控检测到 run_strategy.py 进程消失，已自动重启。\n今日盈亏: {pnl:+.4f} USDT"
            )
        return  # 重启后等下轮再检查

    # ── 2. 检测快照是否过期（系统挂死）──────────────────────
    if snap:
        try:
            snap_ts = datetime.fromisoformat(snap["ts_wall"]).replace(tzinfo=CST)
            age = (datetime.now(CST) - snap_ts).total_seconds()
            if age > NO_TICK_LIMIT:
                log(f"[检测] ⚠️  快照已 {age:.0f}s 未更新（阈值{NO_TICK_LIMIT}s），重启")
                restart_strategy(f"快照{age:.0f}s未更新，系统疑似挂死")
                return
        except Exception:
            pass

    # ── 3. 检测网格激活状态 ───────────────────────────────
    ss = snap.get("status_summary", {})
    grid_active   = ss.get("grid_active", False)
    profit_protect = ss.get("profit_protect", False)

    if not grid_active and not profit_protect and snap:
        if grid_inactive_since == 0.0:
            grid_inactive_since = now
        inactive_secs = now - grid_inactive_since
        if inactive_secs > GRID_INACTIVE_LIMIT:
            log(f"[检测] ⚠️  网格超过 {inactive_secs:.0f}s 未激活，重启")
            restart_strategy(f"网格{inactive_secs:.0f}s未激活")
            grid_inactive_since = 0.0
            return
    else:
        grid_inactive_since = 0.0

    # ── 4. 检测今日亏损上限 ────────────────────────────────
    if pnl < -DAILY_LOSS_LIMIT:
        log(f"[检测] 🛑 今日亏损 {pnl:.2f}U 超过上限 -{DAILY_LOSS_LIMIT}U，暂停系统")
        if can_alert("daily_loss", 3600):
            send_alert(
                f"🛑 ETH Bot 触发日止损 | 今日{pnl:+.1f}U",
                f"今日亏损已达 {pnl:.4f} USDT，超过日止损上限 {DAILY_LOSS_LIMIT} USDT。\n系统已暂停，等待人工确认。"
            )
        subprocess.run(["pkill", "-f", "run_strategy.py"], capture_output=True)
        return

    # ── 5. 检测浮亏告警 ───────────────────────────────────
    unrealized = float(snap.get("unrealized_usdt", 0))
    if unrealized < -DRAWDOWN_ALERT_USDT:
        log(f"[检测] ⚠️  浮亏 {unrealized:.2f}U 超阈值")
        if can_alert("drawdown", 1800):
            send_alert(
                f"⚠️ ETH Bot 浮亏告警 | 浮亏{unrealized:+.1f}U | 今日{pnl:+.1f}U",
                f"当前浮动亏损: {unrealized:.4f} USDT\n今日已实现: {pnl:.4f} USDT\nETH价格: ${snap.get('mid',0):,.2f}"
            )

    # ── 6. 状态日志（每5分钟打印一次）────────────────────────
    if int(now) % 300 < CHECK_INTERVAL_SEC:
        mid = snap.get("mid", 0)
        slots = snap.get("slot_states", {})
        active_slots = [f"{k}:{v}" for k,v in slots.items() if v != "empt"]
        log(f"[状态] ETH=${mid:,.1f} 网格={'激活' if grid_active else '停止'} "
            f"今日={pnl:+.2f}U 浮盈={unrealized:+.2f}U 活跃槽={active_slots}")


def main() -> None:
    log("=" * 50)
    log("[启动] ETH量化实时监控守护进程")
    log(f"[启动] 检查间隔={CHECK_INTERVAL_SEC}s 网格超时={GRID_INACTIVE_LIMIT}s")
    log("=" * 50)
    while True:
        try:
            check_and_act()
        except Exception as e:
            log(f"[错误] 监控循环异常: {e}")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
