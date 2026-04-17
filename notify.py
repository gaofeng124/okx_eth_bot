#!/usr/bin/env python3
"""
升级通知邮件发送器
用法：python notify.py [upgrade|daily|crash]
  upgrade  — 代码升级后调用（watchdog 自动触发）
  daily    — 每日汇报（可加入 cron）
  crash    — 交易系统崩溃告警
"""
import json
import os
import smtplib
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── 配置（从 .env 读取）──────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

SMTP_HOST     = os.getenv("NOTIFY_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("NOTIFY_SMTP_PORT", "587"))
SMTP_USER     = os.getenv("NOTIFY_SMTP_USER", "")        # 发件邮箱
SMTP_PASS     = os.getenv("NOTIFY_SMTP_PASS", "")        # 授权码 / App Password
NOTIFY_TO     = os.getenv("NOTIFY_TO", "1240954013gao@gmail.com")

PROJECT = Path(__file__).parent
LOG_DIR = PROJECT / "data/logs/daily"
CST     = timezone(timedelta(hours=8))


# ── 工具函数 ─────────────────────────────────────────────────────
def now_cst(fmt="%Y-%m-%d %H:%M:%S") -> str:
    return datetime.now(CST).strftime(fmt)

def ts_cst(ts: str) -> str:
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return t.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts

def git_log(n=5) -> list[dict]:
    """最近 n 条提交"""
    try:
        out = subprocess.check_output(
            ["git", "log", f"-{n}", "--format=%H|%ai|%s|%b"],
            cwd=PROJECT, text=True, stderr=subprocess.DEVNULL
        )
        result = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("|", 3)
            result.append({
                "hash":    parts[0][:7] if len(parts) > 0 else "",
                "time":    parts[1].strip() if len(parts) > 1 else "",
                "subject": parts[2].strip() if len(parts) > 2 else "",
                "body":    parts[3].strip() if len(parts) > 3 else "",
            })
        return result
    except Exception:
        return []

def git_diff_stat() -> str:
    """最新提交改了哪些文件"""
    try:
        return subprocess.check_output(
            ["git", "diff", "--stat", "HEAD~1", "HEAD"],
            cwd=PROJECT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "（无法获取 diff）"

def trading_status() -> dict:
    """读取今日最新交易快照"""
    today = datetime.now(CST).strftime("%Y-%m-%d")
    af = LOG_DIR / today / "analysis.jsonl"
    if not af.exists():
        return {}
    try:
        lines = af.read_text(encoding="utf-8").strip().split("\n")
        for line in reversed(lines):
            try:
                return json.loads(line)
            except Exception:
                continue
    except Exception:
        pass
    return {}

def week_summary() -> list[dict]:
    """最近 7 天收益汇总"""
    rows = []
    if not LOG_DIR.exists():
        return rows
    for day_dir in sorted(LOG_DIR.iterdir(), reverse=True)[:7]:
        if not day_dir.is_dir():
            continue
        af = day_dir / "analysis.jsonl"
        if not af.exists():
            continue
        try:
            lines = af.read_text(encoding="utf-8").strip().split("\n")
            for line in reversed(lines):
                try:
                    d = json.loads(line)
                    s = d.get("status_summary", {}).get("session", {})
                    rows.append({
                        "date":   day_dir.name,
                        "trades": s.get("trades", 0),
                        "pnl":    s.get("net_pnl_usdt", 0.0),
                        "wr":     s.get("win_rate", 0.0) * 100,
                        "hours":  s.get("elapsed_hours", 0.0),
                        "pph":    s.get("pnl_per_hour", 0.0),
                    })
                    break
                except Exception:
                    continue
        except Exception:
            pass
    return rows


# ── HTML 模板 ────────────────────────────────────────────────────
def build_html(mode: str, commits: list[dict], diff: str,
               snap: dict, weeks: list[dict]) -> str:
    ss = snap.get("status_summary", {})
    session = ss.get("session", {})

    # 颜色
    grid_color  = "#27ae60" if ss.get("grid_active") else "#e74c3c"
    grid_text   = "✅ 已激活" if ss.get("grid_active") else "❌ 未激活"
    pnl_val     = ss.get("daily_pnl", 0.0)
    pnl_color   = "#27ae60" if pnl_val >= 0 else "#e74c3c"
    regime_map  = {"ranging": "震荡", "trending_up": "上升趋势",
                   "trending_down": "下降趋势", "volatile": "高波动", "warmup": "预热中"}
    regime_cn   = regime_map.get(ss.get("regime", ""), ss.get("regime", "-"))
    vol_map     = {"dead": "冻结", "calm": "平静", "normal": "正常",
                   "elevated": "偏高", "extreme": "极端"}
    vol_cn      = vol_map.get(ss.get("vol_regime", ""), ss.get("vol_regime", "-"))

    subject_map = {
        "upgrade": f"🚀 系统升级完成 | {now_cst()}",
        "daily":   f"📊 每日汇报 | {now_cst('%Y-%m-%d')}",
        "crash":   f"⚠️ 交易系统异常 | {now_cst()}",
    }

    # 提交记录行
    commit_rows = ""
    for c in commits:
        try:
            t = datetime.fromisoformat(c["time"])
            t_cst = t.astimezone(CST).strftime("%m-%d %H:%M")
        except Exception:
            t_cst = c["time"][:16]
        commit_rows += f"""
        <tr>
          <td style="padding:6px 10px;color:#7f8c8d;font-family:monospace">{c['hash']}</td>
          <td style="padding:6px 10px;color:#555">{t_cst}</td>
          <td style="padding:6px 10px;color:#2c3e50">{c['subject']}</td>
        </tr>"""

    # 周收益行
    week_rows = ""
    for r in weeks:
        pnl_c = "#27ae60" if r["pnl"] >= 0 else "#e74c3c"
        week_rows += f"""
        <tr>
          <td style="padding:5px 10px">{r['date']}</td>
          <td style="padding:5px 10px;text-align:center">{r['trades']}</td>
          <td style="padding:5px 10px;text-align:right;color:{pnl_c};font-weight:bold">{r['pnl']:+.4f} USDT</td>
          <td style="padding:5px 10px;text-align:center">{r['wr']:.1f}%</td>
          <td style="padding:5px 10px;text-align:center">{r['hours']:.1f}h</td>
          <td style="padding:5px 10px;text-align:right;color:{pnl_c}">{r['pph']:+.4f}/h</td>
        </tr>"""

    # diff 文本
    diff_html = diff.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    mode_banner = {
        "upgrade": ('<div style="background:#27ae60;color:white;padding:12px 20px;'
                    'border-radius:6px;font-size:15px">🚀 代码升级完成，系统已自动重启</div>'),
        "daily":   ('<div style="background:#2980b9;color:white;padding:12px 20px;'
                    'border-radius:6px;font-size:15px">📊 每日交易汇报</div>'),
        "crash":   ('<div style="background:#e74c3c;color:white;padding:12px 20px;'
                    'border-radius:6px;font-size:15px">⚠️ 交易系统异常退出，守门人正在尝试重启</div>'),
    }.get(mode, "")

    snap_time = ts_cst(snap.get("ts_wall", "")) if snap else "无数据"
    eth_price = snap.get("mid", 0)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:'Helvetica Neue',Arial,sans-serif">
<div style="max-width:680px;margin:30px auto;background:white;border-radius:12px;
            box-shadow:0 2px 12px rgba(0,0,0,0.1);overflow:hidden">

  <!-- 顶部标题 -->
  <div style="background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);
              padding:28px 30px;color:white">
    <div style="font-size:22px;font-weight:700;letter-spacing:0.5px">
      ETH 量化交易系统
    </div>
    <div style="font-size:13px;color:#aab;margin-top:4px">
      OKX · ETH-USDT-SWAP · 10x 网格策略
    </div>
    <div style="margin-top:16px">{mode_banner}</div>
  </div>

  <div style="padding:24px 30px">

    <!-- 实时行情卡片 -->
    <div style="background:#f8f9fa;border-radius:8px;padding:20px;margin-bottom:24px">
      <div style="font-size:13px;color:#7f8c8d;margin-bottom:12px">
        📡 实时状态 · 更新于 {snap_time}
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:16px">
        <div style="flex:1;min-width:120px">
          <div style="font-size:12px;color:#95a5a6">ETH 价格</div>
          <div style="font-size:24px;font-weight:700;color:#2c3e50">
            ${eth_price:,.2f}
          </div>
        </div>
        <div style="flex:1;min-width:120px">
          <div style="font-size:12px;color:#95a5a6">今日盈亏</div>
          <div style="font-size:24px;font-weight:700;color:{pnl_color}">
            {pnl_val:+.4f} U
          </div>
        </div>
        <div style="flex:1;min-width:120px">
          <div style="font-size:12px;color:#95a5a6">网格状态</div>
          <div style="font-size:16px;font-weight:600;color:{grid_color}">{grid_text}</div>
        </div>
      </div>
      <div style="margin-top:16px;display:flex;flex-wrap:wrap;gap:10px">
        {''.join(f'''<span style="background:#eee;border-radius:4px;padding:4px 10px;
                      font-size:12px;color:#555">{k}：{v}</span>''' for k, v in [
            ("市场状态", regime_cn),
            ("波动率",   vol_cn),
            ("ATR短期",  f"{ss.get('atr_short_bps', 0):.2f} bps"),
            ("格宽",     f"{ss.get('grid_spacing_bps', 0):.1f} bps"),
            ("挂单档位", str(ss.get('slots_live', []))),
            ("持仓档位", str(list(ss.get('slots_holding', {{}}).keys()))),
        ])}
      </div>
    </div>

    <!-- 本次会话统计 -->
    <div style="margin-bottom:24px">
      <div style="font-size:14px;font-weight:600;color:#2c3e50;margin-bottom:12px">
        ⚡ 本次会话
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:12px">
        {''.join(f'''<div style="flex:1;min-width:100px;background:#f8f9fa;
                     border-radius:6px;padding:12px;text-align:center">
                     <div style="font-size:11px;color:#95a5a6">{label}</div>
                     <div style="font-size:16px;font-weight:600;color:#2c3e50">{val}</div>
                   </div>''' for label, val in [
            ("运行时长",   f"{session.get('elapsed_hours', 0):.2f}h"),
            ("成交笔数",   f"{session.get('trades', 0)} 笔"),
            ("胜率",       f"{session.get('win_rate', 0)*100:.1f}%"),
            ("净盈亏",     f"{session.get('net_pnl_usdt', 0):+.4f} U"),
            ("每小时",     f"{session.get('pnl_per_hour', 0):+.4f} U/h"),
        ])}
      </div>
    </div>

    <!-- 最近提交 -->
    <div style="margin-bottom:24px">
      <div style="font-size:14px;font-weight:600;color:#2c3e50;margin-bottom:12px">
        📝 最近提交记录
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead>
          <tr style="background:#f8f9fa">
            <th style="padding:8px 10px;text-align:left;color:#7f8c8d;font-weight:500">Hash</th>
            <th style="padding:8px 10px;text-align:left;color:#7f8c8d;font-weight:500">时间</th>
            <th style="padding:8px 10px;text-align:left;color:#7f8c8d;font-weight:500">内容</th>
          </tr>
        </thead>
        <tbody>{commit_rows}</tbody>
      </table>
    </div>

    <!-- 本次升级改动 -->
    {f'''<div style="margin-bottom:24px">
      <div style="font-size:14px;font-weight:600;color:#2c3e50;margin-bottom:12px">
        🔧 本次升级改动
      </div>
      <pre style="background:#1e1e1e;color:#d4d4d4;border-radius:6px;padding:16px;
                  font-size:12px;overflow-x:auto;white-space:pre-wrap">{diff_html}</pre>
    </div>''' if diff_html.strip() else ''}

    <!-- 近7日收益 -->
    {f'''<div style="margin-bottom:24px">
      <div style="font-size:14px;font-weight:600;color:#2c3e50;margin-bottom:12px">
        📈 近7日收益
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead>
          <tr style="background:#f8f9fa">
            <th style="padding:7px 10px;text-align:left;color:#7f8c8d;font-weight:500">日期</th>
            <th style="padding:7px 10px;text-align:center;color:#7f8c8d;font-weight:500">笔数</th>
            <th style="padding:7px 10px;text-align:right;color:#7f8c8d;font-weight:500">净盈亏</th>
            <th style="padding:7px 10px;text-align:center;color:#7f8c8d;font-weight:500">胜率</th>
            <th style="padding:7px 10px;text-align:center;color:#7f8c8d;font-weight:500">时长</th>
            <th style="padding:7px 10px;text-align:right;color:#7f8c8d;font-weight:500">每小时</th>
          </tr>
        </thead>
        <tbody>{week_rows}</tbody>
      </table>
    </div>''' if weeks else ''}

  </div>

  <!-- 底部 -->
  <div style="background:#f8f9fa;padding:16px 30px;border-top:1px solid #eee;
              font-size:12px;color:#95a5a6;text-align:center">
    此邮件由云服务器自动发送 · ETH Bot · {now_cst()}
  </div>
</div>
</body>
</html>"""


def get_subject(mode: str, snap: dict) -> str:
    pnl = snap.get("status_summary", {}).get("daily_pnl", 0.0)
    pnl_str = f"{pnl:+.4f} U"
    eth = snap.get("mid", 0)
    return {
        "upgrade": f"🚀 ETH Bot 升级完成 | ETH ${eth:,.0f} | 今日 {pnl_str} | {now_cst('%m-%d %H:%M')}",
        "daily":   f"📊 ETH Bot 日报 | 今日 {pnl_str} | ETH ${eth:,.0f} | {now_cst('%m-%d')}",
        "crash":   f"⚠️ ETH Bot 异常 | 正在重启 | {now_cst('%m-%d %H:%M')}",
    }.get(mode, f"ETH Bot 通知 | {now_cst()}")


def send_email(subject: str, html: str) -> bool:
    if not SMTP_USER or not SMTP_PASS:
        print("[notify] ⚠️  NOTIFY_SMTP_USER / NOTIFY_SMTP_PASS 未配置，跳过发送")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"ETH Bot <{SMTP_USER}>"
        msg["To"]      = NOTIFY_TO
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [NOTIFY_TO], msg.as_string())
        print(f"[notify] ✅ 邮件已发送 → {NOTIFY_TO}")
        return True
    except Exception as e:
        print(f"[notify] ❌ 发送失败: {e}")
        return False


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "upgrade"
    if mode not in ("upgrade", "daily", "crash"):
        print("用法: python notify.py [upgrade|daily|crash]")
        sys.exit(1)

    commits = git_log(5)
    diff    = git_diff_stat() if mode == "upgrade" else ""
    snap    = trading_status()
    weeks   = week_summary()

    html    = build_html(mode, commits, diff, snap, weeks)
    subject = get_subject(mode, snap)

    send_email(subject, html)
