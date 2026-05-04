# ETH量化系统升级计划

## 本次（2026-05-04 第八十一轮）完成

### grid_pro.py：修复 _position_sync_check 中 mid=0 导致补录永久跳过的边缘 bug

**问题根因：**

第80轮计划标注的 P1 问题：`_position_sync_check` 在 `mid=0`（WS tick gap 时
`_last_bid=0` 且 `_vwap=0`）时，估算成本价 `est_entry = 0.0`，进入
`if est_entry > 0:` 分支为 False → 整个补录块被跳过，`_total_held` 未修正。

后果：
1. 10s 后下次对账仍检测到相同 diff → 反复输出 warning 告警（日志洪泛）
2. `_vwap`、slot 状态、TP 均未更新 → 后续 PnL 计算出错，TP 可能永远不挂

**修复方案（三处改动）：**

1. `__init__`：新增 `_sync_pending_ts: float = 0.0`（延迟对账计时器）
2. `_reset_grid`：清仓时同步清零 `self._sync_pending_ts = 0.0`
3. `_position_sync_check`：
   - 函数顶部：若 `_sync_pending_ts > 0` 且 `_last_bid > 0`，强制重置
     `_last_pos_sync = 0.0` 触发即时重试，清空 `_sync_pending_ts`
   - `diff > _sync_threshold` 分支：`est_entry <= 0` 时不再跳过，而是：
     a. `_total_held = exchange_sz`（止住反复告警）
     b. `_sync_pending_ts = now`（标记待完整对账）
     c. return early（等 bid 恢复）
   - `est_entry > 0` 正常路径：清零 `_sync_pending_ts = 0.0`
   - 移除冗余 `if est_entry > 0:` 包装层，修正内部缩进

**效果预期：**
- mid=0 边缘情况下：最多1次"暂时以交易所为准"的告警（非反复），bid 恢复后
  自动完整补录（VWAP + slot + TP），不留持仓/TP空洞
- 正常路径：逻辑完全不变，`_sync_pending_ts=0` 分支不触发

---

## 历史完成（节选）

### 第八十轮（2026-05-04）
- [x] grid_pro.py: 新增 _tp_exposed_since 裸仓计时器：_place_tp 连续失败60秒触发 emergency_close，彻底封堵无限裸仓敞口

### 第七十九轮（2026-05-03）
- [x] grid_pro.py: 修复 _update_tp / _maybe_trail_tp 三处静默失败：加重试（0.5s）+ error/warning 日志

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

- [ ] P1: round82: _emergency_close 自身失败时无二次兜底——若 API 中断 >120s，第一次强平失败后，裸仓一直暴露直到 API 恢复。需要添加：若 _emergency_close 失败后 60s 仍无 TP，进入 circuit_break 暂停所有入场并持续重试强平
- [ ] P2: 验证服务实际运行状态（systemctl status / journalctl 最新日志）
- [ ] P2: 验证 analysis.jsonl 中 orphan_close 事件是否已被触发（第78轮新增）

## 下次优先行动

**round82：**
1. 阅读 `_emergency_close` 完整实现（lines ~1125-1205）
2. 确认失败路径：API 调用抛异常时是否有 except + log，还是静默失败
3. 添加 `_emergency_close_failed_ts: float = 0.0`：首次失败记录时间
4. 在 tick 主循环：若 `_emergency_close_failed_ts > 0` 且距今 >60s：
   - 暂停所有入场（`_grid_active = False`）
   - 再次尝试 `_emergency_close`（带不同 reason 标记）
   - 同时写入 analysis.jsonl `circuit_break` 事件

## 系统评估
- **策略有效性**：9/10
  - 81轮迭代，持仓同步路径现已形成完整闭环：正常同步 → mid=0降级 → bid恢复重试
  - TP保护：挂单 → 重试 → 裸仓计时 → 强平（60s上限）
  - 幽灵仓：检测 → 等比缩减/全清
- **当前主要风险**：
  1. 沙盒网络受限，实盘逻辑和市场响应无法验证
  2. _emergency_close 在 API 中断时自身也可能失败（二次兜底缺失，round82 P1）
- **累计运行轮次**：81
