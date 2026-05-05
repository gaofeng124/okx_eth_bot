# ETH量化系统升级计划

## 本次（2026-05-05 第八十九轮）完成

### 修复：_maybe_trail_tp 节流在 spacing 恢复后的短暂盲区

**问题分析：**
`_maybe_trail_tp` 开头有 20s（RANGING）/ 30s（TRENDING）节流：
```python
if now - self._last_tp_trail_ts < _min_trail_iv:
    return
```
`_last_tp_trail_ts` 仅在 trail 触发时更新。若：
1. Trail 在 T=0 触发，设 `_last_tp_trail_ts = T`
2. T+1s：spacing 被 `_reset_grid_state` 清零（重置时 total_held=0，或进入清仓路径）
3. T+5s：spacing 从另一条路径恢复（total_held > 0 后重算）
4. T+11s：`_maybe_trail_tp` 检查：`11 - 0 = 11 < 20` → 被额外阻塞 9s

最坏情形：trail 刚触发，spacing 立即清零再恢复，最多额外阻塞 20-30s。
在此窗口内，TP 仍在原位，但无法追踪价格延伸，损失锁利机会。

**修复：**
在两处 spacing 恢复赋值后追加 `self._last_tp_trail_ts = 0.0`：
1. `_reset_grid_state`（line ~1934）：`total_held > 0` 分支恢复 spacing 后
2. `_position_sync_check`（line ~2369）：`_grid_spacing <= 0` 补录恢复 spacing 后

**效果预期：**
- spacing 恢复后 trail 可立即（本 tick）检查，消除最多 20-30s 盲区
- 实际多出的 API 调用极少：触发条件仍需 `mid > tp + 1格宽`，不会导致 API 频繁调用

### 已关闭的 P1 问题（round89 调查结论）

**_emergency_close / circuit_break 幽灵 slot 问题：已关闭**
- `_slots` 在 `__init__` 中始终重新初始化（全 EMPTY），重启后无跨会话幽灵 slot
- 会话内：`_close_ok=False` 时设 `_emergency_close_failed_ts`；60s 后 tick 循环调用
  `_emergency_close(circuit_break_retry)` 重试；重试成功后调用 `_reset_grid()` 清空所有 slot
- 逻辑完整，无需修改

---

## 历史完成（节选）

### 第八十八轮（2026-05-05）
- [x] grid_pro.py: _position_sync_check untracked 补录路径：_cancel_order 返回值被忽略，撤单失败时 slot 错误设为 HOLDING；改为撤单失败时 continue 跳过，保留 ENTRY_LIVE 状态

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

- [ ] P1: round90: 检查 _adaptive_trail_trigger / _adaptive_trail_offset 的 EWMA 权重衰减 — 确认 TRENDING 模式使用 _EWMA_HALFLIFE_TRENDING 常量（应为 2700s），RANGING 使用 900s；检查常量定义位置和是否正确传入
- [ ] P2: 评估 _refresh_fgi/_refresh_funding REST fallback 在沙盒环境 timeout 代价（均有 except 兜底，不影响主策略；实盘无此问题）
- [ ] P3: analysis.jsonl 中记录每次 position_sync_check 的 diff 摘要（不仅在异常时，也在正常时每N次记录一次），便于长期 API 一致性监控

## 下次优先行动

**round90：**
1. `grep -n '_EWMA_HALFLIFE\|_ewma_profit_avg\|halflife' quant/strategy/grid_pro.py` — 确认 TRENDING / RANGING 半衰期常量值及传参路径
2. 检查 `_adaptive_trail_trigger` 和 `_adaptive_trail_offset` 是否正确切换 halflife
3. 若发现 TRENDING 误用 900s（RANGING halflife），会导致成交稀疏时 EWMA 过快衰减，trail 参数无法积累有效样本

## 系统评估
- **策略有效性**：9/10
  - 89 轮迭代，trail 节流盲区已修复
  - 核心链条：entry→fill→vwap→TP→trail→timeout→emergency→circuit_break 全覆盖
  - spacing 恢复后 trail 可立即响应，顺势行情锁利能力提升
- **当前主要风险**：
  1. 沙盒网络受限，实盘逻辑无法通过 API 调用验证
  2. EWMA 半衰期配置有待确认（下轮 P1）
- **累计运行轮次**：89
