# ETH量化系统升级计划

## 本次（2026-04-27 第五十一轮）完成

### regime.py：提取 _MACRO_TICK_UP_MIN 命名常量（round51）

**问题**：
- `_classify_by_ticks` 第215行使用硬编码 `-0.001`，是 regime.py 中最后一个宏观偏差魔法数字
- 所有其他阈值（`_MACRO_UP_STRONG`, `_MACRO_UP_WEAK`, `_MACRO_DOWN_STOP`, `_MACRO_DOWN_KILL`）均已有命名常量
- 代码审查时无法直接理解此值的含义及其与其他阈值的位置关系

**修复（round51）**：
```python
# 新增类常量（位于_MACRO_DOWN_STOP下方）：
_MACRO_TICK_UP_MIN = -0.0010  # tick分类中 TRENDING_UP 的最低宏观偏差门槛（介于DOWN_STOP与UP_WEAK之间）

# _classify_by_ticks 第215行：
旧：if up_frac >= self._TREND_TICK_FRAC and macro_bias > -0.001:
新：if up_frac >= self._TREND_TICK_FRAC and macro_bias > self._MACRO_TICK_UP_MIN:
```

**完整宏观偏差常量体系（最终态）**：

| 常量 | 值 | 语义 |
|------|-----|------|
| `_MACRO_UP_STRONG` | +0.0015 | 强上涨触发 TRENDING_UP |
| `_MACRO_UP_WEAK` | +0.0003 | 弱上涨触发 TRENDING_UP |
| `_MACRO_TICK_UP_MIN` | -0.0010 | tick分类允许 TRENDING_UP 的最低偏差 |
| `_MACRO_DOWN_STOP` | -0.0020 | 下跌警戒区下界 |
| `_MACRO_DOWN_KILL` | -0.0030 | 下跌全停触发点 |

**效果预期**：
- regime.py 宏观偏差阈值体系完全命名化，无任何魔法数字
- 维护者可一眼看到 -0.0010 介于 DOWN_STOP(-0.002) 和 UP_WEAK(+0.0003) 之间，理解其"中间警戒区"含义
- 行为逻辑不变（等价替换）

---

## 历史完成（节选）

### 第五十轮（2026-04-27）
- [x] regime.py: 激活死代码常量，TRENDING_UP宏观阈值从 -0.002 收紧为 +0.0003/+0.0015

### 第四十九轮（2026-04-27）
- [x] grid_pro.py: _adaptive_trail_offset 第二层阈值 0.35→0.40，trigger/offset 全区间对称完成

### 第四十八轮（2026-04-27）
- [x] grid_pro.py: _ewma_profit_avg 最小样本门槛 TRENDING=5→3

### 第一~四十七轮（2026-04-18~26）
- [x] 全部P0/P1问题已解决；trail/EWMA系统完整化（12轮细化）；Regime检测修正

---

## 待解决问题（按优先级）

- [ ] P3: round52：审计 _VOL_HIGH(0.0032) 与 SP_VOL_CEIL(0.0028) 的对齐关系
  - `_VOL_HIGH = 0.0032` 注释说"当前SP_VOL_CEIL=0.0028"，两者差距0.04%
  - 若 SP_VOL_CEIL 是触发 VOLATILE 的前置条件，则 _VOL_HIGH 应 <= SP_VOL_CEIL 以避免逻辑断层
  - 需要查看 grid_pro.py 中 rel_vol 的计算和 SP_VOL_CEIL 的实际用途
- [ ] P3: round53：检查 _classify_by_ticks 中 (1 - _TREND_TICK_FRAC) 是否需要对称常量
  - 当前：`if up_frac <= (1 - self._TREND_TICK_FRAC)` = 0.30
  - 下跌tick比例阈值与上涨共享同一常量，天然对称，可接受
  - 若未来需要非对称阈值（如下跌更灵敏），可提取 `_TREND_TICK_FRAC_DOWN`

## 下次优先行动

**round52：SP_VOL_CEIL 与 _VOL_HIGH 对齐审计**
1. 读取 quant/strategy/grid_pro.py 中 SP_VOL_CEIL 和 rel_vol 的使用场景
2. 读取 quant/settings.py 中 SP_VOL_CEIL 当前值
3. 判断 _VOL_HIGH(0.0032) 是否需要调整（若SP_VOL_CEIL已变更，或两者语义存在逻辑冲突）
4. 若确认有问题，更新 _VOL_HIGH 或注释

## 系统评估
- **策略有效性**：9/10
  - 51轮迭代；全P0/P1已解决
  - trail/offset 系统完整对称（round38~49，12轮细化）
  - Regime 检测：TRENDING_UP 阈值修正（round50）；宏观偏差常量全命名化（round51）
  - 代码完整性达到历史最高：regime.py 零魔法数字
- **当前主要风险**：
  1. 外部API网络受限（沙盒，无实时市场监控）
  2. 实盘日志无法访问（无法验证 TRENDING_UP 频率变化）
  3. _VOL_HIGH 与 SP_VOL_CEIL 潜在语义对齐问题（待round52确认）
- **累计运行轮次**：51
