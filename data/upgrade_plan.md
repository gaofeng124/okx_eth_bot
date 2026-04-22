# ETH量化系统升级计划

## 本次（2026-04-22 第二十五轮）完成

### grid_pro.py：ATR 基线动态止盈（_eff_tp_mult × atr_ratio）

**背景：**
第24轮实现了分Regime双桶EWMA，使TP自适应在RANGING/TRENDING市场中各自独立工作。
但 `_eff_tp_mult` 的值对"当前波动率是否高于历史正常水平"无感知：
- 高波动行情（ETH急拉/急跌时 ATR 是平时3×）：TP目标过近，只捕获了价格延伸的一小段
- 低波动静市（ATR 是平时0.5×）：TP目标过远，价格根本走不到，TP超时/不成交

**改动：**

1. **`_atr_baseline: float = 0.0`** 新增状态变量
   - 慢速 EMA（α=0.05，≈20次_place_grid更新后趋稳）
   - 冷启动：首次_place_grid时直接初始化为当前spacing（安全）

2. **`_place_grid` 中更新基线**（在raw spacing计算后、FGI/趋势调整前）
   - `_atr_baseline = 0.05*spacing + 0.95*_atr_baseline`
   - 追踪"正常格宽"，不受单次FGI/趋势调整污染

3. **`_update_tp` 中应用 ATR 联动**
   - `_atr_ratio = clamp(self._grid_spacing / _atr_baseline, 0.8, 1.3)`
   - `_eff_tp_mult = clamp(_eff_tp_mult * _atr_ratio, 0.4, 2.0)`
   - 高波动（ratio=1.3）：TP延伸30%，捕获更多价格延伸
   - 低波动（ratio=0.8）：TP收紧20%，提高静市成交率

**效果预期：**
- 高ATR时：原来1.0×spacing的TP → 1.3×spacing（更多利润）
- 低ATR时：原来0.8×spacing的RANGING TP → 0.64×spacing（更易成交）
- 与分Regime EWMA叠加：两层自适应协同工作

---

## 历史完成

### 第二十四轮（2026-04-22）
- [x] grid_pro.py: 分Regime EWMA双桶（_tp_profits_ranging/_tp_profits_trending）

### 第二十三轮（2026-04-22）
- [x] grid_pro.py: _replay_tp_history() — 重启从日志恢复TP历史

### 第二十二轮（2026-04-22）
- [x] grid_pro.py: EWMA 时间衰减（半衰期30min）

### 第二十一轮（2026-04-22）
- [x] grid_pro.py: FGI格宽双向调整 + _refresh_funding REST fallback

### 第一~二十轮（2026-04-18/19/20/21）
- [x] 所有P0/P1问题：GRID_DAILY_TARGET=999, lock_path修复, fill事件, WS重连, 持仓同步
- [x] 双维度自适应TP（trigger + offset），动态格宽，FGI感知，资金费率防御等

---

## 待解决问题（按优先级）

- [ ] P3: RANGING/TRENDING的trail trigger/offset上下界独立调参
  - 当前两制度共享 [0.20, 0.50] 和 [0.08, 0.35] 的边界
  - TRENDING 的 trigger 上界可放开到 0.60（更大延伸空间）
  - RANGING 的 offset 下界可收紧到 0.05（贴市价锁利）
- [ ] P3: _atr_baseline 持久化（重启后恢复，避免冷启动期无ATR联动）
  - 方案：在 grid_session.json 中存储 _atr_baseline 值
  - 载入时恢复：`self._atr_baseline = session_data.get("atr_baseline", 0.0)`
- [ ] P3: 验证ATR联动实际效果
  - 日志搜索：`ATR联动 TP: spacing=... baseline=... ratio=... eff_mult=...`
  - ratio应在0.8~1.3之间波动，eff_mult应在0.4~1.6之间

---

## 下次优先行动

1. **P3: RANGING trigger/offset 独立上下界**
   - `_adaptive_trail_trigger` 中：RANGING 上界 0.50，TRENDING 上界 0.60
   - `_adaptive_trail_offset` 中：RANGING 下界 0.05，TRENDING 下界 0.10
   - 避免RANGING行情中trail trigger被拉到TRENDING级别的宽松值

---

## 系统评估
- **策略有效性**：9/10
  - 25轮迭代；全P0/P1已解决
  - 自适应层：ATR联动TP（第25轮）+ 分Regime EWMA（第24轮）+ 时间衰减（第22轮）+ 冷启动恢复（第23轮）
  - FGI感知：三维度（档位-1/+1 + 格宽×0.8/×1.2）
  - 资金费率：runtime优先 + REST fallback
- **主要风险点**：
  1. 外部API网络受限（无法验证实盘运行状态）
  2. _atr_baseline冷启动期（首次运行无历史，但已安全处理）
  3. 无实盘日志可验证，改进效果依赖代码分析
- **累计运行轮次**：25
