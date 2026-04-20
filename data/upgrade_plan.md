# ETH量化系统升级计划

## 本次（2026-04-20 第十二轮）完成

### 1. grid_pro.py：修复 `_sz()` int() 截断 Bug（P1 防御性修复）
- **问题**：`_sz(contracts)` 直接用 `str(int(self._round_sz(contracts)))` 格式化。
  若 `lot_sz < 1.0`（如某些合约 lotSz=0.01），`_round_sz(0.2) = 0.2`，
  `int(0.2) = 0`，`str(0) = "0"`，发给交易所 sz="0" → 订单立即被拒绝。
- **修复**：当 `lot_sz >= 1.0` 时保持 `int()` 格式（向后兼容 ETH-USDT-SWAP）；
  当 `lot_sz < 1.0` 时按 lot_sz 精度格式化（避免截断到0）。
- **影响**：ETH-USDT-SWAP（lot_sz=1.0）行为不变；对其他分数lot_sz合约可安全使用

### 2. grid_pro.py：_fetch_instrument_spec 增加有效张数诊断日志（P1）
- **问题**：`GRID_CONTRACTS_PER_SLOT=0.2` 在 ETH-USDT-SWAP（lot_sz=1, min_sz=1）下
  实际下单 1 张，但日志中从未明确显示此差异。生产环境无法确认实际成交规模。
- **修复**：fetch 仪器规格后比较 `contracts_per_slot` vs `_round_sz(contracts_per_slot)`，
  若不同则打印 WARNING：`contracts_per_slot=0.200 → 实际下单张数=1（受lotSz=1 minSz=1约束）`
- **影响**：生产日志首次启动时明确显示实际下单规模，便于验证

### 3. grid_pro.py：放宽 SHORT_VELOCITY_ALARM_PCT（P2）
- **问题**：`_SHORT_VELOCITY_ALARM_PCT = -0.0025`（4-tick窗口，-0.25%/4tick）。
  ETH 在正常震荡市中，4个tick内-0.25%（$1600下=$4）的微小波动可能频繁触发，
  导致 `short_drop` 事件阻止开格，降低网格套利频率和盈利机会。
- **修复**：-0.0025 → -0.0030（-0.30%/4tick）。
  20-tick主窗口 `_VELOCITY_ALARM_PCT=-0.0020` 已提供飞刀保护，短窗口作辅助。
- **影响**：减少假触发次数，提升正常震荡行情的开格成功率

---

## 历史完成

### 第十一轮（2026-04-20）
- [x] grid_pro.py：持仓同步阈值动态化（硬编码0.5→max(slot*0.5,0.05)）
- [x] grid_pro.py：阻止负资金费率时TRENDING_UP加档抵消减档保护

### 第十轮（2026-04-19）
- [x] grid_pro.py：TP 追踪频率限制（30s节流）
- [x] grid_pro.py：TP 追踪后重置超时计时器
- [x] grid_pro.py：紧急平仓手续费精度修复（4bps→7bps）

### 第九轮（2026-04-19）
- [x] runner.py：修复 `_ws_price_feed_loop` 死代码
- [x] runner.py：新增 `_ws_stall_watchdog_loop`

### 第八轮（2026-04-19）
- [x] runner.py：修复 `orders_attempted` NameError
- [x] runner.py：注册 `_lev5_hourly_report_loop` 后台任务
- [x] grid_pro.py：每小时输出 Regime 分类统计

### 第七轮（2026-04-19）
- [x] strategy/grid_pro.py：持仓同步自动修复
- [x] strategy/grid_pro.py：TP 追踪更积极

### 第六轮（2026-04-18）
- [x] 重启后持仓无TP自动恢复
- [x] macro_bearish 阈值与 regime 对齐
- [x] FGI>60 + TRENDING_UP 多1档

### 第五轮（2026-04-18）
- [x] TRENDING_UP 格宽放大 ×1.3
- [x] 宽限期 45s → 60s
- [x] TP 超时时长 Regime 感知

### 第四轮（2026-04-18）
- [x] WS 静默挂死修复
- [x] 短窗口急跌过滤 -0.0025（本轮进一步放宽至-0.003）

### 第三轮（2026-04-18）
- [x] TRENDING_DOWN 持仓宽限期加长
- [x] FGI 恐贪集成，FGI<25 减1档

### 第二轮（2026-04-18）
- [x] 负资金费率减少激活档位
- [x] TP 超时加速

### 第一轮（2026-04-18）
- [x] P0：GRID_DAILY_TARGET_USDT=999
- [x] P0：run_strategy.py lock_path 动态路径
- [x] P1：analysis.jsonl fill_entry/fill_tp 事件

---

## 已知问题清单（按优先级）

### 待处理
- [ ] P1: 验证生产日志中 contracts_per_slot 诊断WARNING是否出现
  → 若看到 "实际下单张数=1"，则确认每槽位1张(0.01ETH)是正确规模
- [ ] P1: 验证analysis.jsonl生产日志（fill_entry/fill_tp事件是否正常写入）
- [ ] P2: 验证 short_drop 触发频率（放宽后应减少，目标<5次/天）
- [ ] P2: 服务器 .env 确认 BOT_MAX_SESSION_HOURS=24
- [ ] P2: 是否需要将 GRID_CONTRACTS_PER_SLOT 从 0.2 改为 1（明确意图）
- [ ] P3: RANGING 模式 TP 位置优化（0.8×spacing vs 当前1.0×spacing）

---

## 下次优先做

1. **P1: 生产日志分析**
   - 检查启动日志中 `contracts_per_slot 警告` 是否打印
   - 检查 `analysis.jsonl` 中 fill_entry / fill_tp 事件
   - 统计 `short_drop` 触发次数（本轮放宽后应减少）
   - 检查 `[WS][保底]` 日志（WS stall watchdog工作确认）

2. **P2: GRID_CONTRACTS_PER_SLOT 设置澄清**
   - 若生产日志确认实际=1张(0.01ETH), 考虑将设置改为 `GRID_CONTRACTS_PER_SLOT = 1`
   - 这样消除 0.2 的误导性，与实际行为对齐

3. **P3: TP优化（视P1日志结果决定）**
   - RANGING: tp = vwap × (1 + 0.8×spacing)（快速小利润）
   - TRENDING_UP: 保持 1.0×spacing（当前grid_spacing已×1.3，TP实际更远）

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 12轮迭代修复了所有已知P0/P1问题
  - _sz()现在防御fractional lot_sz的截断bug
  - 短窗口速度过滤放宽，提升震荡市开格机会
  - 诊断日志增强，生产验证更容易
- **主要风险点**：
  1. 仍无生产日志确认，所有修复未经实盘验证
  2. GRID_CONTRACTS_PER_SLOT=0.2设置具有误导性（实际=1张）
  3. SHORT_VELOCITY放宽后在剧烈单向行情中保护略弱（主窗口仍有效）
- **累计运行轮次**：12
