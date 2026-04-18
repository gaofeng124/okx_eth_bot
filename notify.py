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
SMTP_USER = os.getenv("NOTIFY_SMTP_USER", "")
SMTP_PASS = os.getenv("NOTIFY_SMTP_PASS", "")
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
    raw = sh(["git", "log", f"-{n}", "--format=>>>%n%H|%ai|%s%n%b"])
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
    raw = sh(["git", "diff", "--numstat", "HEAD~1", "HEAD"])
    files = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            files.append({"add": parts[0], "del": parts[1], "file": parts[2]})
    return files

def git_diff_patch() -> str:
    raw = sh(["git", "diff", "HEAD~1", "HEAD", "--", "*.py", "--unified=2"])
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

def today_pnl_from_snapshots() -> float:
    """
    从 pnl_snapshots.jsonl 精确计算今日自然日已实现净盈亏。
    每条记录的 net_realized_pnl_usdt 是当前 run 内的累计值，
    因此取每个 run 最后一条作为该 run 的终值累加即可。
    """
    today = datetime.now(CST).strftime("%Y-%m-%d")
    snapshots_file = PROJECT / "data" / "logs" / "pnl_snapshots.jsonl"
    if not snapshots_file.exists():
        return 0.0
    try:
        run_pnl: dict[str, float] = {}
        with open(snapshots_file, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    ts_utc = d.get("ts_utc", "")
                    ts_dt = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
                    ts_cst_dt = ts_dt.astimezone(CST)
                    if ts_cst_dt.strftime("%Y-%m-%d") != today:
                        continue
                    run_id = d.get("run_id", "unknown")
                    pnl = d.get("net_realized_pnl_usdt", 0.0)
                    if pnl is not None:
                        run_pnl[run_id] = float(pnl)  # 每次覆盖，最终保留最后一条
                except Exception:
                    pass
        return sum(run_pnl.values())
    except Exception:
        return 0.0

def today_fills_detail() -> list[dict]:
    """
    从 pnl_snapshots.jsonl 提取今日每次成交事件（fills_count 变化时）。
    返回可读的成交摘要列表。
    """
    today = datetime.now(CST).strftime("%Y-%m-%d")
    snapshots_file = PROJECT / "data" / "logs" / "pnl_snapshots.jsonl"
    if not snapshots_file.exists():
        return []
    fills = []
    try:
        prev_count_per_run: dict[str, int] = {}
        with open(snapshots_file, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    ts_utc = d.get("ts_utc", "")
                    ts_dt = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
                    ts_cst_dt = ts_dt.astimezone(CST)
                    if ts_cst_dt.strftime("%Y-%m-%d") != today:
                        continue
                    run_id = d.get("run_id", "unknown")
                    fc = int(d.get("fills_count") or 0)
                    prev = prev_count_per_run.get(run_id, 0)
                    if fc > prev:
                        net_pnl = float(d.get("net_realized_pnl_usdt") or 0.0)
                        fees = float(d.get("fees_usdt") or 0.0)
                        fills.append({
                            "ts":      ts_cst_dt.strftime("%H:%M:%S"),
                            "n_fills": fc - prev,
                            "net_pnl": net_pnl,
                            "fees":    fees,
                        })
                    prev_count_per_run[run_id] = fc
                except Exception:
                    pass
    except Exception:
        pass
    return fills

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
                    pnl = d.get("daily_pnl_realized", d.get("status_summary", {}).get("daily_pnl", 0.0))
                    s = d.get("session_tracker", d.get("status_summary", {}).get("session", {}))
                    rows.append({
                        "date":   day_dir.name,
                        "trades": s.get("trades", 0),
                        "pnl":    float(pnl or 0),
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

def agent_report() -> dict:
    try:
        p = PROJECT / "data" / "agent_report.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

# ─────────────────────── HTML 构建 ────────────────────────────
C = {
    "green":  "#16a34a",
    "red":    "#dc2626",
    "blue":   "#2563eb",
    "orange": "#d97706",
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

def kv_grid(items: list[tuple], cols: int = 3) -> str:
    cells = ""
    for label, val, color in items:
        cells += f"""
    <div style="background:{C['light']};border-radius:6px;padding:12px 14px">
      <div style="font-size:11px;color:{C['gray']};margin-bottom:3px">{label}</div>
      <div style="font-size:15px;font-weight:700;color:{color}">{val}</div>
    </div>"""
    return f'<div style="display:grid;grid-template-columns:repeat({cols},1fr);gap:8px">{cells}</div>'

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

def grid_status_display(ss: dict, has_data: bool) -> tuple[str, str]:
    """智能判断网格状态文字和颜色，避免误报。"""
    if not has_data:
        return "—", C["gray"]
    if ss.get("grid_active"):
        return "✅ 运行中", C["green"]
    if ss.get("profit_protect"):
        return "⏸️ 盈利保护中", C["orange"]
    # 检查是否有槽位在等待
    slot_states = ss.get("slots_live", [])
    if slot_states:
        return "⏳ 挂单等待", C["blue"]
    return "⏸️ 等待信号", C["gray"]

def build_html(mode: str) -> str:
    # ── 数据采集 ──────────────────────────────────────────────
    commits    = git_log(6)
    diff_files = git_diff_files() if mode == "upgrade" else []
    diff_patch = git_diff_patch() if mode == "upgrade" else ""
    records    = today_analysis()
    snap       = records[-1] if records else {}
    weeks      = week_summary()
    agent      = agent_report()
    fills      = today_fills_detail()

    ss         = snap.get("status_summary", {})
    session    = snap.get("session_tracker", ss.get("session", {}))
    slots      = snap.get("slot_states", {})
    eth_px     = snap.get("mid", 0.0)
    snap_ts    = ts_cst(snap.get("ts_wall", ""))

    # 今日已实现盈亏：优先从 pnl_snapshots 精算，fallback 到 analysis 快照
    pnl_day_snap = snap.get("daily_pnl_realized",
                            ss.get("daily_pnl", 0.0))
    pnl_day_precise = today_pnl_from_snapshots()
    pnl_day = pnl_day_precise if abs(pnl_day_precise) > 0.001 else float(pnl_day_snap or 0.0)

    mode_bar_cfg = {
        "upgrade": ("#059669", "🚀", "代码升级完成，系统已自动重启"),
        "daily":   ("#2563eb", "📊", "每日交易汇报"),
        "crash":   ("#dc2626", "⚠️",  "交易系统异常退出，守门人已自动重启"),
    }
    bar_color, bar_icon, bar_text = mode_bar_cfg.get(mode, ("#6b7280", "📌", "系统通知"))

    # ── 1. 状态概览（精简为3格）────────────────────────────────
    grid_txt, grid_col = grid_status_display(ss, bool(snap))
    status_block = kv_grid([
        ("ETH 当前价格",   f"${eth_px:,.2f}" if eth_px else "—",  C["dark"]),
        ("今日已实现盈亏", f"{pnl_day:+.1f} U",                   pnl_color(pnl_day)),
        ("网格状态",        grid_txt,                              grid_col),
    ], cols=3)
    if snap_ts:
        status_block += f'<div style="margin-top:8px;font-size:11px;color:{C["gray"]}">快照时间：{snap_ts}</div>'

    # ── 2. 档位详情 ───────────────────────────────────────────
    slots_html = ""
    active_slots = {k: v for k, v in slots.items() if v != "empt"}
    if active_slots:
        slots_html = '<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:12px">'
        center      = ss.get("grid_center", 0.0)
        spacing_bps = ss.get("grid_spacing_bps", 0.0)
        for slot_id in sorted(active_slots.keys(), key=lambda x: int(x)):
            state   = active_slots[slot_id]
            holding = ss.get("slots_holding", {}).get(slot_id, {})
            detail  = ""
            if state == "entr" and center and spacing_bps:
                n      = int(slot_id)
                buy_px = center * (1 - (n + 1) * spacing_bps / 10000)
                detail = f'<div style="font-size:11px;color:{C["blue"]};margin-top:4px">买入挂单 ${buy_px:,.2f}</div>'
            elif state == "hold" and isinstance(holding, dict):
                ep = holding.get("entry_px", 0)
                sz = holding.get("sz", 0)
                ur = holding.get("unrealized_usdt", 0.0)
                detail = (f'<div style="font-size:11px;color:{C["green"]};margin-top:4px">'
                          f'均价 ${ep:,.2f} | {sz}张 | '
                          f'<span style="color:{pnl_color(ur)}">{ur:+.2f}U</span></div>')
            elif state == "cool":
                detail = f'<div style="font-size:11px;color:{C["gray"]};margin-top:4px">冷却中</div>'
            slots_html += f"""
            <div style="background:{C['light']};border:1px solid {C['border']};
                        border-radius:8px;padding:10px 14px;min-width:120px">
              <div style="font-size:11px;color:{C['gray']};margin-bottom:5px">档位 {slot_id}</div>
              {slot_badge(state)}{detail}
            </div>"""
        slots_html += "</div>"

    # 持仓汇总（仅有持仓时显示）
    holding_dict  = ss.get("slots_holding", {})
    total_held    = ss.get("total_held", 0.0)
    vwap          = ss.get("vwap", 0.0)
    unreal_total  = snap.get("unrealized_usdt", 0.0)
    liq_price     = snap.get("liq_price", 0.0)

    holding_rows = []
    for sid, h in holding_dict.items():
        if isinstance(h, dict) and h.get("sz", 0):
            ep  = h.get("entry_px", 0)
            sz  = h.get("sz", 0)
            ur  = h.get("unrealized_usdt", 0.0)
            dur = h.get("hold_secs", 0)
            holding_rows.append([
                f"档位 {sid}",
                f"${ep:,.2f}",
                f"{sz} 张",
                f'<span style="color:{pnl_color(ur)};font-weight:600">{ur:+.2f} U</span>',
                f"{dur//60}分钟" if dur else "—",
            ])

    holding_block = ""
    if holding_rows:
        holding_block = (
            '<div style="margin-top:10px">'
            '<div style="font-size:12px;font-weight:600;color:#374151;margin-bottom:8px">当前持仓明细</div>'
            + table(["档位", "开仓均价", "张数", "浮盈亏", "持仓时长"],
                    holding_rows, ["left", "right", "center", "right", "center"])
            + f'<div style="margin-top:8px;font-size:12px;color:{C["gray"]};display:flex;gap:16px">'
            + f'<span>合计 {total_held:.4f} ETH</span>'
            + (f'<span>均价 ${vwap:,.2f}</span>' if vwap else '')
            + f'<span>浮动 <b style="color:{pnl_color(unreal_total)}">{unreal_total:+.2f} U</b></span>'
            + (f'<span style="color:{C["red"]}">强平 ${liq_price:,.2f}</span>' if liq_price else '')
            + '</div></div>'
        )
    elif not slots_html:
        holding_block = f'<div style="color:{C["gray"]};font-size:13px">当前无持仓，等待信号入场</div>'

    grid_block = slots_html + holding_block

    # ── 3. 今日成交记录 ───────────────────────────────────────
    if fills:
        # 按成交事件分组显示，累计盈亏
        fill_rows = []
        cumulative = 0.0
        for f in fills:
            cumulative += f["net_pnl"]
            fill_rows.append([
                f["ts"],
                f'{f["n_fills"]} 笔',
                f'<span style="color:{pnl_color(f["net_pnl"])};font-weight:600">{f["net_pnl"]:+.2f} U</span>',
                f'<span style="color:{C["red"]}">{f["fees"]:.3f} U</span>',
                f'<span style="color:{pnl_color(cumulative)}">{cumulative:+.2f} U</span>',
            ])
        trade_block = table(
            ["时间", "笔数", "净盈亏", "手续费", "当日累计"],
            fill_rows,
            ["left", "center", "right", "right", "right"]
        )
    else:
        trade_block = f'<div style="color:{C["gray"]};font-size:13px;padding:8px 0">今日暂无成交记录</div>'

    # ── 4. 本次升级改动 ───────────────────────────────────────
    upgrade_block = ""
    if mode == "upgrade":
        file_rows = [[
            f.get("file", ""),
            f'<span style="color:{C["green"]}">+{f.get("add","0")}</span>',
            f'<span style="color:{C["red"]}">-{f.get("del","0")}</span>',
        ] for f in diff_files]

        patch_html = (diff_patch
                      .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
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

    # ── 5. 提交记录（可读摘要）────────────────────────────────
    def commit_summary(subject: str) -> str:
        """把技术性的 commit message 转成用户可读的中文摘要。"""
        s = subject.strip()
        # 去掉前缀标签
        for prefix in ["fix:", "feat:", "refactor:", "chore:", "docs:", "perf:",
                        "fix：", "feat：", "🤖", "🚀"]:
            if s.lower().startswith(prefix.lower()):
                s = s[len(prefix):].strip()
        return s if s else subject

    commit_rows = []
    for c in commits[:5]:
        try:
            t = datetime.fromisoformat(c["time"]).astimezone(CST).strftime("%m-%d %H:%M")
        except Exception:
            t = c["time"][:16]
        summary = commit_summary(c["subject"])
        commit_rows.append([t, summary])

    commit_block = table(["时间", "更新内容"], commit_rows, ["left", "left"])

    # ── 6. 近7日收益 ─────────────────────────────────────────
    week_rows_html = []
    total_pnl = 0.0
    for r in weeks:
        total_pnl += r["pnl"]
        pnl_c = pnl_color(r["pnl"])
        week_rows_html.append([
            r["date"],
            str(r["trades"]),
            f'<span style="color:{pnl_c};font-weight:600">{r["pnl"]:+.1f} U</span>',
            f'{r["wr"]:.0f}%',
            f'{r["hours"]:.1f}h',
            f'<span style="color:{pnl_c}">{r["pph"]:+.2f}/h</span>',
        ])
    week_block = (
        table(["日期", "笔数", "净盈亏", "胜率", "时长", "每小时"],
              week_rows_html,
              ["left", "center", "right", "center", "center", "right"])
        + f'<div style="margin-top:8px;text-align:right;font-size:13px;'
          f'color:{pnl_color(total_pnl)};font-weight:700">'
          f'7日合计：{total_pnl:+.1f} USDT</div>'
        if week_rows_html else
        f'<div style="color:{C["gray"]};font-size:13px">暂无历史数据</div>'
    )

    # ── 7. Agent 市场分析 ─────────────────────────────────────
    agent_block = ""
    if agent:
        ran_at     = agent.get("ran_at", "")
        market     = agent.get("market_summary", "")
        decision   = agent.get("decision", "")
        changes    = agent.get("param_changes", [])
        risk       = agent.get("risk_notes", "")
        next_focus = agent.get("next_focus", "")
        fgi        = agent.get("fear_greed_index", "")
        eth_trend  = agent.get("eth_24h_trend", "")
        funding    = agent.get("funding_rate_trend", "")

        def info_row(label, val, color=None):
            c = color or C["mid"]
            return (f'<div style="margin-bottom:8px">'
                    f'<span style="color:{C["gray"]};font-size:12px">{label}：</span>'
                    f'<span style="color:{c};font-size:13px">{val}</span></div>')

        changes_html = ""
        if changes:
            rows = [[c.get("param",""), str(c.get("old","")), str(c.get("new","")), c.get("reason","")] for c in changes]
            changes_html = (
                '<div style="margin-top:12px;font-size:12px;font-weight:600;'
                'color:#374151;margin-bottom:6px">本次参数调整</div>'
                + table(["参数", "调整前", "调整后", "原因"], rows, ["left","center","center","left"])
            )
        else:
            changes_html = (f'<div style="color:{C["gray"]};font-size:13px;margin-top:10px">'
                            f'✅ 本次分析：参数维持不变</div>')

        agent_block = (
            f'<div style="font-size:11px;color:{C["gray"]};margin-bottom:12px">Agent 运行于 {ran_at}</div>'
            + (info_row("ETH 24h", eth_trend) if eth_trend else "")
            + (info_row("市场研判", market) if market else "")
            + (info_row("恐贪指数", fgi,
                        C["blue"] if "贪婪" in fgi else C["red"] if "恐慌" in fgi else C["mid"])
               if fgi else "")
            + (info_row("资金费率", funding) if funding else "")
            + (info_row("本次决策", decision,
                        C["green"] if any(w in decision for w in ["维持","正常","好转"]) else C["orange"])
               if decision else "")
            + (f'<div style="margin-top:8px;padding:10px;background:#fff7ed;'
               f'border-left:3px solid #f59e0b;border-radius:4px;'
               f'font-size:13px;color:#92400e">⚠️ {risk}</div>' if risk else "")
            + (f'<div style="margin-top:8px;padding:8px 12px;background:#f0fdf4;'
               f'border-radius:4px;font-size:12px;color:#166534">⏭️ 下次关注：{next_focus}</div>'
               if next_focus else "")
            + changes_html
        )

    # ── 组装 ─────────────────────────────────────────────────
    sections = [
        card("📡 交易状态", status_block),
    ]
    if grid_block.strip():
        sections.append(card("📋 档位 & 持仓", grid_block))
    sections.append(card("💰 今日成交记录", trade_block))
    if agent_block:
        sections.append(card("🤖 Agent 市场分析与升级", agent_block))
    if mode == "upgrade" and (diff_files or diff_patch):
        sections.append(card("🔧 本次代码改动", upgrade_block))
    sections += [
        card("📝 最近更新记录", commit_block),
        card("📈 近7日收益", week_block),
    ]

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:20px;background:#f3f4f6;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<div style="max-width:720px;margin:0 auto">

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
    eth  = snap.get("mid", 0.0)
    pnl  = today_pnl_from_snapshots()
    if abs(pnl) < 0.001:
        pnl = float(snap.get("daily_pnl_realized", ss.get("daily_pnl", 0.0)) or 0.0)
    grid_txt, _ = grid_status_display(ss, bool(snap))
    return {
        "upgrade": f"🚀 ETH Bot 升级 | ETH ${eth:,.0f} | 今日 {pnl:+.1f}U | {grid_txt} | {now_cst('%m-%d %H:%M')}",
        "daily":   f"📊 ETH Bot 日报 | {now_cst('%m-%d')} | 今日 {pnl:+.1f}U | ETH ${eth:,.0f}",
        "crash":   f"⚠️ ETH Bot 异常重启 | {now_cst('%m-%d %H:%M')} | 今日 {pnl:+.1f}U",
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
