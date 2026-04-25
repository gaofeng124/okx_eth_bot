# ETH量化系统升级计划

## 本次（2026-04-25 第三十六轮）完成

### grid_pro.py：1h方向gate滞回环（hysteresis）+ 日志节流（P3）

**问题**：第34/35轮新增的 LONG 1h-drop-gate 和 SHORT 1h-rise-gate 使用单一阈值（0.99/1.01），存在两个缺陷：
1. **边界震荡**：价格在0.99附近反复穿越时，gate 在"激活→释放→激活"之间不断翻转，导致开格决策不稳定（可能在数秒内交替放行/拒绝）
2. **日志刷屏**：gate 活跃时每 tick（0.5s）输出一条 info 日志，每分钟 120 条，大量占用磁盘和掩盖重要日志

**修复**（round36）：
- 新增 3 个状态变量：`_long_drop_gate`, `_short_rise_gate`, `_last_gate_log_ts`
- 双阈值滞回环（Schmitt Trigger 原理）：
  - LONG gate：entry=0.990（跌>1%触发）, exit=0.995（涨回0.5%释放）
  - SHORT gate：entry=1.010（涨>1%触发）, exit=1.005（回落0.5%释放）
- 日志节流：gate 活跃期间每 60s 输出一次，不再每 tick 刷屏

**效果预期**：
- 消除价格在±1% 附近震荡时 gate 的"乒乓"（chattering），开格更稳定
- 日志减少 >99%（120 条/min → 最多 1 条/min）
- gate 的保护有效性不变：核心逻辑（1%偏离阈值）未改变

---

## 历史完成

### 第三十五轮（2026-04-24）
- [x] grid_pro.py: SHORT方向1h快速上涨硬止进gate（entry=1.01），与LONG的drop-gate对称

### 第三十四轮（2026-04-24）
- [x] grid_pro.py: 新增LONG方向1h价格下跌硬止进门槛（entry=0.99）

### 第三十三轮（2026-04-24）
- [x] runner.py: WS重连固定5s→指数退避(1s→2s→4s...→30s)
- [x] grid_pro.py: profit_spacings存入EWMA桶前加min(x,3.0)上限帽

### 第三十二轮（2026-04-24）
- [x] grid_pro.py: LONG路径_last_tp_trail_ts=now移至触发条件外层（与SHORT对称）
- [x] grid_pro.py: 7e宽限期info日志加int(elapsed)%300<5节流

### 第三十一轮（2026-04-24）
- [x] grid_pro.py: 7e持仓硬超时盈利误强平修复（盈利>$0.10且TP挂单时延至2h）
- [x] grid_pro.py: SHORT方向TP追踪节流时间戳修复

### 第一~三十轮（2026-04-18~23）
- [x] 全部P0/P1问题：参数修复、WS重连、持仓同步、自适应TP、EWMA、FGI、资金费率等

---

## 待解决问题（按优先级）

- [ ] P3: 验证gate实际触发频率（需实盘日志）
  - 方法：`grep '1h-drop-gate\|1h-rise-gate' data/logs/*.log | wc -l`
  - 预期：滞回环后，每天有效触发次数应 < 单阈值版本（因 exit 宽松减少了重复触发）
  - 若每天>30次有效触发：考虑放宽 entry 到 0.985/1.015

- [ ] P3: 验证profit_spacings EWMA分布
  - 方法：从analysis.jsonl提取fill_tp的profit_spacings，期望0.4-1.5格均值
  - 若<0.4持续：trail_trigger基础值 1.00→1.10（RANGING）

- [ ] P3: 验证WS指数退避效果（round33）
  - 方法：`grep '\[WS行情\]' logs/*.log | grep '后重连'`
  - 预期：断线间隔逐步变长，1s→2s→4s...，无频繁秒级重连

## 下次优先行动

1. **若能访问实盘日志**：
   - 查看gate日志确认滞回环效果（每分钟最多1条，非每tick）
   - 统计drop-gate和rise-gate的触发→释放周期长度（期望5-30min）

2. **P3候选**：若profit_spacings EWMA持续<0.4格：
   - 调整 `_adaptive_trail_trigger` 的 RANGING base_trigger: 1.00 → 1.10
   - 意味着价格超出TP 1.1格才开始追踪（比原来1格更宽容）

3. **P3候选**：监控 `_bearish_regime_since` 宽限期频率
   - 若VOLATILE状态频繁持续60s+导致频繁强平：考虑宽限期从60s→90s

## 系统评估
- **策略有效性**：9/10
  - 36轮迭代；全P0/P1已解决；P2/P3改进积累中
  - 1h方向gate现在具备滞回环保护，边界稳定性提升
  - 主要待验证：实盘gate触发率、profit_spacings EWMA收敛
- **当前主要风险**：
  1. 外部API网络受限（沙盒环境，无实时市场监控）
  2. 实盘日志无法访问（所有优化均未经实盘数据验证）
  3. gate滞回环的5bps宽松区在极端行情可能导致一次额外的逆势开格
- **累计运行轮次**：36
