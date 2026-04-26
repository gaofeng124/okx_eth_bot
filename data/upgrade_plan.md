# ETH量化系统升级计划

## 本次（2026-04-26 第四十五轮）完成

### grid_pro.py：trail基准参数提取为命名类常量 + RANGING base_trigger 1.00→1.05（round45）

**问题**：
- `_maybe_trail_tp` 中 `1.00 / 1.20 / 0.50 / 0.60` 四个 inline 字面量散落代码，
  每次调参须同时修改代码+注释+docstring，易遗漏导致不一致
- RANGING base_trigger=1.00 时，在 neutral 区（avg 0.40~0.80）trail 触发条件为
  "price > TP + 1.00×spacing"，即价格超过 TP 恰好一个格宽就触发 trail
  → 在震荡行情中短暂超冲（+1.0格）后立刻回落的情形会触发 trail，把TP拉低，
    导致本可自然成交的TP被提前以较低价格成交

**修复（round45）**：

新增四个类常量（统一管理）：
```python
_RANGING_TRAIL_BASE_TRIGGER  = 1.05   # round45: 1.00→1.05
_RANGING_TRAIL_BASE_OFFSET   = 0.50
_TRENDING_TRAIL_BASE_TRIGGER = 1.20
_TRENDING_TRAIL_BASE_OFFSET  = 0.60
```

`_maybe_trail_tp` 改用常量引用，不再用 inline 数字。

**完整 RANGING adaptive trigger 映射（round45 后）**：
| avg 范围    | 最终 trigger                        | 含义                      |
|------------|-------------------------------------|---------------------------|
| avg < 0.25 | min(1.05+0.20, 1.25) = **1.25**     | 极低利润→trail极晚触发    |
| avg < 0.40 | min(1.05+0.10, 1.25) = **1.15**     | 低利润→trail延迟触发      |
| 0.40~0.80  | **1.05** (base，neutral zone)       | 正常→比旧值多5%缓冲       |
| avg > 0.80 | max(1.05-0.05, 0.85) = **1.00**     | 充足利润→trail与旧base持平|
| avg > 1.00 | max(1.05-0.10, 0.85) = **0.95**     | 丰厚利润→激进trail        |

效果：neutral 区 trigger 从 1.00→1.05，减少短暂超冲误触发；
      高利润区 avg>0.80 的 trigger 从 0.95→1.00，轻微保守（但 avg>1.0 仍激进 0.95）。

---

## 历史完成

### 第四十三/四十四轮（2026-04-26）
- [x] grid_pro.py: _adaptive_trail_trigger 第三层（avg>1.00→trigger-0.10）
- [x] grid_pro.py: _adaptive_trail_offset 对称第三层（avg>1.00→offset-0.05）

### 第四十二轮（2026-04-26）
- [x] grid_pro.py: _ewma_profit_avg total_w 最低有效权重门槛（_EWMA_MIN_TOTAL_W=0.5）

### 第四十一轮（2026-04-26）
- [x] grid_pro.py: _ewma_profit_avg Regime-specific EWMA 半衰期（RANGING=900s/TRENDING=2700s）

### 第四十轮（2026-04-25）
- [x] grid_pro.py: _update_tp ATR ratio 下界收紧 0.8 → 0.85

### 第三十五~三十九轮（2026-04-24~25）
- [x] grid_pro.py: 多轮 trail 自适应层完善、1h方向gate滞回环

### 第一~三十四轮（2026-04-18~24）
- [x] 全部P0/P1问题已解决

---

## 待解决问题（按优先级）

- [ ] P3: round46：观察 avg>1.0 层实际触发频率
  - 若触发频率 < 10%（avg 很少超过 1.0），考虑将阈值降至 avg>0.90 以更积极捕获
  - 若触发过频（>30%），可考虑上调至 avg>1.2
- [ ] P3: round47：评估 neutral trigger 1.05 效果
  - 观察实盘 "adaptive trigger: base=1.05 → X.XX" 日志，确认 avg 0.40~0.80 的频率
  - 若 neutral 区 trail 仍频繁（avg 0.60~0.80 段），可考虑再升至 1.08~1.10
- [ ] P3: RANGING trail_offset 0.50 基准待实盘数据验证
- [ ] P3: 动态止盈：根据波动率调整每格利润（已有 ATR ratio 联动，是否需进一步增强）

## 下次优先行动

1. **round46**：若有实盘日志，分析 `adaptive trigger` 日志中 avg 分布
   - 重点：avg>1.0 层触发了多少次 vs 总trail次数
   - 若 avg>1.0 从未触发：trail 在正常行情中都是 base 触发，1.05 够用
   - 若无日志：实施 TRENDING trigger bounds 收紧（lo 1.00→1.05）以与 RANGING 对称

2. **观察**：`_RANGING_TRAIL_BASE_TRIGGER=1.05` 后 trail 总触发次数变化
   - 预期：neutral 区 trail 次数↓ 10~15%，TP 自然成交率↑

## 系统评估
- **策略有效性**：9/10
  - 45轮迭代；全P0/P1已解决；trail 参数结构化为类常量
  - trigger四层: [1.25] → [1.15] → [1.05 neutral] → [1.00] → [0.95]
  - offset四层:  [0.56] → [0.53] → [0.50 neutral] → [0.47] → [0.45]
- **当前主要风险**：
  1. 外部API网络受限（沙盒，无实时市场监控）
  2. 实盘日志无法访问（avg>1.0 分支触发频率未经验证）
  3. base_trigger 1.05 在 avg>0.80 层给 1.00（与旧 neutral base 相同），
     如果 avg>0.80 市场实际上很少触发 trail，这是合理的防御
- **累计运行轮次**：45
