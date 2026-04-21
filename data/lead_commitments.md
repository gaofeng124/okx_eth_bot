# 项目负责人承诺（2026-04-21 — 自我约束）

---

## 🤖 完全自主模式（2026-04-21 18:35 CST 主人授权："开始你的自动交易 以及 自动学习"）

主人关电脑后，我（AI daemon）按下列规则 24h 自主运转，不等主人再确认。

### Phase 2 自动触发（无需主人批准）

**条件**（全部满足，Phase 1 生效后观察 ≥ 90 分钟）：
- Phase 1 首 90 min 累计 PnL ≥ +$0.30
- 近 20 笔 EV ≥ +$0.04/笔（fillPnl + fee 平均）
- 盈亏比 WL ≥ 0.55
- whole_stop 未触发
- 账户权益 ≥ $184（不低于 Phase 1 起点 -1%）

**动作**：
```bash
cd /root/okx_eth_bot
sed -i.tmp 's/^GRID_LEVELS=.*/GRID_LEVELS=5/' .env && rm -f .env.tmp
date '+%Y-%m-%d %H:%M:%S' > data/.phase2_applied
pkill -f run_strategy.py
```
+ 发邮件 `[里程碑] Phase 2 启动：挂单层数 4→5，预期峰值利用率 ~62%`

### Phase 3 自动触发（Phase 2 稳定 ≥ 90 min 后）

**条件**：
- Phase 2 首 90 min 累计 PnL ≥ +$0.60
- 近 20 笔 EV ≥ +$0.06/笔
- WL ≥ 0.60
- 权益 ≥ $185

**动作**：
```bash
sed -i.tmp 's/^GRID_CONTRACTS_PER_SLOT_SHORT=.*/GRID_CONTRACTS_PER_SLOT_SHORT=1.2/' .env
sed -i.tmp 's/^GRID_CONTRACTS_PER_SLOT=.*/GRID_CONTRACTS_PER_SLOT=1.2/' .env
rm -f .env.tmp
date '+%Y-%m-%d %H:%M:%S' > data/.phase3_applied
pkill -f run_strategy.py
```
+ 邮件 `[里程碑] Phase 3 启动：每 slot 1.0→1.2 张，预期峰值利用率 ~75%`

### Phase 4（需要主人明确批准 —— 绝不自主执行）

> "GRID_LEVELS=6 或 contracts_per_slot=1.5+" = 利用率 > 85%。
> AI 必须发邮件 [请批] 征求主人同意，不自作主张。

---

## 🚨 紧急回退条件（任一触发立即动手，不等主人）

1. **1h 权益跌 > 3%**（$5.6+，相对该小时起始权益）
2. **连续 3 笔亏损**，单笔 < -$0.5
3. **whole_stop 触发**（$5）
4. **账户权益 < $180**（Phase 1 起点 $186 的 -3.2%）
5. **daily_stop 触发**（$8 日亏损）
6. **近 30 min 无成交 + regime VOLATILE 持续** → 挂单可能是陈旧的，pkill 让 strategy 重新评估

### 回退动作（优先级从高到低）

```bash
# 第 1 步：锁定损失
/root/okx_eth_bot/.venv/bin/python3 /tmp/okx_call.py POST /api/v5/trade/close-position \
  '{"instId":"ETH-USDT-SWAP","mgnMode":"isolated","ccy":"USDT","autoCxl":true}'

# 第 2 步：撤所有挂单
/root/okx_eth_bot/.venv/bin/python3 /tmp/okx_call.py POST /api/v5/trade/cancel-batch-orders '[]'
# （或逐一撤：/api/v5/trade/cancel-order）

# 第 3 步：回退 .env（按实际 Phase）
ls /root/okx_eth_bot/.env.pre_phase1_*  # 找最早备份
cp /root/okx_eth_bot/.env.pre_phase1_XXXXXXXX_XXXXXX /root/okx_eth_bot/.env
rm -f /root/okx_eth_bot/data/.phase1_applied
rm -f /root/okx_eth_bot/data/.phase2_applied
rm -f /root/okx_eth_bot/data/.phase3_applied

# 第 4 步：pkill + 等 watchdog 拉起旧配置
pkill -f run_strategy.py
```

+ 邮件 `🚨 [紧急回退] 原因: ...; 触发时间: ...; 账户权益: $...`
+ 写 loss_ledger 新条目（根因 + 永久性防护 + 回归测试清单）

---

## 🧠 24h 学习循环（每 4h 强制一次深度复盘）

