# ETH量化系统升级计划

## 本次（2026-04-26 第四十二轮）完成

### grid_pro.py：_ewma_profit_avg 最低有效权重门槛（P3）

**问题**：
- round41 引入 Regime-specific 半衰期（RANGING=900s, TRENDING=2700s）
- 边缘情况：重启后 `_replay_tp_history` 加载昨日日志（最多40条）
- 若上次成交在 4+ 个半衰期前（RANGING: >1h, TRENDING: >3h），所有桶数据权重趋近于 0
- `total_w > 0.0` 检查仍通过（浮点数非零），EWMA 退化为等权均值（等价于忽略时间衰减）
- 退化后的 avg 可能与实际近期市场利润无关，误触发 trigger/offset 自适应调整

**修复（round42）**：
- 新增类常量 `_EWMA_MIN_TOTAL_W = 0.5`
- 计算完 total_w 后增加检查：`if total_w < 0.5: return None`
- RANGING（900s）：等效"桶内所有数据均超 ~3600s（1h）前"时失效，退回 base 参数
- TRENDING（2700s）：等效"均超 ~10800s（3h）前"时失效
- 0.5门槛具体含义：相当于至少有 0.5 个"刚填充"样本等值的有效权重

**效果预期**：
- 消除重启后旧数据误驱动 adaptive trail 的边缘情况
- 无最近成交时，trail 自动退回 base_trigger=1.00 / base_offset=0.50，更保守
- 对正常运行（每小时 3+ 次RANGING成交）无任何影响：total_w >> 0.5

---

## 历史完成

### 第四十一轮（2026-04-26）
- [x] grid_pro.py: _ewma_profit_avg Regime-specific EWMA 半衰期（RANGING=900s/TRENDING=2700s）

### 第四十轮（2026-04-25）
- [x] grid_pro.py: _update_tp ATR ratio 下界收紧 0.8 → 0.85

### 第三十九轮（2026-04-25）
- [x] grid_pro.py: _adaptive_trail_offset 两级自适应（avg<0.25 → offset +0.06）

### 第三十八轮（2026-04-25）
- [x] grid_pro.py: _adaptive_trail_trigger 两级自适应（avg<0.25 → trigger +0.20）

### 第三十七轮（2026-04-25）
- [x] grid_pro.py: VOLATILE宽限期区分（60s→90s）

### 第三十六轮（2026-04-25）
- [x] grid_pro.py: 1h方向gate滞回环（hysteresis）+ 日志节流

### 第三十五轮（2026-04-24）
- [x] grid_pro.py: SHORT方向1h快速上涨硬止进gate

### 第三十四轮（2026-04-24）
- [x] grid_pro.py: LONG方向1h价格下跌硬止进门槛

### 第三十三轮（2026-04-24）
- [x] runner.py: WS重连指数退避
- [x] grid_pro.py: profit_spacings EWMA上限帽

### 第一~三十二轮（2026-04-18~24）
- [x] 全部P0/P1问题已解决

---

## 待解决问题（按优先级）

- [ ] P3: round43：_adaptive_trail_trigger 第三层扩展（avg>1.00 → trigger-0.10）
  - 当前：avg>0.80 只减 0.05（base 1.00→0.95）
  - 扩展：avg>1.00 → 减 0.10（base 1.00→0.90，积极追踪延伸行情）
  - 约束：RANGING 下界 0.85 不变，avg>1.00 分支边界安全
  - 风险：无实盘数据验证，保守起见仍需观察

- [ ] P3: round44：_adaptive_trail_offset 对称扩展（avg>1.00 → offset-0.05）
  - 与 round43 trigger 扩展配套：利润丰厚时允许 trail 落点更紧
  - 依赖 round43 先上线

- [ ] P3: RANGING base_trigger 1.00 → 1.05（需实盘 trigger 触发频率数据确认）

- [ ] P3: RANGING trail_offset 0.50 是否过宽/过紧（需实盘 profit_spacings 分布）

## 下次优先行动

1. **round43**：实现 `_adaptive_trail_trigger` 第三层（avg>1.00 → trigger-0.10）
   - 修改 `_adaptive_trail_trigger` 中 `elif avg > 0.8:` 分支
   - 改为两级：`avg > 1.0 → -0.10`，`avg > 0.8 → -0.05`
   - 同步更新 docstring bound 注释

2. 若有实盘日志：对比 round42 前后 adaptive trail 激活频率变化
   - 预期：旧数据误触发减少，base trail 使用率略升

## 系统评估
- **策略有效性**：9/10
  - 42轮迭代；全P0/P1已解决；P3精细化积累中
  - round42 的 total_w 门槛是 EWMA 稳健性的基础保障
  - 所有P3待验证项需实盘日志，沙盒环境无法直接监控
- **当前主要风险**：
  1. 外部API网络受限（沙盒环境，无实时市场监控）
  2. 实盘日志无法访问（所有P3优化均未经实盘数据验证）
  3. total_w=0.5 门槛在极低频成交市场下会延迟自适应激活（但比噪声均值更安全）
- **累计运行轮次**：42
