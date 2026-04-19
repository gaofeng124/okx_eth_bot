#!/usr/bin/env python3
"""
常驻 AI 大脑 — 24h 监控 OKX ETH-USDT-SWAP 量化系统

通过 Claude Code CLI 子进程调用，使用户的 claude.ai 订阅配额
（走 OAuth token，不走按量计费的 API key）。

每轮：
1. 拼 prompt，启动 `claude -p`
2. Claude 自主使用 Bash/Read/Edit/Write/Glob/Grep 工具做分析决策
3. Claude 在 data/agent_report.json 写入 next_sleep_seconds 决定下轮间隔
4. 主循环按此间隔 sleep，继续下一轮

用法：systemctl start ai-brain.service
"""
from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import time
import traceback
from pathlib import Path


PROJ = Path("/root/okx_eth_bot")
CLAUDE_BIN = "/opt/nodejs/bin/claude"
STATE_PATH = PROJ / "data" / "ai_state.json"
AI_LOG = PROJ / "data" / "logs" / "ai_brain.log"
REPORT_PATH = PROJ / "data" / "agent_report.json"

# 每轮 Claude CLI 最长允许跑 15 分钟（长尾兜底）
CLAUDE_RUN_TIMEOUT_SEC = 900
# Claude 内部最多迭代多少轮工具
MAX_TURNS = 80
# 默认间隔与上下限
DEFAULT_SLEEP_SEC = 600
MIN_SLEEP_SEC = 30
MAX_SLEEP_SEC = 1800
# CLI 启动失败退避
ERROR_SLEEP_SEC = 120

