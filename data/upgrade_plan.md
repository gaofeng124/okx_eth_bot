# ETH量化系统升级计划

## 本次（2026-05-03 第七十七轮）完成

### grid_pro.py：修复紧急平仓孤儿仓位双重缺陷

**问题根因**：

**缺陷1 — `_emergency_close` 忽略入场单成交状态**：
- 原逻辑（1143-1148行）：`_cancel_order(oid)` 返回值被丢弃，slot 无条件设为 EMPTY
- 场景：入场单在撤单窗口内恰好成交（高频交易时的正常竞态）
- 后果：`_market_close_all` 只关闭 HOLDING 状态的槽位，EMPTY 槽位被忽略
  → 实盘 OKX 有持仓，bot 不知道 → 幽灵持仓不受止损保护

**缺陷2 — `_reset_grid` 孤儿检测逻辑错误**：
- 原逻辑：`if not self._cancel_order(s.entry_order_id)` 才查询订单
- 问题：`_cancel_order` 对 sCode=51401（订单不存在/已成交）返回 True
  → 51401 情形下完全不查询 → 孤儿检测形同虚设
- 即使检测到：只打 "需要手动处理或等待reconcile" 日志，无任何实际动作

**修复（round77）**：

**`_emergency_close` 修复**：
1. 取消入场单时保存 `(slot, oid)` 列表
2. 取消后对每个 oid 调用 `_query_order`
3. 若 state in ("filled", "partially_canceled") 且 fill_sz > 0：
   - 将 slot 恢复为 HOLDING（fill_price/fill_sz/fill_ts）
   - 更新 _total_held 和 _vwap
   - 使随后的 `_market_close_all` 能正确发现并关闭该仓位

**`_reset_grid` 修复**：
1. 每次撤单后都查询订单状态（不依赖 cancel 返回值）
2. 发现成交孤儿：直接调用 `_rest.request` 发市价 reduce_only 单平仓
3. 异常时 log.error 而非静默失败

**效果预期**：
- 消除极端行情触发止损时（止损+快速成交竞态）的幽灵持仓
- `_reset_grid` 成为真正的安全兜底，而不只是日志记录
- 总体止损路径完整性：_emergency_close → _market_close_all → _reset_grid 三层都能处理孤儿

---

## 历史完成（节选）

### 第七十六轮（2026-05-03）
- [x] grid_pro.py: 修复 _cancel_order 未检查 OKX sCode（sCode=51401视为成功）

### 第七十五轮（2026-05-03）
- [x] grid_pro.py: 修复 _sync_tp partially_canceled 分支 PnL 双算 bug

### 第七十四轮（2026-05-02）
- [x] grid_pro.py: status_summary() 增加 sz_scale_last/loss_streak/slot持仓时长

### 第七十三轮（2026-05-02）
- [x] grid_pro.py: 修复 _check_phase4_trend_guard 5处硬编码路径

### 第一~七十二轮（2026-04-18~05-02）
- [x] 全部P0/P1问题：WS重连/持仓同步/自适应TP/EWMA/FGI/资金费率/1h gate等

---

## 待解决问题（按优先级）

- [ ] P1: round78: 审查 _sync_tp 完整调用链
  - _sync_tp 在 partially_canceled 分支后可能调用 _reset_grid，检查整个流程
  - 确认 PnL 记录路径与 round75 fix 一致
- [ ] P1: round78: 检查 analysis.jsonl
  - 若有 emergency_close 事件，统计频率（应该极少发生）
  - 检查 fill_entry / fill_tp 事件数量是否合理
- [ ] P2: 验证服务实际运行状态（systemctl status / journalctl 最新日志）
- [ ] P2: 考虑 _reset_grid 孤儿市价平仓后是否应记录 analysis 事件（便于追溯）

## 下次优先行动

**round78：**
1. 搜索 `_sync_tp` 所有代码，确认 partially_canceled 后的调用链完整性
2. 检查 analysis.jsonl 中 emergency_close 事件频率
3. 若发现问题，修复

## 系统评估
- **策略有效性**：9/10
  - 77轮迭代，止损路径的完整性逐轮加固
  - round76: 撤单sCode → round77: 紧急平仓孤儿仓双修复
  - 孤儿仓问题是实盘中最难发现的静默损失来源之一
- **当前主要风险**：
  1. 沙盒网络受限，无法验证实盘逻辑和市场数据
  2. _reset_grid 孤儿市价平仓暂无 analysis 事件记录（下轮考虑添加）
  3. _sync_tp 完整调用链尚未审查（下轮P1）
- **累计运行轮次**：77
