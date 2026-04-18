# ETH量化系统升级计划

## 本次（2026-04-18 本轮）完成

### 1. runner.py：默认会话超时 4h → 24h（P1已修复）
- **文件**：`quant/app/runner.py` 第3253行
- **原因**：默认4小时自动SIGTERM，频繁重启导致网格中心漂移、订单丢失
- **改动**：`"4"` → `"24"`，注释更新说明原因

### 2. grid_pro.py：构造函数默认值与 settings.py 对齐（P0残留）
- **文件**：`quant/strategy/grid_pro.py` 第299-305行
- **改动**：
  - `atr_mult: 0.8 → 1.2`（与settings.py一致）
  - `min_spacing_pct: 0.0012 → 0.0010`
  - `max_spacing_pct: 0.0040 → 0.0050`
  - `whole_stop_usdt: 3.0 → 5.0`
  - `daily_stop_usdt: 5.0 → 6.0`
  - `daily_target_usdt: 2.5 → 999.0`（消除过早停止交易的根本原因）
  - `drawdown_from_peak_usdt: 1.5 → 3.0`
- **原因**：构造函数默认值与settings.py不一致时，若env变量缺失则采用错误的旧值

### 3. grid_pro.py：成交事件写入 analysis.jsonl（P1完成）
- **文件**：`quant/strategy/grid_pro.py`
- **改动**：在入场成交（`_sync_entry`）和TP成交（`_sync_tp`）时各添加一次 `record_analysis` 调用
- **新增事件类型**：`fill_entry`（含 level/fill_price/fill_sz/target_price/vwap/total_held）
  和 `fill_tp`（含 fill_price/fill_sz/net_pnl_usdt/daily_pnl_realized/entry_vwap）
- **原因**：analysis.jsonl 此前只有 grid_status 心跳，无成交记录，无法事后分析盈亏

---

## 已知问题清单（按优先级）

### 已修复 ✅
- [x] P0: GRID_DAILY_TARGET_USDT = 999.0（settings.py）
- [x] P0: GRID_DRAWDOWN_FROM_PEAK_USDT = 3.0（settings.py）
- [x] P0: run_strategy.py lock_path 使用动态 Path
- [x] P1: runner.py BOT_MAX_SESSION_HOURS 默认 24
- [x] P1: grid_pro.py 构造函数默认值与settings.py完全一致
- [x] P1: analysis.jsonl 新增 fill_entry / fill_tp 事件

### 待处理
- [ ] P1: 服务器.env 需要确认追加（Agent无法SSH，依赖watchdog+push触发）
- [ ] P2: FGI<25 极度恐慌时动态减少档位（当前市场API不可达，暂缓）
- [ ] P2: 资金费率为负时减少多头敞口逻辑
- [ ] P2: TP 超时止损阈值从 10min/1倍格宽 → 8min/0.5U 浮亏（策略优化）
- [ ] P3: 动量过滤：价格快速下跌时暂停开格（快速下跌判断逻辑）
- [ ] P3: 趋势跟踪：上升趋势中激进格宽（已有雏形，需调参）

---

## 下次优先做

1. **验证 analysis.jsonl 是否记录到 fill 事件**（读取 data/analysis.jsonl 检查新事件类型）
2. **P2: FGI 动态档位调整**：在 `_place_grid` 中根据市场上下文的 `fear_greed` 字段调整 `n_active`
3. **P2: 资金费率负值减仓**：在 `_refresh_funding` 后，若 `_funding_rate < -0.0003` 则 `_max_levels` 临时减1
4. **P2: TP超时止损优化**：_TP_AGING_SEC 从600改为480，浮亏条件从"跌破VWAP-1格宽"改为"浮亏>0.5U"

---

## 系统当前状态评估
- **策略有效性**：6/10——P0参数问题已全部修复，代码逻辑无语法错误，但服务器实际运行效果未知（市场API本次不可达）
- **主要风险点**：
  1. TRENDING_DOWN/VOLATILE regime 触发紧急平仓可能过于频繁（volatile 判断阈值需结合实际ATR校验）
  2. TP超时止损10分钟可能在低流动性时太短，导致不必要割肉
  3. runner.py 虽改为24h，但服务器.env如有显式设置则代码默认值不生效

