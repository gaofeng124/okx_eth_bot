# 量化引擎演进路线图

**目标**：现账户 ~42 USDT → 稳定日收益 2 USDT 保底 → 资金规模扩大后日收益 30 USDT+
**原则**：每一步都有可验证的 hypothesis + 回退条件，失败则回滚，绝不叠加改动掩盖错误。

## 🚀 2026-04-20 主人授权 —— 目标驱动原则

> "我想让你没有规则，像个真实、急迫、专业的量化交易专家来思考问题。
>  一切围绕'稳定赚钱'这个目标。不是为了升级而升级。
>  你觉得系统不好就一直升级。需要观察市场反馈就观察。"

### 核心框架
- **唯一目标**：稳定赚钱
- **roadmap 仅是参考**，不是圣经；AI 按自己的职业判断选下一步
- **时间无预设**：升级多久都行，关键是有价值
- **观察也是工作**：数据不够时观察比瞎改更好
- **急迫但不焦躁**：活跃风险 → 分钟级；结构性缺陷 → 小时级；优化 → 积累数据后

### 当前识别的结构性缺陷（AI 自选优先级）
1. 只做多 → 下跌段必挨刀（Phase 2 做空）
2. 因子单一 → 信号弱（Phase 1 因子）
3. 单策略 → 市况僵化（Phase 3 多策略）

AI 每轮开始，先问自己：
- 当前账户最大的痛点是什么？
- 这个痛点用数据能验证吗？
- 有没有证据充分的修复方向？
- 有的话 → 动手
- 没有的话 → 观察、等数据、不瞎改

---

## Phase 0（已完成）：基础设施

- [x] 紧急风控修复（leverage 5、contracts 0.2、per_slot_stop 1.5、ctVal 自适应）
- [x] 常驻 AI 大脑（ai_brain.py + systemd，走 Max 订阅）
- [x] 智能 watchdog（保留 agent 本地 commit 不被 reset）
- [x] 日志轮转 + cleanup cron
- [x] Loss Ledger 纪律系统

---

## Phase 1（进行中，~1-2 周）：精进 grid_pro + 因子 v2

**目标**：给 grid 加"眼睛"，让它知道什么时候该放开做、什么时候该收缩。

### 1.1 新因子引入（按优先级）
- [x] **Book Imbalance** （R47 已激活 —— null→-0.81 验证通过）
- [ ] **OI Δ Rate**（1 分钟 OI 变化率）
  - 数据源：OKX REST `/api/v5/public/open-interest`（每 30s 拉）
  - 用途：OI 激增 + 价格拉动 = 趋势信号
- [ ] **Taker Buy/Sell CVD**（主动买/卖累积差）
  - 数据源：OKX WS `trades-all`
  - 用途：CVD 与价格背离 → 反转信号
- [ ] **Funding Basis**（永续 vs 现货基差，与 funding rate 不同）
  - 数据源：OKX REST 现货 + 永续价格
  - 用途：极端基差时减仓
- [ ] **Exchange Net Flow** (2026-04-20 新增，Etherscan 链上数据)
  - 数据源：`quant/tools/onchain.exchange_flow_recent()`
  - 用途：大型交易所热钱包 24h 净流入 > 0 → 抛压前兆（用户充币打算卖）
  - 验证：记录 7 天内 Binance-14 + Hot-20 净流入 vs 当日 PnL，找相关
- [ ] **Whale Tx Count** (2026-04-20 新增)
  - 数据源：同上 large_inflow_count / large_outflow_count (> 50 ETH 转账)
  - 用途：鲸鱼活动增加时降低仓位激进度
- [ ] **Gas Price**（2026-04-20 新增）
  - 数据源：`gas_oracle()` safe/propose/fast (gwei)
  - 用途：gas 激增往往伴随 FOMO 或抛售，作为波动率前兆

### 1.2 特征落盘
- [ ] 新增 `data/features.jsonl`：每 tick 写一条所有因子值 + 下一秒价格变化
- [ ] 为将来 ML 合成准备数据集

### 1.3 Regime V2
- [ ] 当前 Regime 只用 EMA 斜率，太粗
- [ ] 新 Regime 综合：EMA + ATR + Book Imbalance + OI Δ + Funding + CVD
- [ ] 输出"连续 Regime 分数" [-1, +1]，而非 5 级分类

**回退触发**：若 24h 胜率 < 35% 或 PnL < -3 USDT，回退到 Phase 0 代码。

---

## Phase 2（**立即启动** / 预计 1 周）：做空能力（grid_bidi）

**目标**：彻底解决"下跌段只能挨打"的结构性缺陷。过去 8h ETH 跌 2% 账户跟跌 2%，这是单向做多的天花板。

