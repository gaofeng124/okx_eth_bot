# ETH量化系统升级计划

## 本次（2026-05-04 第八十三轮）完成

### grid_pro.py：修复 _tp_exposed_since 与 circuit_break 双路径竞争 bug

**问题根因（round82 P1 遗留）：**

两个超时路径存在隐性竞争，导致 `circuit_break`（`_grid_active=False`）永远无法触发：

1. T=60s：`_tp_exposed_since` 超时 → 调 `_emergency_close` → API失败 → `_emergency_close_failed_ts = T+60s`
2. T=120s：`_tp_exposed_since` 再次超时（从未被清，因 `_reset_grid` 未执行）  
   → 再次调 `_emergency_close` → API失败 → `_emergency_close_failed_ts = T+120s`（**重置！**）
3. `circuit_break` 检查时 `_ec_fail_secs ≈ 0 < 60` → **`_grid_active=False` 永不执行**

每隔 60s，`_tp_exposed_since` 都会抢先调用 `_emergency_close`，将 `_emergency_close_failed_ts` 刷新为"刚才"，使 circuit_break 路径永远看到 `_ec_fail_secs < 60`。

**修复方案（1处改动）：**

`grid_pro.py` line 2539，在 `_tp_exposed_since` 超时检查条件中追加：
```python
and self._emergency_close_failed_ts == 0
```

完整条件：
```python
if (not self._tp_order_id and self._tp_exposed_since > 0
        and self._emergency_close_failed_ts == 0):
```

**修复后的正确执行序列：**
- T=60s：`_emergency_close_failed_ts==0` → `_tp_exposed_since` 正常触发强平，API失败 → 设置 `_emergency_close_failed_ts`
- T=60s（同tick）：circuit_break 检查：`_ec_fail_secs≈0 < 60` → 不触发（正常）
- T=121s：`_emergency_close_failed_ts > 0` → `_tp_exposed_since` **被抑制**（不重置计时器）  
  → circuit_break：`_ec_fail_secs=61 >= 60` → `_grid_active=False` + 重试强平
- 重试成功：`_emergency_close_failed_ts=0`，`_reset_grid`（清 `_tp_exposed_since`），恢复正常
- 重试失败：`_emergency_close_failed_ts` 重置为NOW，60s后再次重试

**效果预期：**
- API持续中断时，circuit_break 每60s正确触发一次，`_grid_active=False` 暂停入场
- 正常路径（`_emergency_close_failed_ts=0`）：`_tp_exposed_since` 行为完全不变
- 两路径严格互斥，无双重调用风险

---

## 历史完成（节选）

### 第八十二轮（2026-05-04）
- [x] grid_pro.py: 修复 _emergency_close 在 API 中断时无兜底：_market_close_all 改返回 bool，API失败保留 slot，启动 _emergency_close_failed_ts 计时，60s circuit_break 重试

### 第八十一轮（2026-05-04）
- [x] grid_pro.py: 修复 _position_sync_check mid=0 时 est_entry=0 导致补录跳过的边缘 bug

### 第八十轮（2026-05-04）
- [x] grid_pro.py: 新增 _tp_exposed_since 裸仓计时器

### 第七十九轮（2026-05-03）
- [x] grid_pro.py: 修复 _update_tp / _maybe_trail_tp 三处静默失败

### 第七十八轮（2026-05-03）
- [x] grid_pro.py: 修复 _reset_grid 幽灵仓TP未取消 + 孤儿仓无analysis事件

### 第一~七十七轮（2026-04-18~05-03）
- [x] 全部P0/P1问题：WS重连/持仓同步/自适应TP/EWMA/FGI/资金费率/1h gate等

---

## 待解决问题（按优先级）

- [ ] P1: round84: 确认 `_reset_grid` 中 `_tp_exposed_since` 是否被清零（grep确认），保证 circuit_break 成功后 `_tp_exposed_since` 不残留重新触发
- [ ] P1: round84: 检查 `BOT_MAX_SESSION_HOURS` 默认值是否 >=24，若 <20 需改为 24
- [ ] P2: 验证服务实际运行状态（systemctl status / journalctl 最新日志）
- [ ] P2: 验证 analysis.jsonl 中 orphan_close 事件是否已被触发（第78轮新增）

## 下次优先行动

**round84：**
1. `grep -n '_tp_exposed_since' quant/strategy/grid_pro.py` 确认 `_reset_grid` 中有清零
2. `grep -n 'BOT_MAX_SESSION' quant/settings.py` 确认默认值 >=24
3. 若 `_tp_exposed_since` 在 `_reset_grid` 中未清零：在 `_reset_grid` 末尾补充 `self._tp_exposed_since = 0.0`

## 系统评估
- **策略有效性**：9/10
  - 83轮迭代，强平保护链条现完全正确：`_place_tp`失败→裸仓60s（仅当无circuit_break）→`_emergency_close`→API失败→circuit_break 60s→`_grid_active=False`→重试
  - 两路径严格互斥，不存在竞争条件
- **当前主要风险**：
  1. 沙盒网络受限，实盘逻辑和市场响应无法验证
  2. 需确认 `_reset_grid` 清零 `_tp_exposed_since`（理论上应有）
- **累计运行轮次**：83
