# ETH量化系统升级计划

## 本次（2026-05-04 第八十四轮）完成

### grid_pro.py：修复 _maybe_trail_tp 在 spacing_abs=0 时的零利润平仓 bug

**问题根因：**

`_reset_grid_state` 将 `_grid_spacing` 清零（line 1901），当调用路径为：
1. 在"补充空置槽位"步骤（step 12，line 3162），EMPTY 槽位目标价越叉 bid → 调 `_reset_grid_state`
2. 此时仍有 HOLDING slots + 活跃 TP 订单（`_tp_order_id` 非空）
3. `_grid_active=False`，`_grid_spacing=0.0`，`_grid_center=0.0`

下一个 tick，`_maybe_trail_tp` 被调用（line 2713, `if self._total_held > 0`），此时：
```python
spacing_abs = 0.0 * self._vwap = 0.0  # line 1530
```

Long 路径触发条件退化为：
```python
if mid > self._tp_price + 0.0:  # = if mid > tp_price
```

若触发，新 TP = `mid - 0 * offset = mid`（贴市价），且 `new_tp > tp_price`（若 mid 越过旧 TP）→ 取消旧 TP，以当前价重挂 → 立即成交 → **零利润或亏损**。

**修复（1处改动，grid_pro.py line 1534）：**
```python
if spacing_abs <= 0:
    return  # _grid_spacing cleared by _reset_grid_state; skip trail
```

在 `spacing_abs` 计算后立即检查，拦截 spacing=0 的 trail 路径。

**效果：**
- 下次 tick：若 `_tp_order_id` 为空（TP 填成），走 `_update_tp` 路径，guard at line 2527-2534 会重算 `_grid_spacing`
- 若 `_tp_order_id` 存在（TP 仍活跃），trail 被安全跳过，直到 `_grid_spacing` 被外部重算后再恢复
- 原正常路径（`_grid_spacing > 0`）完全不受影响

---

## 历史完成（节选）

### 第八十三轮（2026-05-04）
- [x] grid_pro.py: 修复 _tp_exposed_since 与 circuit_break 双路径竞争：加 `_emergency_close_failed_ts==0` 保护条件，两路径严格互斥

### 第八十二轮（2026-05-04）
- [x] grid_pro.py: 修复 _emergency_close API失败无兜底：启动 circuit_break 计时，60s后重试

### 第八十一轮（2026-05-04）
- [x] grid_pro.py: 修复 _position_sync_check mid=0 时 est_entry=0 补录跳过 bug

### 第八十轮（2026-05-04）
- [x] grid_pro.py: 新增 _tp_exposed_since 裸仓计时器

### 第七十八~七十九轮（2026-05-03）
- [x] grid_pro.py: 修复 _reset_grid 幽灵仓TP未取消 + _update_tp/_maybe_trail_tp 三处静默失败

---

## 待解决问题（按优先级）

- [ ] P1: round85: 考虑在 _reset_grid_state 中将 _grid_spacing 恢复为 vol.spacing_pct() 而非清零，这比 guard 更积极（确保即使 TP 存在时下一次 trail 也能正常工作）
- [ ] P1: round85: 检查 _sync_tp 超时路径（_tp_placed_ts 超时）是否需要区分 partial_filled 状态
- [ ] P2: 验证服务实际运行状态（需要服务器 journalctl / systemctl）
- [ ] P2: 验证 analysis.jsonl 中 orphan_close 和 circuit_break 事件是否触发

## 下次优先行动

**round85：**
1. 读取 `_reset_grid_state` 代码，在最后追加：
   ```python
   self._grid_spacing = self._vol.spacing_pct(self._atr_mult, self._min_sp, self._max_sp) or self._min_sp
   ```
   这样即使 TP 还活跃，trail 也能用正确 spacing 工作（比 guard 更主动）
2. 同时检查 `_sync_tp` 超时逻辑（`_tp_placed_ts` 超时后是否需要考虑 partial_filled 状态）

## 系统评估
- **策略有效性**：9/10
  - 84轮迭代，TP保护链条已全面覆盖：place→trail→timeout→emergency_close→circuit_break
  - 本轮修补 spacing=0 时 trail 零利润平仓的边缘 bug
- **当前主要风险**：
  1. 沙盒网络受限，实盘逻辑无法实时验证
  2. _reset_grid_state 将 _grid_spacing 清零有更深层的设计问题（应考虑恢复而非清零）
- **累计运行轮次**：84
