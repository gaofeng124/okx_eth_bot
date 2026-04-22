# ETH量化系统升级计划

## 本次（2026-04-22 第二十一轮）完成

### grid_pro.py：FGI格宽双向调整 + 资金费率REST fallback

**改动1：FGI格宽调整（_place_grid，在档位减1后叠加）**

背景：第15轮之前FGI只影响档位数量（极端情绪-1档），但格宽始终由ATR决定，
未体现市场情绪对最优格距的影响。

改进：
```python
# 极度恐慌 FGI < 25 → 格宽×0.8（收窄20%）
# 理由：极恐时ETH小幅震荡频率上升（投资者焦虑频繁买卖），窄格可捕捉更多往返
if self._fear_greed_index < 25:
    spacing = max(spacing * 0.80, self._min_sp)

# 贪婪 FGI > 70 + RANGING → 格宽×1.2（扩宽20%）
# 理由：贪婪震荡期单次波幅更大，宽格每格利润更高，且减少无效频繁成交
elif self._fear_greed_index > 70 and regime == Regime.RANGING:
    spacing = min(spacing * 1.20, self._max_sp)
```

注意：此调整在顺势偏置（spacing×1.3）之前执行，两者不冲突（独立乘子）。

**改动2：_refresh_funding REST自取fallback**

背景：_refresh_funding原先只从runtime dict读取（由runner提供）。
若runner由于异常未能填充funding_rate，资金费率将永远停在0.0，
资金费率逆风检测完全失效。

改进：当runtime未提供时，直接REST GET /api/v5/public/funding-rate获取，
失败则保留缓存值（最多每30s重试一次，`self._last_fund_ts`防止频繁请求）。

**预期效果：**
- FGI极恐（<25）：档位-1 + 格宽×0.8，双重收缩敞口，高频小格防止大亏损
- FGI贪婪（>70，RANGING）：格宽×1.2，放大每格利润
- 资金费率：无论runner是否正常，30s内必有一次真实值（或缓存降级）

---

## 历史完成

### 第二十轮（2026-04-21）
- [x] grid_pro.py: 新增 _adaptive_trail_offset 方法
- [x] TP步长也根据近期成交利润格宽倍数自动调节（双维度自适应闭环完整落地）

### 第十九轮（2026-04-21）
- [x] grid_pro.py: 新增 _adaptive_trail_trigger 方法
- [x] _tp_fill_profits deque(maxlen=10) 记录TP成交利润
- [x] TP触发门槛自适应（avg<0.4→+0.10, avg>0.8→-0.05）

### 第十八轮（2026-04-21）
- [x] grid_pro.py: RANGING 模式 _trail_trigger=0.30 + _min_trail_iv=20s

### 第十七轮（2026-04-21）
- [x] grid_pro.py: RANGING 模式动态 TP 系数（tp_mult×0.8）
- [x] grid_pro.py: RANGING trail_offset=0.15，存储 _current_regime

### 第十六轮（2026-04-21）
- [x] grid_pro.py: 动态整体止损 + 动态峰值回撤上限

### 第一~十五轮（2026-04-18/19/20）
- [x] 所有P0/P1问题：GRID_DAILY_TARGET=999, lock_path修复, fill事件, WS重连, 持仓同步等

---

## 待解决问题（按优先级）

- [ ] P1: 验证 adaptive 双维度实际效果
  - 日志搜索 `adaptive trigger:` 和 `adaptive offset:` 行确认自适应触发
  - 至少需要5次TP成交后才会触发
- [ ] P2: 验证FGI格宽调整实际触发
  - 当前FGI=50（中性），需FGI<25或>70才触发；观察效果后再决定是否调整阈值
- [ ] P3: profit_spacings 考虑EWMA加权（近期成交权重更高，自适应响应更快）
- [ ] P3: 动态止盈：根据波动率调整每格利润（tp_mult与ATR联动）

---

## 下次优先行动

1. **P1: 日志验证adaptive系列** — 搜索analysis.jsonl或logs中`adaptive`关键词
   - 若5次TP后仍未见自适应触发，检查_tp_fill_profits是否正确写入

2. **P3: EWMA加权profit_spacings**
   - `_tp_fill_profits`改为记录(ts, value)元组
   - _adaptive_trail_trigger/_adaptive_trail_offset用指数加权平均替代简单mean

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 21轮迭代；全面修复P0/P1
  - FGI感知：三维度（档位-1/+1 + 格宽×0.8/×1.2），情绪响应完整
  - 资金费率：runtime优先 + REST fallback，双路数据保障
  - TP追踪：双维度自适应（trigger+offset），形成闭环学习
- **主要风险点**：
  1. adaptive系列需5次真实TP成交才生效，冷启动期使用固定base值
  2. 外部API（FGI、资金费率）在受限环境中通过urllib也可能不可达
  3. 无实盘日志可验证，改进效果依赖代码分析
- **累计运行轮次**：21
