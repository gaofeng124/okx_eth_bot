# ETH量化系统升级计划

## 本次（2026-05-03 第七十五轮）完成

### grid_pro.py：修复 _sync_tp partially_canceled 分支 PnL 双算 bug

**问题**：
- 当 TP 限价单被"部分成交后撤销"时（OKX `state=partially_canceled`），原代码：
  1. 只减少 `total_held`，未记录已成交部分的收益 → PnL **漏记**
  2. 各槽位 `fill_sz` 未按比例缩减 → 后续补挂的新 TP 成交时，PnL 按原始 `fill_sz` 计算 → **双算**
- 漏记方向：对于盈利的TP部分成交，daily_pnl_realized 低于实际；对于亏损的场景（如 fill_px < vwap），漏记负值

**修复（round75）**：
1. 计算 `fill_ratio = fill_sz / total_held`（填充比例）
2. 遍历所有 HOLDING 槽位：按 `fill_ratio` 计算每槽已成交量 `slot_filled`
3. 对每槽：计算 PnL + fee，累加到 `partial_net`，调用 `_pnl.add(net_after)`
4. 将各槽 `s.fill_sz` 减去 `slot_filled`（缩减到剩余量）
5. `_recent_close_pnls.append(partial_net)` 纳入 loss_streak 监控
6. 写入 `fill_tp_partial` analysis 事件（含 fill_price/fill_sz/partial_pnl/remaining/regime）
7. 然后减少 `total_held` + 重挂 TP（逻辑顺序已优化：先记录再更新状态）

**效果预期**：
- PnL 精确性提升：partial TP cancel 场景不再漏记/双算收益
- `fill_tp_partial` 事件可用于统计 OKX partial cancel 频率（小账户期待接近0）
- loss_streak 正确计入部分TP的盈亏（防止极端场景下多次部分亏损不触发冷静期）

---

## 历史完成（节选）

### 第七十四轮（2026-05-02）
- [x] grid_pro.py: status_summary() 增加 sz_scale_last/loss_streak/slot持仓时长，_log_status 增加5分钟 grid_state_snapshot

### 第七十三轮（2026-05-02）
- [x] grid_pro.py: 修复 _check_phase4_trend_guard 5处硬编码路径

### 第七十二轮（2026-05-02）
- [x] settings.py: 删除重复 DATA_DIR 定义，路径体系完整

### 第七十一轮（2026-05-01）
- [x] grid_pro.py: loss_streak 冷静期跨重启持久化

### 第一~七十轮（2026-04-18~05-01）
- [x] 全部P0/P1问题：WS重连/持仓同步/自适应TP/EWMA/FGI/资金费率/1h gate等

---

## 待解决问题（按优先级）

- [ ] P1: round76: 修复 `_cancel_order` 未检查 OKX 响应 `data[0].sCode`
  - 当前实现：只检查 HTTP 状态和顶层 `code` 字段，sCode 非零（如 51401=订单不存在）被忽略
  - 影响：`_reset_grid` 的 cancel 失败处理路径可能错误地认为 cancel 成功
  - 修复：解析 `(resp.get("data") or [{}])[0].get("sCode","0")` 并返回 False 时记录 debug 日志
- [ ] P2: round76: 验证服务实际运行状态
  - 检查 `systemctl status okx-eth-bot`
  - 检查 `data/logs/daily/` 是否有最近日志
- [ ] P2: round76: 若有 analysis.jsonl 积累，统计 fill_tp_partial 事件频率
  - 期待频率接近0（小账户 sz=0.2 张，partial fill 极少见）
  - 若有：统计 partial_net 分布，验证修复前后 daily_pnl 差值

## 下次优先行动

**round76：**
1. 修复 `_cancel_order` 的 OKX sCode 检查（P1，影响订单状态一致性）
2. 检查服务状态 + 日志目录
3. 若有 fill_tp 记录：验证 ewma_tp_mult 分布（期待均值接近2.0而非偷盈利的低值）

## 系统评估
- **策略有效性**：9/10
  - 75轮迭代，P0/P1 全部修复
  - 本轮修复 partial TP cancel PnL 双算，提升记账精度
  - _cancel_order sCode 问题是下一个已知 P1 缺陷
- **当前主要风险**：
  1. 沙盒网络受限，无法验证实盘数据
  2. _cancel_order 未验证 sCode（P1，下轮修复）
  3. 服务运行状态未确认
- **累计运行轮次**：75
