# 亏损知识库（Loss Ledger）

**用途**：每一笔亏损都必须在此登记"**根因 + 永久性防护**"。
**铁律**：**同一根因不允许造成第二次亏损**。如果出现，视为系统失败，必须升级防护到代码层。

---

## 分类编号（便于 AI 引用）

- **L1** 配置错误（参数与实际不符）
- **L2** 策略逻辑缺陷（代码 Bug）
- **L3** 市场环境误判（Regime 分类错）
- **L4** 仓位过大 / 杠杆过激
- **L5** 反应不及时（止损慢 / 追踪迟）
- **L6** 系统故障（挂单未撤、进程崩溃）
- **L7** 外部冲击（黑天鹅、系统性事件）
- **L8** 数据延迟 / 网络故障
- **L9** 手续费错算 / 价格精度问题
- **L10** 运行态异常未主动检测（daemon 的学习循环缺口）

---

## 已登记亏损事件

### L1-001 | 2026-04-19 10:56 UTC | -8.757 USDT（单笔最大）

**事件**：买入 1 张 @2405.74 → ETH 跌至 2318.17 → 平仓认赔

**根因**：OKX ETH-USDT-SWAP 实际 `ctVal=0.1 ETH`，但代码 `LEV5_CT_VAL_BASE=0.01`（差 10 倍）。GridSlot 默认 contracts=1.0，导致每格真实名义 233 USDT，而非代码以为的 23 USDT。在 42U 账户上 = 5.6× 账户名义敞口，3.6% 的行情下跌吃掉 16% 账户。

**永久性防护**（commit faee388）：
1. 新增 `GRID_CONTRACTS_PER_SLOT` 参数（默认 0.2），替代硬编码 1.0
2. 新增 `GRID_PER_SLOT_STOP_USDT=1.5` 单仓硬止损
3. 启动时打印 `ct_val_init=0.100`，任何 CI/监控可对比
4. .env 强制 `RISK_MAX_NOTIONAL_USDT=100`，双保险

**回归测试**（下次须验证）：
- [ ] 启动日志显示 `ct_val_init=0.100 contracts_per_slot=0.200`
- [ ] 任一 HOLDING 槽位浮亏 > 1.5 USDT 立即触发 `per_slot_stop` 紧急平仓

**状态**：✅ 已防护（faee388）

---

### L4-001 | 2026-04-18 02:02 UTC | -2.009 USDT

**事件**：2 张平仓 → 单笔净亏 2U

**根因**：与 L1-001 同一根因（ctVal 错），只是规模较小未暴雷。属于 L1 家族。

**永久性防护**：同 L1-001。

**状态**：✅ 已防护

---

### L4-002 | 2026-04-18 05:37 UTC | -1.432 USDT

**事件**：同上，ctVal bug 导致的小额系列亏损之一。

**状态**：✅ 已防护

---

### L3-001 | 2026-04-17 ~ 2026-04-19 | 累计 ~-3.5 USDT

**事件**：多次"买涨挨砸"。买入后 ETH 短期下跌，TP 没触发、止损慢。

**根因**：
1. 只做多，ETH 下跌段无法盈利（结构性缺陷）
2. Regime 分类未能及时识别 TRENDING_DOWN，继续开格

**永久性防护**：
- 部分已做：`TRENDING_DOWN` + `VOLATILE` regime 时立即撤挂单，持仓 60s 宽限期
- 未做：**做空能力**（roadmap Phase 2）
- 未做：**更精细的 Regime 信号**（roadmap Phase 1）

**回归测试**：
- [ ] 追踪 24h 内 TRENDING_DOWN regime 判定的准确率
- [ ] 做空能力上线后，下跌段 PnL 应为正

**状态**：🟡 部分防护，roadmap Phase 1-2 解决

---

### L3-002 | 2026-04-20 22:55 & 2026-04-21 11:02 CST | 累计 ~-$1.75（**🔴 L3-001 家族重复！**）

**事件**：连续两笔同类亏损
- 04-20 22:55 CST: sell 0.3@2288.67 净 -$0.87（从 ~$2318 买入 → 跌到 2288 触发 per_slot_stop）
- 04-21 11:02 CST: sell 0.3@2299.60 净 -$0.92（05:10 从 2328.97 买入 → 跌到 2299.6 触发 per_slot_stop）

**根因诊断**（职业复盘）：
- 两笔都是 **ranging regime 下多头入场，但随后 ETH 下跌 1.2-1.5%**
- per_slot_stop=$0.80 **设计正确触发**（防止更大亏损）—— 保护层 OK
- **但入场本身质量差** —— 这才是核心问题
- **本应该防护的 book_imbalance 入场门控实测为 None** —— 因子数据没灌进来！
  - `analysis.jsonl` 每条 snapshot 都是 `book_imb: None`
  - R71 的 L3-001 入场门控 + R94 的 WS books5 升级，**都没真正工作**
  - 因子层死代码，以为防护了实际没有

