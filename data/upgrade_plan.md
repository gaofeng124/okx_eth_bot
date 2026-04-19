# ETH量化系统升级计划

## 本次（2026-04-19 第八轮）完成

### 1. runner.py：修复 `orders_attempted` NameError（P1 Bug修复）
- **位置**：`_lev5_hourly_report_loop` 函数，`append_runtime_checkpoint` 调用前
- **问题**：函数体内使用了 `orders_attempted` 变量但从未定义，
  被 `try/except Exception: pass` 静默吞掉，导致每小时 checkpoint 永远写不进去
- **修复**：在 try 块前加：
  `orders_attempted = int(snap.get("orders_ok", 0)) + int(snap.get("orders_fail", 0) or 0)`

### 2. runner.py：注册 `_lev5_hourly_report_loop` 后台任务（P1 Bug修复）
- **位置**：后台任务列表 `bg.append(...)` 区域（ACCOUNT_SNAPSHOT 之后）
- **问题**：`_lev5_hourly_report_loop`（每小时聚合 fills/fees/PnL）完整实现但
  **从未被 create_task 调度**，等于死代码，运行期间完全没有每小时性能报告
- **修复**：在 bg 列表末尾注册 `_lev5_hourly_report_loop(lev5_runtime, metrics, ...)`

### 3. grid_pro.py：每小时输出 Regime 分类统计（P3 验证体系）
- **位置**：`_log_status` 方法 + 新增 `_REGIME_STATS_INTERVAL = 3600.0`
- **问题**：`RegimeDetector.stats_summary()` 已实现但从无入口输出，
  无法验证 Regime 分类是否有效区分市场状态
- **修复**：每小时在 `_log_status` 中调用 `stats_summary()` 并 `log.warning` 输出，
  格式：`RANGING:trades=12 wins=9 pnl=0.450U | TRENDING_UP:trades=5 wins=4 pnl=0.230U`

---

## 历史完成

### 第七轮（2026-04-19）
- [x] strategy/grid_pro.py：持仓同步自动修复（多仓/幽灵仓双向处理）（P2）
- [x] strategy/grid_pro.py：TP追踪更积极（触发0.5→0.4格，新TP 0.3→0.25格）（P3）

### 第六轮（2026-04-18）
- [x] 重启后持仓无TP自动恢复（P1 Bug修复）
- [x] macro_bearish阈值与regime对齐(-0.0015→-0.0020)（P2）
- [x] FGI>60 + TRENDING_UP 多1档（P3 顺势加仓）

### 第五轮（2026-04-18）
- [x] TRENDING_UP 格宽放大 ×1.3（P3 趋势激进策略）
- [x] 宽限期 45s → 60s（P2 减少误割）
- [x] TP 超时时长 Regime 感知（TRENDING_UP 延长至 10min）

### 第四轮（2026-04-18）
- [x] exchange/__init__.py：WS 静默挂死修复（asyncio.wait_for recv + stall_count）
- [x] strategy/grid_pro.py：短窗口急跌过滤 _SHORT_VELOCITY_ALARM_PCT=-0.0025

### 第三轮（2026-04-18）
- [x] grid_pro.py：TRENDING_DOWN 持仓宽限期 45s→60s（P2）
- [x] grid_pro.py：FGI 恐贪指数集成，FGI<25 减1档（P2）

### 第二轮（2026-04-18）
- [x] grid_pro.py：负资金费率减少激活档位
- [x] grid_pro.py：TP超时止损加速（10m→8m）
- [x] grid_pro.py：宏观偏空阈值 _MACRO_DOWN_STOP = -0.0020

### 第一轮（2026-04-18）
- [x] P0: GRID_DAILY_TARGET_USDT = 999.0（原1.5U上限是致命缺陷）
- [x] P0: GRID_DRAWDOWN_FROM_PEAK_USDT = 3.0（原1.0U太敏感）
- [x] P0: run_strategy.py lock_path 动态路径
- [x] P1: runner.py BOT_MAX_SESSION_HOURS 默认 24h
- [x] P1: grid_pro.py 构造函数默认值与settings.py一致
- [x] P1: analysis.jsonl 新增 fill_entry / fill_tp 事件

---

## 已知问题清单（按优先级）

### 待处理
- [ ] P1: 服务器.env 需确认追加（Agent无法SSH，依赖watchdog+push触发）
- [ ] P2: WS synthetic-tick 保底 — 冷静期结束后若 WS stream 静默（网络抖动），
  `on_tick` 不会被调用，网格无法自动恢复。需在 WS 路径加 synthetic tick 机制。
- [ ] P2: 日志驱动调参 — 需收集 analysis.jsonl 生产数据后评估：
  - `_SHORT_VELOCITY_ALARM_PCT = -0.0025` 是否误拦正常回调
  - TRENDING_DOWN 触发次数/天（若>5次考虑放宽阈值）
  - TP trailing 触发次数/天（若>10次考虑回调至0.5格触发）
- [ ] P3: stall_count 告警频率评估（需生产日志）

---

## 下次优先做

1. **P2: WS synthetic-tick 保底** — 在 runner.py WS 路径中，
   检测 `time.time() - runtime.get("ws_ts", 0) > N秒` 时人工触发一次 `_dispatch_tick`
   （用 `runtime.get("ws_last")` 最后已知价格），确保冷静期后可自动重激活网格。
   建议 N=30s（WS 正常200ms一条，30s静默基本可确认异常）。
2. **P1: 验证 lev5_hourly_report 正常运行** — 本轮修复后，如有API Key，
   首次运行1小时后应在日志见到 `[报告][lev5][1h]` 行
3. **P3: 根据 Regime stats 调参** — 观察 `[grid·regime·stats]` 日志：
   若 RANGING 胜率 < 60% 说明格宽偏窄；若 TRENDING_DOWN pnl 大幅为负说明宽限期仍太长

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 本轮修复两处"已写但从未运行"的死代码bug（orders_attempted + hourly task）
  - Regime stats 每小时输出，可用于验证分类效果
  - 每小时PnL报告将首次真正运行
- **主要风险点**：
  1. 仍无生产日志，所有阈值参数未经实盘验证
  2. WS 静默死锁风险尚未修复（P2 遗留，下次优先处理）
  3. TP trailing 触发阈值 0.4 在震荡市中可能频繁重挂（增加手续费）
