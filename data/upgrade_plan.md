# ETH量化系统升级计划

## 本次（2026-05-05 第八十五轮）完成

### 修复1：grid_pro.py `_reset_grid_state` 根治 spacing 清零问题

**问题：**
第84轮在 `_maybe_trail_tp` 加了 `if spacing_abs <= 0: return` guard，是防御性补丁。
根本原因是 `_reset_grid_state`（line 1906）将 `_grid_spacing = 0.0`，导致：
- 有 HOLDING slots 时 trail 被无条件跳过（gap 期间 TP 无法追踪）
- `_init_grid` 重启前若市价大幅偏离，TP 停在原处（过时定价）

**修复（line 1904-1914）：**
```python
if self._total_held > 0:
    self._grid_spacing = (
        self._vol.spacing_pct(self._atr_mult, self._min_sp, self._max_sp)
        or self._min_sp
    )
else:
    self._grid_spacing = 0.0
```

**效果：**
- 有持仓时，重置后 spacing 立即恢复为当前 ATR 计算值
- `_maybe_trail_tp` 的 round84 guard（`if spacing_abs <= 0: return`）保留为安全网，但正常路径下不再触发
- 无持仓时（无需 TP trail）照旧清零，`_init_grid` 重算

---

### 修复2：grid_pro.py `_emergency_close` 补录 TP partial fill PnL

**问题：**
`_emergency_close` 取消 TP 后直接 `_tp_order_id = ""`，未查询 TP 是否已 partial fill。
若 TP 在取消窗口内已成交部分：`state=partially_canceled, fillSz>0` → PnL 静默丢失。

**修复（line 1179-1206）：**
取消 TP 后查询订单状态；若 `partially_canceled` 且 `fillSz>0`，按比例：
1. 计算各 slot 对应份额的净 PnL（含手续费）并 `self._pnl.add()`
2. 缩减对应的 `s.fill_sz` 和 `self._total_held`
3. 记录 warning 日志

**效果：**
- 避免紧急平仓时漏记 TP 已成交部分的正向 PnL
- `_market_close_all` 后续只平剩余仓位，数量准确

---

## 历史完成（节选）

### 第八十四轮（2026-05-04）
- [x] grid_pro.py: 修复 _maybe_trail_tp 在 spacing_abs=0 时的零利润平仓 bug（防御性 guard）

### 第八十三轮（2026-05-04）
- [x] grid_pro.py: 修复 _tp_exposed_since 与 circuit_break 双路径竞争

### 第八十二轮（2026-05-04）
- [x] grid_pro.py: 修复 _emergency_close API失败无兜底

### 第八十一轮（2026-05-04）
- [x] grid_pro.py: 修复 _position_sync_check mid=0 时 bug

### 第八十轮（2026-05-04）
- [x] grid_pro.py: 新增 _tp_exposed_since 裸仓计时器

---

## 待解决问题（按优先级）

- [ ] P1: round86: 在 _init_grid 结束时将 grid_spacing_bps 记录到 analysis.jsonl，便于事后复盘格宽历史（当前只有 fill_tp 事件包含格宽）
- [ ] P1: round86: 验证 _update_tp guard（line 2336）与本轮 _reset_grid_state 修复的交互——理论上两者互补，guard 更少触发但保留无害
- [ ] P2: 验证服务实际运行状态（需服务器 journalctl / systemctl）
- [ ] P2: 验证 analysis.jsonl 中 orphan_close 和 circuit_break 事件是否触发

## 下次优先行动

**round86：**
1. 在 `_init_grid` 末尾（line ~1420 后）追加记录：
   ```python
   record_analysis("grid_init", grid_spacing_bps=round(self._grid_spacing*10000,2),
       atr_bps=round(self._vol.atr_short*10000,2), regime=self._current_regime.value)
   ```
   这样 analysis.jsonl 中有完整格宽时间序列，便于判断 ATR 是否过低导致格宽贴近下限。
2. 检查 `_update_tp` 中 line 2336 guard 与 `_reset_grid_state` 新逻辑的交互（读代码确认，不需修改）

## 系统评估
- **策略有效性**：9/10
  - 85轮迭代，TP保护链条已全面覆盖：place→trail→timeout→emergency_close→circuit_break
  - 本轮将 spacing 清零的根本问题消除（round84 guard 仍留作安全网）
  - 新增 emergency_close 中 partial fill PnL 补录，账务更完整
- **当前主要风险**：
  1. 沙盒网络受限，实盘逻辑无法实时验证
  2. _emergency_close 新增一次 query_order API 调用，极端行情下若 API 延迟高，紧急平仓会稍慢
- **累计运行轮次**：85
