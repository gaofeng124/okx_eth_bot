# OKX ETH-USDT-SWAP 量化交易系统 —— 白皮书

**草稿起始**：2026-04-22 15:20 CST
**交付目标**：2026-04-23 上午
**读者**：项目出资人 / 负责人
**作者**：AI 首席量化决策者

---

## 目录
1. [量化因子：多还是少？](#1-量化因子多还是少)
2. [信号哪个重要？（IC 排名）](#2-信号哪个重要)
3. [如何判断下单？（决策路径）](#3-如何判断下单)
4. [买多还是买空？（方向判断）](#4-买多还是买空)
5. [OKX 策略（用了哪些、还能用什么）](#5-okx-策略)
6. [手续费如何计算？](#6-手续费计算)
7. [交易策略：多还是少？](#7-交易策略多还是少)
8. [当前系统问题 TOP 10](#8-当前系统问题-top-10)
9. [升级路线图（按 ROI 排序）](#9-升级路线图)
10. [结论与建议](#10-结论)

---

## 1. 量化因子：多还是少？

### 学术共识

- 经典 Fama-French 3 因子 → 5 因子
- 机构多因子模型通常 **5-10 个核心因子**
- **超过 15 个** 基本全是噪声 / 重复
- 关键是 **IC > 0.05 且因子间低相关**

### 我们现状（待 IC 数据填充）

| 因子 | 来源 | 权重 | 当前 IC | 相关性 | 建议 |
|---|---|---|---|---|---|
| Book Imbalance 即时 | 盘口 | 25% | **待采样** | 低 | 核心保留 |
| Taker Aggressor 10s | trades | 25% | **待采样** | 低 | 核心保留 |
| CVD 5min | trades 累计 | 15% | **待采样** | 与 Taker 高 | 待验证 |
| EMA fast/slow | tick | 15% | **待采样** | 与 Macro 高 | 可能冗余 |
| Macro bias | 5min EMA | 10% | **待采样** | 与 EMA 高 | **可能可删** |
| Funding rate | 8h | 10% | 慢信号 | 独立 | 保留 |
| Price Position | 1h 高低 | 20% | **今早实证有效** | 独立 | 核心保留 |
| Orderbook wall | 深度 20 | 10% | **待采样** | 与 book_imb 高 | 待验证 |

**判断**：**7 维偏多**。IC 数据出来后预计保留 **5 维**。

---

## 2. 信号哪个重要？（IC 排名）

**待 signal_attribution 24h 数据填充**。明天上午给数据表。

历史实盘观察（经验判断）：
- **Price Position**（今早实证）：防追顶，最保护
- **Taker Aggressor 10s**：实时市场力量
- **Book Imbalance 即时**：毫秒级反应

### 🔴 Time-of-Day Bias（最强烈的已验证 alpha！）

**100 笔实测**（2026-04-22 15:54）：

| UTC 小时 | CST | 笔数 | net PnL | 评级 |
|---|---|---|---|---|
| 00 | 08 | 2 | +0.079 | ✅ |
| 01 | 09 | 4 | +0.167 | ✅ |
| 02 | 10 | 4 | +0.281 | ✅ |
| 04 | 12 | 5 | +0.108 | ✅ |
| 06 | 14 | 3 | +0.001 | ✅ |
| 11 | 19 | 3 | +0.573 | ✅ |
| 13 | 21 | 3 | +0.488 | ✅ |
| 15 | 23 | 6 | +0.322 | ✅ |
| 18 | 02 | 9 | +1.024 | ✅ |
| 20 | 04 | 3 | +0.219 | ✅ |
| **16** | **00** | **3** | **-2.104** | **🔴 最差** |
| **17** | **01** | **7** | **-0.783** | **🔴** |
| **19** | **03** | **9** | **-1.286** | **🔴** |
| 14 | 22 | 6 | -0.251 | ❌ |
| 07 | 15 | 8 | -0.829 | ❌ |
| 03 | 11 | 2 | -0.930 | ❌ |
| 05 | 13 | 18 | -0.449 | ❌ |

### 🎓 专业诊断（2026-04-22 17:00 主人纠正后深度分析）

**样本 19 笔 vs 其他 81 笔 对比**：

| 指标 | CST 00-03 | 其他时段 | 关键点 |
|---|---|---|---|
| 胜率 | 67% | 81% | 小降 14% |
| avg_win | +$0.21 | +$0.21 | **几乎一样** |
| **avg_loss** | **-$1.60** | -$0.53 | **3× 放大！** |
| avg_per_fill | -$0.22 | -$0.001 | |
| t 检验值 | -1.39 | | 边际显著 |

**真正的诊断（不是"关闭"）**：
不是"时段坏"，是"**亏损幅度失控**"。
- avg_win 未变 → grid 盈利能力在此时段正常
- avg_loss 3× 放大 → 单笔亏损管理失效

**根因假设**（需 200+ 笔数据验证）：
UTC 16-19 = 美国早中盘，波动 + 单边性强。
grid 在单边行情连续套仓 → per_slot_stop 多次触发 → 大额亏损堆积。

### 💡 升级方案（替代"关闭时段"的业余做法）

| 方案 | 动作 | 优先级 | 预期效果 |
|---|---|---|---|
| **A 动态 per_slot_stop** | CST 00-03 时段 $0.8 → $0.4 | **P0 立刻** | avg_loss 减半，亏损可控 |
| **B 动态 ATR scale** | ATR > 40bps 时 sz × 0.5 | **P0 立刻** | 大仓不失控 |
| **C 时段切策略** | 那时段 grid → trend_follow | P1 待数据 | 单边行情赚趋势而非亏 |
| **D 根因特征信号** | 200+ 笔 挖"亏损前特征" | P2 长期 | 识别特征 → 自动切玩法 |

**只有 D 失败后，才退路 E："真关闭"** —— 我之前一上来就跳到 E，业余。

### 样本量警告

19 笔做不出可靠结论。**需要 200+ 笔**才能 95% 置信度下判断。
当前 signal_attribution 每 15s 采样，24h 可达 ~5000 条。
72h 样本足够做真正的 Time-of-Day 研究。

---

## 3. 如何判断下单？

### 当前 grid_pro 完整决策路径（11 道 gate）

```
on_tick →
  Gate 1. 热身期（< 30 ticks 不开）
  Gate 2. 日亏损/峰值止损（check_stop）
  Gate 3. 危险 regime 保护（TRENDING_DOWN/VOLATILE + 持仓宽限 60s）
  Gate 4. TP 追踪 / TP 超时 / 慢出血 aging
  Gate 5. 利润保护模式（达日目标）
  Gate 6. 网格中心偏移检查
  Gate 7. 市场条件 OK（spread / velocity / liquidity）
  Gate 8. 宏观趋势过滤（macro_bias）
  Gate 9. 盘口不平衡 EMA gate
  Gate 10a. Taker Aggressor gate（warn/block）
  Gate 10b. 浮亏保护（持仓 > $0.3 拒开新格）
  Gate 10c. 7 维方向评分（|score| > 0.15 与 direction 反向 → 跳过）
  Gate 10d. Orderbook spread 保护（> 3bps 拒）
  Gate 10e. 开仓节流（2min 内 > 2 次拒）
  Gate 11.  Circuit Breaker（速率级熔断）
  Gate 12.  Strategy Pool（regime 不匹配拒）
  Gate 13.  激活网格 → _place_grid（决定档位数、spacing、sz）
```

**评估**：gate 层数 **过多**，每加一层都在减少成交机会。需要精简到 5-7 个核心 gate。

### 核心挑战

Grid 策略赚钱需要 **fills 足够多**（高频累积）。
但当前 gate 层数 → 可能导致"挂单 fill 不到"。

### 13 gate 决策路径完整流程图

```
┌─────────────────────────────────────────────────────────┐
│ Tick (WS/REST) → grid_pro.on_tick(last, bid, ask, ctx)  │
└───────────────────────┬─────────────────────────────────┘
                        ▼
  【阶段 A：热身 + 基础检查】
  Gate 1. 热身期 < 30 ticks → return None
  Gate 2. 日亏损/峰值止损 check_stop → emergency_close
                        ▼
  【阶段 B：持仓管理】
  Gate 3. 危险 regime (TRENDING_DOWN/VOLATILE) → 撤挂单+60s 宽限+浮亏 >$1.5 止损
  Gate 4. TP trail / TP aging（480-600s + 破位）/ 慢出血 aging（30min+$0.30）
  Gate 5. 利润保护模式（达日目标 → 只守不开）
                        ▼
  【阶段 C：市场过滤】
  Gate 6. 网格中心偏移 → 重定位
  Gate 7. 市场条件（spread / velocity / liquidity）
  Gate 8. 宏观趋势 macro_bias gate
  Gate 9. 盘口不平衡 EMA gate
                        ▼
  【阶段 D：方向/质量评分】
  Gate 10a. Taker Aggressor 逆势 gate（warn/block）
  Gate 10b. 浮亏保护（持仓浮亏 > $0.3 拒开新）
  Gate 10c. 7 维方向评分 |score| > 0.15 与 direction 反向 → skip
  Gate 10d. Orderbook spread > 3bps → skip
  Gate 10e. 开仓节流（2min > 2 次拒 + 补仓 60s > 2 次拒）
                        ▼
  【阶段 E：系统协调】
  Gate 11. Circuit Breaker（速率级熔断）
  Gate 12. Strategy Pool（当前 regime 是否允许 grid）
                        ▼
  【阶段 F：下单】
  Gate 13. _place_grid → 计算 spacing / n_active / bias / sz_scale
          → 分档挂 post_only 限价单
```

**每个 gate 的统计信息** —— daemon 需做的工作：
- 每个 gate 的"拒绝率"（被拒/总次数）
- 每个 gate 拒绝后 PnL 的"反事实模拟"：如果没拒绝会赚还是亏？
- **专业优化**：反事实 PnL > 0 → gate 过严需放宽；反事实 PnL < 0 → gate 价值大

目前没做这个统计。**这是明天白皮书的优化点之一**。

---

## 4. 买多还是买空？

### 当前逻辑

1. **静态 GRID_DIRECTION=long/short**（.env 配置）
2. **Regime Router** 5min 评估投票切换（但持仓不能切）
3. **7 维实时评分** 在开仓前判断是否与 direction 一致

### 问题

- **Direction 切换要求无持仓**，但我们一直持仓 → **切换从未发生**
- 结果：**方向由静态配置决定**，实时评分只能"拒开错方向仓"

### 专业做法（待升级）

**Bidirectional grid**：同时挂 long 和 short 档位，根据实时 score 选方向。
这需要架构改造（当前 grid_pro 代码中 direction 硬编码到 slot 结构）。

---

## 5. OKX 策略

### 当前用到的 OKX 特性

| 特性 | 当前使用 | 说明 |
|---|---|---|
| post_only 挂单 | ✅ | 入场用（争 maker 费率）|
| market order | ✅ | 紧急平仓 |
| isolated margin | ✅ | 每仓独立保证金 |
| cross margin | ❌ | 未用（可提高资金效率）|
| OCO（止盈止损）| ✅ | trend_follow 用 |
| iceberg | ❌ | smart_order_router 写了但未接入 |
| trigger order（条件单）| ❌ | 未用 |
| TP/SL 挂单 | ✅ | TP 挂单 |
| WS public trades | ✅ | trades_analyzer |
| WS public books | ✅ | 盘口 |
| WS private account | ❌ | 未用（持仓状态仍 REST 轮询）|
| WS private orders | ❌ | 未用 |

### 未用但值得用的

1. **WS private**：订单状态实时推送，替代 REST 轮询（延迟 -95%）
2. **Iceberg**：大单拆分（当前 sz=1 暂不需要）
3. **Trigger order**：OCO 止损不需要持仓就能挂（减少 strategy 管理复杂度）

### 关键 OKX 参数（ETH-USDT-SWAP）

- ctVal = 0.1 ETH/张
- lot_sz = 0.1 张（最小步进）
- min_sz = 0.1 张
- tick_sz = 0.01 USDT
- maxLmtSz = 150 张

### OKX 特色机制（专业量化能用的）

**1. funding settlement arbitrage**（资金费率套利）
每 8h 结算（UTC 00:00 / 08:00 / 16:00），funding rate 极端时：
- > +0.05%: 多头付空头 → 结算前开空 + 结算后平 → **纯收 funding**
- < -0.05%: 空头付多头 → 结算前开多
- 注：套利需 **spot 对冲**才真 risk-free（我们只做 perp，有方向风险）

**2. 保证金模式选择**
- isolated：仓位独立，爆仓仅损失该仓保证金（我们用）
- cross：账户共享保证金，资金效率高但爆仓连累全账户
- **升级方向**：grid 仓 isolated，trend_follow 仓可试 cross（大仓时效率 +30%）

**3. 订单簿深度优化**
- OKX books WS 更新 10-100ms（vs REST 15s）
- **升级方向**：我们已订阅 books5，可升级 books-l2（更深、更新更快）

**4. 降低 fee 路径**
- VIP 0 → maker -0.02%（我们当前）
- VIP 1（月交易量 $5M） → maker -0.025%（-25%）
- VIP 2 → maker -0.03%
- **现实**：小账户 ~$186 × 100 笔/日 × 2x 杠杆 = $37k/日 notional → 月 ~$1.1M
- 远达不到 VIP 1，fee 优化靠"减少 taker 比例"而非 VIP 升级

**5. OKX 特色单类型未用**
- **conditional order**（条件单）：预设触发价，不占挂单配额
- **MMP（Market Maker Protection）**：做市商保护机制，大批量挂单时防止被"扫"
- **Algo order with TP/SL**：开仓即带 TP/SL，不需要 strategy 单独管理

### API 配额限制（专业必须知）

| API | 限速 |
|---|---|
| 私有（下单/撤单）| 60 req/2s |
| 行情（K线/深度）| 40 req/2s |
| 账户查询 | 10 req/2s |

**当前使用估算**：
- WS private（订单推送）：0 req（未用，主升级机会）
- WS public：< 1/s（book + trades）
- REST 查询（positions / fills）：~5/分钟（节流）
- **空间很大**：订阅 WS private 即可把 REST 查询砍 90%

---

## 6. 手续费如何计算？

### OKX 永续合约费率

| 类型 | 默认 VIP 0 |
|---|---|
| Maker | -0.02% (即 -2 bps)|
| Taker | +0.05% (即 +5 bps)|
| Round-trip (纯 maker) | 4 bps |
| Round-trip (纯 taker) | 10 bps |
| Round-trip (maker + taker 混合) | 7 bps |

### 我们实际 fee 占比（最新近 100 笔，2026-04-22 15:54 更新）

- 总 gross: **-$0.67**（亏损中）
- 总 fee: **-$3.19**
- 总 notional: $11,894
- 每笔 avg fee: **$0.032**
- fee_bps per fill: **2.69 bps**（符合 OKX maker -0.02% 预期 ✅）
- **Round-trip fee: 5.37 bps** —— 理论最优
- **100 笔 100% maker** ✅（post_only 生效，无 taker）

**关键结论**：
1. fee 执行层面**没问题**（全 maker，2.69 bps/笔）
2. **问题在 gross 本身负** —— 策略不赚钱，不是被手续费吃
3. spacing 30 bps - fee 5.37 bps = **理论每 cycle 净利 24.6 bps**
   但实际净利 -$0.67，说明 **fill 后方向经常错**（追顶/追底）

**结论修正**：我之前说 "fee/gross 50%" 是特定时段数据。
**真正的问题是 gross 负**，不是 fee 占比。

### 原因

1. 入场 post_only（maker）+ 平仓 TP post_only（maker）= 理想情况 4 bps
2. **但 per_slot_stop 触发时用 market（taker）** → 单笔 fee 变 2+5=7 bps
3. spacing 30 bps → 4-7 bps fee 占 **13-23%**
4. 如果 TP 偏离成本不大，净利很薄

### 优化方向

1. **入场 + TP 都保持 maker** → 固定 4 bps
2. **per_slot_stop 尝试用 post_only 限价止损**（但可能 fill 不到）
3. **申请 VIP 1 费率**（30 天交易量 $5M 可升，我们远未达）
4. **降低 market order 比例**（当前 market = emergency_close / per_slot_stop）

---

## 7. 交易策略：多还是少？

### 现状

1. **grid_pro**（主）：震荡市
2. **trend_follow_watcher**（备）：突破追势
3. **mean_reversion_watcher**（已关闭）：Z-score 回归
4. **funding_arb_watcher**（已关闭）：资金费率

### 实际工作的

**只有 grid_pro**。trend_follow 今日 0 次触发（突破条件苛刻）。

### 专业观点

- 单一策略（grid）：简单但单市况脆弱
- 多策略：抗 regime 切换但复杂
- **关键是每个策略都必须有证明过的 edge**

### 我们目前的问题

**grid_pro 的 edge 未证明**（Sharpe -8.64）。
加更多策略 = **叠加 edge 未证明的噪声**。

### 建议

1. **先修好 grid_pro**（让它 Sharpe 转正）
2. **再考虑加其他策略**
3. 用 backtest 证明每个策略独立 edge

---

## 8. 当前系统问题 TOP 10

（明天填充完整诊断 + 数据）

（2026-04-22 15:54 实测数据修订）

1. **🔴 Sharpe -8.64 / Kelly -0.12** —— 数学证明策略在亏
2. **🔴 午夜时段致命亏损**：CST 00-03 合计 -$4.17（11 笔），**占 100 笔总亏 85%+**
   → **立刻措施**：把 UTC 16-19 (CST 00-03) 加入 DISABLED_UTC_HOURS
3. **🔴 节流 gate 补仓路径 bug**（2026-04-22 15:54 发现）：
   15:18:58 同秒 buy 2 笔，堆仓到 sz=3 → 15:19:02 sell 亏 -$0.82
   原因：节流 gate 只管 `_place_grid` 首次开格，`_slot_fill_补仓` 绕过
   → **立刻修复**：补仓前也检查 `_recent_entries_ts`（本轮 commit 已修）
4. **🟡 sz 分布混乱**：100 笔中 sz=0.3(46) / sz=0.5(22) / sz=1.0(20) / sz=3.0(1)
   → 参数频繁调整造成的历史噪声，不利于统计一致性
5. **🟡 7-11 维信号未 IC 验证** —— 可能全是噪声（等 24h 数据）
6. **🟡 direction 切换从未发生** —— 持仓阻塞 Regime Router
7. **🟢 Fee 执行层正常**：100% maker，fee_bps 2.69（预期 2.0），近 OKX 极限
   → 问题不在 fee，在 **gross 本身负**（方向错 / 追顶追底）
8. **🟡 执行质量未测量** —— fill rate / slippage / latency 黑盒（本轮已写 fill_quality_logger）
9. **🟡 无实时 dashboard** —— 主人看不到状态
10. **🟡 backtest look-ahead bias** —— 用 close 价触发，实盘无法复现

---

## 9. 升级路线图

（明天完善，按 ROI 排序）

### 升级 ROI 评分矩阵（2026-04-22 17:00 正向思维版）

按 **(预期收益 × 概率) / 开发成本 × 风险** 排序：

| # | 升级项 | 预期 ROI | 开发时间 | 风险 | 优先级 |
|---|---|---|---|---|---|
| 1 | **时段 per_slot_stop 动态**（CST 00-03 砍半）| **日 +$2-4** | 1h | 低 | P0-A |
| 2 | **ATR-aware sz scale**（ATR > 40bps 缩 sz）| **日 +$1-2** | 1h | 低 | P0-A |
| 3 | **WS private 订阅**（订单实时推送替代 REST）| 延迟 -95% | 4h | 中 | P0-B |
| 4 | **signal_attribution 24h 数据 → 砍 IC<0.05** | **系统净化** | 1h 后 | 低 | P0-B |
| 5 | **真 backtest（30天1m+next-bar撮合）** | **决策支撑** | 8h | 中 | P1 |
| 6 | **Dashboard HTML**（P&L曲线+Kelly实时）| 主人可观察 | 4h | 低 | P1 |
| 7 | **gate 反事实统计**（每 gate 的"错杀"率）| **信号优化依据** | 4h | 低 | P1 |
| 8 | **时段切策略 grid→trend_follow** | 结构升级 | 8h | 中 | P1-B |
| 9 | **Alert 体系邮件+Telegram** | 主人安心 | 4h | 低 | P1 |
| 10 | **OCO conditional order 替代 strategy TP** | -50% 代码复杂度 | 6h | 中 | P2 |
| 11 | **Bidirectional grid**（同时 long/short）| **结构性 +20%** | 16h | 高 | P2 |
| 12 | **代码重构拆 grid_pro.py** | 维护性 | 12h | 中 | P2 |
| 13 | **funding rate 结算 arb**（需 spot 对冲）| 5-15bps/8h | 20h | 高 | P3 |
| 14 | **多资产（BTC/SOL）分散** | 风险分散 | 20h | 高 | P3 |

### 关键迭代原则（正向思维，非防御）

1. **每个 P0 升级完成后**，运行 2h 验证 Sharpe 是否改善
2. **不盲改**：backtest 说好 → 才上生产
3. **优先做"改善 avg_loss"的升级**（当前 avg_loss -$0.53 → 目标 -$0.40）
4. **不做"关闭交易"**（除非紧急熔断）—— 关闭 = 放弃赚钱机会

### 本周（P0）
- [x] Circuit Breaker 已上线 ✅
- [x] fill_quality_logger 采集 ✅
- [x] signal_attribution 采样 ✅
- [x] 节流 gate 补仓绕过 bug 修复 ✅
- [ ] **升级 #1**：时段 per_slot_stop 动态
- [ ] **升级 #2**：ATR-aware sz scale
- [ ] **升级 #4**：IC 数据分析 → 砍噪声信号

### 下周（P1）
- [ ] WS private 订阅
- [ ] 真 backtest 引擎（next-bar撮合）
- [ ] Dashboard HTML
- [ ] gate 反事实统计
- [ ] 时段切策略方案 C

### 下月（P2-P3）
- [ ] Bidirectional grid
- [ ] 代码重构
- [ ] OCO 替代 strategy TP

---

## 10. 结论

（明天给）

**我作为首席决策者给主人的底线建议**：
（待 24h 数据 + circuit_breaker 工作后一并给）

---

*文件持续更新中。最后更新：2026-04-22 15:25 CST*
