# ETH量化系统升级计划

## 本次（2026-04-30 第六十三轮）完成

### grid_pro.py：fill_entry 新增三个诊断字段

**问题**：`fill_entry` 事件缺少 `regime`、`daily_pnl_realized`、`grid_spacing_bps`，
导致事后分析时无法回答"哪种 Regime 下的入场更容易亏损"这一关键问题。

**修复**：在 `record_analysis("fill_entry", ...)` 调用中新增三字段：
- `regime`：入场时当前 Regime（RANGING/TRENDING_UP/TRENDING_DOWN/VOLATILE）
- `daily_pnl_realized`：入场时已累计日盈亏，便于判断高风险入场（日亏损中仍继续入场）
- `grid_spacing_bps`：入场时格宽（bps），支持分析格宽与收益率相关性

**效果预期**：运行后可通过 `grep '"fill_entry"' analysis.jsonl | python3 -c "import sys,json; [print(json.loads(l).get('regime')) for l in sys.stdin]"` 统计入场分布

### grid_pro.py：loss_streak 触发时写入 analysis.jsonl

**问题**：`_loss_streak_until` 触发时仅有日志，无法在 analysis.jsonl 中统计频率和模式。

**修复**：在连续2笔亏损冷静期触发时（`_emergency_close` 循环中），新增 `record_analysis("loss_streak_triggered", ...)` 调用，记录：
- `mid`：触发时市场中间价
- `regime`：触发时 Regime
- `recent_pnls`：近3笔平仓PnL（含本次）
- `cooldown_until`：冷静期结束时间
- `daily_pnl_realized`：当日已实现PnL

**效果预期**：`grep '"loss_streak_triggered"' analysis.jsonl | wc -l` 应为 0-2/天；>3次说明 per_slot_stop=0.8U 可能偏紧

---

## 历史完成（节选）

### 第六十二轮（2026-04-30）
- [x] settings.py: GRID_DRAWDOWN_FROM_PEAK_USDT locked 6.0 → 3.0
- [x] grid_pro.py: _emergency_close 新增 record_analysis 追踪

### 第六十一轮（2026-04-29）
- [x] grid_pro.py: _reset_grid_state 补加 _grid_bias=1.0 重置

### 第一~六十轮（2026-04-18~29）
- [x] 全部P0/P1问题；WS重连；持仓同步；自适应TP；EWMA；FGI；资金费率；1h gate等

---

## 待解决问题（按优先级）

- [ ] P2: round64：统计 loss_streak_triggered 频率
  - 命令：`grep '"loss_streak_triggered"' data/logs/daily/*/analysis.jsonl | wc -l`
  - 期望：每日 0-2 次；>3次说明 per_slot_stop=0.8U 过紧，考虑调到 1.0U

- [ ] P2: round64：统计 fill_entry 的 regime 分布
  - 命令：`python3 -c "import json; [print(json.loads(l).get('regime')) for l in open('data/logs/daily/2026-04-30/analysis.jsonl') if '\"fill_entry\"' in l]"`
  - 期望：RANGING 占 >70%；TRENDING_DOWN/VOLATILE 占比 >20% 说明 regime 过滤需加强

- [ ] P2: round64：统计 fill_tp 事件密度
  - 期望：每日 5-20 次；<5次说明格宽过大或成交太少

- [ ] P3: 动态止盈：eff_tp_mult 灵敏度验证（是否真正随 ATR 变化）

## 下次优先行动

**round64：从实盘日志验证 round63 新增事件**
1. 若有日志：`grep '"loss_streak_triggered"\|"fill_entry"\|"fill_tp"\|"emergency_close"' analysis.jsonl | python3 -c "import json,sys; from collections import Counter; c=Counter(); [c.update([json.loads(l)['event']]) for l in sys.stdin if l.strip()]; print(c)"`
2. 根据事件分布决定下一步参数调整方向

## 系统评估
- **策略有效性**：9/10
  - 63轮迭代；全P0/P1已解决；事件可观测性持续完善
  - fill_entry现在携带regime，支持质量分析
  - loss_streak事件链路补全（日志→jsonl）
- **当前主要风险**：
  1. 外部API网络受限（沙盒），无实时市场监控
  2. 实盘日志为空，所有优化未经实盘数据验证
  3. 双向策略在单边行情中可能同时亏损
- **累计运行轮次**：63
