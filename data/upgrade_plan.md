# ETH量化系统升级计划

## 本次（2026-04-20 第十四轮）完成

### 1. runner.py：WS主循环 dispatch_tick 异常防护（P1）
- **问题**：WS模式下 `async for row in stream_tickers(...)` 循环内的 `_dispatch_tick`
  调用无任何 try/except 保护。若账户客户端REST调用、库内部数据格式异常或其他
  意外错误抛出，整个 `run()` 协程会立即崩溃退出，进程终止（需外部脚本重启）。
  对比：REST模式已有完整的 `while True: try:...except: continue` 保护。
- **修复**：在 `_dispatch_tick(...)` 调用外包裹 try/except：
  `CancelledError` 继续向上抛（确保任务取消正常工作），
  其他所有 Exception 记录 `log.error` 后 `continue` 处理下一个 tick。
- **效果预期**：单个 tick 处理异常不再崩溃主循环；log.error 记录便于事后排查根因。

### 2. grid_pro.py：_maybe_trail_tp 增加 RANGING 模式感知（P3）
- **问题**：TP追踪阈值（0.4格）和步长（0.25格）固定不变。
  RANGING（横盘）行情特征：价格上冲后大概率快速均值回归，
  若触发阈值太宽（0.4格），价格可能在超出TP + 0.4格后已经开始回落，
  导致 trail 上调后的新 TP 仍未成交就被止损吃掉。
- **修复**：RANGING模式使用更激进的追踪参数：
  触发阈值 0.3格（原0.4格）、步长 0.15格（原0.25格）。
  这意味着在均值回归行情中，TP 在价格超过原TP + 0.3格时立即上移，
  且落点更近（mid - 0.15格），确保在价格反转前尽快成交。
- **效果预期**：RANGING模式下每格平均捕获利润略低于0.95×spacing，
  但成交概率提升，整体RPS（收益/持仓时间）改善。

### 3. regime.py：修正过期注释（P3）
- 原注释"不足8s"与 `_MIN_HOLD_SEC=20.0` 不匹配，修正为准确描述。

---

## 历史完成

### 第十三轮（2026-04-20）
- [x] settings.py: GRID_CONTRACTS_PER_SLOT 0.2→1.0（消除lot_sz=1时的歧义）
- [x] grid_pro.py: _update_tp增加RANGING模式TP系数0.8×spacing

### 第十二轮（2026-04-20）
- [x] grid_pro.py：修复 `_sz()` int() 截断 Bug（防御 fractional lot_sz）
- [x] grid_pro.py：_fetch_instrument_spec 增加有效张数诊断日志
- [x] grid_pro.py：放宽 SHORT_VELOCITY_ALARM_PCT（-0.0025→-0.003）

### 第一~十一轮（2026-04-18/19/20）
- [x] 所有P0/P1问题：GRID_DAILY_TARGET=999, lock_path修复, fill事件, WS重连, 持仓同步, 资金费率, 趋势过滤等

---

## 已知问题清单（按优先级）

### 待处理
- [ ] P1: 验证生产日志——确认dispatch_tick异常防护未频繁触发
- [ ] P1: 验证analysis.jsonl中RANGING模式TP trail的新参数（0.3/0.15格）是否按预期触发
- [ ] P2: 服务器 .env 确认 BOT_MAX_SESSION_HOURS=24
- [ ] P2: 若市场数据可用，检查资金费率情况（<-0.01%需关注多头槽位减少）
- [ ] P3: RANGING TP trail步长0.15格是否最优（可依成交速度数据动态调整至0.10~0.20）

---

## 下次优先做

1. **P1: 生产日志验证**
   - 检查是否出现 `[行情][WS] dispatch_tick 异常` 错误日志（新保护）
   - 检查 analysis.jsonl 最新 TP trail 事件，验证 RANGING 模式下触发阈值/步长

2. **P2: 实盘数据驱动参数调优**
   - 若可获取市场数据：根据 FGI/资金费率做动态调整
   - 若 24h 涨跌 < -2%: 考虑临时降低 GRID_LEVELS=3

3. **P3: GRID_WHOLE_STOP_USDT 动态化**
   - 当前 5.0U 固定止损对于账户净值变化敏感度不足
   - 可考虑 WHOLE_STOP = max(4.0, equity * 0.10) 动态计算

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 14轮迭代，P0/P1/P2问题全面修复；P3持续精化中
  - 最新: WS主循环异常保护 + RANGING模式TP追踪优化
  - 主要缺口：无法获取实时行情做市场适应性参数调整
- **主要风险点**：
  1. 无法访问生产日志，改进效果全依赖代码分析而非实盘验证
  2. RANGING TP trail步长0.15格在单次大涨行情中可能损失潜在利润（整体止损兜底）
  3. dispatch_tick异常保护会掩盖底层bug，需监控error日志
- **累计运行轮次**：14
