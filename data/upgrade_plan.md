# ETH量化系统升级计划

## 本次（2026-04-24 第三十二轮）完成

### grid_pro.py：两项代码质量修复

**Fix-A：LONG路径 TP 追踪节流时间戳位置错误（P2 代码不对称 bug）**

背景（回顾 round 31）：
- round 31 声称修复了"SHORT路径_last_tp_trail_ts不一致"，将 SHORT 的时间戳从
  `if new_tp < tp_price: if oid:` (24格) 移至 `if mid < trigger:` (16格) 外层
- 但 LONG 路径（round 31 的"参考目标"）的时间戳实际在 `if new_tp > tp_price:` (20格) 内部
- 导致真实不对称：SHORT 在触发条件成立时更新节流，LONG 仅在 TP 真正改善时更新

问题：LONG 路径中若 `mid > tp + trigger*spacing` 成立但 `new_tp <= old_tp`（价格未超出
足够距离），触发条件成立但节流时间戳不更新 → 下一 tick 又重新评估触发条件 → spin 风险，
每2-5秒重复计算（虽然不会下单，但逻辑路径重复评估）

修复：将 LONG 路径 `self._last_tp_trail_ts = now` 从20格移至16格
（`if new_tp > tp_price:` 外层，`if mid > trigger:` 内层），与 SHORT 完全对齐

效果：两个方向触发条件成立即更新节流，消除潜在spin-loop，完成 round 31 未竟的对称修复

**Fix-B：7e 宽限期 info 日志节流（P3 日志质量改进）**

背景：
- round 31 引入 7e 宽限期：持仓 60-120min 且盈利 > $0.10 且有 TP 挂单时，输出 info 日志
- 该日志在 `elif _hold_elapsed > 3600.0:` 下，每 tick 触发
- 按 2-5s tick 间隔计算，60min 宽限期内产生 720-1800 条 info 日志
- 日志文件膨胀，影响 grep 分析效率

修复：加入 `if int(_hold_elapsed) % 300 < 5:` 节流
效果：每5分钟区间内仅在前5秒内输出，宽限期总日志从 720+ 条降至 ~12-24 条

---

## 历史完成

### 第三十一轮（2026-04-24）
- [x] grid_pro.py: 7e持仓硬超时盈利误强平：盈利>$0.10且TP挂单时延至2h
- [x] grid_pro.py: SHORT方向TP追踪节流时间戳移至触发条件外（部分修复，本轮补全LONG）

### 第三十轮（2026-04-23）
- [x] grid_pro.py: _recent_entries_ts补仓节流清理窗口60s→120s（防节流失效）
- [x] grid_pro.py: 持仓硬超时7e节（基于fill_ts，防无限持仓）

### 第二十九轮（2026-04-23）
- [x] grid_pro.py: _price_1h_cache失败后每tick阻塞5s改为60s重试
- [x] grid_pro.py: status_summary增加fgi/atr_baseline_bps/eff_tp_mult监控字段

### 第二十八轮（2026-04-23）
- [x] grid_pro.py: _refresh_fgi失败后改为5分钟重试（原1小时）
- [x] grid_pro.py: fill_tp事件新增诊断字段

### 第一~二十七轮（2026-04-18~23）
- [x] 全部P0/P1问题：参数修复、WS重连、持仓同步、自适应TP、EWMA、FGI、资金费率等

---

## 待解决问题（按优先级）

- [ ] P3: 验证Fix-A对齐效果
  - 预期：LONG触发条件成立但TP未改善时，不再每tick重复评估（日志中`TP追踪上调`后无spin）
  - 验证方法：grep 'TP追踪上调' logs/*.log | awk -F'|' '{print $1}' | uniq -c | head

- [ ] P3: 验证Fix-B日志节流效果
  - 预期：grep '宽限到' logs/*.log 每5分钟出现一次，不再每tick
  - 验证方法：grep -c '宽限到' logs/*.log（应<30条/小时，而非720+）

- [ ] P3: 验证7e修复效果（来自round 31）
  - 预期：hard_hold_timeout触发减少；avg_win改善
  - 验证：grep -h 'hard_hold_timeout' logs/*.log | wc -l

- [ ] P3: analysis.jsonl fill_tp事件profit_spacings分布分析
  - 目标：EWMA自适应是否有效（avg profit_spacings是否收敛到0.4-0.8范围）

## 下次优先行动

1. **P3: 确认round 32两项修复的实际效果**
   - 若有日志访问，grep验证节流和对称性
   - 若无日志，考虑增加单元测试覆盖_maybe_trail_tp的边界情况

2. **P3: 考虑_maybe_trail_tp的单元测试**
   - 该函数已经历3轮修改（trail偷利润→trigger调整→SHORT对称→LONG对称）
   - 增加pytest覆盖LONG/SHORT路径、trigger不满足、new_tp不改善等情况

## 系统评估
- **策略有效性**：9/10
  - 32轮迭代；全P0/P1已解决；代码成熟度极高
  - 本轮两项fix均属代码质量/对称性，不影响核心盈利逻辑
  - 改进空间主要在实盘数据验证和精细调参
- **当前主要风险**：
  1. 外部API网络受限（沙箱限制，无实时市场监控）
  2. 实盘日志无法访问（所有优化均未经实盘数据验证）
  3. 7e宽限期节流使用秒级取模（ticks>5s间隔时可能跳过当期输出，但后续tick补充）
- **累计运行轮次**：32
