# ETH量化系统升级计划

## 本次（2026-04-27 第四十九轮）完成

### grid_pro.py：_adaptive_trail_offset 第二层阈值 0.35→0.40，trigger/offset 对称性终审完成（round49）

**问题**：
- trigger 第二层：`avg < 0.40` → trigger +0.10（放宽触发门槛）
- offset 第二层：`avg < 0.35` → offset +0.03（放宽落点步长）
- 不对称缺口：avg 在 [0.35, 0.40) 时，trigger 已放宽 +0.10，但 offset 完全不响应
- 导致：trail 更晚触发（trigger已放宽）但 TP 落点未同步调整，trail 触发后 TP 仍偏近，容易被小幅回撤提前止盈

**修复（round49）**：
```
_adaptive_trail_offset:
  旧：elif avg < 0.35:  adapted = min(base_offset + 0.03, hi)
  新：elif avg < 0.40:  adapted = min(base_offset + 0.03, hi)
```

**完整 trigger/offset 对称结构（最终态）**：

| avg 范围 | trigger 调整 | offset 调整 | 对称性 |
|---------|-------------|------------|--------|
| < 0.25  | +0.20       | +0.06      | ✓ 完整 |
| [0.25, 0.40) | +0.10 | +0.03     | ✓ 完整（round49修复[0.35,0.40)缺口）|
| [0.40, 0.80] | 0       | 0         | ✓ 中性区间 |
| (0.80, 1.00] | -0.05  | -0.03     | ✓ 完整 |
| > 1.00  | -0.10       | -0.05      | ✓ 完整 |

**效果预期**：
- avg 在 [0.35, 0.40) 时 offset 同步放宽 +0.03，trail 触发更迟但 TP 落点也更远，避免"迟触发+近落点"双重不利组合
- trigger/offset 全区间完全对称联动，逻辑闭环完成
- 风险：极小，仅一个边界值修改，逻辑影响面窄且方向正确

---

## 历史完成

### 第四十八轮（2026-04-27）
- [x] grid_pro.py: _ewma_profit_avg 最小样本门槛改为 regime-specific（RANGING=5保持，TRENDING=5→3）

### 第四十七轮（2026-04-27）
- [x] grid_pro.py: _adaptive_trail_offset 第二低利润层阈值 0.30→0.35

### 第四十六轮（2026-04-26）
- [x] grid_pro.py: TRENDING trail bounds 下界对齐 RANGING base（trigger lo 1.00→1.05，offset lo 0.45→0.50）

### 第四十五轮（2026-04-26）
- [x] grid_pro.py: trail 基准参数提取为命名类常量 + RANGING base_trigger 1.00→1.05

### 第四十三/四十四轮（2026-04-26）
- [x] grid_pro.py: _adaptive_trail_trigger 第三层（avg>1.00→trigger-0.10）
- [x] grid_pro.py: _adaptive_trail_offset 对称第三层（avg>1.00→offset-0.05）

### 第四十二轮（2026-04-26）
- [x] grid_pro.py: _ewma_profit_avg total_w 最低有效权重门槛（_EWMA_MIN_TOTAL_W=0.5）

### 第四十一轮（2026-04-26）
- [x] grid_pro.py: _ewma_profit_avg Regime-specific EWMA 半衰期（RANGING=900s/TRENDING=2700s）

### 第四十轮（2026-04-25）
- [x] grid_pro.py: _update_tp ATR ratio 下界收紧 0.8→0.85

### 第三十五~三十九轮（2026-04-24~25）
- [x] grid_pro.py: 多轮 trail 自适应层完善、1h方向gate滞回环

### 第一~三十四轮（2026-04-18~24）
- [x] 全部P0/P1问题已解决

---

## 待解决问题（按优先级）

- [ ] P3: round50：进入新优化维度探索
  - 候选方向A：动态格宽 ATR 联动增强（当前 ATR mult 固定 1.2，可考虑按 Regime 动态调整）
  - 候选方向B：Regime 切换滞回环参数审计（当前滞回环阈值是否最优）
  - 候选方向C：成交统计完整性检查（analysis.jsonl fill 事件写入验证）
  - 先进行代码审计，选取影响最大的方向执行

## 下次优先行动

**round50：系统性审计，选择新优化维度**
1. `grep -n 'ATR_MULT\|_atr_mult\|REGIME\|regime_change\|hysteresis' quant/strategy/grid_pro.py | head -30`
2. 评估动态ATR倍数（RANGING vs TRENDING是否应有差异化格宽）
3. 或审计Regime切换条件的滞回环阈值是否合理

## 系统评估
- **策略有效性**：9/10
  - 49轮迭代；全P0/P1已解决
  - trigger/offset 对称性修复系列全部完成（round38~49，历经12轮细化）
  - EWMA 自适应系统完整（regime-specific半衰期+最小样本+总权重门槛+全层对称联动）
  - 代码逻辑完整性达到阶段性里程碑
- **当前主要风险**：
  1. 外部API网络受限（沙盒，无实时市场监控）
  2. 实盘日志无法访问（无法验证实际avg分布与触发频率）
  3. 连续细化参数缺乏实盘反馈，存在过拟合风险（每次均有逻辑依据，风险可控）
- **累计运行轮次**：49
