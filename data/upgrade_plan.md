# ETH量化系统升级计划

## 本次（2026-04-24 第三十四轮）完成

### grid_pro.py：1h价格下跌硬止进门槛（P2）

**问题**：S7价格位置因子（权重0.30）只在距1h高点<20bps时有效；
当价格已回落>100bps时（如从$2000跌到$1980，跌1%），S7=0（中性），
方向评分不会自动阻止新LONG格开仓。Regime检测器的TRENDING_DOWN需要
macro_bias<=-0.30%（5min EMA偏离），对1h中期下行有盲区窗口（约15-30分钟）。

**修复**：在`_update_entry`的10e-2位置（dir-score gate之后、节流gate之前）插入：
```python
if not self._is_short and not self._grid_active and _hi_1h > 0 and mid < _hi_1h * 0.99:
    log.info("[grid][1h-drop-gate] ...")
    return None
```
- 仅影响新格激活（`not self._grid_active`），不影响已有仓位的TP/止损执行
- 仅影响LONG策略（`not self._is_short`），SHORT策略价格下跌是顺风
- `_hi_1h`在此位置已赋值（line 2611），安全访问
- API未缓存时`_hi_1h=0`，条件自动跳过，无副作用

**效果**：价格在1h内下跌>1%时，新LONG格开仓被硬性阻止，
在S7软信号（0分）和regime检测器之间填补盲区，防止中期下行中持续接刀。

---

## 历史完成

### 第三十三轮（2026-04-24）
- [x] runner.py: WS重连固定5s→指数退避(1s→2s→4s...→30s，成功收数重置)
- [x] grid_pro.py: profit_spacings存入EWMA桶前加min(x,3.0)上限帽+replay_tp_history同步修复

### 第三十二轮（2026-04-24）
- [x] grid_pro.py: LONG路径_last_tp_trail_ts=now移至触发条件外层（与SHORT对称）
- [x] grid_pro.py: 7e宽限期info日志加int(elapsed)%300<5节流

### 第三十一轮（2026-04-24）
- [x] grid_pro.py: 7e持仓硬超时盈利误强平修复（盈利>$0.10且TP挂单时延至2h）
- [x] grid_pro.py: SHORT方向TP追踪节流时间戳修复

### 第三十轮（2026-04-23）
- [x] grid_pro.py: _recent_entries_ts补仓节流清理窗口60s→120s
- [x] grid_pro.py: 持仓硬超时7e节（基于fill_ts）

### 第一~二十九轮（2026-04-18~23）
- [x] 全部P0/P1问题：参数修复、WS重连、持仓同步、自适应TP、EWMA、FGI、资金费率等

---

## 待解决问题（按优先级）

- [ ] P3: 验证1h-drop-gate触发频率
  - 方法：`grep '1h-drop-gate' data/logs/*.log | wc -l`
  - 预期：每天触发5-20次（太少=无效；太多=阈值过严导致机会损失）

- [ ] P3: 验证WS指数退避效果（round33）
  - 方法：`grep '\[WS行情\]' logs/*.log | grep '后重连'` — 查看backoff序列是否递增

- [ ] P3: 验证profit_spacings上限帽效果（round33）
  - 方法：`grep 'adaptive trigger\|adaptive offset' logs/*.log | tail -20`

- [ ] P3: 分析实盘profit_spacings分布
  - 方法：从analysis.jsonl提取fill_tp的profit_spacings，期望0.5-1.5格均值

## 下次优先行动

1. **若能访问实盘日志**：
   - 验证1h-drop-gate的触发率（过高→考虑放宽到0.985；过低→确认正常）
   - 查看profit_spacings分布确认EWMA收敛状态

2. **P3候选**：若profit_spacings长期<0.4格，考虑放宽trail_trigger基础值（1.20→1.30）

3. **P3候选**：SHORT策略对称实现1h上涨>1%时暂停新SHORT格
   - 当前SHORT使用`macro_bias <= MACRO_DOWN_KILL`保护，类似保护不如LONG完善

## 系统评估
- **策略有效性**：9/10
  - 34轮迭代；全P0/P1已解决；P2中期下行防护完善
  - 新增1h下跌硬gate填补S7软信号与regime检测器之间的盲区
  - 主要待验证：实盘1h-drop-gate触发率、profit_spacings EWMA收敛
- **当前主要风险**：
  1. 外部API网络受限（沙盒环境，无实时市场监控）
  2. 实盘日志无法访问（所有优化均未经实盘数据验证）
  3. 1h-drop-gate阈值1%可能在高波动市场（ETH>100bps/h）过于保守
- **累计运行轮次**：34