### 2.1 架构设计（第一优先任务）
- [ ] 读懂 grid_pro.py 现有 long-only 逻辑（_place_grid / _update_tp / _emergency_close）
- [ ] 设计 `GridSlot.side ∈ {"long", "short"}` 扩展
- [ ] 镜像逻辑：
  - short 入场：在 center 上方挂 post-only sell（预期价格上冲后回落）
  - short TP：在 fill 价下方 k× spacing 处挂 buy（反向）
  - short per_slot_stop：fill_price + threshold/sz 触发市价平仓
- [ ] 一个时刻只做一个方向（由 Regime Router 决定）→ 避免对冲抵消
- [ ] **设计选项**（AI 自行评估）：
  - A) 在 `grid_pro.py` 上扩展（最小改动，风险小）
  - B) 新写 `grid_bidi.py` 作为平行策略（清晰但多代码）
  - 建议 A，但如果 grid_pro 太耦合 long-only 逻辑，果断选 B

### 2.2 风控
- [ ] liq price 做空公式：`liq = avg * (1 + 1/lev - maint_margin)`（OKX isolated net 模式）
- [ ] short 初始 `GRID_CONTRACTS_PER_SLOT_SHORT=0.1`（独立于 long 的 0.2，先保守）
- [ ] 新增 .env 参数 `GRID_DIRECTION={long,short,both}`；默认 both；Regime Router 自动选
- [ ] short 启动前必须通过：小批量 "dry run"（0.01 张测试 1 次反向成交再自动撤）
  - 注意：这里 dry run **是内部逻辑路径验证**，不是"下实单测试"，禁止下单 API

### 2.3 Regime Router（简化版 V1，足以上线）
- [ ] RegimeScore 计算：
  - EMA_fast/slow 斜率 + book_imbalance 短窗口 + OI Δ（如 Phase 1 已有）
  - 归一到 [-1, +1]
- [ ] RegimeScore > +0.3 → long-only grid
- [ ] RegimeScore < -0.3 → short-only grid
- [ ] |RegimeScore| ≤ 0.3 → ranging（可选：两边都挂 0.1 张极小 grid，更谨慎）

### 2.4 启动条件（2026-04-20 降门槛）

**原条件**：wl_ratio ≥ 0.5 稳定
**新条件**：**wl_ratio ≥ 0.4 且连续 20 笔 EV > 0 即可启动**（不用等主人确认）

### 2.5 验证与放量（AI 自主把关）
- [ ] Step 1: short 能正常开仓 + TP + 止损 (小批量 0.1 张 × 2 层)
- [ ] Step 2: 观察 48h，short side PnL ≥ 0 才进入 Step 3
- [ ] Step 3: 单边 contracts_per_slot_short 0.1 → 0.15
- [ ] Step 4: 如果过去 3 天 **任何一天** total PnL < -3 USDT → 自动 `.env GRID_DIRECTION=long` 回退并邮件通知

**铁律**：永不允许 long + short 同时持仓（净敞口对冲 = 白给手续费）。

**回退触发**：连续 2 天做空 PnL < -2 USDT，或硬风控被触发 > 3 次 → 自动关闭做空。

---

## Phase 3（~3-4 周）：多策略热插拔

**目标**：grid 不是最优；要让多策略在不同市况并行跑。

### 3.1 策略接口统一
- [ ] `TickStrategy` 协议已有，扩展为支持"策略分类"（momentum / mean_reversion / arb）
- [ ] 每个策略声明适用 regime
- [ ] 主循环按 regime 激活对应策略

### 3.2 第二个策略：trend_follow
- [ ] 简单趋势跟随：EMA 9/21 金叉死叉 + ATR trailing stop
- [ ] 只在 TRENDING_UP / TRENDING_DOWN 激活
- [ ] grid 在震荡时跑，trend 在趋势时跑

### 3.3 资金分配
- [ ] 风险预算：总可用保证金的 50% 给当前主策略，50% 储备
- [ ] 单策略亏损 > 日限额 → 关闭，储备金接手

**回退触发**：多策略跑 3 天，总 PnL < 单 grid 基准 PnL → 回退。

---

## Phase 4（~4-8 周）：ML 因子合成

**目标**：十几个单因子线性叠加不如一个训练好的模型。

### 4.1 数据准备
- [ ] Phase 1 的 features.jsonl 累计 10000+ 样本
- [ ] 标签：下 N 分钟价格变化（回归）或方向（分类）

### 4.2 简单模型
- [ ] 用 scikit-learn / XGBoost 训练
- [ ] 在线推理：每 tick 输出 `alpha_score ∈ [-1, 1]`
- [ ] 作为 Regime + 入场 filter 的信号

### 4.3 持续重训
- [ ] 每天夜间用最近 30 天数据重训
- [ ] A/B 测试新模型 vs 老模型 7 天

**回退触发**：新模型上线 7 天 PnL < 老模型（无 ML gate）→ 回退。

---

## Phase 5+（远期）：多币种 / 链上情绪 / 事件驱动

用户指示：ETH 稳定盈利后再做。

---

## AI 当前重点（每轮必查）

从以上路线图，取**最高优先级未完成项**作为本轮 focus。
不跳步、不叠加、不敷衍。
