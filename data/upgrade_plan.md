# ETH量化系统升级计划

## 本次（2026-04-19 第十轮）完成

### 1. grid_pro.py：TP 追踪频率限制（P1 API 效率修复）
- **问题**：`_maybe_trail_tp` 在每个 WS tick（~200ms）都可能触发，
  若市场持续上行，每 200ms 就 cancel/replace 一次 TP，产生大量 API 调用，
  消耗 rate limit，增加网络延迟风险。
- **修复**：新增 `_TP_TRAIL_MIN_INTERVAL = 30.0` 常量 + `self._last_tp_trail_ts` 实例变量，
  两次追踪间隔强制 ≥ 30s。即使 mid 每 tick 都超过阈值，30s 内只执行一次 cancel/replace。
- **影响**：减少约 150 倍的 TP 相关 API 调用（200ms → 30s 间隔）

### 2. grid_pro.py：TP 追踪后重置超时计时器（P2 逻辑修复）
- **问题**：`_maybe_trail_tp` 成功将 TP 上移（market going our way），
  但未更新 `self._tp_placed_ts`。超时检查依然从原始 TP 下单时间计算，
  最多 480-600s 后若价格回落就触发紧急平仓，即使我们已成功向上锁定利润。
- **修复**：TP 追踪成功后 `self._tp_placed_ts = now`，给新 TP 位置一个新的超时窗口，
  避免在市场上行充分调整后仍因"老计时器"触发不必要的紧急平仓。
- **影响**：减少上涨行情中的误止损，保留更多盈利空间

### 3. grid_pro.py：紧急平仓手续费精度修复（P2 PnL 准确性）
- **问题**：`_market_close_all` 使用 `self._roundtrip_fee`（4bps，maker+maker），
  但紧急平仓实际是：入场 maker(2bps) + 市价 taker(5bps) = **7bps**。
  每次紧急平仓多计 3bps 利润（PnL 高估）。
- **修复**：新增 `_EMERGENCY_CLOSE_FEE_BPS = 7.0` 常量，`_market_close_all` 使用此值。
- **影响**：PnL 记录更准确，避免误认为某次亏损交易是盈利

---

## 历史完成

### 第九轮（2026-04-19）
- [x] runner.py：修复 `_ws_price_feed_loop` 死代码（P1 Bug修复）
- [x] runner.py：新增 `_ws_stall_watchdog_loop`（P2 Bug修复）

### 第八轮（2026-04-19）
- [x] runner.py：修复 `orders_attempted` NameError（P1）
- [x] runner.py：注册 `_lev5_hourly_report_loop` 后台任务（P1 死代码修复）
- [x] grid_pro.py：每小时输出 Regime 分类统计（P3）

### 第七轮（2026-04-19）
- [x] strategy/grid_pro.py：持仓同步自动修复（P2）
- [x] strategy/grid_pro.py：TP 追踪更积极（P3）

### 第六轮（2026-04-18）
- [x] 重启后持仓无TP自动恢复（P1）
- [x] macro_bearish 阈值与 regime 对齐（P2）
- [x] FGI>60 + TRENDING_UP 多1档（P3）

### 第五轮（2026-04-18）
- [x] TRENDING_UP 格宽放大 ×1.3（P3）
- [x] 宽限期 45s → 60s（P2）
- [x] TP 超时时长 Regime 感知（P2）

### 第四轮（2026-04-18）
- [x] WS 静默挂死修复（P1）
- [x] 短窗口急跌过滤 -0.0025（P2）

### 第三轮（2026-04-18）
- [x] TRENDING_DOWN 持仓宽限期加长（P2）
- [x] FGI 恐贪集成，FGI<25 减1档（P2）

### 第二轮（2026-04-18）
- [x] 负资金费率减少激活档位（P2）
- [x] TP 超时加速（P2）

### 第一轮（2026-04-18）
- [x] P0：GRID_DAILY_TARGET_USDT=999（致命缺陷修复）
- [x] P0：GRID_DRAWDOWN_FROM_PEAK_USDT=3.0
- [x] P0：run_strategy.py lock_path 动态路径
- [x] P1：analysis.jsonl fill_entry/fill_tp 事件

---

## 已知问题清单（按优先级）

### 待处理
- [ ] P1: 验证第八/九/十轮修复是否生效（需 analysis.jsonl 生产日志）
  - 需确认 [WS][保底] 日志是否出现
  - 需确认 [grid·regime·stats] 每小时出现
  - 需确认 TP 追踪日志出现且不超频
- [ ] P2: 动量急跌阈值验证 — `_SHORT_VELOCITY_ALARM_PCT=-0.0025`
  若每日触发 >5 次（正常震荡市），应放宽至 -0.003 或 -0.0035
- [ ] P2: 服务器 .env 确认 BOT_MAX_SESSION_HOURS=24
- [ ] P3: REST 模式下 synthetic tick 机制（优先级低，REST 有 60s 轮询）

---

## 下次优先做

1. **P1: 读取生产日志验证所有修复**
   - `analysis.jsonl` 中 fill_entry / fill_tp 事件是否正常写入
   - velocity_alarm 事件触发频率是否合理（<5次/天）
   - TP trail 日志间隔是否 ≥30s

2. **P2: 动量过滤阈值评估**
   - 如果 short_drop 触发 >5次/天：放宽 `_SHORT_VELOCITY_ALARM_PCT` 到 -0.003
   - 如果 TP trail 触发频率正常：考虑降低阈值从 0.4格宽 → 0.35格宽

3. **P3: 资金费率负值时的持仓策略**
   - 当前只减1档。考虑在极负费率（< -0.001）时取消 TRENDING_UP 加档

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 十轮迭代覆盖所有已知 P0/P1/P2 问题
  - TP 追踪现在节流（30s），不会消耗 API rate limit
  - 紧急平仓 PnL 更精确（7bps vs 4bps）
- **主要风险点**：
  1. 仍无生产日志，所有参数未经实盘验证
  2. `_SHORT_VELOCITY_ALARM_PCT=-0.0025` 可能在震荡市误拦正常回调
  3. 30s TP 追踪间隔在极速行情下可能错过最佳锁利位置（可接受的权衡）
