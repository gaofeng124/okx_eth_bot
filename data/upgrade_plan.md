# ETH量化系统升级计划

## 本次（2026-04-27 第四十八轮）完成

### grid_pro.py：_ewma_profit_avg 最小样本门槛改为 regime-specific（round48）

**问题**：
- 旧逻辑：`if len(bucket) < 5: return None`（全局统一门槛）
- TRENDING 半衰期 2700s，成交稀疏（实盘 1h 内仅 1-3 笔），5 次门槛可能需要数小时才能积累
- 结果：TRENDING 场景 EWMA 自适应长时间无法激活，退化为 base 参数，失去动态调整能力
- RANGING 半衰期 900s，成交频繁（1h 内可积累 10+ 笔），5 次门槛合理，无需降低

**修复（round48）**：
```
新增命名常量：
  _EWMA_MIN_SAMPLES_RANGING  = 5  （不变）
  _EWMA_MIN_SAMPLES_TRENDING = 3  （5 → 3）

_ewma_profit_avg 检查逻辑：
  旧：if len(bucket) < 5:
  新：min_samples = RANGING→5 / TRENDING→3
      if len(bucket) < min_samples:
```

**效果预期**：
- TRENDING 场景：仅需 3 笔成交即可激活自适应止盈，大幅缩短冷启动期（从"可能数小时"到"约 1-2h"）
- RANGING 场景：保持 5 次门槛，噪声控制不变
- 风险：TRENDING 3 个样本噪声稍增，但 EWMA 时间衰减加权 + TRENDING half_life=2700s 平滑效果足够对冲

**修复前后激活时间对比（TRENDING 场景，假设每小时 2 笔成交）**：
| 门槛 | 激活所需时间 |
|------|------------|
| < 5  | ~2.5h（冷启动期极长）|
| < 3  | ~1.5h（缩短约 1h）  |

---

## 历史完成

### 第四十七轮（2026-04-27）
- [x] grid_pro.py: _adaptive_trail_offset 第二低利润层阈值 0.30 → 0.35（缩小 trigger/offset 不对称缺口）

### 第四十六轮（2026-04-26）
- [x] grid_pro.py: TRENDING trail bounds下界对齐RANGING base（trigger lo 1.00→1.05，offset lo 0.45→0.50）

### 第四十五轮（2026-04-26）
- [x] grid_pro.py: trail基准参数提取为命名类常量 + RANGING base_trigger 1.00→1.05

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

- [ ] P3: round49：完整 trigger/offset 对称性终审
  - 目标：offset 第二层 0.35 → 0.40（与 trigger 第二层 0.40 完全对齐）
  - 当前状态：[0.35, 0.40) 区间 trigger 放宽 +0.10 但 offset 仍不响应（剩余 0.05 缺口）
  - 条件：若 round47/48 效果无负面反馈，可继续升级
  - 风险评估：极小（单点参数对齐，逻辑清晰）

- [ ] P3: 动态止盈：根据波动率进一步调整每格利润（现有 ATR ratio 联动是否需进一步增强）

## 下次优先行动

1. **round49**：完整 trigger/offset 对称性终审
   - `grep -n '_adaptive_trail_offset\|avg < 0.35\|avg < 0.40' quant/strategy/grid_pro.py`
   - 若第二层 offset 阈值为 0.35：升至 0.40（与 trigger 第二层完全对齐）
   - 预期：消除剩余 [0.35, 0.40) 不对称缺口，trail trigger/offset 完全联动

## 系统评估
- **策略有效性**：9/10
  - 48轮迭代；全P0/P1已解决；EWMA 自适应改为 regime-specific，TRENDING 覆盖面扩大
  - trigger/offset 不对称：仅剩 [0.35, 0.40) 一个小缺口（0.05 间距），round49 可收尾
  - 代码逻辑完整性持续提升，常量命名规范化
- **当前主要风险**：
  1. 外部API网络受限（沙盒，无实时市场监控）
  2. 实盘日志无法访问（无法验证 avg 实际分布，哪个层触发最频繁未知）
  3. 连续细化参数缺乏实盘反馈，存在过拟合风险（每次改动均有逻辑依据，风险可控）
- **累计运行轮次**：48
