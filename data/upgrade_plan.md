# ETH量化系统升级计划

## 本次（2026-04-18 第二轮）完成

### 1. grid_pro.py：负资金费率减少激活档位（P2）
- **位置**：`_place_grid` 方法，`n_active` 计算后
- **改动**：若 `self._funding_rate < -0.0003` 且 `n_active > 1`，则 `n_active -= 1`
- **原因**：负资金费率意味着多头溢价消失、空头情绪占主导，此时减少多头暴露降低风险

### 2. grid_pro.py：TP超时止损更快、条件更丰富（P2）
- **位置**：主循环步骤7b
- **改动**：
  - `_TP_AGING_SEC: 600.0 → 480.0`（8分钟，原10分钟）
  - 新增 `_tp_loss_breach = unrealized < -0.5`：浮亏超0.5U也触发止损
  - 触发条件：超时 AND（价格跌破VWAP-格宽 OR 浮亏>0.5U）
- **原因**：防止持仓长时间亏损无法止损；0.5U浮亏触发比等价格穿透更快响应

### 3. grid_pro.py：宏观偏空阈值收紧（P2）
- **位置**：主循环步骤10b
- **改动**：`macro_bearish = macro_bias < -0.0020` → `< -0.0015`（0.20% → 0.15%）
- **原因**：ETH中等波动时0.20%偏空阈值太宽松，会在下跌中继续开格

---

## 历史完成（上轮 2026-04-18 第一轮）

- [x] P1: runner.py BOT_MAX_SESSION_HOURS 默认 24h（修复4h崩溃）
- [x] P1: grid_pro.py 构造函数默认值与settings.py完全一致
- [x] P1: analysis.jsonl 新增 fill_entry / fill_tp 事件记录
- [x] P0: GRID_DAILY_TARGET_USDT = 999.0（不限制每日收益上限）
- [x] P0: GRID_DRAWDOWN_FROM_PEAK_USDT = 3.0（峰值回撤阈值放宽）
- [x] P0: run_strategy.py lock_path 使用动态路径

---

## 已知问题清单（按优先级）

### 待处理
- [ ] P1: 服务器.env 需确认追加（Agent无法SSH，依赖watchdog+push触发）
- [ ] P2: FGI<25 极度恐慌时动态减少档位（market API本次不可达，暂缓）
- [ ] P2: TP 超时止损后的冷静期是否足够（当前 300s，可能需要加长）
- [ ] P3: 动量过滤：价格快速下跌时暂停开格
- [ ] P3: 趋势跟踪：上升趋势中激进格宽

---

## 下次优先做

1. **验证 analysis.jsonl fill 事件**：读取 data/analysis.jsonl 检查是否有 fill_entry/fill_tp
2. **P2: FGI 动态档位**：在 `_place_grid` 中读取 runtime 的 fear_greed 字段，FGI<25 时再减1档
3. **P2: TRENDING_DOWN 持仓时是否过激平仓**：检查 Regime 判断阈值，避免微小下跌误触发紧急平仓
4. **P3: 动量过滤**：检测最近4个tick的价格斜率，快速下跌（>0.3%/4s）时跳过开格

---

## 系统当前状态评估
- **策略有效性**：7/10——P0/P1问题全修复，P2优化持续迭代，实盘效果待观测
- **主要风险点**：
  1. 宏观偏空阈值 -0.0015 在震荡市可能误触（偏激进），首周观察触发频率
  2. TP超时止损 480s 在低流动性深夜可能切了正常持仓，初期监控 tp_timeout_stoploss 频率
  3. 服务器.env变量若未更新，settings.py改动不生效（最高优先级确认项）
