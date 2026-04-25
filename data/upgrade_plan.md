# ETH量化系统升级计划

## 本次（2026-04-25 第三十九轮）完成

### grid_pro.py：_adaptive_trail_offset 两级自适应（P3）

**问题**：round38 为 `_adaptive_trail_trigger` 添加了两级（avg<0.25 → +0.20 步），但 `_adaptive_trail_offset` 仍是单级（avg<0.30 → +0.03）。两者不对称：

- 极低利润时 trigger 放宽到 1.20（需价格超出 TP 1.2 格宽才启动 trail）
- 但 offset 只放宽 +0.03 → 0.53（trail 触发后新 TP = mid - 0.53 × spacing）
- 结果：trail 很难触发（1.2 格宽门槛），一旦触发新 TP 又贴得偏近（0.53 格），容易被回撤立即夹击

**修复（round39）**：
- 新增 `avg < 0.25` 层级：步长 +0.06（vs 原 +0.03）
- RANGING: min(0.50+0.06, 0.65) = 0.56（仍在上界内，无需改边界）
- TRENDING: min(0.60+0.06, 0.75) = 0.66（仍在上界内，无需改边界）
- 原有 avg<0.30 判断保留（avg 0.25-0.30 范围仍 +0.03，行为不变）

**trigger × offset 组合效果（RANGING，avg<0.25）**：
- trigger = 1.20，offset = 0.56
- 新 TP = mid - 0.56 × spacing = (old_TP + 1.2 spacing) - 0.56 spacing = old_TP + 0.64 spacing
- 比修改前：old_TP + (1.2 - 0.53) spacing = old_TP + 0.67 spacing（变化极小，<0.03 spacing）
- 核心改善：两个函数的极低利润边界统一在 0.25，逻辑一致性更强

---

## 历史完成

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

- [ ] P3: round40：_update_tp ATR ratio下界 max(0.8,...) → max(0.85,...)
  - 当前：RANGING × ATR_min(0.8) = tp_mult × 0.8 × 0.8 = 0.64 × tp_mult（TP被压至64%基准）
  - 修复后：0.8 × 0.85 = 0.68 × tp_mult（提升6.25%，防止极低ATR场景TP过近）

- [ ] P3: 验证 avg<0.25 层级实际命中频率（round38/39 新增）
  - 方法：`grep 'adaptive trigger.*0\.20\|adaptive offset.*0\.06' data/logs/*.log | wc -l`
  - 预期：每天应有数次命中记录（若极低利润场景存在）

- [ ] P3: 若 avg<0.25 极少触发（<3次/天），检查是否 profit_spacings 计算偏高
  - 方法：从 analysis.jsonl 提取 fill_tp 的 profit_spacings 字段分布

- [ ] P3: RANGING base_trigger 从 1.00 → 1.05（若确认 trail 仍频繁导致 TP 偏低）

## 下次优先行动

1. **round40**：`_update_tp` ATR ratio 下界收紧
   - 文件：`quant/strategy/grid_pro.py` 第 1311 行
   - 改动：`_atr_ratio = max(0.8, min(1.3, ...))` → `max(0.85, min(1.3, ...))`
   - 理由：在极低ATR环境下（spacing < 0.8×baseline），RANGING TP被压至 0.64×tp_mult，
     直接导致 avg profit < 0.25 触发两级自适应——修复根源比靠自适应补偿更有效

2. **若实盘日志可访问**：验证 round38/39 改动实际命中频率

## 系统评估
- **策略有效性**：9/10
  - 39轮迭代；全P0/P1已解决；P2/P3改进积累中
  - trigger与offset两级自适应对称完成，极低利润场景保护更完整
  - 所有P3待验证项需实盘日志，沙盒环境无法直接监控
- **当前主要风险**：
  1. 外部API网络受限（沙盒环境，无实时市场监控）
  2. ATR联动min=0.8在极低ATR时将RANGING TP压至0.64×基准（下轮修复）
  3. 实盘日志无法访问（所有P3优化均未经实盘数据验证）
- **累计运行轮次**：39
