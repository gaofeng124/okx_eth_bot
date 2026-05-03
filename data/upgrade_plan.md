# ETH量化系统升级计划

## 本次（2026-05-03 第七十八轮）完成

### grid_pro.py：修复 _reset_grid 两个缺陷

**缺陷1 — 幽灵持仓路径TP悬浮**：

- 问题：`_reset_grid` 清除 `_tp_order_id` 前不调用 `_cancel_order`
- 触发场景：`_sync_pos`（持仓对账）发现内部认为有仓但交易所实际为0（幽灵仓），
  直接调用 `_reset_grid()` 而不提前取消TP → TP单继续悬浮在OKX
- 后果：若市价随后触及TP价格，该TP单可能在无仓情况下被拒绝或意外成交
  （reduce_only on zero position → 大概率被拒，但不取消就是隐患）
- 修复：在 `_reset_grid` 内 `self._tp_order_id = ""` 前增加：
  `if self._tp_order_id: self._cancel_order(self._tp_order_id)`
- 安全性：`_cancel_order` 对 sCode=51401（已成交/已撤）返回 True，对所有调用路径无副作用：
  - `_emergency_close` 路径：调用前已取消TP（`_tp_order_id = ""`），此处为no-op ✓
  - `_sync_tp filled` 路径：TP已成交，51401视为成功，无害 ✓
  - `_sync_tp partially_canceled` 路径：`_tp_order_id`已清空，no-op ✓
  - 幽灵持仓路径：**正式修复，TP被取消** ✓

**缺陷2 — 孤儿仓平仓无追溯记录**：

- 问题：`_reset_grid` 检测到孤儿仓并执行市价平仓成功后，无 `record_analysis` 调用
- 后果：无法从 analysis.jsonl 统计孤儿仓频率，也无法审计每次事件的具体数值
- 修复：市价平仓成功后添加 `record_analysis("orphan_close", level, fill_sz, fill_px, daily_pnl_realized)`
- 效果：`orphan_close` 事件现在可被 analysis.jsonl 追溯，频率可量化

---

## 历史完成（节选）

### 第七十七轮（2026-05-03）
- [x] grid_pro.py: 修复 _emergency_close 孤儿仓双缺陷 + _reset_grid 孤儿检测形同虚设

### 第七十六轮（2026-05-03）
- [x] grid_pro.py: 修复 _cancel_order 未检查 OKX sCode（sCode=51401视为成功）

### 第七十五轮（2026-05-03）
- [x] grid_pro.py: 修复 _sync_tp partially_canceled 分支 PnL 双算 bug

### 第一~七十四轮（2026-04-18~05-02）
- [x] 全部P0/P1问题：WS重连/持仓同步/自适应TP/EWMA/FGI/资金费率/1h gate等

---

## 待解决问题（按优先级）

- [ ] P1: round79: 检查 _update_tp 的原子性：cancel→place 期间若 place 失败，_tp_order_id 为空但持仓存在（无TP保护）
  - 特别关注：_place_tp 失败的处理分支是否有兜底（补挂重试/发 emergency_close？）
- [ ] P1: round79: 检查 analysis.jsonl 中 orphan_close 事件频率（新增事件的首次验证）
- [ ] P2: 验证服务实际运行状态（systemctl status / journalctl 最新日志）
- [ ] P2: _sync_tp partially_canceled 分支 fill_ratio 精度：若 fill_sz 与 _total_held 存在微小浮点差导致 fill_ratio > 1，各字段是否正确处理

## 下次优先行动

**round79：**
1. 读取 `_update_tp` 和 `_place_tp` 的失败处理逻辑
2. 确认 `_place_tp` 失败（返回空串）时是否有重试/报警/fallback
3. 若 `_tp_order_id` 变为空且 `_total_held > 0`，下次 tick 是否会触发补挂

## 系统评估
- **策略有效性**：9/10
  - 78轮迭代，止损/重置路径的健壮性逐轮加固
  - 三层止损防护（emergency_close → market_close_all → reset_grid）各路径均经审查
  - 新增 orphan_close analysis 事件，提升线上可观测性
- **当前主要风险**：
  1. 沙盒网络受限，无法验证实盘逻辑和市场数据
  2. _update_tp cancel→place 原子性未审查（下轮P1）
  3. 无法确认 orphan_close 修复在实盘中是否被触发（需等待 analysis.jsonl 数据）
- **累计运行轮次**：78
