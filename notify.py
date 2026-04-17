#!/usr/bin/env python3
"""
升级 / 崩溃 / 日报 邮件通知
用法：python notify.py [upgrade|daily|crash]
"""
import json, os, smtplib, subprocess, sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

# ── SMTP 配置 ──────────────────────────────────────────────────
SMTP_HOST = os.getenv("NOTIFY_SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("NOTIFY_SMTP_PORT", "587"))
SMTP_USER = os.getenv("NOTIFY_SMTP_USER", "")   # QQ 邮箱地址
SMTP_PASS = os.getenv("NOTIFY_SMTP_PASS", "")   # QQ 邮箱授权码
NOTIFY_TO = os.getenv("NOTIFY_TO", "1240954013@qq.com")

PROJECT = Path(__file__).parent
LOG_DIR = PROJECT / "data/logs/daily"
CST     = timezone(timedelta(hours=8))

# ─────────────────────── 工具 ──────────────────────────────────
def now_cst(fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.now(CST).strftime(fmt)

def ts_cst(ts: str) -> str:
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return t.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts[:19]

def sh(cmd: list, **kw) -> str:
    try:
        return subprocess.check_output(cmd, cwd=PROJECT, text=True,
                                       stderr=subprocess.DEVNULL, **kw).strip()
    except Exception:
        return ""

def git_log(n=5) -> list[dict]:
    raw = sh(["git", "log", f"-{n}",
              "--format=>>>%n%H|%ai|%s%n%b"])
    blocks, cur = [], {}
    for line in raw.splitlines():
        if line == ">>>":
            if cur:
                blocks.append(cur)
            cur = {}
        elif "|" in line and "hash" not in cur:
            parts = line.split("|", 2)
            cur = {"hash": parts[0][:7],
                   "time": parts[1].strip(),
                   "subject": parts[2].strip() if len(parts) > 2 else "",
                   "body": []}
        elif cur:
            s = line.strip()
            if s and "Co-Authored-By" not in s:
                cur["body"].append(s)
    if cur:
        blocks.append(cur)
    return blocks

def git_diff_files() -> list[dict]:
    """本次提交各文件变更行数"""
    raw = sh(["git", "diff", "--numstat", "HEAD~1", "HEAD"])
    files = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            files.append({"add": parts[0], "del": parts[1], "file": parts[2]})
    return files

def git_diff_patch() -> str:
    """本次提交代码 diff（只看 .py 文件，最多 120 行）"""
    raw = sh(["git", "diff", "HEAD~1", "HEAD", "--", "*.py",
              "--unified=2"])
    lines = raw.splitlines()
    if len(lines) > 120:
        lines = lines[:120] + [f"... (共 {len(lines)} 行，已截断)"]
    return "\n".join(lines)

def today_analysis() -> list[dict]:
    today = datetime.now(CST).strftime("%Y-%m-%d")
    af = LOG_DIR / today / "analysis.jsonl"
    if not af.exists():
        return []
    records = []
    for line in af.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            records.append(json.loads(line))
        except Exception:
            pass
    return records

def today_trades(records: list[dict]) -> list[dict]:
    """从 analysis 里筛出所有成交事件"""
    trades = []
    for r in records:
        if r.get("event") in ("fill", "trade", "slot_closed", "tp_hit", "sl_hit"):
            trades.append(r)
    return trades

def week_summary() -> list[dict]:
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
            lines = [l for l in af.read_text(encoding="utf-8").splitlines() if l.strip()]
            for line in reversed(lines):
                try:
                    d = json.loads(line)
                    s = d.get("status_summary", {}).get("session", {})
                    rows.append({
                        "date":   day_dir.name,
                        "trades": s.get("trades", 0),
                        "pnl":    s.get("net_pnl_usdt", 0.0),
                        "fees":   s.get("total_fees_usdt", 0.0),
                        "wr":     s.get("win_rate", 0.0) * 100,
                        "hours":  s.get("elapsed_hours", 0.0),
                        "pph":    s.get("pnl_per_hour", 0.0),
                    })
                    break
                except Exception:
                    pass
        except Exception:
            pass
    return rows

# ─────────────────────── HTML 构建 ────────────────────────────
C = {
    "green":  "#16a34a",
    "red":    "#dc2626",
    "blue":   "#2563eb",
    "gray":   "#6b7280",
    "light":  "#f9fafb",
    "border": "#e5e7eb",
    "dark":   "#111827",
    "mid":    "#374151",
}

def card(title: str, body: str) -> str:
    return f"""
<div style="margin-bottom:22px">
  <div style="font-size:13px;font-weight:700;color:{C['gray']};
              text-transform:uppercase;letter-spacing:0.8px;
              padding-bottom:8px;border-bottom:2px solid {C['border']};
              margin-bottom:14px">{title}</div>
  {body}
</div>"""

def kv_grid(items: list[tuple]) -> str:
    cells = ""
    for label, val, color in items:
        cells += f"""
    <div style="background:{C['light']};border-radius:6px;padding:12px 14px">
      <div style="font-size:11px;color:{C['gray']};margin-bottom:3px">{label}</div>
      <div style="font-size:15px;font-weight:700;color:{color}">{val}</div>
    </div>"""
    return f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">{cells}</div>'

def table(headers: list, rows: list[list], alignments: list = None) -> str:
    aligns = alignments or ["left"] * len(headers)
    th_row = "".join(
        f'<th style="padding:7px 10px;text-align:{aligns[i]};'
        f'color:{C["gray"]};font-weight:600;font-size:12px;'
        f'background:{C["light"]}">{h}</th>'
        for i, h in enumerate(headers)
    )
    td_rows = ""
    for i, row in enumerate(rows):
        bg = "white" if i % 2 == 0 else C["light"]
        tds = "".join(
            f'<td style="padding:7px 10px;text-align:{aligns[j]};'
            f'font-size:13px;color:{C["mid"]}">{cell}</td>'
            for j, cell in enumerate(row)
        )
        td_rows += f'<tr style="background:{bg}">{tds}</tr>'
    return (f'<table style="width:100%;border-collapse:collapse;'
            f'border:1px solid {C["border"]};border-radius:6px;overflow:hidden">'
            f'<thead><tr>{th_row}</tr></thead><tbody>{td_rows}</tbody></table>')

def slot_badge(state: str) -> str:
    cfg = {
        "entr": ("#dbeafe", "#1d4ed8", "挂单中"),
        "hold": ("#dcfce7", "#15803d", "持仓中"),
        "empt": ("#f3f4f6", "#6b7280", "空闲"),
        "cool": ("#fef9c3", "#a16207", "冷却"),
        "exit": ("#fee2e2", "#b91c1c", "平仓中"),
    }
    bg, color, label = cfg.get(state, ("#f3f4f6", "#6b7280", state))
    return (f'<span style="background:{bg};color:{color};padding:2px 8px;'
            f'border-radius:4px;font-size:12px;font-weight:600">{label}</span>')

def pnl_color(v: float) -> str:
    return C["green"] if v >= 0 else C["red"]

def build_html(mode: str) -> str:
    # ── 数据采集 ──────────────────────────────────────────────
    commits    = git_log(5)
    diff_files = git_diff_files() if mode == "upgrade" else []
    diff_patch = git_diff_patch() if mode == "upgrade" else ""
    records    = today_analysis()
    snap       = records[-1] if records else {}
    trades     = today_trades(records)
    weeks      = week_summary()

    ss      = snap.get("status_summary", {})
    session = ss.get("session", {})
    slots   = snap.get("slot_states", {})
    eth_px  = snap.get("mid", 0.0)
    pnl_day = ss.get("daily_pnl", 0.0)
    snap_ts = ts_cst(snap.get("ts_wall", ""))

    regime_cn = {"ranging": "震荡区间", "trending_up": "上升趋势",
                 "trending_down": "下降趋势", "volatile": "高波动",
                 "warmup": "预热中"}.get(ss.get("regime", ""), ss.get("regime", "-"))
    vol_cn    = {"dead": "冻结", "calm": "平静", "normal": "正常",
                 "elevated": "偏高", "extreme": "极端"}.get(ss.get("vol_regime", ""), "-")

    mode_bar_cfg = {
        "upgrade": ("#059669", "🚀", "代码升级完成，系统已自动重启"),
        "daily":   ("#2563eb", "📊", "每日交易汇报"),
        "crash":   ("#dc2626", "⚠️",  "交易系统异常退出，守门人已自动重启"),
    }
    bar_color, bar_icon, bar_text = mode_bar_cfg.get(mode, ("#6b7280", "📌", "系统通知"))

    # ── 1. 实时状态卡片 ────────────────────────────────────────
    grid_txt = "✅ 已激活" if ss.get("grid_active") else "❌ 未激活"
    grid_col = C["green"] if ss.get("grid_active") else C["red"]
    status_block = kv_grid([
        ("ETH 价格 (USDT)",  f"${eth_px:,.2f}",  C["dark"]),
        ("今日已实现盈亏",   f"{pnl_day:+.4f} U", pnl_color(pnl_day)),
        ("网格状态",          grid_txt,             grid_col),
        ("市场状态",          regime_cn,            C["mid"]),
        ("波动率",            vol_cn,               C["mid"]),
        ("ATR 短期",          f"{ss.get('atr_short_bps', 0):.2f} bps", C["mid"]),
        ("ATR 中期",          f"{ss.get('atr_medium_bps', 0):.2f} bps", C["mid"]),
        ("网格中心价",        f"${ss.get('grid_center', 0):,.2f}", C["mid"]),
        ("格宽",              f"{ss.get('grid_spacing_bps', 0):.1f} bps", C["mid"]),
    ])
    status_block += f'<div style="margin-top:8px;font-size:12px;color:{C["gray"]}">快照时间：{snap_ts}</div>'

    # ── 2. 挂单 & 持仓档位 ────────────────────────────────────
    slots_html = '<div style="display:flex;flex-wrap:wrap;gap:10px">'
    if slots:
        for slot_id in sorted(slots.keys(), key=lambda x: int(x)):
            state = slots[slot_id]
            # 计算挂单价格（中心价 ± n * 格宽）
            center = ss.get("grid_center", 0.0)
            spacing_bps = ss.get("grid_spacing_bps", 0.0)
            n = int(slot_id)
            buy_px = center * (1 - (n + 1) * spacing_bps / 10000)
            sell_px = center * (1 + (n + 1) * spacing_bps / 10000)
            holding = ss.get("slots_holding", {}).get(slot_id, {})
            hold_px = holding.get("entry_px", 0) if holding else 0
            hold_sz = holding.get("sz", 0) if holding else 0
            unreal  = holding.get("unrealized_usdt", 0) if holding else 0

            detail = ""
            if state == "entr":
                detail = f'<div style="font-size:11px;color:{C["blue"]};margin-top:4px">买入挂单 ${buy_px:,.2f}</div>'
            elif state == "hold":
                detail = (f'<div style="font-size:11px;color:{C["green"]};margin-top:4px">'
                          f'持仓均价 ${hold_px:,.2f}<br>'
                          f'数量 {hold_sz} 张 | 浮盈 {unreal:+.4f}U</div>')
            elif state == "cool":
                detail = f'<div style="font-size:11px;color:{C["gray"]};margin-top:4px">冷却后重新挂单</div>'

            slots_html += f"""
            <div style="background:{C['light']};border:1px solid {C['border']};
                        border-radius:8px;padding:12px 16px;min-width:130px">
              <div style="font-size:12px;color:{C['gray']};margin-bottom:6px">档位 {slot_id}</div>
              {slot_badge(state)}
              {detail}
            </div>"""
    else:
        slots_html += f'<div style="color:{C["gray"]};font-size:13px">暂无档位数据</div>'
    slots_html += "</div>"

    # 持仓汇总
    holding_dict = ss.get("slots_holding", {})
    total_held   = ss.get("total_held", 0.0)
    vwap         = ss.get("vwap", 0.0)
    unreal_total = snap.get("unrealized_usdt", 0.0)
    tp_price     = ss.get("tp_price", 0.0)
    liq_price    = snap.get("liq_price", 0.0)
    funding      = ss.get("funding_rate", 0.0)

    holding_rows = []
    for sid, h in holding_dict.items():
        if isinstance(h, dict):
            ep  = h.get("entry_px", 0)
            sz  = h.get("sz", 0)
            ur  = h.get("unrealized_usdt", 0.0)
            dur = h.get("hold_secs", 0)
            holding_rows.append([
                f"档位 {sid}",
                f"${ep:,.2f}",
                f"{sz} 张",
                f'<span style="color:{pnl_color(ur)};font-weight:600">{ur:+.4f} U</span>',
                f"{dur//60}分钟" if dur else "-",
            ])

    holding_block = ""
    if holding_rows:
        holding_block = (
            f'<div style="margin-top:14px">'
            f'<div style="font-size:12px;font-weight:600;color:{C["mid"]};margin-bottom:8px">当前持仓明细</div>'
            + table(["档位", "开仓均价", "张数", "浮盈亏", "持仓时长"],
                    holding_rows, ["left", "right", "center", "right", "center"])
            + f'<div style="margin-top:8px;font-size:12px;color:{C["gray"]}'
              f';display:flex;gap:20px">'
            + f'<span>总持仓：{total_held:.4f} ETH</span>'
            + (f'<span>均价：${vwap:,.2f}</span>' if vwap else '')
            + (f'<span>浮动盈亏：<b style="color:{pnl_color(unreal_total)}">{unreal_total:+.4f} U</b></span>')
            + (f'<span>止盈价：${tp_price:,.2f}</span>' if tp_price else '')
            + (f'<span style="color:{C["red"]}">强平价：${liq_price:,.2f}</span>' if liq_price else '')
            + f'<span>资金费率：{funding:.5f}</span>'
            + '</div></div>'
        )

    grid_block = slots_html + holding_block

    # ── 3. 会话统计 ───────────────────────────────────────────
    net_pnl  = session.get("net_pnl_usdt", 0.0)
    fees     = session.get("total_fees_usdt", 0.0)
    session_block = kv_grid([
        ("运行时长",   f"{session.get('elapsed_hours', 0):.2f} h",  C["mid"]),
        ("成交笔数",   f"{session.get('trades', 0)} 笔",            C["mid"]),
        ("胜率",       f"{session.get('win_rate', 0)*100:.1f}%",    C["mid"]),
        ("净盈亏",     f"{net_pnl:+.4f} U",  pnl_color(net_pnl)),
        ("已付手续费", f"{fees:.4f} U",       C["red"]),
        ("每小时盈亏", f"{session.get('pnl_per_hour', 0):+.4f} U/h", pnl_color(session.get("pnl_per_hour", 0))),
    ])

    # ── 4. 成交记录 ───────────────────────────────────────────
    trade_rows = []
    for t in trades[-10:]:
        side  = t.get("side", "-")
        sc    = C["red"] if side == "sell" else C["blue"]
        trade_rows.append([
            ts_cst(t.get("ts_wall", ""))[-8:],
            f'<span style="color:{sc};font-weight:600">{"卖出" if side=="sell" else "买入"}</span>',
            f'${t.get("fill_px", t.get("px", 0)):,.2f}',
            f'{t.get("sz", "-")} 张',
            f'<span style="color:{pnl_color(t.get("pnl", 0))}">{t.get("pnl", 0):+.4f} U</span>',
            t.get("reason", "-"),
        ])

    trade_block = (
        table(["时间", "方向", "成交价", "数量", "盈亏", "原因"],
              trade_rows, ["left", "center", "right", "center", "right", "left"])
        if trade_rows
        else f'<div style="color:{C["gray"]};font-size:13px;padding:12px 0">本次会话暂无成交记录</div>'
    )

    # ── 5. 本次升级改动 ───────────────────────────────────────
    upgrade_block = ""
    if mode == "upgrade":
        # 文件变更表
        file_rows = [[
            f.get("file", ""),
            f'<span style="color:{C["green"]}">+{f.get("add","0")}</span>',
            f'<span style="color:{C["red"]}">-{f.get("del","0")}</span>',
        ] for f in diff_files]

        patch_html = (diff_patch
                      .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

        # 给 diff 着色
        colored = []
        for line in patch_html.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                colored.append(f'<span style="color:#86efac">{line}</span>')
            elif line.startswith("-") and not line.startswith("---"):
                colored.append(f'<span style="color:#fca5a5">{line}</span>')
            elif line.startswith("@@"):
                colored.append(f'<span style="color:#93c5fd">{line}</span>')
            elif line.startswith("diff ") or line.startswith("index "):
                colored.append(f'<span style="color:#d8b4fe">{line}</span>')
            else:
                colored.append(line)

        upgrade_block = (
            (table(["文件", "新增行", "删除行"], file_rows, ["left", "center", "center"])
             if file_rows else "")
            + (f'<pre style="background:#1e1e1e;color:#d4d4d4;border-radius:8px;'
               f'padding:16px;font-size:12px;line-height:1.5;overflow-x:auto;'
               f'white-space:pre;margin-top:12px">'
               + "\n".join(colored) +
               f'</pre>' if diff_patch.strip() else "")
        )

    # ── 6. 提交记录 ───────────────────────────────────────────
    commit_rows = []
    for c in commits:
        try:
            t = datetime.fromisoformat(c["time"]).astimezone(CST).strftime("%m-%d %H:%M")
        except Exception:
            t = c["time"][:16]
        body_html = ""
        if c.get("body"):
            items = "".join(f'<li style="margin:2px 0;color:{C["gray"]}">{b}</li>'
                            for b in c["body"])
            body_html = f'<ul style="margin:4px 0 0 0;padding-left:18px;font-size:12px">{items}</ul>'
        commit_rows.append([
            f'<code style="color:{C["blue"]}">{c["hash"]}</code>',
            t,
            f'<div>{c["subject"]}</div>{body_html}',
        ])

    commit_block = table(["Hash", "时间", "提交说明"],
                         commit_rows, ["left", "left", "left"])

    # ── 7. 近7日收益 ──────────────────────────────────────────
    week_rows_html = []
    total_pnl = 0.0
    for r in weeks:
        total_pnl += r["pnl"]
        pnl_c = pnl_color(r["pnl"])
        week_rows_html.append([
            r["date"],
            str(r["trades"]),
            f'<span style="color:{pnl_c};font-weight:600">{r["pnl"]:+.4f} U</span>',
            f'{r["wr"]:.1f}%',
            f'{r["hours"]:.1f}h',
            f'<span style="color:{pnl_c}">{r["pph"]:+.4f} U/h</span>',
            f'<span style="color:{pnl_color(r["fees"] if "fees" in r else 0)}">{r.get("fees", 0):.4f} U</span>',
        ])
    week_block = (
        table(["日期", "笔数", "净盈亏", "胜率", "时长", "每小时", "手续费"],
              week_rows_html,
              ["left", "center", "right", "center", "center", "right", "right"])
        + f'<div style="margin-top:8px;text-align:right;font-size:13px;'
          f'color:{pnl_color(total_pnl)};font-weight:700">'
          f'7日合计：{total_pnl:+.4f} USDT</div>'
        if week_rows_html else
        f'<div style="color:{C["gray"]};font-size:13px">暂无历史数据</div>'
    )

    # ── 组装 HTML ─────────────────────────────────────────────
    sections = [
        card("📡 实时行情 & 网格状态", status_block),
        card("📋 档位详情 & 持仓", grid_block),
        card("⚡ 本次会话统计", session_block),
        card("💰 成交记录（最近10笔）", trade_block),
    ]
    if mode == "upgrade" and (diff_files or diff_patch):
        sections.append(card("🔧 本次升级改动", upgrade_block))
    sections += [
        card("📝 最近提交记录", commit_block),
        card("📈 近7日收益汇总", week_block),
    ]

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:20px;background:#f3f4f6;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<div style="max-width:720px;margin:0 auto">

  <!-- 顶栏 -->
  <div style="background:{bar_color};color:white;border-radius:10px 10px 0 0;
              padding:20px 28px;display:flex;align-items:center;gap:12px">
    <span style="font-size:28px">{bar_icon}</span>
    <div>
      <div style="font-size:18px;font-weight:700">ETH 量化交易系统</div>
      <div style="font-size:13px;opacity:0.85;margin-top:2px">{bar_text}</div>
    </div>
    <div style="margin-left:auto;text-align:right;font-size:12px;opacity:0.85">
      OKX · ETH-USDT-SWAP · 10x<br>{now_cst()}
    </div>
  </div>

  <!-- 正文 -->
  <div style="background:white;border-radius:0 0 10px 10px;
              padding:24px 28px;box-shadow:0 2px 8px rgba(0,0,0,.08)">
    {"".join(sections)}
  </div>

  <div style="text-align:center;font-size:11px;color:{C['gray']};
              margin-top:14px;padding-bottom:20px">
    自动发送 · {now_cst()} · 服务器 8.208.25.221
  </div>
</div>
</body></html>"""


def get_subject(mode: str, snap: dict) -> str:
    ss   = snap.get("status_summary", {})
    pnl  = ss.get("daily_pnl", 0.0)
    eth  = snap.get("mid", 0.0)
    grid = "✅网格激活" if ss.get("grid_active") else "❌网格停止"
    return {
        "upgrade": f"🚀 ETH Bot 升级完成 | ETH ${eth:,.0f} | 今日 {pnl:+.4f}U | {grid} | {now_cst('%m-%d %H:%M')}",
        "daily":   f"📊 ETH Bot 日报 | {now_cst('%m-%d')} | 今日 {pnl:+.4f}U | ETH ${eth:,.0f}",
        "crash":   f"⚠️ ETH Bot 异常告警 | 正在重启 | {now_cst('%m-%d %H:%M')}",
    }.get(mode, f"ETH Bot 通知 | {now_cst()}")


def send(subject: str, html: str) -> bool:
    if not SMTP_USER or not SMTP_PASS:
        print("[notify] ⚠️  NOTIFY_SMTP_USER / NOTIFY_SMTP_PASS 未配置，跳过")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"ETH Bot <{SMTP_USER}>"
        msg["To"]      = NOTIFY_TO
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.ehlo(); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [NOTIFY_TO], msg.as_string())
        print(f"[notify] ✅ 已发送 → {NOTIFY_TO}")
        return True
    except Exception as e:
        print(f"[notify] ❌ 发送失败: {e}")
        return False


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "upgrade"
    if mode not in ("upgrade", "daily", "crash"):
        print("用法: python notify.py [upgrade|daily|crash]"); sys.exit(1)
    snap = today_analysis()
    snap = snap[-1] if snap else {}
    send(get_subject(mode, snap), build_html(mode))
