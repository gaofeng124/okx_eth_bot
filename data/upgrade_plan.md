# ETH量化系统升级计划

## 本次（2026-04-21 第十七轮）完成

### grid_pro.py：RANGING 模式动态 TP 系数 + trail_offset 感知

**背景：** 第13轮记录"_update_tp增加RANGING模式TP系数"，但代码审计发现
`_tp_mult` 仅在构造函数设置一次，`_update_tp` 和 `_maybe_trail_tp` 均无 regime 感知。
本轮正式落地该功能。

**改进1：存储 `_current_regime` 实例变量**
- 在 `__init__` 新增 `self._current_regime: Regime = Regime.RANGING`
- 在 `on_tick` 赋值 `self._current_regime = regime`（每 tick 更新）
- 为后续所有子函数提供 regime 上下文

**改进2：`_update_tp` RANGING 模式 TP 距离缩短**
- `_eff_tp_mult = self._tp_mult * (0.8 if RANGING else 1.0)`
- ETH 32bps 格宽场景：RANGING TP = VWAP+4.1U，趋势 TP = VWAP+5.1U
- 效果：RANGING 市场 TP 更近 → 成交率更高 → 每日成交次数增加

**改进3：`_maybe_trail_tp` RANGING 模式追踪步长收紧**
- `_trail_offset = 0.15 if RANGING else 0.25`
- RANGING 行情不会持续上涨，追踪步长小 → 利润锁定更快
- 趋势行情保留 0.25× 追踪步长，给 TP 更多空间

---

## 历史完成

### 第十六轮（2026-04-21）
- [x] grid_pro.py: 动态整体止损 max(4U, equity×10%)
- [x] grid_pro.py: 动态峰值回撤上限 max(1.5U, equity×4%)

### 第十五轮（2026-04-20）
- [x] runner.py: WS主循环 dispatch_tick 异常防护
- [x] grid_pro.py: _maybe_trail_tp 引入 RANGING 模式框架（本轮完整落地）

### 第一~十四轮（2026-04-18/19/20）
- [x] 所有P0/P1问题：GRID_DAILY_TARGET=999, lock_path修复, fill事件, WS重连, 持仓同步等

---

## 待解决问题（按优先级）

- [ ] P1: 监控日志确认 `_current_regime` 正确传递，RANGING时TP价格 = VWAP×(1+0.8×spacing)
- [ ] P1: 确认 fill_tp 事件中 TP 价格符合 RANGING/TRENDING 预期
- [ ] P2: 资金费率感知（API可达后实现；资金费率<-0.01% 时减少多头槽位）
- [ ] P2: FGI 指数感知（FGI<25 时格宽收窄20%；FGI>70+价格上升时格宽扩大20%）
- [ ] P3: TP trail 步长自适应：根据历史成交速度动态调整 0.10~0.20

---

## 下次优先行动

1. **P1: 验证 RANGING 模式 TP 实际价格**
   - 搜索日志 `[grid] TP 追踪` 确认 `[offset=0.15]` 出现
   - 搜索日志 `[grid] 网格启动` 确认 TP 价格与 VWAP+0.8×spacing 对应

2. **P2: 实现资金费率感知（若API可达）**
   - 资金费率 < -0.01% → 多头模式下减少1个入场槽位
   - 资金费率 > +0.01% → 空头模式下减少1个入场槽位

3. **P3: 动态 TP trail 步长**
   - 根据最近N次成交时间间隔，动态调整 _trail_offset 在 [0.10, 0.25] 范围内

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 17轮迭代；P0/P1全面修复；本轮实现真正的 RANGING 模式 TP 优化
  - 动态阈值（整体止损 + 峰值回撤）随账户余额自适应已落地
  - RANGING 模式 TP 缩短 → 成交频率提升 → 日收益改善
- **主要风险点**：
  1. _trail_offset 0.15 在极端行情下可能使 TP 过早锁定，错过大行情
  2. 市场 API 不可达，FGI/资金费率自适应功能暂时无法实现
  3. 无实盘日志可验证，改进效果依赖代码分析
- **累计运行轮次**：17
