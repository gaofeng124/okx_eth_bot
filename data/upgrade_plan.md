# ETH量化系统升级计划

## 本次（2026-05-03 第七十九轮）完成

### grid_pro.py：修复 _update_tp / _maybe_trail_tp 静默失败问题

**问题根因：**

`_update_tp` 和 `_maybe_trail_tp` 在调用 `_place_tp()` 失败后（返回空串），
均静默返回，无任何日志输出。这意味着：

- `_tp_order_id` 被清空（cancel 执行了），但新 TP 未挂成功
- 持仓处于裸露状态（无止盈保护），直到下一 tick 的恢复检查（line 2484）才补救
- 运维无法从日志中察觉此类事件的频率

**三处修复：**

1. `_update_tp`：`_place_tp` 首次失败后等 0.5s 立即重试；两次均失败时输出
   `log.error` 含完整上下文（held/vwap/tp）
2. `_maybe_trail_tp` short 分支：`_place_tp` 失败时输出 `log.warning`
3. `_maybe_trail_tp` long 分支：同上

**恢复检查注释更新（line 2481）：**
将注释从"仅描述重启场景"扩展为"明确覆盖运行时 _place_tp 失败场景"，
与实际逻辑对齐，方便后续维护者理解。

**效果预期：**
- 裸仓窗口从"一整tick（可达数秒）"降低到"0.5s重试后若仍失败则最多一tick"
- 失败事件现在有日志可查，可被监控系统告警

---

## 历史完成（节选）

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

- [ ] P1: round80: 检查 _sync_pos（持仓对账）逻辑：_total_held 与交易所实际持仓出现差异时的处理路径（告警？强制重置？）
- [ ] P1: round80: 验证 analysis.jsonl 中 orphan_close 事件是否已被触发（第78轮新增）
- [ ] P2: 验证服务实际运行状态（systemctl status / journalctl 最新日志）
- [ ] P2: _place_tp 连续失败场景：是否需要在达到 N 次失败后触发 emergency_close？

## 下次优先行动

**round80：**
1. 阅读 `_sync_pos`（或 `_position_sync_check`）函数的完整逻辑
2. 确认内外持仓差异的判断阈值和处理分支
3. 若差异处理存在缺口（如仅告警不纠偏），补充强制对齐逻辑

## 系统评估
- **策略有效性**：9/10
  - 79轮迭代，止损/TP路径健壮性持续加固
  - _update_tp 新增重试机制，裸仓窗口缩短
  - 三处静默失败改为有日志，可观测性提升
- **当前主要风险**：
  1. 沙盒网络受限，无法验证实盘逻辑和市场数据
  2. _sync_pos 对账失败处理未审查（下轮P1）
  3. analysis.jsonl orphan_close 事件待实盘验证
- **累计运行轮次**：79
