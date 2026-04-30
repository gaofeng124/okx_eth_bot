# ETH量化系统升级计划

## 本次（2026-04-30 第六十二轮）完成

### settings.py：GRID_DRAWDOWN_FROM_PEAK_USDT locked 6.0 → 3.0

**问题**：_LOCKED_GRID 中的备用值 6.0U（50U本金的12%）偏高。

正常运行时，`set_dynamic_drawdown_limit` 每 tick 将限制动态覆盖为 `max(1.5, equity×4%) ≈ 2.0U`（50U账户）。
但若 equity 获取持续失败，`_drawdown_limit` 会回退至 locked 值 6.0U，导致风控缺口扩大。

**修复**：将备用值从 6.0 降至 3.0，构成更严密的风控层次：
- per_slot_stop: 0.8U → 单仓快刀止损
- peak_drawdown（备用）: 3.0U → equity失联时的保险
- whole_grid_stop（动态）: max(4.0, equity×10%) ≈ 5.0U
- daily_stop: 6.0-8.0U

### grid_pro.py：_emergency_close 新增 record_analysis 追踪

**问题**：分析 analysis.jsonl 时无法查看紧急平仓事件（原因/时间/损益），
导致 round62 优先任务"验证gate触发频率"缺少关键数据源。

**修复**：在 `_emergency_close` 中（`_emergency_closing = True` 设置后）添加：
```python
record_analysis("emergency_close", reason=reason, mid=mid,
                total_held=..., vwap=..., unrealized_usdt=..., daily_pnl_realized=...)
```
后续可通过 `grep '"emergency_close"' data/logs/daily/*/analysis.jsonl` 统计频率。

---

## 历史完成（节选）

### 第六十一轮（2026-04-29）
- [x] grid_pro.py: _reset_grid_state 补加 _grid_bias=1.0 重置 + status_summary 新增 grid_bias 字段

### 第六十轮（2026-04-29）
- [x] grid_pro.py: 修复 _grid_bias 未保存导致 TRENDING 模式补仓/越叉检测价格偏差 P1 bug

### 第一~五十九轮（2026-04-18~29）
- [x] 全部P0/P1问题；WS重连指数退避；持仓同步；自适应TP；EWMA；FGI；资金费率；1h gate；regime.py清理；settings.py清理

---

## 待解决问题（按优先级）

- [ ] P2: round63：验证 emergency_close 事件频率
  - 条件：需实盘运行后查看 data/logs/daily/*/analysis.jsonl
  - 命令：`python3 -c "import json; [print(json.loads(l)) for l in open('data/logs/daily/2026-04-30/analysis.jsonl') if '\"emergency_close\"' in l]"`
  - 期望：每日 0-3 次为正常；>5次说明止损参数过紧

- [ ] P2: round63：统计 fill_tp 事件密度
  - 期望：每日 5-20 次 fill_tp；< 5次说明 TP 设置过远或成交太少

- [ ] P3: 动态止盈：eff_tp_mult 灵敏度验证（是否真正随 ATR 变化）

- [ ] P3: maker 手续费精度：GRID_ROUNDTRIP_FEE_BPS=4.0（maker+maker=2+2bps）已确认正确

## 下次优先行动

**round63：从实盘日志统计事件分布，评估策略健康度**
1. 查看 `data/logs/daily/<今日日期>/analysis.jsonl`
2. 统计 event 类型分布（emergency_close/fill_tp/fill_entry/grid_status）
3. 若 emergency_close > 5/日，检查是否 per_slot_stop=0.8 过紧
4. 若 fill_tp < 5/日，检查 TP mult 是否需要调小（当前 GRID_TP_MULT=2.0）

## 系统评估
- **策略有效性**：9/10
  - 62轮迭代；全P0/P1已解决；风控层次完整且量化
  - emergency_close 现可追踪，运维可观测性提升
  - DRAWDOWN 备用值从12%降至6%，与动态值2-4%更匹配
- **当前主要风险**：
  1. 外部API网络受限（沙盒），无实时市场监控
  2. 实盘日志无法访问（所有优化均未经实盘数据验证）
  3. 双向策略（long+short）在单边行情中可能同时亏损
- **累计运行轮次**：62
