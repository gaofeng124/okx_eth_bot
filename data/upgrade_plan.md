# ETH量化系统升级计划

## 本次（2026-04-21 第十九轮）完成

### grid_pro.py：_trail_trigger 自适应优化

**背景：** 第17/18轮落地了 RANGING 模式 TP 追踪三件套（距离×0.8 + 触发0.30 + 步长0.15 + 间隔20s），
但触发门槛 `_trail_trigger` 仍是固定值，无法根据实盘反馈自我调整。

**改进：从实盘TP成交数据中学习最优trigger**

新增 `_tp_fill_profits: deque[float]` (maxlen=10)，记录每次TP成交时：
```
profit_spacings = abs(fill_px - vwap) / (vwap * grid_spacing)
```
即：TP成交价相对VWAP的距离，以格宽为单位（> 1.0 表示超出1个格宽才成交）。

新增 `_adaptive_trail_trigger(base: float) -> float` 方法：
- 近期均值 < 0.4格 → 锁利太少（trigger太小/offset太紧，TP太早被追）→ trigger +0.10（放宽）
- 近期均值 > 0.8格 → RANGING中价格延伸过大后才成交 → trigger -0.05（收紧）
- 中间范围 → 保持 base，不干预
- 有界：[0.20, 0.50]，防止极端飘移
- 至少5次成交数据才生效，否则直接用 base_trigger

`_maybe_trail_tp` 原有逻辑不变，仅将硬编码改为：
```python
_trail_trigger = self._adaptive_trail_trigger(0.30 if _is_ranging else 0.40)
```

**预期效果：**
- RANGING 模式前期：base=0.30（第18轮设定）
- 若成交利润持续偏低（< 0.4格）：自动升至0.40，与趋势模式一致
- 若成交利润较高（> 0.8格）：自动降至0.25，更快锁定利润

---

## 历史完成

### 第十八轮（2026-04-21）
- [x] grid_pro.py: RANGING 模式 _trail_trigger=0.30（触发门槛更敏感）
- [x] grid_pro.py: RANGING 模式 _min_trail_iv=20s（节流更短）
- [x] RANGING TP 三件套完整落地（距离0.8x + trigger0.30 + offset0.15 + iv20s）

### 第十七轮（2026-04-21）
- [x] grid_pro.py: RANGING 模式动态 TP 系数（tp_mult×0.8）
- [x] grid_pro.py: RANGING 模式 trail_offset=0.15（趋势=0.25）
- [x] grid_pro.py: 存储 _current_regime 实例变量

### 第十六轮（2026-04-21）
- [x] grid_pro.py: 动态整体止损 max(4U, equity×10%)
- [x] grid_pro.py: 动态峰值回撤上限 max(1.5U, equity×4%)

### 第一~十五轮（2026-04-18/19/20）
- [x] 所有P0/P1问题：GRID_DAILY_TARGET=999, lock_path修复, fill事件, WS重连, 持仓同步等

---

## 待解决问题（按优先级）

- [ ] P1: 验证第19轮 adaptive trigger 实际效果
  - 实盘日志搜索 `adaptive trigger: base` 行确认调整方向
  - 至少需要5次TP成交后才会触发自适应
- [ ] P2: 资金费率感知（API可达后）
  - 资金费率 < -0.01% → 多头减少1档
  - 资金费率 > +0.01% → 空头减少1档
- [ ] P2: FGI感知代码已有框架，待确认外部API可达
- [ ] P3: 进一步优化 _trail_offset 也改为自适应
  - 同样根据 profit_spacings：太小→紧些(0.10→0.08)，太大→松些(0.15→0.20)

---

## 下次优先行动

1. **P1: 验证 adaptive trigger**
   - 日志搜索 `[grid] adaptive trigger:` 确认参数被调整
   - 搜索 `profit_spacings` 或 TP 成交日志统计平均利润

2. **P2: 若外网恢复，实现资金费率实时感知**
   - funding_rate < -0.01% → _max_levels -= 1
   - funding_rate > +0.01% → short 方向 _max_levels -= 1

3. **P3: _trail_offset 自适应**
   - 与 trigger 自适应类似，根据 profit_spacings 微调步长

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 19轮迭代；P0/P1全面修复；RANGING 模式 TP 追踪三件套完整落地
  - 第19轮：trigger 从固定值 → 实盘自适应，形成闭环学习
- **主要风险点**：
  1. adaptive trigger 前5次成交前不生效，仍使用固定 base 值
  2. 外部 API（FGI、资金费率）长期不可达，相关功能降级为默认值运行
  3. 无实盘日志可验证，改进效果依赖代码分析
- **累计运行轮次**：19
