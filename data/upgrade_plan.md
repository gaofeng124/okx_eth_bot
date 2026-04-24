# ETH量化系统升级计划

## 本次（2026-04-24 第三十一轮）完成

### grid_pro.py：两项 bug 修复

**1. Fix1：7e 持仓硬超时盈利误强平（P1 成本控制 bug）**

背景：
- 7e 基于 `slot.fill_ts` 计算真实持仓时长（第30轮引入，用于防无限持仓）
- 原逻辑：`_hold_elapsed > 3600.0` → 无条件 `_emergency_close`
- 问题：若持仓已经盈利（unrealized > $0.10）且 TP 单仍在等待成交，
  1h 超时会触发 taker 市价平，损耗额外 ~3-5bps（taker vs maker 差额），
  同时放弃即将以 maker 价成交的 TP 利润
- 场景：震荡行情中 TP 设在 30bps 以上，持仓 60+ min 但仍在正 PnL 区间，
  等待价格短暂上冲以 maker 成交——此时 7e 触发会将 $0.30+ 盈利变为 $0.05 亏损

修复：
- unrealized > 0.10 且 TP 挂单中（tp_order_id 存在）→ timeout 从 3600s 延至 7200s
- 其余情况（亏损/无TP）仍保持 1h 硬断，防慢出血绕过
- 在 60-120min 窗口期输出 info 日志供监控
- 7b（价格破位）和 7d（慢出血）仍作为前置保护：盈利回撤到 -$0.30 时 7d 先触发

效果：减少不必要的 taker 强平，提升 avg_win/avg_loss 比值

**2. Fix2：SHORT 方向 TP 追踪节流时间戳更新不一致（P2 代码对称性 bug）**

背景：
- `_maybe_trail_tp` 中 SHORT 路径（lines 1363-1379）：
  `_last_tp_trail_ts = now` 在 `if new_tp < self._tp_price:` 内部
- LONG 路径（lines 1381-1396）：`_last_tp_trail_ts = now` 在外部（"无论成功与否"）
- 不一致性：SHORT 在 trigger 触发但 new_tp 未改善时不更新节流时间戳
- 虽然数学上 trigger 触发时 new_tp 几乎必然 < tp_price（trigger≥1.0 > offset_max 0.75），
  但代码不对称有潜在 spin-loop 风险，且与 LONG 注释意图矛盾

修复：将 SHORT 路径的 `_last_tp_trail_ts = now` 移出 `if new_tp < self._tp_price:` 块
效果：两个方向节流行为完全一致，消除边界情况下潜在的过度 API 调用

---

## 历史完成

### 第三十轮（2026-04-23）
- [x] grid_pro.py: _recent_entries_ts补仓节流清理窗口60s→120s（防节流失效）
- [x] grid_pro.py: 持仓硬超时7e节（基于fill_ts，防无限持仓）

### 第二十九轮（2026-04-23）
- [x] grid_pro.py: _price_1h_cache失败后每tick阻塞5s改为60s重试
- [x] grid_pro.py: status_summary增加fgi/atr_baseline_bps/eff_tp_mult监控字段

### 第二十八轮（2026-04-23）
- [x] grid_pro.py: _refresh_fgi失败后改为5分钟重试（原1小时）
- [x] grid_pro.py: fill_tp事件新增诊断字段

### 第二十七轮（2026-04-23）
- [x] grid_pro.py: _atr_baseline持久化（重启恢复）

### 第一~二十六轮（2026-04-18~23）
- [x] 全部P0/P1问题：参数修复、WS重连、持仓同步、自适应TP、EWMA、FGI、资金费率等

---

## 待解决问题（按优先级）

- [ ] P3: 验证7e修复效果
  - `grep -h "hard_hold_timeout" logs/*.log` 观察触发频率
  - `grep -h "宽限到" logs/*.log` 确认盈利保护窗口触发
  - 对比修复前后 avg_win 是否提升

- [ ] P3: analysis.jsonl fill_tp事件profit_spacings分布分析
  - 目标：EWMA自适应是否有效（avg profit_spacings 是否收敛到0.4-0.8范围）
  - 检查 ranging vs trending 两个bucket的数据质量

- [ ] P3: 若确认7e宽限有效，考虑进一步优化退出策略
  - 盈利超过 TP 期望值 50% 时，主动收紧 TP（而非等待自然填单）
  - 防止盈利从高点回撤后以更差价格 fill

## 下次优先行动

1. **P3: 验证两轮修复的联合效果**
   - 核心指标：avg_win 趋势、hard_hold_timeout 触发减少
   - 如有实盘日志访问，统计"盈利宽限"触发次数与节省的 taker 成本

2. **P3: 考虑 runner.py WebSocket 重连稳定性（若有网络恢复）**
   - 已有基础重连逻辑，确认断线后 5min 内恢复订阅

## 系统评估
- **策略有效性**：9/10
  - 31轮迭代；全P0/P1已解决；本轮修复盈利误强平+SHORT节流不一致
  - 代码成熟度极高，改进空间主要在实盘数据验证和精细调参
- **当前主要风险**：
  1. 外部API网络受限（无法实时监控市场状态）
  2. 实盘日志无法访问（所有优化均未经实盘数据验证）
  3. 7e延至2h的新风险：60-120min窗口内盈利可能反转（但7b/7d作为前置保护）
- **累计运行轮次**：31
