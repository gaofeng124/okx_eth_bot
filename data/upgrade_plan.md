# ETH量化系统升级计划

## 本次（2026-05-05 第八十八轮）完成

### 修复：_position_sync_check 中 ENTRY_LIVE 撤单失败时孤立订单风险

**问题：**
`_position_sync_check` 的 untracked 补录路径（diff > threshold）在为 ENTRY_LIVE slot 撤单时：
```python
self._cancel_order(s.entry_order_id)  # 返回值被丢弃
s.entry_order_id = ""                  # 无论成功失败都清空
# 然后直接 s.state = HOLDING
```
若 `_cancel_order` 返回 False（网络抖动或 OKX API 拒绝），该 slot 的：
- 真实订单仍在 OKX 交易所活跃（ENTRY_LIVE 状态）
- 内部 `entry_order_id` 已清空（失去追踪）
- slot 状态设为 HOLDING（以为已成交）

**后果**：若该孤立订单后来成交，fill 回调找不到对应 slot，导致持仓量计算偏差，PnL 记录错误。

**修复：**
检查 `_cancel_order` 返回值；若为 False，记录 warning 并 `continue` 跳过此 slot，保留其 ENTRY_LIVE 状态不变：
```python
cancelled = self._cancel_order(s.entry_order_id)
if not cancelled:
    log.warning("[grid] untracked补录：slot撤单失败 oid=%s，跳过此slot防止孤立订单", s.entry_order_id)
    continue
s.entry_order_id = ""
```

**效果预期：**
- 撤单失败时 slot 保持 ENTRY_LIVE 不变，fill 回调仍能正确匹配
- log.warning 可在日志中检索 "untracked补录：slot撤单失败"，监控 API 可靠性

### 确认（无需修改）：_update_tp 首次 0→正值 total_held 路径
- `_vwap_value += est_entry * diff` → `_vwap = est_entry` 正确初始化
- `_tp_spacing_sign()` 返回 ±1.0，TP 方向由 `_is_short` 决定，逻辑正确
- `_cancel_order` 本身有 try/except，不会抛出异常到调用方

---

## 历史完成（节选）

### 第八十七轮（2026-05-05）
- [x] grid_pro.py: _position_sync_check positive-diff 路径补充 record_analysis('untracked_position_sync') 监控事件

### 第八十六轮（2026-05-05）
- [x] grid_pro.py: _position_sync_check 负差路径 ratio 变量定义但未使用，改为等比缩减 slot fill_sz
- [x] grid_pro.py: 新增 ghost_position_sync 监控事件

### 第八十五轮（2026-05-05）
- [x] grid_pro.py: _reset_grid_state 根治 spacing 清零问题
- [x] grid_pro.py: _emergency_close 补录 TP partial fill PnL

---

## 待解决问题（按优先级）

- [ ] P1: round89: 检查 _maybe_trail_tp 在 _grid_spacing 从 0 恢复后首次触发时，_last_tp_trail_ts 节流是否导致 trail 窗口延迟（spacing=0 会 return，恢复后第一次 trail 可能被 20-30s 节流拦截）
- [ ] P1: round89: 检查 _emergency_close / circuit_break 触发后是否清空所有 slot 状态（包括 ENTRY_LIVE、HOLDING），避免重启后幽灵 slot 残留
- [ ] P2: 评估 _refresh_fgi/_refresh_funding REST fallback 在沙盒环境 timeout 代价（均有 except 兜底，不影响主策略；实盘无此问题）
- [ ] P3: analysis.jsonl 中记录每次 position_sync_check 的 diff 摘要（不仅在异常时，也在正常时每N次记录一次），便于长期 API 一致性监控

## 下次优先行动

**round89：**
1. 搜索 `def _maybe_trail_tp` 附近的 `_last_tp_trail_ts` 重置逻辑，确认 spacing 从 0 恢复时是否需要重置节流时间戳
2. 搜索 `def _emergency_close` 和 circuit_break 相关代码，检查 slot 状态清理完整性

## 系统评估
- **策略有效性**：9/10
  - 88 轮迭代，孤立订单风险已修复
  - untracked 补录路径：撤单失败时正确跳过，不污染 slot 状态
  - core 链条：entry→fill→vwap→TP→trail→timeout→emergency→circuit_break 全覆盖
- **当前主要风险**：
  1. 沙盒网络受限，实盘逻辑无法通过 API 调用验证
  2. _maybe_trail_tp 节流在 spacing 恢复后可能造成短暂 trail 盲区
- **累计运行轮次**：88
