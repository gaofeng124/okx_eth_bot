# ETH量化系统升级计划

## 本次（2026-04-22 第二十四轮）完成

### grid_pro.py：分Regime的EWMA双桶

**背景：**
第22~23轮实现了 EWMA 时间衰减 + 冷启动恢复，但 RANGING 和 TRENDING 共享同一个
`_tp_fill_profits` deque。问题：震荡行情 profit_spacings ≈ 0.3~0.6，趋势行情 ≈ 0.8~1.5，
混合后 EWMA 均值被稀释，导致：
- 震荡时均值被趋势拉高 → adaptive trigger 偏宽 → 锁利不足
- 趋势时均值被震荡压低 → adaptive trigger 偏窄 → 过早退出利润延伸

**改动：**

1. **`_tp_profits_ranging` / `_tp_profits_trending`** 替代 `_tp_fill_profits`
   - 各 maxlen=20，合计最多40条历史
   - `_tp_current_bucket` property 按 `self._current_regime` 路由

2. **`_ewma_profit_avg()`** 改用 `self._tp_current_bucket`
   - 同一Regime内部的EWMA，信号纯净

3. **fill_tp 写入** 改为 `self._tp_current_bucket.append(...)`
   - `record_analysis("fill_tp", ..., regime=self._current_regime.value)` 写入日志

4. **`_replay_tp_history()`** 按日志 `regime` 字段分流
   - 旧格式无 `regime` 字段时默认归入 RANGING bucket（向后兼容）
   - 取最近40条（两桶各最多20），日志分别报告 ranging/trending 可用条数

5. **debug日志** 新增 `regime=` 字段，便于追踪自适应在哪个制度生效

**效果预期：**
- RANGING 制度：EWMA 只看震荡行情成交，trigger/offset 响应震荡节奏
- TRENDING 制度：EWMA 只看趋势行情成交，trigger/offset 响应趋势延伸
- 两个制度冷启动各需 ≥5 条，初期冷启动期无自适应（正常行为）

---

## 历史完成

### 第二十三轮（2026-04-22）
- [x] grid_pro.py: _replay_tp_history() — 重启从日志恢复TP历史，EWMA无冷启动死区

### 第二十二轮（2026-04-22）
- [x] grid_pro.py: EWMA 时间衰减（半衰期30min，maxlen 10→20）

### 第二十一轮（2026-04-22）
- [x] grid_pro.py: FGI格宽双向调整 + _refresh_funding REST fallback

### 第一~二十轮（2026-04-18/19/20/21）
- [x] 所有P0/P1问题：GRID_DAILY_TARGET=999, lock_path修复, fill事件, WS重连, 持仓同步
- [x] 双维度自适应TP（trigger + offset），动态格宽，FGI感知，资金费率防御等

---

## 待解决问题（按优先级）

- [ ] P2: 验证分Regime桶实际分流
  - 日志搜索 `冷启动恢复 TP 历史: 找到 X 条，ranging=Y trending=Z`
  - 若 trending=0 说明近期无趋势行情成交（正常）或旧日志无regime字段（第一天运行正常）
- [ ] P3: tp_mult 与 ATR 联动的动态止盈
  - 当前 tp_mult 固定；ATR 高时每格利润空间大，可以提高 tp_mult
  - 方案：`_eff_tp_mult = self._tp_mult * clamp(atr_ratio, 0.8, 1.3)`
  - atr_ratio = current_atr / ewma_atr_baseline（20期）
- [ ] P3: RANGING/TRENDING 的 trigger/offset 上下界独立调参
  - 当前两制度共享 [0.20, 0.50] 和 [0.08, 0.35] 的边界
  - TRENDING 的上界可以放开至 0.60（更大延伸空间）

---

## 下次优先行动

1. **P3: tp_mult 与 ATR 联动**
   - 在 GridProStrategy 新增 `_atr_baseline: float`（初始化为0，首次由20期均值设置）
   - 每次 `_place_grid` 时：`atr_ratio = spacing / _atr_baseline`（近似ATR比率）
   - `_eff_tp_mult *= clamp(atr_ratio, 0.8, 1.3)`
   - 边界：tp_mult 最终值不超过 [0.4, 2.0]

---

## 系统评估
- **策略有效性**：9/10
  - 24轮迭代；全P0/P1已解决
  - 自适应层：分Regime EWMA（第24轮）+ 时间衰减（第22轮）+ 冷启动恢复（第23轮）
  - FGI感知：三维度（档位-1/+1 + 格宽×0.8/×1.2）
  - 资金费率：runtime优先 + REST fallback
- **主要风险点**：
  1. 外部API网络受限（无法验证实盘运行状态）
  2. 分Regime桶初期均<5条：自适应不激活，完全依赖固定base值（安全但无动态调整）
  3. 无实盘日志可验证，改进效果依赖代码分析
- **累计运行轮次**：24
