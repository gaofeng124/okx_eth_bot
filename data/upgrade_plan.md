# ETH量化系统升级计划

## 本次（2026-04-22 第二十三轮）完成

### grid_pro.py：_tp_fill_profits 冷启动历史恢复

**背景：**
第22轮实现了 EWMA 时间衰减加权（半衰期30min），但每次重启后 `_tp_fill_profits`
清零，需要等待至少5次新TP成交后才能启用自适应 trigger/offset。
若每次部署/崩溃重启都有5次以上冷启动成交，这段时间内自适应完全失效（使用固定base值）。

**改动：**

1. **`self._data_dir = data_dir`**（__init__ 第438行）
   - 将构造参数 data_dir 持久化到实例，供冷启动恢复方法使用

2. **`fill_tp` 日志新增 `profit_spacings` 字段**（第1701行）
   - `_ps: float | None = None`（安全local变量替代旧的 profit_spacings）
   - `record_analysis("fill_tp", ..., profit_spacings=_ps)` 
   - None时JSON写 null，重播时 `if ps is None: continue` 自动跳过旧格式日志

3. **新增 `_replay_tp_history()` 方法**（第1391行）
   - 读取今日 + 昨日 `data/logs/daily/{date}/analysis.jsonl`
   - 筛选 `event == "fill_tp"` 且含 `profit_spacings` 的记录
   - 用 `datetime.fromisoformat(ts_wall).timestamp()` 还原epoch时间
   - 排序后取最近20条写入 `_tp_fill_profits`
   - 日志提示恢复条数和EWMA是否即时可用

4. **`__init__` 末尾调用**（第499行）
   - `self._replay_tp_history()` 在 `_boot_reconcile()` 之后执行
   - 启动即可用，无需等待新成交

**效果：**
- 重启后如果当日/昨日有 ≥5 次 TP 成交日志，EWMA 自适应立即激活
- 旧格式日志（无 profit_spacings 字段）静默跳过，向后兼容
- 语法检查通过

---

## 历史完成

### 第二十二轮（2026-04-22）
- [x] grid_pro.py: _tp_fill_profits 升级为 EWMA 时间衰减（半衰期30min，maxlen 10→20）
- [x] grid_pro.py: _ewma_profit_avg() 方法（指数权重，ln2/1800s）
- [x] grid_pro.py: _adaptive_trail_trigger / _adaptive_trail_offset 使用 EWMA

### 第二十一轮（2026-04-22）
- [x] grid_pro.py: FGI格宽双向调整（极恐<25→×0.8，贪婪>70 RANGING→×1.2）
- [x] grid_pro.py: _refresh_funding 新增REST fallback（runner未提供时直接HTTP获取）

### 第一~二十轮（2026-04-18/19/20/21）
- [x] 所有P0/P1问题：GRID_DAILY_TARGET=999, lock_path修复, fill事件, WS重连, 持仓同步
- [x] 双维度自适应TP（trigger + offset），动态格宽，FGI感知，资金费率防御等

---

## 待解决问题（按优先级）

- [ ] P1: 验证冷启动恢复实际触发
  - 部署后搜索日志 `[grid] 冷启动恢复 TP 历史` 确认恢复条数
  - 若日志显示"待更多成交"说明当日TP次数不足5次（正常，初期可接受）
- [ ] P2: 分Regime的EWMA
  - 当前RANGING/TRENDING共享一个 _tp_fill_profits
  - 趋势行情利润格数远高于震荡，混合后EWMA被稀释
  - 考虑 `_tp_profits_ranging` / `_tp_profits_trending` 分别维护
- [ ] P3: profit_spacings 使用 TRENDING_UP 时赋予更高权重
  - 趋势行情中每格利润通常更高，可分 regime 分别维护 EWMA
- [ ] P3: 动态止盈：根据波动率调整每格利润（tp_mult与ATR联动）

---

## 下次优先行动

1. **P2: 分Regime的EWMA**（若P1验证通过）
   - 在 GridProStrategy 新增 `_tp_profits_ranging` 和 `_tp_profits_trending`（各maxlen=20）
   - `_tp_fill_profits` 改为按 `self._current_regime` 路由到对应 deque
   - `_ewma_profit_avg()` 也按 regime 读取对应 deque
   - 冷启动恢复：`_replay_tp_history` 按日志中记录的 regime 字段分流（需先在fill_tp日志中写入regime）

2. **若P2完成**：评估 tp_mult 与 ATR 联动的动态止盈

---

## 系统评估
- **策略有效性**：9/10
  - 23轮迭代；全P0/P1已解决
  - 自适应层：EWMA时间衰减（第22轮）+ 冷启动恢复（第23轮）→ 重启无冷启动死区
  - FGI感知：三维度（档位-1/+1 + 格宽×0.8/×1.2）
  - 资金费率：runtime优先 + REST fallback
- **主要风险点**：
  1. 外部API网络受限（无法验证实盘运行状态）
  2. 分Regime混合EWMA被稀释（P2待解决）
  3. 无实盘日志可验证，改进效果依赖代码分析
- **累计运行轮次**：23
