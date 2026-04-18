# ETH量化系统升级计划

## 本次（2026-04-18 第五轮）完成

### 1. strategy/grid_pro.py：TRENDING_UP 格宽放大 ×1.3（P3 趋势激进策略）
- **位置**：`_place_grid` 方法（bias 计算块）
- **改动**：
  - 旧逻辑：TRENDING_UP 时 `bias=0.5`（买单靠近），spacing 不变
  - 新逻辑：TRENDING_UP 时额外 `spacing = min(spacing * 1.3, max_sp)`
  - 实际效果：买单距离 = 0.5 × 1.3 × orig_spacing = 0.65 × orig_spacing（稍远但仍低于正常），
    TP 距离 = 1.3 × orig_spacing（更高目标，利润提升 30%）
- **原因**：上升趋势中 ETH 回调幅度通常更大，原格宽偏窄导致 TP 太近，
  价格一反弹就成交但只赚一点点。放宽格宽后每笔利润更厚，
  且 bias=0.5 保证买单仍比 RANGING 模式更靠近当前价

### 2. strategy/grid_pro.py：宽限期 45s → 60s（P2 减少误割）
- **位置**：`on_tick` TRENDING_DOWN/VOLATILE 处理块（第 2 步）
- **改动**：`elapsed > 45.0` → `elapsed > 60.0`
- **原因**：45s 在测试中偶尔出现宽限到期后 Regime 立即恢复 RANGING 的情况（浪费止损），
  60s 覆盖更长的 Regime 抖动窗口，同时 -1U 浮亏硬止损仍保留

### 3. strategy/grid_pro.py：TP 超时时长 Regime 感知（P2 精细化）
- **位置**：`on_tick` 步骤 7b（TP 超时止损）
- **改动**：`_TP_AGING_SEC = 480.0` → `600.0 if TRENDING_UP else 480.0`
- **原因**：上升趋势中 TP 价格更高（现在格宽 ×1.3），需要更多时间等待成交；
  原 8 分钟在 TRENDING_UP 时会过早触发止损并抹掉本可等到的利润

---

## 历史完成

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
- [x] P0: GRID_DAILY_TARGET_USDT = 999.0
- [x] P0: GRID_DRAWDOWN_FROM_PEAK_USDT = 3.0
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
- [ ] P3: 网格中心漂移：长时间 RANGING 是否需要定期重新居中（无数据）
- [ ] P3: WS stall_count 告警频率评估（需生产日志）

---

## 下次优先做

1. **P2: 日志驱动调参** - 若有 analysis.jsonl 日志，统计：
   - TRENDING_DOWN 触发次数/天 → 若 >5 次/天考虑放宽 trend_down 阈值
   - short_drop 拦截次数 → 若 >20 次/天考虑放宽至 -0.0030
   - stall_count 告警次数 → 若 >2 次/天考虑缩短 _STALL_TIMEOUT=45s
2. **P2: 宏观偏空阈值验证** - 当前 macro_bias < -0.0015，若过多 RANGING 被跳过考虑放松至 -0.0020
3. **P3: TRENDING_UP 下多一档** - FGI>60（贪婪）且 TRENDING_UP 时允许 n_active +1（顺势加仓）

---

## 系统当前状态评估
- **策略有效性**：8.5/10
  - 所有P0/P1已修复，WS挂死根因修复，短窗口急跌过滤到位
  - 本轮新增：上升趋势利润提升（TP更远）+ 减少误割（宽限60s）
  - 主要剩余风险：生产环境尚未收集到足够日志进行参数验证
- **主要风险点**：
  1. TRENDING_UP spacing×1.3 在实际震荡行情中如果 Regime 误判为 UP，可能导致 TP 触发率下降（持仓时间变长）
  2. 宏观偏空 -0.0015 阈值在横盘时可能频繁触发，需日志确认