**永久性防护**（P0 必做）：
1. **紧急**：检查为什么 status_summary.book_imbalance 永远 None
   - 可能：WS books5 订阅了但数据没进 `runtime["order_book"]`
   - 可能：runtime 有数据但 grid_pro 在错误字段读取
   - 可能：计算函数在某处抛 silent exception
2. **立即临时兜底**：在入场前增加"近 30 分钟最高点缓冲"检查
   - 如果 entry_price > recent_30min_high × 0.995 → 拒绝入场
   - （"接近近期高点买入 = 大概率买顶"的简单启发式）
3. **建立"防护失效检测"**：每小时统计 book_imb 的 non-None 比例，< 50% → 🚨 邮件
4. 在 TP aging 触发机制上加"持仓 > 2h 且 UPL < -$0.40 → 主动平，不等 per_slot_stop=0.80"
   - 减小亏损幅度从 $0.80 → $0.40（砍半）

**回归测试**（下次须验证）：
- [ ] book_imb 数据连续 > 95% 有值（不能是 None）
- [ ] entry 被 book_imb gate 拦截的比例 > 0%（说明 gate 真的在运行）
- [ ] 每周统计"接近近期高点买入"事件数

**状态更新**：🟡 部分防护（2026-04-21 12:00 CST 主人推动下）
- ✅ L2 做空已上线（GRID_DIRECTION=short）—— 结构性修复
- ✅ book_imb gate 日志实测在工作（之前以为坏的是记录层而非因子层）
- 🟡 待做：TP aging 加强 / 30min_high 兜底 / analysis.jsonl 记录修复
- 🟡 L3-001 家族"持仓期间下跌"问题：L2 上线后下跌时做空也能赚 → 部分自愈

---

### L10-001 | 2026-04-21 21:30 CST | 无直接亏损但 **机会成本 = 1h47min 资金闲置**

**事件**：Phase 1 部署后 CST 19:43:35 最后一笔成交，此后 1h47min **零成交**，账户保证金利用率仅 7.5%，挂单数量仅 1 档（设计 4 档）。

**根因（多层）**：
1. **`grid_pro.py` line 1144 US session hard cap**：UTC 13-23 时段 `n_active=1`（不管 max_levels）
   - 这是前任 AI daemon 基于 3/3 亏损样本加的保守，**样本太小**
   - 加的时期是**纯多头**，现在 L2 做空结论可能反向
   - 做空方向下 US session 反而常常是有利的（美股下跌带 ETH 下跌）
2. **`spacing 硬下限 32bps`**：市场 30min range 34bps 时，挂单距离 > 波动幅度 → 等不到成交
3. **`vol_regime active_levels` 限档**：CALM/ELEVATED 强制 2 档（应放到 3-max）
4. **缺少运行态主动监控**：daemon prompt 里没有"近 30min 无成交→立即诊断"规则

**永久性防护**（commit 待定）：
1. **代码层**：
   - `active_levels()`: CALM/ELEVATED 从 `min(2,max)` 放到 `min(3,max)`
   - US session cap: `=1` 改为 `=min(2,n_active)`（保留一定保守但不过度）
   - `spacing_pct` 的 min_sp 默认从 0.0032 → 0.0020（让静市能挂近）
2. **监控层** 新增 `scripts/system_health.py`：
   - 每轮计算：近 30/60min 成交数 / 资金利用率 / 挂单数量 / fee-gross 比率
   - daemon 每轮 Step 2 必调用
3. **AI prompt 层**：
   - 新增 **异常信号表**：信号 → 必做诊断（如"近 30min 零成交 → 检查 spacing vs ATR"）
   - 把 L10 列为与亏损同级的"系统失败"告警
4. **Phase 2 前置**：GRID_LEVELS 4→5（更多档，减轻"一档失效全军覆没"）

**回归测试**（下次须验证）：
- [ ] US session 期间挂单数 ≥ 2（不再只 1 档）
- [ ] 当 ATR < 25bps 时，spacing ≤ 25bps（贴合市场不超 ATR）
- [ ] `system_health.py` 返回 `idle_min_count` 字段，daemon 日志证实每轮读了
- [ ] 资金利用率常态 ≥ 25%（不是峰值，是平均）

**状态**：🟡 待修复（commit 进行中）

---

## AI 使用说明

**每次发现新亏损**：
1. 先查历史 ledger：`grep` 根因关键词（如 ctVal、regime、fill_price）
2. 若根因已有条目 → **🚨 红字告警**（原本应该被防护住，说明防护失败或参数回退）
3. 若是新根因 → 开新条目 `Lx-xxx`，写清根因 + 防护 + 回归测试
4. 任何防护措施 commit 后，更新 ledger 条目的 "状态" 为 ✅

**每次 prompt 启动时**：
- `Read data/loss_ledger.md`
- 把**未完全防护（🟡）**的条目作为当前轮的优先任务
