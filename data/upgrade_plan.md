# ETH量化系统升级计划

## 本次（2026-04-21 第十八轮）完成

### grid_pro.py：_maybe_trail_tp 三维 RANGING 模式优化完成

**背景：** 第17轮实现了 RANGING 模式 `trail_offset=0.15`（步长收紧）和 `tp_mult×0.8`（TP距离缩短）。
但触发门槛（0.4）和节流间隔（30s）仍是全局固定值，未考虑 RANGING 行情"价格延伸有限"的特性。

**改进1：触发门槛 `_trail_trigger` regime 感知**
- RANGING：`_trail_trigger = 0.30`（比原来的 0.40 更敏感）
- 趋势模式：`_trail_trigger = 0.40`（保持原值，给趋势价格更多延伸空间）
- 效果：RANGING 中价格超出 TP 0.3 格（原 0.4 格）即启动追踪，缩短"TP 挂在空中"时间

**改进2：节流间隔 `_min_trail_iv` regime 感知**
- RANGING：`_min_trail_iv = 20.0s`（原 30s，每分钟最多 3 次追踪）
- 趋势模式：`_min_trail_iv = 30.0s = _TP_TRAIL_MIN_INTERVAL`（保持原值）
- 效果：RANGING 中 TP 追踪更频繁，让 TP 始终贴近市场

**改进3：日志增强**
- 日志格式增加 `trigger` 和 `iv` 参数输出，便于实盘日志验证参数是否生效

**RANGING 模式 TP 三件套（17+18轮完整落地）：**
```
_update_tp:       TP距离 = VWAP + spacing × tp_mult × 0.8   (缩短20%)
_maybe_trail_tp:  触发门槛 = 0.30 格                          (比趋势敏感25%)
                  步长偏移  = 0.15 格                          (锁利更紧40%)
                  节流间隔  = 20s                               (比趋势频繁33%)
```

---

## 历史完成

### 第十七轮（2026-04-21）
- [x] grid_pro.py: RANGING 模式动态 TP 系数（tp_mult×0.8）
- [x] grid_pro.py: RANGING 模式 trail_offset=0.15（趋势=0.25）
- [x] grid_pro.py: 存储 _current_regime 实例变量

### 第十六轮（2026-04-21）
- [x] grid_pro.py: 动态整体止损 max(4U, equity×10%)
- [x] grid_pro.py: 动态峰值回撤上限 max(1.5U, equity×4%)

### 第十五轮（2026-04-20）
- [x] runner.py: WS主循环 dispatch_tick 异常防护
- [x] grid_pro.py: _maybe_trail_tp 引入 RANGING 模式框架

### 第一~十四轮（2026-04-18/19/20）
- [x] 所有P0/P1问题：GRID_DAILY_TARGET=999, lock_path修复, fill事件, WS重连, 持仓同步等

---

## 待解决问题（按优先级）

- [ ] P1: 验证 RANGING 三件套实际效果
  - 搜索日志 `[grid] TP 追踪上调` 确认 `[trigger=0.30 iv=20s]` 参数出现
  - 对比 RANGING/TRENDING 下 TP 成交速度和平均持仓时间
- [ ] P2: 资金费率感知（API可达后）
  - 资金费率 < -0.01% → 多头减少1档
  - 资金费率 > +0.01% → 空头减少1档
- [ ] P2: FGI 已有感知代码（< 25 减档，> 60 顺势加档），待确认 API 是否可达
- [ ] P3: _trail_trigger 自适应
  - 根据最近 N 次成交间隔动态调整 0.25~0.40

---

## 下次优先行动

1. **P1: 通过日志验证第18轮改进**
   - `[trigger=0.30]` 出现 → RANGING 触发门槛生效
   - `[iv=20s]` 出现 → 节流间隔生效
   - 统计 RANGING 场景下 TP 成交率变化

2. **P3: _trail_trigger 自适应优化**
   - 记录每次 TP 成交时的 mid-to-TP 距离
   - 若近期平均距离 < 0.2 格 → trigger 收至 0.25
   - 若近期平均距离 > 0.5 格 → trigger 扩至 0.45

3. **P2: 若外网恢复，实现资金费率实时感知**

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 18轮迭代；P0/P1全面修复；RANGING 模式 TP 追踪三件套完整落地
  - 动态止损、宽限期、慢出血保护、盘口不平衡过滤、US session 限档均已实现
- **主要风险点**：
  1. RANGING 触发门槛 0.30 较激进，价格在 TP 附近震荡时可能触发多次 cancel/replace（但 20s 节流控制在合理范围）
  2. 外部 API（FGI、资金费率）长期不可达，相关功能降级为默认值运行
  3. 无实盘日志可验证，改进效果依赖代码分析
- **累计运行轮次**：18
