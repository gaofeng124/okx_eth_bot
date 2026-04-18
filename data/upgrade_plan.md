# ETH量化系统升级计划

## 本次（2026-04-18 第三轮）完成

### 1. grid_pro.py：TRENDING_DOWN 持仓宽限期（P2 核心改进）
- **位置**：`on_tick` 步骤2，`_bearish_regime_since` 新增状态变量
- **改动**：
  - 旧逻辑：一旦 Regime=TRENDING_DOWN/VOLATILE 且有持仓 → 立即 emergency_close
  - 新逻辑：立即撤销挂单（无损）；持仓给 **45s 宽限期**，让 TP 自然成交或价格恢复
  - 触发平仓条件：宽限期 > 45s **或** 浮亏 > 1U
  - 安全 Regime 恢复时（else）立即重置计时器
- **原因**：Regime 最小持有 20s（`_MIN_HOLD_SEC=20`），但原来可立即进入 TRENDING_DOWN。
  ETH 价格经常出现短暂 0.3% 下跌后快速恢复的假信号，旧代码每次都割肉，
  导致反复亏损后冷静期叠加，严重影响交易频率和日收益。

### 2. grid_pro.py：FGI 恐贪指数集成（P2）
- **位置**：新增 `_refresh_fgi` 方法 + `__init__` 增加 `_fear_greed_index`/`_last_fgi_ts`
- **改动**：
  - 每小时调用 `alternative.me/fng` API，失败时保留缓存值（默认50中性）
  - `_place_grid` 中：FGI < 25（极度恐慌）时激活档位额外减1
  - 在 `on_tick` 资金费率刷新后调用 `_refresh_fgi(now)`
- **原因**：市场极度恐慌时（FGI<25）波动大、假突破多，减少持仓档位降低风险

---

## 历史完成

### 第二轮（2026-04-18）
- [x] grid_pro.py：负资金费率减少激活档位
- [x] grid_pro.py：TP超时止损加速（480s + 浮亏>0.5U触发）
- [x] grid_pro.py：宏观偏空阈值收紧（-0.0015）

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
- [ ] P2: TP 超时止损冷静期是否足够（当前 300s，可能需要加长）
- [ ] P2: `_MACRO_DOWN_STOP = -0.0020` 是否导致太频繁的 TRENDING_DOWN（宽限期后观察）
- [ ] P3: 动量过滤：最近4 tick 价格斜率快速下跌时跳过开格
- [ ] P3: 趋势跟踪：上升趋势中激进格宽

---

## 下次优先做

1. **观察宽限期效果**：检查日志中 "宽限期开始" 和 "宽限到期" 频率，确认阈值合理
2. **P2: `_MACRO_DOWN_STOP` 阈值微调**：若宽限期触发太频繁（>5次/天），考虑放宽至 -0.0025
3. **P3: 动量过滤**：检测最近4个tick价格斜率，快速下跌（>0.3%/4s）时跳过开格
4. **P1: 确认服务器.env 变量**：BOT_MAX_SESSION_HOURS、GRID_DAILY_TARGET_USDT 等环境变量

---

## 系统当前状态评估
- **策略有效性**：7.5/10——P0/P1修复+本轮TRENDING_DOWN宽限期，大幅减少割肉亏损
- **主要风险点**：
  1. TRENDING_DOWN 宽限期 45s 内浮亏可能超过整体止损（5U），但宽限触发时通常≤1U
  2. FGI 极度恐慌（<25）时减档后仍可能因 TP 超时止损触发平仓
  3. 宏观偏空阈值 -0.0015 相对激进，初期观察误触发频率
