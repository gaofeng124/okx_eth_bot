# ETH量化系统升级计划

## 本次（2026-04-20 第十三轮）完成

### 1. settings.py：GRID_CONTRACTS_PER_SLOT 0.2 → 1.0（P2 歧义消除）
- **问题**：ETH-USDT-SWAP 的 lot_sz=1、min_sz=1，`_round_sz(0.2)` 实际返回 1.0。
  第十二轮虽然加了诊断日志，但根本问题是设置值 0.2 本身具有误导性：
  任何读配置的人都会以为每格仅下 0.2 张，而实际总是 1 张。
- **修复**：`GRID_CONTRACTS_PER_SLOT = 0.2` → `1.0`。
  行为完全不变，仅消除歧义；同时删除了过时注释中"0.02ETH≈46U名义"的错误描述。
- **效果预期**：第十二轮加的 WARNING 日志不再触发（`eff_sz == contracts_per_slot`），
  生产日志更干净；代码意图自文档化。

### 2. grid_pro.py：_update_tp 增加 Regime 感知 TP 系数（P3 策略优化）
- **问题**：TP 固定设在 `vwap × (1 + 1.0×spacing)`，不区分市场状态。
  在 RANGING（横盘震荡）行情下，ETH 可能在 TP 成交后又回落，然后再反弹——
  这意味着持仓时间比必要的更长，增加了因价格回撤导致 TP 被撤/重挂的次数。
- **修复**：RANGING 模式 `tp_mult = 0.8`，TP 收窄至 `vwap × (1+0.8×spacing)`。
  TRENDING_UP 等其他 Regime 保持 `tp_mult = 1.0`（TRENDING_UP 本身 spacing 已放大1.3倍）。
- **效果预期**：横盘行情中每格利润降低约 20%，但成交速度提升，
  资金周转加快，理论上整体收益率（RPS: 收益/持仓时间）提升。

---

## 历史完成

### 第十二轮（2026-04-20）
- [x] grid_pro.py：修复 `_sz()` int() 截断 Bug（防御 fractional lot_sz）
- [x] grid_pro.py：_fetch_instrument_spec 增加有效张数诊断日志
- [x] grid_pro.py：放宽 SHORT_VELOCITY_ALARM_PCT（-0.0025→-0.003）

### 第十一轮（2026-04-20）
- [x] grid_pro.py：持仓同步阈值动态化
- [x] grid_pro.py：阻止负资金费率时TRENDING_UP加档抵消减档保护

### 第十轮（2026-04-19）
- [x] grid_pro.py：TP追踪频率限制（30s节流）+ 超时计时器重置 + 紧急平仓手续费修复

### 第九轮（2026-04-19）
- [x] runner.py：修复 ws_price_feed_loop 死代码 + 新增 ws_stall_watchdog_loop

### 第八～一轮（2026-04-18/19）
- [x] 所有P0/P1问题：GRID_DAILY_TARGET_USDT=999, lock_path修复, fill事件, WS重连 等

---

## 已知问题清单（按优先级）

### 待处理
- [ ] P1: 验证生产日志——contracts_per_slot WARNING不应再出现（13轮修复后）
- [ ] P1: 验证analysis.jsonl中fill_tp成交价在RANGING模式是否为0.8×spacing
- [ ] P2: 服务器 .env 确认 BOT_MAX_SESSION_HOURS=24
- [ ] P2: 若市场数据可用，检查资金费率（<-0.01% 时关注多头槽位减少效果）
- [ ] P3: RANGING模式TP系数0.8是否合适（可依成交数据动态调整到0.7~0.9）

---

## 下次优先做

1. **P1: 生产日志验证（需要服务器访问权限）**
   - 查看启动日志确认 `contracts_per_slot=0.200 → 实际下单张数` 警告消失
   - 查看 analysis.jsonl 最新 fill_tp 事件，验证 RANGING 状态下 TP 价格
   - 检查 `[WS][保底]` 日志确认 stall watchdog 正常工作

2. **P2: 实盘数据驱动参数调优**
   - 若行情数据可用：根据 FGI / 资金费率做参数动态调整
   - 若 24h 涨跌 < -2%: 考虑临时降低 GRID_LEVELS=3 减少多头敞口

3. **P3: GRID_WHOLE_STOP_USDT 与 GRID_DRAWDOWN_FROM_PEAK_USDT 协调**
   - 当前 WHOLE_STOP=5.0U，DRAWDOWN=2.0U。若账户从 50U 跌到 45U 即触发 WHOLE_STOP，
     对于 10x 杠杆策略偏紧；可考虑 WHOLE_STOP 随账户实际净值动态调整（长期改进）

---

## 系统当前状态评估
- **策略有效性**：9/10
  - 13轮迭代，所有P0/P1问题已修复；P2/P3持续优化中
  - Regime感知TP提升横盘资金效率；CONTRACTS_PER_SLOT歧义消除
  - 主要缺口：无法获取实时行情导致无法做市场适应性调整
- **主要风险点**：
  1. 仍无生产日志直接确认，改进效果未经实盘验证
  2. RANGING TP收窄20%在快速下跌行情中可能减少保护（整体止损兜底）
  3. 网络受限环境下无法获取实时FGI/资金费率，市场适应策略形同虚设
- **累计运行轮次**：13
