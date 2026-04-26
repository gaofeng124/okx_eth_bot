# ETH量化系统升级计划

## 本次（2026-04-26 第四十六轮）完成

### grid_pro.py：TRENDING trail bounds 下界对齐 RANGING base（round46）

**问题**：
- `_adaptive_trail_trigger` TRENDING lo=1.00，`_adaptive_trail_offset` TRENDING lo=0.45
- 当前实际最低值为 trigger=1.10 / offset=0.55（均高于旧lo），lo 约束从未生效
- 但缺乏明确的设计不变式：TRENDING trail 参数调整时，无理论下界保证其不比 RANGING 基线更激进
- 若未来将 `_TRENDING_TRAIL_BASE_TRIGGER` 降低（如调至 1.10），旧 lo=1.00 会允许
  avg>1.0 时 trigger 降到 1.00，比 RANGING 基线 1.05 更激进，违反设计意图

**修复（round46）**：

```
_adaptive_trail_trigger:
  TRENDING bounds: [1.00, 1.50] → [1.05, 1.50]
  lo = _RANGING_TRAIL_BASE_TRIGGER = 1.05（精确对齐）

_adaptive_trail_offset:
  TRENDING bounds: [0.45, 0.75] → [0.50, 0.75]
  lo = _RANGING_TRAIL_BASE_OFFSET = 0.50（精确对齐）
```

**设计不变式（round46 后）**：
> TRENDING trail trigger ≥ RANGING base trigger（1.05）
> TRENDING trail offset ≥ RANGING base offset（0.50）
> 即：趋势行情下的 trail 激进程度永远不超过震荡行情基线

**完整 bounds 表（round46 后）**：
| Regime   | 参数    | lo   | base | hi   |
|----------|---------|------|------|------|
| RANGING  | trigger | 0.85 | 1.05 | 1.25 |
| RANGING  | offset  | 0.35 | 0.50 | 0.65 |
| TRENDING | trigger | 1.05 | 1.20 | 1.50 |
| TRENDING | offset  | 0.50 | 0.60 | 0.75 |

TRENDING lo 现在精确等于对应 RANGING base，形成清晰的层级结构。

**当前行为影响**：无变化（实际值 trigger≥1.10, offset≥0.55 均高于新lo）。
**前向保护效果**：若 `_TRENDING_TRAIL_BASE_TRIGGER` 未来降至 1.10，
avg>1.0 时: max(1.10-0.10, 1.05)=1.05（旧版会到 1.00，新版 floor 在 1.05）。

---

## 历史完成

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

- [ ] P3: round47：若有实盘日志，分析 `adaptive trigger` 日志中 avg 分布
  - 重点：avg>1.0 层触发了多少次 vs 总trail次数
  - 若 avg>1.0 从未触发（avg 都 < 0.80）：trail 基本都是 base 触发，不需要 -0.10 层
  - 若无日志：评估 `_adaptive_trail_offset` 第二低利润层阈值 0.30→0.35（对齐 trigger 的 0.40→更近）

- [ ] P3: round48：EWMA bucket 容量上限（deque maxlen）是否合理？
  - 当前可能无限增长，极长运行后旧数据权重趋 0 但仍占内存
  - 若确认无 maxlen，设置 maxlen=200（~5-10 天数据）

- [ ] P3: 动态止盈：根据波动率进一步调整每格利润（现有 ATR ratio 联动，是否需进一步增强）

## 下次优先行动

1. **round47**：检查 `_tp_profits_ranging` / `_tp_profits_trending` 的 deque maxlen
   - `grep -n 'deque\|maxlen\|_tp_profits' quant/strategy/grid_pro.py`
   - 若无 maxlen：加 maxlen=200 防内存泄漏
   - 若已有：确认大小合理（>100 for RANGING, 可较小 for TRENDING）

2. **观察**：`_RANGING_TRAIL_BASE_TRIGGER=1.05` 后 neutral 区 trail 总触发次数变化

## 系统评估
- **策略有效性**：9/10
  - 46轮迭代；全P0/P1已解决；trail 参数结构化为类常量+bounds 层级清晰
  - TRENDING lo 现精确对齐 RANGING base，建立设计不变式
  - trigger四层: [1.25] → [1.15] → [1.05 neutral] → [1.00] → [0.95]（RANGING）
  - offset四层:  [0.56] → [0.53] → [0.50 neutral] → [0.47] → [0.45]（RANGING）
- **当前主要风险**：
  1. 外部API网络受限（沙盒，无实时市场监控）
  2. 实盘日志无法访问（avg 实际分布未知）
  3. 若实盘中 avg 长期 < 0.25，trail 基本处于最放松状态（trigger 1.25），
     TP 可能在自然成交前就因极端行情触发 trail 后也需要很大超冲
- **累计运行轮次**：46