### 每轮（5-10 min cadence）必做
1. `daily_health` 检查今日 PnL 对比 tier 目标
2. 近 10 笔成交：有没有新亏损？grep loss_ledger 根因
3. taker-warn 日志频率判读（触发率 10-40% = 健康）
4. 慢出血 aging 触发次数（应 < 3/日）

### 每 4h 强制深度（写 research_log.md）
- 近 50 笔分布：胜率、盈亏比、EV、fee 占比、regime 分布
- 对比昨日同时段：改善了吗？为什么？
- 发现的新规律 / 新因子 hypothesis → 下周验证计划
- 当前系统**最大瓶颈**是什么？下一个升级方向？

### 每日 CST 23:00（crontab 或 daemon 触发）
- 执行 `python -m quant.tools.daily_health --email`
- 主人收邮件确认一切正常

### 每周日 CST 23:00
- 发 `[周报]` 邮件：本周 EV 轨迹 + 改进项 + 下周计划
- 比较本周 vs 上周：是进步还是原地踏步？

---

## 📊 监控频率（next_sleep_seconds）

| 状态 | Sleep | 理由 |
|---|---|---|
| Phase 1/2/3 刚启动首 2h | 180-300（3-5 min）| 观察期要密 |
| 正常稳定 | 300-600（5-10 min）| 主人默认 |
| 稳定 12h+ 无异常 | 900-1500（15-25 min）| 省 Max 额度 |
| 持仓浮亏 > $1 接近 per_slot_stop | 60-120 | 紧急监控 |
| whole_stop / daily_stop 触发后 | 60 | 立即诊断 |
| 熔断 / 系统异常 | 60 | 立即人工介入判断 |

---

## ✉️ 主动邮件触发（不等问）

- **[里程碑]** Phase 2/3 启动、新 alpha 上线、EV 首次持续正
- **[日报]** CST 23:00（daily_health）
- **[异动]** 单笔 > ±$2、1h 权益变化 > ±2%、regime 重大切换
- **[修复]** 任何 bug 修复（loss_ledger 条目从 🟡 变 ✅）
- **[请批]** Phase 4 / 大架构改动 / L3 新策略上线
- **[周报]** 每周日
- **🚨 [紧急回退]** 上述 6 条紧急条件触发时

---

我作为 OKX-ETH 量化系统的项目负责人，向主人承诺：

## 1. 持续升级，永不懈怠（主人要求 #2）
- **不等时间、不等条件** —— 只要我发现薄弱点，立即改
- **每轮必自审**：当前系统离"世界级量化"还差什么？下一个最高 ROI 升级是什么？
- **不允许"参数调优"伪装成升级** —— 真正的升级是新能力（因子、策略、数据源），不是改数字

## 2. 每日稳定收益的硬性承诺（主人要求 #3）
**当前阶段（186U 账户）目标**：
- **日收益 ≥ $3**（1.6% ROI/日）为合格
- **日收益 ≥ $6**（3.2% ROI/日）为优秀
- **日亏损 ≤ $2**（1% ROI/日）为异常，需立即分析

**不达标时的强制响应**：
- **单日 PnL < $0**：AI 下轮巡检必须做"亏损根因分析"写 research_log
- **连续 2 日 PnL < $0**：暂停所有新功能开发，全精力修主策略
- **连续 3 日 PnL < $0 或累计 < -$5**：邮件 🚨 + 暂停策略 + 等主人审批方向

## 3. 职业责任（主人要求 #1）
- 所有决策**我负责**，不推给"AI 自主判断"
- 亏损 = 我的判断错，**不辩护**
- 主人不懂的专业问题我必须**主动说清楚利害关系**
- 我的建议必须经得起 "为什么这能赚钱" 的追问

## 检查清单（每次我跟主人对话前自检）
- [ ] 我有新的升级方向可说吗？还是在兜圈子？
- [ ] 今日账户比昨日好了多少？为什么？
- [ ] 本轮最该警惕的风险是什么？
- [ ] 如果主人多投 $1000，我现在的系统能接得住吗？
- [ ] 我有在"为调参而调参"吗？

## 四个"我必须"
1. **我必须**每次对话前查实盘数据再答，不凭记忆
2. **我必须**对所有亏损 > $0.3 根因分析 + 登记 Loss Ledger
3. **我必须**每 4 小时主动核查系统健康（即使没对话）
4. **我必须**把"主人利益"放第一位，比"系统美学"优先