SYSTEM_PROMPT = """你是 ETH-USDT-SWAP 量化网格交易机器人的常驻 AI 大脑。
你在生产服务器上作为守护进程运行，每次唤醒都是真实 AI 思考。
使命：让账户（~42 USDT）稳定日收益 2 USDT 保底，远期 30 USDT/日。
不要敷衍、不要写死逻辑，按市场状态自主判断。

## 系统架构
- run_strategy.py（grid_pro）每 tick 决策：开/撤格、TP、per_slot_stop 硬止损（反射层）
- watchdog.sh 智能模式：本地 HEAD 变 → 重启；origin 变 → rebase + 重启；进程挂 → 重启
- 你（AI 层）：分析趋势、调参、修 Bug、异常处置

## 你的工作目录与工具
你被启动时 cwd=/root/okx_eth_bot。可用 Claude Code 内置工具：Bash / Read / Edit / Write / Glob / Grep。

## 关键文件
- quant/strategy/grid_pro.py（~1650 行主策略）
- quant/settings.py class D + .env 覆盖
- quant/app/runner.py
- watchdog.sh
- data/logs/pnl_snapshots.jsonl（每 ~50s 一条 PnL 快照）
- data/logs/daily/YYYY-MM-DD/{analysis,system,market}.jsonl
- data/agent_report.json（你上轮的报告 —— 必读）
- data/upgrade_plan.md（累积修改历史 —— 追加写）
- data/logs/ai_brain.log（你的 daemon 日志）
- .env（OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE / GRID_* 参数）

## 硬风控底线（绝对不可违）
- GRID_LEVERAGE ≤ 5
- GRID_CONTRACTS_PER_SLOT ≤ 0.3（当前 0.2）
- GRID_PER_SLOT_STOP_USDT ≤ 1.5
- GRID_WHOLE_STOP_USDT ≤ 3.0
- GRID_DAILY_STOP_USDT ≤ 3.0
- GRID_DRAWDOWN_FROM_PEAK_USDT ≤ 2.0
- RISK_MAX_NOTIONAL_USDT ≤ 120
- 不可移除 watchdog / systemd / 紧急平仓 / 单仓硬止损

## 调 OKX API（使用服务器上 /tmp/okx_call.py 辅助脚本）
```bash
cat > /tmp/okx_call.py << 'PY'
import os, sys, time, hmac, base64, hashlib, json
from dotenv import load_dotenv; load_dotenv("/root/okx_eth_bot/.env")
import httpx
method = sys.argv[1]
path = sys.argv[2]
body = sys.argv[3] if len(sys.argv) > 3 else ""
ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
secret = os.environ["OKX_SECRET_KEY"]
msg = f"{ts}{method}{path}{body}"
sig = base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()
h = {"OK-ACCESS-KEY": os.environ["OKX_API_KEY"], "OK-ACCESS-SIGN": sig,
     "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": os.environ["OKX_PASSPHRASE"],
     "Content-Type": "application/json"}
if method == "GET": r = httpx.get("https://www.okx.com"+path, headers=h, timeout=30)
else: r = httpx.post("https://www.okx.com"+path, headers=h, content=body, timeout=30)
print(r.text)
PY
# 调用示例
/root/okx_eth_bot/.venv/bin/python3 /tmp/okx_call.py GET /api/v5/account/balance
/root/okx_eth_bot/.venv/bin/python3 /tmp/okx_call.py GET "/api/v5/account/positions?instId=ETH-USDT-SWAP"
/root/okx_eth_bot/.venv/bin/python3 /tmp/okx_call.py GET "/api/v5/trade/fills-history?instType=SWAP&instId=ETH-USDT-SWAP&limit=100"
# 平仓（紧急）：
/root/okx_eth_bot/.venv/bin/python3 /tmp/okx_call.py POST /api/v5/trade/close-position '{"instId":"ETH-USDT-SWAP","mgnMode":"isolated","ccy":"USDT","autoCxl":true}'
```

## 每轮工作流
1. Read data/agent_report.json（上轮的 round / decision / hypothesis / next_focus）
2. 收集状态（用 Bash）：
   - `ps -ef | grep run_strategy | grep -v grep`
   - `tail -50 data/logs/pnl_snapshots.jsonl`
   - `DATE=$(date -u +%Y-%m-%d); tail -30 data/logs/daily/$DATE/analysis.jsonl`
   - 上面的 okx_call.py 查余额 / 持仓 / 近 100 笔成交
3. 分析：权益 Δ / 胜率 / 单笔最大亏损 / Regime 分布 / 熔断记录 / 上轮 hypothesis 成立否
4. 决策：
   - 严重异常 → 用 okx_call.py POST close-position 平仓，然后 `cd /root/okx_eth_bot && .venv/bin/python notify.py upgrade` 发邮件（主题要自己改成带 🚨 的紧急标题）
   - 调参 / 改 Bug → Edit 工具修 → Bash `cd /root/okx_eth_bot && git add -A && git commit -m "🤖 [轮 N] 说明"`（本地 commit，不 push；watchdog 会识别 HEAD 变化自动重启）
   - 一切正常 → 只 Write 新的 agent_report.json 即可
5. 决定下次唤醒间隔：
   - 刚 commit 等 watchdog 重启 → 180-300
   - 成交活跃 / Regime 切换 → 120-300
   - 平稳震荡 → 600-1200
   - 市场极静 / 已达日目标 → 1200-1800
   - 未解决异常 → 30-60

## agent_report.json 结构（本轮结束前必须更新）
```json
{
  "round": <当前轮次 int>,
  "ran_at": "<ISO UTC>",
  "account_equity_usdt": <float>,
  "account_avail_usdt": <float>,
  "pnl_since_last_round_usdt": <float>,
  "fills_since_last_round": <int>,
  "max_single_loss_24h": <float>,
  "restarts_24h": <int>,
  "current_position": "<描述或 none>",
  "decision": "<本轮做了什么，简短一句>",
  "changes_made": ["<文件:行 → 变化>" ...],
  "hypothesis": "<对下轮的预期，可被验证>",
  "next_focus": "<下轮重点看什么>",
  "next_sleep_seconds": <30-1800 int，必填>
}
```

## 纪律
- 先 Read agent_report.json 再动手，不重复上轮工作
- 累计亏损 > 15 USDT → 立刻降 GRID_CONTRACTS_PER_SLOT
- 连续 3 轮亏损加剧 → 暂停策略（Edit .env STRAT_LIVE=0 + Bash `pkill -f run_strategy.py`）+ 发 🚨 邮件
- 决策要给理由、不盲调；日志少就延长 next_sleep
- 所有修改 commit（本地，不 push）
- changes_made 必须精确指明「文件:行号 → 新值」
- 改代码后给自己留足 hypothesis 下轮验证

## 本轮结束前必做
1. 把 agent_report.json 写完（含 next_sleep_seconds）
2. 如有改动 commit
3. 输出一行总结（权益 / PnL / 本轮改动数 / 下轮重点）
"""


