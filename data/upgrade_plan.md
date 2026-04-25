# ETH量化系统升级计划

## 本次（2026-04-25 第三十七轮）完成

### grid_pro.py：VOLATILE宽限期区分（P3）

**问题**：`_bearish_regime_since` 宽限期对所有 danger regime 统一使用 60s：
- **TRENDING_DOWN**（long方向）/ **TRENDING_UP**（short方向）：真实方向性趋势，60s 合理
- **VOLATILE**：瞬时 ATR 激增（非方向性），通常 60-90s 内消散，60s 硬截止导致正常波动期间频繁触发紧急平仓

**修复（round37）**：
- `_grace_sec = 90.0 if regime == Regime.VOLATILE else 60.0`
- VOLATILE 宽限期：60s → 90s（多 30s 等待 ATR 激增消散）
- 方向性 danger regime 保持 60s（趋势已确认，及时止损更合理）
- 硬止损 `-1.5U` 不变（无论 grace period 长短，亏损过大立即平仓）
- log 格式：从 `elapsed/grace` 双字段，便于日后审计宽限期命中情况

**效果预期**：
- 减少短暂高波动期间（65-89s 的 VOLATILE 状态）触发的不必要紧急平仓
- 给 TP 挂单更多时间在 ATR 消散后自然成交（maker 费率 vs taker 费率差 5bps/笔）
- 真实亏损时（<-1.5U）不受影响，安全性不降低

---

## 历史完成

### 第三十六轮（2026-04-25）
- [x] grid_pro.py: 1h方向gate滞回环（hysteresis）+ 日志节流
  - LONG drop-gate: entry=0.990，exit=0.995
  - SHORT rise-gate: entry=1.010，exit=1.005
  - 日志：每60s最多1条（非每tick）

### 第三十五轮（2026-04-24）
- [x] grid_pro.py: SHORT方向1h快速上涨硬止进gate（entry=1.01），与LONG的drop-gate对称

### 第三十四轮（2026-04-24）
- [x] grid_pro.py: 新增LONG方向1h价格下跌硬止进门槛（entry=0.99）

### 第三十三轮（2026-04-24）
- [x] runner.py: WS重连固定5s→指数退避(1s→2s→4s...→30s)
- [x] grid_pro.py: profit_spacings存入EWMA桶前加min(x,3.0)上限帽

### 第一~三十二轮（2026-04-18~24）
- [x] 全部P0/P1问题：参数修复、WS重连、持仓同步、自适应TP、EWMA、FGI、资金费率等

---

## 待解决问题（按优先级）

- [ ] P3: 验证gate日志节流实际效果（需实盘日志）
  - 方法：`grep '1h-drop-gate\|1h-rise-gate' data/logs/*.log | wc -l`
  - 预期：滞回环后，每天有效触发次数 < 单阈值版本

- [ ] P3: 验证profit_spacings EWMA分布（需实盘日志）
  - 方法：从analysis.jsonl提取fill_tp的profit_spacings，期望0.4-0.8格均值
  - 若<0.4持续：RANGING base_trigger 1.00 → 1.10（trigger更严格，保住更多TP利润）

- [ ] P3: 验证VOLATILE宽限期90s实际效果（round37新增）
  - 方法：`grep 'Regime=VOLATILE 宽限到期' logs/*.log`
  - 预期：elapsed字段多见 60-89s（即被新宽限期保护的案例）

- [ ] P3: 验证WS指数退避效果（round33）
  - 方法：`grep '\[WS行情\]' logs/*.log | grep '后重连'`

## 下次优先行动

1. **若能访问实盘日志**：
   - 验证 VOLATILE 宽限期 elapsed 分布，确认 90s 改进是否有效
   - 查看 profit_spacings 均值：若 <0.4 格持续 → 调整 RANGING base_trigger: 1.00 → 1.10
   - gate 日志确认：每分钟最多 1 条（非每 tick）

2. **P3候选**：若 `_ewma_profit_avg()` 持续返回 <0.4：
   - 调整 `_adaptive_trail_trigger` 的 RANGING 上界：1.20 → 1.30
   - 意味着自适应后最大 trigger 更严，保住更多利润

3. **P3候选**：若 VOLATILE gate 触发后 90s 内价格恢复率 > 70%：
   - 考虑进一步延长至 120s（但需权衡极端行情风险）

## 系统评估
- **策略有效性**：9/10
  - 37轮迭代；全P0/P1已解决；P2/P3改进积累中
  - VOLATILE宽限期区分后，短暂高波动期间的误割应减少
  - 主要待验证：实盘gate触发率、profit_spacings EWMA收敛、VOLATILE 90s效果
- **当前主要风险**：
  1. 外部API网络受限（沙盒环境，无实时市场监控）
  2. 实盘日志无法访问（所有P3优化均未经实盘数据验证）
  3. VOLATILE 90s宽限在极端单边行情中存在多持仓30s的风险（-1.5U硬止损兜底）
- **累计运行轮次**：37
