# ETH量化系统升级计划

## 本次（2026-05-04 第八十二轮）完成

### grid_pro.py：_emergency_close 二次兜底——API中断时的 circuit_break 机制

**问题根因（round81 P1 遗留）：**

`_emergency_close` 调用 `_market_close_all` 时，若 REST API 抛出异常：
- 旧代码：只 `log.error` 后**继续执行** → 调用 `_reset_grid()` 清空所有 slot 状态
- 后果：交易所实际仍有持仓，机器人认为已清仓（`_total_held=0`），形成无限裸仓敞口
- API 恢复后机器人也不会再尝试平仓（slot 已 EMPTY，触发条件不满足）

**修复方案（5处改动）：**

1. **`__init__`**：新增 `_emergency_close_failed_ts: float = 0.0`（强平API失败计时器）

2. **`_reset_grid`**：清仓时同步清零 `self._emergency_close_failed_ts = 0.0`

3. **`_market_close_all`**：
   - 返回类型 `None` → `bool`
   - `total <= 0` 时返回 `True`（无仓=视为成功）
   - API 调用成功：执行 PnL 记录和 slot 清空，末尾 `return True`
   - API 调用失败（except）：立即 `return False`，不清 slot，不记 PnL

4. **`_emergency_close`**：
   - 接收 `_close_ok = self._market_close_all(mid, reason)` 返回值
   - `_close_ok=False`：设 `_emergency_close_failed_ts = time.time()`，不调 `_reset_grid`，清 `_emergency_closing=False`，提前 return
   - `_close_ok=True`：清零 `_emergency_close_failed_ts = 0.0`，继续正常流程

5. **tick 主循环**（`_tp_exposed_since` 检查之后新增）：
   - 若 `_emergency_close_failed_ts > 0` 且距今 ≥60s：
     - `self._grid_active = False`（暂停所有入场）
     - 写入 `analysis.jsonl` `circuit_break` 事件
     - 再次调用 `self._emergency_close(f"circuit_break_retry_{elapsed:.0f}s", mid)`

**效果预期：**
- API 恢复后（60s内）：重试成功 → slot 清空，normal cooldown，circuit_break 解除
- API 持续中断：每60s写一条 `circuit_break` 事件 + 一次重试，不形成无限裸仓
- 正常路径：`_emergency_close_failed_ts=0`，circuit_break 分支不触发，行为完全不变

---

## 历史完成（节选）

### 第八十一轮（2026-05-04）
- [x] grid_pro.py: 修复 _position_sync_check 中 mid=0（tick gap）时 est_entry=0 导致补录全程跳过的边缘 bug；引入 _sync_pending_ts 延迟对账机制

### 第八十轮（2026-05-04）
- [x] grid_pro.py: 新增 _tp_exposed_since 裸仓计时器：_place_tp 连续失败60秒触发 emergency_close

### 第七十九轮（2026-05-03）
- [x] grid_pro.py: 修复 _update_tp / _maybe_trail_tp 三处静默失败

### 第七十八轮（2026-05-03）
- [x] grid_pro.py: 修复 _reset_grid 两个缺陷：幽灵仓TP未取消 + 孤儿仓无analysis事件

### 第七十七轮（2026-05-03）
- [x] grid_pro.py: 修复 _emergency_close 孤儿仓双缺陷 + _reset_grid 孤儿检测形同虚设

### 第七十六轮（2026-05-03）
- [x] grid_pro.py: 修复 _cancel_order 未检查 OKX sCode（sCode=51401视为成功）

### 第一~七十五轮（2026-04-18~05-03）
- [x] 全部P0/P1问题：WS重连/持仓同步/自适应TP/EWMA/FGI/资金费率/1h gate等

---

## 待解决问题（按优先级）

- [ ] P1: round83: 检查 `_emergency_close_failed_ts > 0` 期间 `_tp_exposed_since` 超时路径是否会重复调用 `_emergency_close`（两者均检查 `_emergency_closing` flag，理论上互斥，但需 Read 确认）
- [ ] P1: round83: `circuit_break_retry` 重试若再次失败，`_emergency_close_failed_ts` 会被重置为新时间（重新计时），但 `_grid_active=False` 已设置；需确认连续失败时 slot 状态是否仍保持 HOLDING
- [ ] P2: 验证服务实际运行状态（systemctl status / journalctl 最新日志）
- [ ] P2: 验证 analysis.jsonl 中 orphan_close 事件是否已被触发（第78轮新增）

## 下次优先行动

**round83：**
1. Read grid_pro.py lines 2525-2575（circuit_break新增段）+ lines 1127-1135（_emergency_closing guard）
2. 确认：`_tp_exposed_since` 超时（line ~2544）调用 `_emergency_close` 时，若此时 `_emergency_close_failed_ts > 0`（circuit_break 正在计时），是否因 `_emergency_closing=False` 允许重入导致双重触发
3. 若有干扰：在 `_tp_exposed_since` 超时检查前加 `if self._emergency_close_failed_ts == 0:` 保护
4. 同时检查连续 circuit_break 失败时 slot HOLDING 状态是否保持

## 系统评估
- **策略有效性**：9/10
  - 82轮迭代，强平保护链条现已完整：`_place_tp`失败→裸仓60s→`_emergency_close`→API失败→circuit_break 60s→重试
  - 每个失败节点均有计时器兜底，不存在永久裸仓状态
- **当前主要风险**：
  1. 沙盒网络受限，实盘逻辑和市场响应无法验证
  2. circuit_break 两路径（_tp_exposed_since + _emergency_close_failed_ts）可能在极端情况下同时触发，需round83验证互斥性
- **累计运行轮次**：82
