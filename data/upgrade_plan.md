# ETH量化系统升级计划

## 本次（2026-04-27 第四十七轮）完成

### grid_pro.py：_adaptive_trail_offset 第二低利润层阈值 0.30 → 0.35（round47）

**问题**：
- `_adaptive_trail_trigger` 第二低利润层：avg < 0.40 → trigger +0.10
- `_adaptive_trail_offset` 第二低利润层：avg < 0.30 → offset +0.03
- 不对称缺口：avg 在 [0.30, 0.40) 时 trigger 已放宽 +0.10，但 offset 完全不响应
- 逻辑矛盾：trail 需要更大价格超冲才启动（+0.10），但启动后 TP 落点距离不变
- 应有设计：trigger 放宽时 offset 应同步放宽，两者联动

**修复（round47）**：
```
_adaptive_trail_offset:
  第二低利润层阈值: avg < 0.30 → avg < 0.35
  效果: avg∈[0.30, 0.35) 时 offset 也放宽 +0.03（与 trigger 的 +0.10 联动）
```

**修复前后对比**：
| avg 范围       | trigger delta | offset delta（旧）| offset delta（新）|
|----------------|--------------|-------------------|-------------------|
| < 0.25         | +0.20        | +0.06             | +0.06（不变）     |
| [0.25, 0.30)   | +0.10        | +0.03             | +0.03（不变）     |
| **[0.30, 0.35)**| **+0.10**   | **0（不响应）**   | **+0.03（修复）** |
| [0.35, 0.40)   | +0.10        | 0                 | 0（仍存在小缺口） |
| [0.40, 0.80)   | 0            | 0                 | 0                 |
| (0.80, 1.00]   | -0.05        | -0.03             | -0.03（不变）     |
| > 1.00         | -0.10        | -0.05             | -0.05（不变）     |

**设计注记（round47 后）**：
- trigger 与 offset 现在在 avg < 0.35 以下均有响应（不对称缺口从 0.10 缩至 0.05）
- round48 可考虑将 offset 阈值进一步升至 0.40 以完全对齐（更激进，留待实盘数据验证）

**deque maxlen 分析（round47 顺带确认）**：
- `_tp_profits_ranging/trending` 各 maxlen=20，经分析足够
- RANGING 半衰期 900s（15min），1h 外数据权重 < 6%，实际有效样本仅最近 ~15-30 分钟
- 增大至 200 无统计收益，不改动（避免不必要代码变更）

---

## 历史完成

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

- [ ] P3: round48：评估 `_ewma_profit_avg` 启用门槛 `len(bucket) < 5` 是否可降至 3
  - 成交稀少时（如 TRENDING 每小时 1-2 笔），5 次门槛可能导致自适应长时间无法激活
  - 若降至 3：EWMA 在更少样本下激活，噪声稍增但覆盖面扩大
  - 权衡：3 vs 5，TRENDING 场景更可能受益（成交稀疏）

- [ ] P3: round49：完整 trigger/offset 对称性终审
  - 目标：offset 第二层 0.35 → 0.40（与 trigger 第二层 0.40 完全对齐）
  - 条件：若 round47 效果无负面反馈，可继续升级

- [ ] P3: 动态止盈：根据波动率进一步调整每格利润（现有 ATR ratio 联动，是否需进一步增强）

## 下次优先行动

1. **round48**：检查 `_ewma_profit_avg` 中 `len(bucket) < 5` 门槛
   - `grep -n 'len(bucket)\|< 5\|< 3' quant/strategy/grid_pro.py`
   - 若为 5：评估是否改为 3（TRENDING 成交稀疏场景受益）
   - 若已为 3：确认无需改动，记录

2. **观察**：`_adaptive_trail_offset` 第二层 0.30→0.35 对 avg∈[0.30,0.35) 的实际触发次数

## 系统评估
- **策略有效性**：9/10
  - 47轮迭代；全P0/P1已解决；trigger+offset 联动逻辑进一步对称化
  - 当前不对称缺口：[0.35, 0.40) 范围 trigger 仍放宽但 offset 不响应（缩小至 0.05 间距）
  - 完整对称方案（0.40 对齐）可作 round49 选项
- **当前主要风险**：
  1. 外部API网络受限（沙盒，无实时市场监控）
  2. 实盘日志无法访问（avg 实际分布未知，无法验证哪个层最常触发）
  3. 连续细化参数但缺乏实盘反馈，存在过拟合风险（虽然每次改动都有逻辑依据）
- **累计运行轮次**：47
