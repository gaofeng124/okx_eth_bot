# 量化引擎演进路线图

**目标**：现账户 ~42 USDT → 稳定日收益 2 USDT 保底 → 资金规模扩大后日收益 30 USDT+
**原则**：每一步都有可验证的 hypothesis + 回退条件，失败则回滚，绝不叠加改动掩盖错误。

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
- [ ] **Book Imbalance** (`book_imb = (bid_vol - ask_vol) / total`, 亚秒级 alpha)
  - 数据源：OKX WS `books5` 或 `books-l2-tbt`
  - 用途：开格方向微调（imb > 0.3 偏多、< -0.3 偏空）
  - 验证：记录 1000 笔成交前后 10 秒的 imbalance → 算信息比
- [ ] **OI Δ Rate**（1 分钟 OI 变化率）
  - 数据源：OKX REST `/api/v5/public/open-interest`（每 30s 拉）
  - 用途：OI 激增 + 价格拉动 = 趋势信号
- [ ] **Taker Buy/Sell CVD**（主动买/卖累积差）
  - 数据源：OKX WS `trades-all`
  - 用途：CVD 与价格背离 → 反转信号
- [ ] **Funding Basis**（永续 vs 现货基差，与 funding rate 不同）
  - 数据源：OKX REST 现货 + 永续价格
  - 用途：极端基差时减仓

### 1.2 特征落盘
- [ ] 新增 `data/features.jsonl`：每 tick 写一条所有因子值 + 下一秒价格变化
- [ ] 为将来 ML 合成准备数据集

### 1.3 Regime V2
- [ ] 当前 Regime 只用 EMA 斜率，太粗
- [ ] 新 Regime 综合：EMA + ATR + Book Imbalance + OI Δ + Funding + CVD
- [ ] 输出"连续 Regime 分数" [-1, +1]，而非 5 级分类

**回退触发**：若 24h 胜率 < 35% 或 PnL < -3 USDT，回退到 Phase 0 代码。

---

## Phase 2（~2-3 周）：做空能力（grid_bidi）

**目标**：彻底解决"下跌段只能挨打"的结构性缺陷。

### 2.1 架构
- [ ] 策略层抽象 → 策略支持 `direction ∈ {long, short, both}`
- [ ] 做空 grid：镜像做多逻辑，short @ 上方、cover @ 下方
- [ ] 一个时刻只做一个方向（通过 Regime 决定），避免对冲抵消

### 2.2 风控增强
- [ ] 做空爆仓机制不同（无限上涨 vs 归零下跌），需单独测算 liq price
- [ ] 初始只在小账户（当前 42U）开一周，每次 `GRID_CONTRACTS_PER_SLOT=0.1`
- [ ] 连续 2 天做空 PnL 为正，才放大到 0.2

### 2.3 Regime Router
- [ ] RegimeScore > 0.3 → 只做多
- [ ] RegimeScore < -0.3 → 只做空
- [ ] |RegimeScore| < 0.3 → 震荡模式，grid 双向同时挂（谨慎）

**回退触发**：做空一周 PnL < -2 USDT，关闭做空能力。

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
