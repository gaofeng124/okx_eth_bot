# ETH量化系统升级计划

## 本次（2026-04-25 第四十轮）完成

### grid_pro.py：_update_tp ATR ratio 下界收紧 0.8 → 0.85（P3根源修复）

**问题**：
- round38/39 为极低利润场景添加了 `avg<0.25` 两级自适应（trigger+0.20，offset+0.06）
- 但极低利润的根源之一是 ATR ratio 下界过低（0.80），导致极低ATR时：
  - RANGING TP = 0.8（模式因子）× 0.80（ATR ratio最小值）× tp_mult = **0.64 × tp_mult**
  - TP被压至基准的64%，价格轻微回撤即触发fill_tp，realized profit偏低
  - 进而导致 profit_spacings EWMA持续低位，命中 avg<0.25 两级自适应分支

**修复（round40）**：
- `max(0.8, min(1.3, ...))` → `max(0.85, min(1.3, ...))`
- 极低ATR时 RANGING TP = 0.8 × 0.85 × tp_mult = **0.68 × tp_mult**（+6.25%）
- 根源修复优于依赖自适应补偿；round38/39仍保留作为极端场景兜底

**效果预期**：
- 极低ATR环境下TP落点提升6.25%，每格利润期望值改善
- avg<0.25 极低利润分支触发频率应下降（验证方式：实盘日志）
- 趋势模式不受影响（ratio=1.0×1.0×tp_mult，下界不起作用）

---

## 历史完成

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

- [ ] P3: round41：验证 avg<0.25 两级自适应实际命中频率
  - 方法：`grep 'adaptive trigger.*0\.20\|adaptive offset.*0\.06' data/logs/*.log | wc -l`
  - 预期：round40修复后，每天命中次数应少于round38/39修复前（若实盘日志可访问）

- [ ] P3: 若 avg<0.25 频率仍高，检查 profit_spacings 计算是否存在系统性偏低
  - 方法：从 analysis.jsonl 提取 fill_tp 的 profit_spacings 字段分布（中位数、P25）
  - 阈值：若 P25 < 0.25，说明根源在fill价格计算而非ATR

- [ ] P3: RANGING base_trigger 从 1.00 → 1.05（若trail仍频繁导致TP偏低）
  - 需实盘数据确认 trail 触发率后决定

- [ ] P3: _maybe_trail_tp 的 RANGING trail_offset 基准（0.15）是否过紧
  - 若实盘中 TP 被追踪后频繁被回撤吃掉，考虑 0.15 → 0.18

## 下次优先行动

1. **round41（下次运行）**：若能访问实盘日志，验证以下指标：
   - `grep 'adaptive' data/logs/*.log` 统计 avg<0.25 触发次数
   - `python3 -c "import json; [print(l) for l in open('data/analysis.jsonl') if 'fill_tp' in l]" | head -20`
   - 对比 round40 前后 profit_spacings 分布

2. **若无实盘日志**：转向下一个 P3 候选：
   - RANGING trail_offset 基准值评估（当前 0.15）
   - 或检查 _update_tp 中 `max(0.4, min(2.0, ...))` 外层边界是否合理

## 系统评估
- **策略有效性**：9/10
  - 40轮迭代；全P0/P1已解决；P3根源修复积累中
  - round40 解决 ATR ratio 下界过低（根源），round38/39 两级自适应作为兜底
  - 所有P3待验证项需实盘日志，沙盒环境无法直接监控
- **当前主要风险**：
  1. 外部API网络受限（沙盒环境，无实时市场监控）
  2. 实盘日志无法访问（所有P3优化均未经实盘数据验证）
  3. ATR联动链路（spacing→baseline→ratio→eff_mult）复杂，潜在边界条件未完全测试
- **累计运行轮次**：40