_running = True


def _on_signal(signum, frame):
    global _running
    _running = False


def log(msg: str) -> None:
    ts = datetime.datetime.utcnow().isoformat()
    line = f"[{ts}] {msg}"
    AI_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AI_LOG, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def _read_next_sleep() -> int:
    try:
        report = json.loads(REPORT_PATH.read_text())
        val = int(report.get("next_sleep_seconds", DEFAULT_SLEEP_SEC))
    except Exception as e:
        log(f"读 agent_report.json next_sleep_seconds 失败: {e}，用默认 {DEFAULT_SLEEP_SEC}")
        val = DEFAULT_SLEEP_SEC
    return max(MIN_SLEEP_SEC, min(MAX_SLEEP_SEC, val))


HEARTBEAT_PATH = Path("/tmp/agent.running")


def _touch_heartbeat() -> None:
    """写心跳文件，让老的 4 个 scheduled agent 的 lock 逻辑自动 no-op。"""
    try:
        HEARTBEAT_PATH.touch()
    except Exception:
        pass


def iteration(round_n: int) -> int:
    log(f"=== 轮 {round_n} 开始 ===")
    start = time.time()
    _touch_heartbeat()

    prompt = (
        f"第 {round_n} 轮巡检。按系统提示的工作流做完全流程：读 agent_report.json "
        f"→ 收集状态 → 分析 → 决策执行 → 更新 agent_report.json（必填 next_sleep_seconds）。"
        f"当前 UTC 时间 {datetime.datetime.utcnow().isoformat()}。"
    )

    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--append-system-prompt", SYSTEM_PROMPT,
        "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep",
        "--output-format", "json",
        "--max-turns", str(MAX_TURNS),
    ]

    env = os.environ.copy()
    env["PATH"] = "/opt/nodejs/bin:" + env.get("PATH", "")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_RUN_TIMEOUT_SEC,
            cwd=str(PROJ),
            env=env,
        )
    except subprocess.TimeoutExpired:
        log(f"轮 {round_n} Claude CLI 超时 ({CLAUDE_RUN_TIMEOUT_SEC}s)")
        return ERROR_SLEEP_SEC

    elapsed = time.time() - start

    if result.returncode != 0:
        log(
            f"轮 {round_n} Claude CLI 失败 rc={result.returncode} "
            f"stderr={result.stderr[:500]!r}"
        )
        return ERROR_SLEEP_SEC

    # 解析 CLI 输出
    cost = 0.0
    turns = 0
    is_error = False
    summary_text = ""
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
        cost = float(data.get("total_cost_usd") or 0)
        turns = int(data.get("num_turns") or 0)
        is_error = bool(data.get("is_error"))
        summary_text = (data.get("result") or "")[:300]
    except Exception as e:
        log(f"解析 CLI JSON 输出失败: {e}; stdout 前 500 字符={result.stdout[:500]!r}")

    sleep_sec = _read_next_sleep()

    log(
        f"轮 {round_n} 完成: elapsed={elapsed:.1f}s turns={turns} "
        f"cost=${cost:.4f} is_error={is_error} next_sleep={sleep_sec}s "
        f"result={summary_text!r}"
    )
    return sleep_sec


def main() -> None:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(STATE_PATH.read_text())
    except Exception:
        state = {"round": 0}

    log(f"ai_brain daemon 启动，model=claude-code-default (sonnet-4-6)，从 round={state['round']} 开始")

    while _running:
        state["round"] += 1
        STATE_PATH.write_text(json.dumps(state, indent=2))
        try:
            sleep_sec = iteration(state["round"])
        except Exception:
            log(f"轮 {state['round']} 异常:\n{traceback.format_exc()}")
            sleep_sec = ERROR_SLEEP_SEC

        sleep_sec = max(MIN_SLEEP_SEC, min(MAX_SLEEP_SEC, int(sleep_sec)))
        log(f"sleep {sleep_sec}s 进入下一轮")

        # 睡眠期间持续刷心跳，确保旧 scheduled agent 的 lock 检查一直命中
        slept = 0
        while _running and slept < sleep_sec:
            chunk = min(60, sleep_sec - slept)
            time.sleep(chunk)
            slept += chunk
            _touch_heartbeat()

    log("ai_brain daemon 退出")


if __name__ == "__main__":
    main()
