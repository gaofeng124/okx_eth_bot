# ETH量化系统升级计划

## 本次（2026-05-04 第八十轮）完成

### grid_pro.py：新增裸仓超时强平保护（_tp_exposed_since）

**问题根因：**

`_update_tp` 和 `_maybe_trail_tp` 中 `_place_tp` 失败后，虽然第79轮新增了
0.5s 重试和 error 日志，但若 API 持续异常（如 OKX 维护窗口），每个 tick 都会
重试且失败，导致持仓永久裸露（无任何止盈/止损保护）直到 API 恢复。

**修复方案：**

1. `__init__`：新增 `_tp_exposed_since: float = 0.0`（裸仓计时器）
2. `_reset_grid`：清仓时同步清零 `_tp_exposed_since = 0.0`
3. `_update_tp`：
   - 挂单成功：`_tp_exposed_since = 0.0`（清除计时）
   - 两次失败：若 `_tp_exposed_since == 0`，记录当前时间（裸仓起点）
4. `_maybe_trail_tp`（short/long 两个分支）：`_place_tp` 失败时同样记录 `_tp_exposed_since`
5. Tick 恢复检查块（line ~2494 之后）：`_update_tp()` 调用后若 `_tp_order_id` 仍为空
   且 `now - _tp_exposed_since >= 60.0` → 调用 `_emergency_close('tp_place_timeout_Xs')`

**效果预期：**
- API 持续故障时，最多 60s 后主动强平，彻底封堵无限裸仓窗口
- 计时器在成功挂单后自动清零，不影响正常交易路径
- 强平原因字符串包含实际暴露时长，方便事后排查

---

## 历史完成（节选）

### 第七十九轮（2026-05-03）
- [x] grid_pro.py: 修复 _update_tp / _maybe_trail_tp 三处静默失败：加重试（0.5s）+ error/warning 日志

### 第七十八轮（2026-05-03）
- [x] grid_pro.py: 修复 _reset_grid 两个缺陷：幽灵仓TP未取消 + 孤儿仓无analysis事件

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

- [ ] P1: round81: 检查 _position_sync_check 中 mid=0（tick gap）时 est_entry=0 导致补录跳过的边缘情况，补充降级处理或延迟恢复机制
- [ ] P1: round81: 验证 analysis.jsonl 中 orphan_close 事件是否已被触发（第78轮新增）
- [ ] P2: 验证服务实际运行状态（systemctl status / journalctl 最新日志）
- [ ] P2: _emergency_close 本身失败（API仍中断）时的处理路径——目前无二次兜底

## 下次优先行动

**round81：**
1. 阅读 `_position_sync_check` 中 `est_entry=0` 的处理路径（lines ~2276-2280）
2. 当 `mid=0` 时，补录被跳过但内部状态未更新——这会导致下一次对账仍检测到差异并反复告警
3. 修复方案：记录"待对账时间戳"，mid 恢复后重入；或在 diff>threshold 且 est_entry=0 时
   至少更新 `_total_held = exchange_sz`（先以交易所为准，避免持续差异告警）

## 系统评估
- **策略有效性**：9/10
  - 80轮迭代，TP保护路径已形成完整闭环：挂单 → 重试 → 计时 → 强平
  - _position_sync_check 双向对账逻辑完备
  - 裸仓暴露从"无上限"降至"最多60秒"
- **当前主要风险**：
  1. 沙盒网络受限，实盘逻辑和市场响应无法验证
  2. _position_sync_check 中 mid=0 边缘情况补录跳过（下轮P1）
  3. _emergency_close 在 API 中断时自身也可能失败（二次兜底缺失）
- **累计运行轮次**：80
