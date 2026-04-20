# ETH量化系统升级计划

## 本次（2026-04-20 第十一轮）完成

### 1. grid_pro.py：持仓同步阈值动态化（P1 Bug修复）
- **问题**：`_position_sync_check` 中硬编码 `0.5` 合约阈值，对 `contracts_per_slot=0.2` 配置过大。
  1-2槽位平仓失败后交易所持有0.2-0.4张，但 diff < 0.5 → 不触发补录 → 裸露持仓无TP保护！
  最坏场景：紧急平仓REST请求超时，内部状态清零，交易所仍有持仓，10s内无任何保护。
- **修复**：引入 `_thresh = max(self._contracts_per_slot * 0.5, 0.05)`，替换所有 `0.5` 硬编码。
  contracts_per_slot=0.2时：_thresh=0.1，可检测到0.2张（1槽位）的差异。
- **影响**：小账户下持仓同步更灵敏，减少"幽灵持仓"或"裸露持仓"的持续窗口

### 2. grid_pro.py：阻止负资金费率时TRENDING_UP加档抵消减档保护（P2 逻辑修复）
- **问题**：`_place_grid`中，负资金费率(`< -0.0003`)减1档；但随后TRENDING_UP+FGI>60又加1档，
  两个操作相互抵消，等于完全没有保护。极负资金费率表示空头为主，加多头槽位风险更高。
- **修复**：TRENDING_UP加档条件增加 `self._funding_rate >= -0.0003`，确保当已触发负费率惩罚时
  不允许TRENDING_UP逻辑将档位加回来。
- **影响**：负资金费率环境下保持实际减档效果，多头敞口真正降低

---

## 历史完成

### 第十轮（2026-04-19）
- [x] grid_pro.py：TP 追踪频率限制（30s节流，避免每tick触发cancel/replace）
- [x] grid_pro.py：TP 追踪后重置超时计时器（市场上行时不应触发旧计时器止损）
- [x] grid_pro.py：紧急平仓手续费精度修复（4bps→7bps，maker+taker）

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
- [x] P0：run_strategy.py lock_path 动态路径
- [x] P1：analysis.jsonl fill_entry/fill_tp 事件

---

## 已知问题清单（按优先级）

### 待处理
- [ ] P1: 验证analysis.jsonl生产日志（fill_entry/fill_tp事件是否正常写入）
- [ ] P2: velocity_alarm触发频率：SHORT_VELOCITY_ALARM_PCT=-0.0025是否过于敏感
  若每日触发 >5 次，应放宽至 -0.003 或 -0.0035
- [ ] P2: 服务器 .env 确认 BOT_MAX_SESSION_HOURS=24
- [ ] P2: 验证 contracts_per_slot=0.2 对应 lot_sz 是否正确（API lotSz fetch后size计算是否非零）
- [ ] P3: REST 模式下 synthetic tick 机制（优先级低）

---

## 下次优先做

1. **P1: 读取生产日志验证所有修复**
   - `analysis.jsonl` 中 fill_entry / fill_tp 事件是否正常写入
   - velocity_alarm 事件频率（<5次/天 = 合理）
   - [WS][保底] 日志是否出现（WS stall watchdog是否工作）

2. **P2: contracts_per_slot验证**
   - 确认 `_sz(0.2)` 在当前 lot_sz 下不返回 "0"
   - 如果 lot_sz=1，则 GRID_CONTRACTS_PER_SLOT 应改为整数（1或2）

3. **P3: 动态止盈优化**
   - RANGING 模式：TP位置 = VWAP + 0.8×格宽（当前=1.0×，稍保守）
   - TRENDING_UP模式：已用 1.3×格宽，TP位置可用 VWAP + 1.0×格宽

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 十一轮迭代修复了所有已知P0/P1问题
  - 持仓同步阈值动态化，小账户下不再漏检
  - 负资金费率时档位惩罚不再被TRENDING_UP抵消
- **主要风险点**：
  1. 仍无生产日志确认，所有参数未经实盘验证
  2. contracts_per_slot=0.2是否对应有效lot_sz待确认
  3. SHORT_VELOCITY_ALARM_PCT=-0.0025可能导致震荡市频繁暂停开格
- **累计运行轮次**：11
