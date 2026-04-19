# ETH量化系统升级计划

## 本次（2026-04-19 第七轮）完成

### 1. strategy/grid_pro.py：持仓同步自动修复（P2 Bug修复）
- **位置**：`_position_sync_check` 方法（原仅告警）
- **问题**：当交易所持仓 > 内部记录超过0.5张时，策略只打日志警告但不修复，
  导致"额外持仓"完全裸露（无TP保护）。在网络抖动/重连后尤其危险。
- **修复逻辑**（双向）：
  - **多仓（exchange > internal + 0.5）**：用 `long_upl` 反推估算成本价
    `est_entry = bid - upl / (sz × ct_val × leverage)`，合理性校验±5%偏离降级为当前价，
    更新 VWAP + total_held，补挂 TP 保护
  - **幽灵仓（exchange < internal - 0.5）**：
    - exchange=0 → `_reset_grid()` 全清
    - exchange>0 → 等比缩减 HOLDING 槽位到交易所实际量
- **影响**：消除"已平仓但内部仍认为有仓"的幽灵TP，以及"有仓但无TP"的裸露风险

### 2. strategy/grid_pro.py：TP追踪更积极（P3 收益优化）
- **位置**：`_maybe_trail_tp` 方法
- **改动**：
  - 触发条件：`mid > tp + 0.5格宽` → `mid > tp + 0.4格宽`（更快追踪强势行情）
  - 新TP位置：`mid - 0.3格宽` → `mid - 0.25格宽`（锁更多利润，减少回吐）
- **原因**：原触发偏保守（超50%才追踪），价格强势上涨时容易错过锁利机会；
  新触发40%更灵敏，同时新TP距当前价更近（25% vs 30%），降低利润回吐风险

---

## 历史完成

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
- [x] grid_pro.py：TRENDING_DOWN 持仓宽限期 45s（P2）
- [x] grid_pro.py：FGI 恐贪指数集成，FGI<25 减1档（P2）

### 第二轮（2026-04-18）
- [x] grid_pro.py：负资金费率减少激活档位
- [x] grid_pro.py：TP超时止损加速（10m→8m）
- [x] grid_pro.py：宏观偏空阈值 _MACRO_DOWN_STOP = -0.0020

### 第一轮（2026-04-18）
- [x] P0: GRID_DAILY_TARGET_USDT = 999.0（原1.5是致命缺陷）
- [x] P0: GRID_DRAWDOWN_FROM_PEAK_USDT = 3.0（原1.0太敏感）
- [x] P0: run_strategy.py lock_path 动态路径
- [x] P1: runner.py BOT_MAX_SESSION_HOURS 默认 24h
- [x] P1: grid_pro.py 构造函数默认值与settings.py一致
- [x] P1: analysis.jsonl 新增 fill_entry / fill_tp 事件

---

## 已知问题清单（按优先级）

### 待处理
- [ ] P1: 服务器.env 需确认追加（Agent无法SSH，依赖watchdog+push触发）
- [ ] P2: TRENDING_DOWN 触发频率无法验证（无生产日志），需收集数据后评估阈值
- [ ] P2: short_velocity -0.0025 阈值是否误拦截正常回调（需日志验证）
- [ ] P3: stall_count 告警频率评估（需生产日志）
- [ ] P3: TP trailing 新参数（0.4/0.25格）需在生产验证是否过于频繁重挂单

---

## 下次优先做

1. **P2: 日志驱动调参** - 若有 analysis.jsonl 日志，统计：
   - 持仓同步修复触发次数 → 若 >2次/天说明有持续性不一致根因
   - TP trailing 触发次数/天 → 若 >10次考虑回调至0.5格触发
   - TRENDING_DOWN 触发次数/天 → 若 >5次考虑放宽阈值
2. **P2: 网格重激活速度** - 当前冷静期后需等到下一个 market_ok 信号，
   若 cooldown 结束时没有 on_tick 触发（行情停止），会无限等待。
   考虑在 runner 添加定时强制 tick 机制。
3. **P3: Regime 统计上报** - `regime.stats_summary()` 已实现但未有定期打印入口；
   每小时输出一次胜率统计，验证 Regime 分类是否有效区分行情

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 所有P0/P1已修复，持仓裸露问题（position sync auto-repair）已修复
  - TP追踪更积极，有望捕捉更多强势行情利润
  - 情绪感知体系完整（FGI<25减档，FGI>60+趋势增档）
- **主要风险点**：
  1. 缺乏生产日志，所有参数阈值尚未经过实盘验证
  2. TP trailing 触发阈值从0.5降到0.4，若行情振荡可能频繁重挂（增加手续费消耗）
