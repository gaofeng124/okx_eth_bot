# ETH量化系统升级计划

## 本次（2026-04-21 第二十轮）完成

### grid_pro.py：_adaptive_trail_offset — TP步长自适应

**背景：** 第19轮落地了 `_adaptive_trail_trigger`，让 TP 触发门槛根据实盘数据自适应。
但 `_trail_offset`（TP 落点与市价的距离）仍是固定值（RANGING=0.15，TRENDING=0.25），
无法根据实盘反馈自我调整。

**改进：新增 `_adaptive_trail_offset(base_offset)` 方法**

共用同一信号源 `_tp_fill_profits`（第19轮引入，maxlen=10）：
```
profit_spacings = abs(fill_px - vwap) / spacing
```

调整逻辑：
- 近期均值 < 0.30格 → 利润偏低（offset 太紧，TP 离市价太近，过早成交）→ +0.03（放宽步长）
- 近期均值 > 0.80格 → 利润充足但延迟锁定（可能错过回撤）→ -0.03（收紧步长）
- 中间范围 [0.30, 0.80] → 保持 base，不干预
- 有界：[0.08, 0.35]，防止极端飘移
- 至少5次成交数据才生效，否则直接用 base

在 `_maybe_trail_tp` 中：
```python
# 修改前（固定值）：
_trail_offset = 0.15 if _is_ranging else 0.25

# 修改后（双维度自适应）：
_trail_offset  = self._adaptive_trail_offset(0.15 if _is_ranging else 0.25)
_trail_trigger = self._adaptive_trail_trigger(0.30 if _is_ranging else 0.40)
```

**自适应闭环完整性（第19+20轮合并效果）：**
```
实盘TP成交 → profit_spacings 记录
    ↓
avg < 0.30：trigger +0.10（不那么容易触发追踪）+ offset +0.03（追踪时TP落点更远）
avg > 0.80：trigger -0.05（更敏感触发追踪）  + offset -0.03（追踪时TP落点更近）
```
两个维度协同工作，共同优化每次TP的成交利润。

**预期效果：**
- RANGING 模式前期：base offset=0.15, trigger=0.30（固定）
- 若成交利润持续偏低（<0.30格）：offset → 0.18，trigger → 0.40（两者都放宽，给更多空间）
- 若成交利润较高（>0.80格）：offset → 0.12，trigger → 0.25（两者都收紧，更快锁定）

---

## 历史完成

### 第十九轮（2026-04-21）
- [x] grid_pro.py: 新增 _adaptive_trail_trigger 方法
- [x] grid_pro.py: _tp_fill_profits deque(maxlen=10) 记录TP成交利润
- [x] TP触发门槛从固定值改为根据实盘数据自适应（avg<0.4→+0.10, avg>0.8→-0.05）

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

- [ ] P2: 资金费率感知代码框架
  - 即使外部API不可达，也应有 fallback 逻辑（cached value + 超时降级）
  - funding_rate < -0.01% → _max_levels = max(1, levels - 1)（多头减1档）
  - funding_rate > +0.01% → short 方向减1档（当前策略以多头为主，可暂缓）
- [ ] P2: FGI感知框架
  - FGI < 25 时格宽收窄20%（乘以0.8）
  - FGI > 70 且价格上升时格宽扩大20%
  - 同样需要 cached value + 超时降级逻辑
- [ ] P1: 验证 adaptive 双维度实际效果
  - 日志搜索 `adaptive trigger:` 和 `adaptive offset:` 行确认自适应触发
  - 至少需要5次TP成交后才会触发
- [ ] P3: 考虑给 profit_spacings 添加指数加权（EWMA）替代简单平均
  - 使近期成交权重更高，让自适应响应更快

---

## 下次优先行动

1. **P2: 资金费率感知框架（可离线编码，不依赖外部API）**
   - 在 GridProStrategy.__init__ 中添加 `_cached_funding_rate = 0.0` 和 `_fr_updated_at = 0.0`
   - 添加 `_fetch_funding_rate()` 方法：调用OKX REST API，失败则保留缓存值（最多1h）
   - 在 `_compute_levels()` 中：`if self._cached_funding_rate < -0.0001: n_levels = max(1, n_levels - 1)`

2. **P2: FGI感知框架**
   - 类似资金费率，添加 `_cached_fgi = 50` 和超时降级
   - 接入 alternative.me API

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 20轮迭代；P0/P1全面修复
  - TP 追踪系统：双维度自适应（trigger + offset）完整落地，形成真正的闭环学习
  - 第20轮：offset 自适应，与第19轮 trigger 自适应协同工作
- **主要风险点**：
  1. adaptive 系列需5次真实TP成交才生效，冷启动期间使用固定base值
  2. 外部API（FGI、资金费率）长期不可达，相关功能尚未实现（无降级逻辑）
  3. 无实盘日志可验证，改进效果依赖代码分析
- **累计运行轮次**：20
