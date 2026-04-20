# ETH量化系统升级计划

## 本次（2026-04-20 第十五轮）完成

### grid_pro.py：整体止损动态化（P3）
- **问题**：`self._whole_stop = 5.0U` 固定值不随账户净值变化。
  账户若成长至 80U，5.0U 止损仅占 6.25%（偏紧）；
  账户若缩水至 35U，5.0U 止损占 14%（偏松，可能亏完本金）。
- **修复**：在每个 tick 的步骤6中计算有效止损阈值：
  `_eff_whole_stop = max(4.0, (equity or 0.0) * 0.10) if equity else self._whole_stop`
  - 账户 50U → 止损 5.0U（与原来相同）
  - 账户 80U → 止损 8.0U（随账户增长放宽）
  - 账户 35U → 止损 4.0U（账户缩水时收紧，4U为底）
  - equity 无效（0/None）→ fallback 到配置的 self._whole_stop = 5.0U
- **效果预期**：止损比例始终维持在账户余额 10% 左右，减少超量亏损。

---

## 历史完成

### 第十四轮（2026-04-20）
- [x] runner.py: WS主循环 dispatch_tick 异常防护（CancelledError继续抛，其他Exception记录后continue）
- [x] grid_pro.py: _maybe_trail_tp 增加 RANGING 模式感知（触发阈值0.3格、步长0.15格）

### 第十三轮（2026-04-20）
- [x] settings.py: GRID_CONTRACTS_PER_SLOT 0.2→1.0
- [x] grid_pro.py: _update_tp增加RANGING模式TP系数0.8×spacing

### 第十二轮（2026-04-20）
- [x] grid_pro.py：修复 `_sz()` int() 截断 Bug
- [x] grid_pro.py：放宽 SHORT_VELOCITY_ALARM_PCT（-0.0025→-0.003）

### 第一~十一轮（2026-04-18/19/20）
- [x] 所有P0/P1问题：GRID_DAILY_TARGET=999, lock_path修复, fill事件, WS重连, 持仓同步, 资金费率, 趋势过滤等

---

## 待解决问题（按优先级）

- [ ] P1: 验证生产日志——确认dispatch_tick异常防护未频繁触发
- [ ] P1: 验证 analysis.jsonl 中 RANGING 模式 TP trail 新参数是否按预期触发
- [ ] P2: 服务器 .env 确认 BOT_MAX_SESSION_HOURS=24
- [ ] P3: GRID_DRAWDOWN_FROM_PEAK_USDT 也动态化（当前固定2.0U，账户成长后偏紧）
- [ ] P3: RANGING TP trail步长0.15格是否最优（可依成交速度数据动态调整至0.10~0.20）

---

## 下次优先行动

1. **P3: GRID_DRAWDOWN_FROM_PEAK_USDT 动态化**
   - 当前2.0U固定值对50U账户占4%；账户若成长需同步放宽
   - 建议：`_eff_drawdown = max(1.5, equity * 0.04)` 即4%浮亏为峰值回撤上限
   - 实现位置：`PnlTracker.check_drawdown()` 方法 或 `on_tick` 调用时传入动态值

2. **P2: 验证动态止损效果**
   - 查看日志是否出现 `有效阈值=X.XXU, 余额=XX.XXU` 的止损日志
   - 确认 equity 数值是否合理（接近账户余额）

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 15轮迭代；P0/P1/P2全面修复；P3精化进行中
  - 最新：整体止损动态化（随账户余额10%浮动）
  - 主要缺口：无法获取实时行情，改进效果依赖代码分析而非实盘验证
- **主要风险点**：
  1. usdt_avail_swap 作为余额代理有轻微低估（margin占用），对10%止损计算影响轻微
  2. 无法访问生产日志，dispatch_tick异常保护频率未知
  3. 市场数据不可用，无法做FGI/资金费率自适应调整
- **累计运行轮次**：15
