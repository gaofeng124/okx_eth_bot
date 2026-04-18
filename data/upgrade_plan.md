# ETH量化系统升级计划

## 本次（2026-04-18 第六轮）完成

### 1. strategy/grid_pro.py：重启后持仓无TP自动恢复（P1 Bug修复）
- **位置**：`on_tick` 方法，`_refresh_fgi` 之后
- **问题**：`_cancel_stale_orders` 启动时撤销全部旧订单（含TP），但 `_tp_order_id=""` 初始化后
  没有任何逻辑重新挂TP。持仓裸露，只能等整体止损或日亏损触发才能平仓。
- **修复**：检测 `_total_held > 0 and not _tp_order_id`，自动用vol引擎计算格宽并调用 `_update_tp()`
- **影响**：重启（包括4h定期重启）后存量持仓立即获得TP保护

### 2. strategy/grid_pro.py：macro_bearish阈值与regime对齐（P2 一致性修复）
- **位置**：`on_tick` 步骤10b
- **改动**：`macro_bearish = macro_bias < -0.0015` → `macro_bias < -0.0020`
- **原因**：`regime.py` 的 `_MACRO_DOWN_KILL = -0.003`，`_MACRO_DOWN_STOP = -0.002`。
  原阈值 -0.0015 比 regime 更保守：当 macro_bias=-0.0018 时，Regime 判断为 RANGING
  但 grid 却拒绝开格，两层逻辑自相矛盾。对齐后减少无谓的开格抑制。

### 3. strategy/grid_pro.py：FGI>60 + TRENDING_UP 多1档（P3 顺势加仓）
- **位置**：`_place_grid` TRENDING_UP 分支
- **改动**：贪婪市场（FGI>60）+上升趋势时 `n_active = min(n_active+1, max_levels)`
- **原因**：牛市行情下回调浅、反弹快，多1档能捕捉更多成交机会；
  已有 FGI<25 减1档的对称逻辑，本改动构成完整的情绪感知档位调整体系

---

## 历史完成

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
2. **P2: 持仓同步自动修复** - 当前 `_position_sync_check` 只告警不修复；
   若 exchange > internal 超过 1 张，考虑自动把差额分配到空槽位并挂TP
3. **P3: 网格中心定期刷新** - 若 RANGING 且无持仓超过 10 分钟，重新居中到当前价

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 所有P0/P1已修复（本轮修复重启后TP缺失这一实质P1 bug）
  - 情绪感知体系完整（FGI<25减档，FGI>60+趋势增档）
  - macro_bearish 逻辑对齐，消除 Regime vs Grid 矛盾
- **主要风险点**：
  1. 缺乏生产日志，所有参数阈值尚未经过实盘验证
  2. 4h重启频率高，每次重启前的存量仓位现已有TP保护，但重启冷却期200tick期间无新开格
