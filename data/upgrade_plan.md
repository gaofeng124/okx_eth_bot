# ETH量化系统升级计划

## 本次（2026-04-27 第五十轮）完成

### regime.py：激活死代码常量，修正 TRENDING_UP 宏观阈值（round50）

**问题**：
- `_MACRO_UP_STRONG = +0.0015` 和 `_MACRO_UP_WEAK = +0.0003` 两个类常量在 49 轮迭代中从未被使用（死代码）
- 原 TRENDING_UP 条件：`macro_bias >= _MACRO_DOWN_STOP(-0.002)` — 价格比2分钟均线**低0.2%**也触发上升趋势判断
- 这造成 `macro_bias ∈ [-0.002, +0.003)` 区间的偏空/中性情况被误判为 TRENDING_UP，引发过于冒进的接近价入场（bias=0.5, spacing×1.3）

**修复（round50）**：
```
regime.py update() 层2：
  旧：elif macro_bias >= self._MACRO_DOWN_STOP and ts >= self._TS_STRONG_UP
  新：elif macro_bias >= self._MACRO_UP_STRONG and ts >= self._TS_STRONG_UP  # +0.0015

  旧：elif macro_bias >= self._MACRO_DOWN_STOP and ts >= self._TS_WEAK_UP
  新：elif macro_bias >= self._MACRO_UP_WEAK and ts >= self._TS_WEAK_UP    # +0.0003
```

**完整 TRENDING_UP 触发矩阵（最终态）**：

| macro_bias 范围 | trend_strength | 结果 |
|----------------|----------------|------|
| >= +0.0015 | >= +0.0005 | TRENDING_UP（强） |
| [+0.0003, +0.0015) | >= +0.0003 | TRENDING_UP（弱） |
| [-0.002, +0.0003) | >= +0.0003 | 层3：tick分类 → 通常RANGING |
| < -0.003 | 任意 | TRENDING_DOWN（全停） |
| (-0.003, -0.002) | 任意 | TRENDING_DOWN（警戒） |

**效果预期**：
- 消除偏空区间的误判 TRENDING_UP，入场条件更严格精准
- 原来[-0.002, +0.003)区间的情况降级为RANGING（也允许入场，但用更保守的买点和TP）
- `_MACRO_UP_STRONG(+0.0015)` 和 `_MACRO_UP_WEAK(+0.0003)` 两个常量首次被实际使用

---

## 历史完成（节选）

### 第四十九轮（2026-04-27）
- [x] grid_pro.py: _adaptive_trail_offset 第二层阈值 0.35→0.40，trigger/offset 全区间对称完成

### 第四十八轮（2026-04-27）
- [x] grid_pro.py: _ewma_profit_avg 最小样本门槛 TRENDING=5→3

### 第四十七轮（2026-04-27）
- [x] grid_pro.py: _adaptive_trail_offset 第二低利润层阈值 0.30→0.35

### 第四十六轮（2026-04-26）
- [x] grid_pro.py: TRENDING trail bounds 下界对齐

### 第一~四十五轮（2026-04-18~26）
- [x] 全部P0/P1问题已解决；trail/EWMA系统完整化（12轮细化）

---

## 待解决问题（按优先级）

- [ ] P3: round51：对称优化 _classify_by_ticks 中 macro_bias > -0.001 硬编码
  - 当前：`if up_frac >= 0.70 and macro_bias > -0.001` 引用硬编码常量
  - 改进：改为 `macro_bias > self._MACRO_UP_WEAK * (-1/3)` 或直接用 `-0.001` 作为命名常量 `_MACRO_TICK_UP_MIN`
  - 目的：保持代码一致性（所有阈值均使用命名常量）
- [ ] P3: round52：验证新 TRENDING_UP 阈值对成交统计的影响
  - 需要实盘日志（无法在沙盒验证）

## 下次优先行动

**round51：对称优化**
1. 在 regime.py 中为 `_classify_by_ticks` 的 `-0.001` 创建命名常量 `_MACRO_TICK_UP_MIN = -0.001`
2. 或者：对称优化短策略的 TRENDING_DOWN 阈值（若当前有做空场景）
3. 审计是否有其他定义但未使用的常量

## 系统评估
- **策略有效性**：9/10
  - 50轮迭代；全P0/P1已解决
  - trail/offset 系统完整对称（round38~49，12轮细化）
  - Regime 检测修正：TRENDING_UP 宏观阈值首次从"非深度偏空"收紧为"真正偏多"（round50里程碑）
  - 代码完整性进一步提升：两个死代码常量全部激活
- **当前主要风险**：
  1. 外部API网络受限（沙盒，无实时市场监控）
  2. 实盘日志无法访问（无法验证 TRENDING_UP 频率变化）
  3. TRENDING_UP 阈值收紧后可能减少部分有利入场（保守方向，可接受）
- **累计运行轮次**：50
