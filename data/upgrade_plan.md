# ETH量化系统升级计划

## 本次（2026-04-24 第三十五轮）完成

### grid_pro.py：SHORT方向1h上涨硬止进gate（P3）

**问题**：第34轮为LONG方向添加了1h-drop-gate（价格从1h高点下跌>1%时暂停新LONG格），
但SHORT方向缺少对称保护。当价格从1h低点快速上涨>1%时，SHORT策略同样存在：
- S7（价格位置因子，权重0.30）仅在距1h低点<20bps时生效，价格已上涨>100bps时S7=0（中性）
- regime TRENDING_UP需要macro_bias>=+0.30%（5min EMA偏离）才触发，对1h中期上行有盲区窗口

**修复**：在10e-2 LONG gate之后插入10e-3 SHORT gate：
```python
if self._is_short and not self._grid_active and _lo_1h > 0 and mid > _lo_1h * 1.01:
    log.info("[grid][1h-rise-gate] ...")
    return None
```
- 仅影响新格激活（`not self._grid_active`），不影响已有仓位的TP/止损
- 仅影响SHORT策略（`self._is_short`）
- `_lo_1h`在此位置已赋值（line 2612），安全访问
- API未缓存时`_lo_1h=0`，条件自动跳过，无副作用

**效果**：价格在1h内上涨>1%时，新SHORT格开仓被硬性阻止，LONG/SHORT双向保护现在完全对称。

---

## 历史完成

### 第三十四轮（2026-04-24）
- [x] grid_pro.py: 新增1h价格下跌硬止进门槛——从1h高点回落>1%时暂停新LONG格

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

- [ ] P3: 验证1h-drop-gate + 1h-rise-gate触发频率
  - 方法：`grep '1h-drop-gate\|1h-rise-gate' data/logs/*.log | wc -l`
  - 预期：每天5-20次（太少=无效；太多=阈值0.99/1.01过严导致机会损失）
  - 若每天>50次：考虑放宽到0.985/1.015

- [ ] P3: 验证WS指数退避效果（round33）
  - 方法：`grep '\[WS行情\]' logs/*.log | grep '后重连'`

- [ ] P3: 分析实盘profit_spacings分布
  - 方法：从analysis.jsonl提取fill_tp的profit_spacings，期望0.5-1.5格均值

## 下次优先行动

1. **若能访问实盘日志**：
   - 验证1h-drop/rise-gate触发率（期望5-20次/天）
   - 查看profit_spacings分布确认EWMA收敛

2. **P3候选**：若两个gate触发率均过高（>50次/天），统一放宽阈值到0.985/1.015

3. **P3候选**：若profit_spacings长期<0.4格，考虑放宽trail_trigger基础值（1.20→1.30）

## 系统评估
- **策略有效性**：9/10
  - 35轮迭代；全P0/P1已解决；P2/P3改进积累中
  - LONG/SHORT方向的1h快速趋势保护现在完全对称
  - 主要待验证：实盘gate触发率、profit_spacings EWMA收敛
- **当前主要风险**：
  1. 外部API网络受限（沙盒环境，无实时市场监控）
  2. 实盘日志无法访问（所有优化均未经实盘数据验证）
  3. 两个1h gate阈值1%在高波动市场可能需要动态调整
- **累计运行轮次**：35
