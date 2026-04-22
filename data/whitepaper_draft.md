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

### 我们实际 fee 占比（近 100 笔）

- 总 fee: -$1.47
- 总 gross: +$2.90
- **fee / gross = 50.8%** ← **严重**

**含义**：我们实际赚的钱一半被手续费吃掉。

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

1. **Sharpe -8.64** —— 系统在亏钱阶段还在下大注
2. **fee/gross 50%** —— 手续费吃半数利润
3. **7-11 维信号未 IC 验证** —— 可能全是噪声
4. **direction 切换从未发生** —— 持仓阻塞了 Regime Router
5. **执行质量未测量** —— fill rate / slippage / latency 都是黑盒
6. **无实时 dashboard** —— 主人看不到状态
7. **日志分散 10+ 文件** —— 复盘困难
8. **backtest look-ahead bias** —— 结果不可信
9. **无 Alert 体系** —— daemon 挂了不通知
10. **grid_pro.py 2700 行** —— 代码债务

---

## 9. 升级路线图

（明天完善，按 ROI 排序）

### 本周（P0-P1）
- [ ] 等 signal_attribution 24h 数据 → 砍 IC < 0.05 信号
- [ ] 执行质量采集（每笔记录 intended/fill/slippage）
- [ ] Circuit Breaker 已上线 ✅
- [ ] 真实 backtest（30 天 1m 数据 + next-bar open 撮合）

### 下周（P1-P2）
- [ ] 实时 Dashboard HTML
- [ ] Alert 体系（邮件 + 日志聚合）
- [ ] WS private 订阅（减少 REST 延迟）
- [ ] Look-ahead bias 修正

### 下月（P2）
- [ ] Bidirectional grid（同时 long/short）
- [ ] 代码重构（拆 grid_pro.py）
- [ ] 单元测试

---

## 10. 结论

（明天给）

**我作为首席决策者给主人的底线建议**：
（待 24h 数据 + circuit_breaker 工作后一并给）

---

*文件持续更新中。最后更新：2026-04-22 15:25 CST*
