# ETH量化系统升级计划

## 本次（2026-04-30 第六十六轮）完成

### grid_pro.py：修复 _market_close_all 中 loss_streak 会话粒度 bug

**问题（bug 描述）**：
`_market_close_all` 在 `for s in held:` 循环内，对每个 slot 单独 append `net_after` 到 `_recent_close_pnls(maxlen=3)` 并立刻检查连亏条件。

当网格有 3 个 HOLDING slot 全部亏损时执行一次紧急平仓：
- slot1 append(-x1) → 检查 → 不触发
- slot2 append(-x2) → 检查：最后2个 [-x1, -x2] 均负 → **loss_streak 立即触发！**
- slot3 append(-x3) → 检查：重复触发

结果：**1次单一紧急平仓会话** 就触发30分钟冷静禁交期，相当于全天最多交易 24h/(0.5h wait + grid time) ≈ 严重限制交易机会。

**修复**：
- 引入 `session_net = 0.0`，循环中只累加，不 append 不检查
- 循环结束后一次性 `append(session_net)` 并做 loss_streak 检查
- 语义变为：**2次独立的紧急平仓会话均亏损** 才触发冷静期（正确行为）

**效果预期**：
- loss_streak 触发频率从 "任一3-slot亏损紧急平仓" → "2次独立亏损会话"
- 每日有效交易时间显著提升（减少不必要的30分钟封锁）
- 与 per_slot_stop=1.0U（round65）协同：既延长了单 slot 持仓时间，又降低了整体平仓的冷静代价

---

## 历史完成（节选）

### 第六十五轮（2026-04-30）
- [x] settings.py: GRID_PER_SLOT_STOP_USDT 0.8 → 1.0，减少ATR正常范围(30bps)下的噪声止损触发

### 第六十四轮（2026-04-30）
- [x] grid_pro.py: _place_grid 新增 record_analysis('grid_opened') 事件，完成网格会话事件链路

### 第六十三轮（2026-04-30）
- [x] grid_pro.py: fill_entry 新增 regime/daily_pnl_realized/grid_spacing_bps 三字段
- [x] grid_pro.py: loss_streak_triggered 时写入 record_analysis

### 第六十二轮（2026-04-30）
- [x] settings.py: GRID_DRAWDOWN_FROM_PEAK_USDT locked 6.0 → 3.0
- [x] grid_pro.py: _emergency_close 新增 record_analysis 追踪

### 第一~六十一轮（2026-04-18~29）
- [x] 全部P0/P1问题；WS重连；持仓同步；自适应TP；EWMA；FGI；资金费率；1h gate等

---

## 待解决问题（按优先级）

- [ ] P2: round67：评估 loss_streak 冷静期 1800s 是否可缩短为 900s
  - 当前：30分钟禁开新仓（已修复为按会话触发）
  - 考量：若每日触发 2-3 次，900s×3 = 45min vs 1800s×3 = 90min 损失
  - 条件：仅在有实盘数据验证触发频率后决定

- [ ] P2: round67：_sz_scale 中间档 ATR 28-35bps → sz=0.85
  - 现状：<35bps 全部 sz=1.0；per_slot_stop=1.0U 已给足 42bps 缓冲
  - 建议：暂缓，当实盘出现 ATR 30-35bps 频繁触发 per_slot_stop 时再加

- [ ] P2: round68：fill_tp 中 _recent_close_pnls.append 的粒度检查
  - fill_tp 当前也是 per-slot append（循环内）
  - 若 TP 单次命中多 slot，每个正收益 slot 单独 append，对 loss_streak 是有利的
  - 但建议统一为 session 粒度（append 一次 total_net），风格一致

- [ ] P3: 动态止盈 eff_tp_mult 灵敏度验证

## 下次优先行动

**round67：**
1. 检查 fill_tp 的 _recent_close_pnls.append 是否也应改为 session 粒度（统一风格）
2. 评估 loss_streak 冷静期：当前 1800s 是否可降至 900s 以提升交易密度
3. 若仍无实盘日志，检查 analysis.jsonl 的写入路径配置是否正确

## 系统评估
- **策略有效性**：9/10
  - 66轮迭代；全P0/P1已解决；事件可观测性完整
  - 最新修复：loss_streak 按会话粒度追踪，消除单次多slot平仓误触发30min冷静期
- **当前主要风险**：
  1. 实盘日志仍为空，所有优化依赖理论推导未经实盘数据验证
  2. 沙盒网络受限，无法实时获取市场数据
  3. loss_streak 冷静期 1800s 对交易密度影响仍需实测
- **累计运行轮次**：66
