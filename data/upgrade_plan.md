# ETH量化系统升级计划

## 本次（2026-04-25 第三十八轮）完成

### grid_pro.py：_adaptive_trail_trigger 两级自适应（P3）

**问题**：`_adaptive_trail_trigger` 仅有单级判断（avg<0.4 → +0.10），无法区分"利润偏低"与"利润极低"两种场景：
- **avg 0.25-0.40**：利润偏低，+0.10步长较合适
- **avg < 0.25**：利润极低（TP被trail拉到极近位置），+0.10步长明显不足，trail连续发生后trigger仍只到1.10，无法有效延迟

**修复（round38）**：
- 新增 `avg < 0.25` 层级：步长 +0.20（vs 原 +0.10）
- RANGING hi 上界：1.20 → 1.25（为 base=1.00 + step=0.20 = 1.20 留有余量，避免立即贴顶）
- TRENDING 边界不变（[1.00, 1.50]，base=1.20+0.20=1.40 仍在范围内）
- 原有 avg<0.40 判断保留（avg 0.25-0.40 范围仍 +0.10，行为不变）

**效果预期**：
- 极低利润场景（avg<0.25）：trigger从1.10→1.20，需价格超出TP 1.2格宽才启动trail（vs 原1.1格宽）
- 中低利润场景（avg 0.25-0.40）：trigger=1.10，与修改前完全一致，零影响
- 高利润场景（avg>0.80）：trigger=0.95，不变
- 冷启动期（<5次成交）：自适应不激活，不影响

---

## 历史完成

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

- [ ] P3: 验证avg<0.25层级实际命中频率（round38新增）
  - 方法：`grep 'adaptive trigger.*0\.20\b\|avg_profit=0\.[01]' data/logs/*.log`
  - 预期：若极低利润场景存在，每天应有数次命中记录

- [ ] P3: 验证gate日志节流实际效果（round36）
  - 方法：`grep '1h-drop-gate\|1h-rise-gate' data/logs/*.log | wc -l`

- [ ] P3: 验证profit_spacings EWMA均值分布
  - 方法：从analysis.jsonl提取fill_tp的profit_spacings均值
  - 若<0.25持续 → round38修改直接生效（trigger升至1.20）
  - 若>0.40持续 → 说明TP定价合理，此改动处于待机状态

- [ ] P3: ATR联动TP atr_ratio下界调整候选
  - 当前：`max(0.8, min(1.3, spacing/baseline))`，下界0.8允许TP缩至0.64×（RANGING×0.8×0.8）
  - 若极低利润场景确认由ATR联动过度缩减TP导致：下界0.8→0.85（修复优先级提升至P2）

## 下次优先行动

1. **若能访问实盘日志**：
   - 确认 avg<0.25 层级命中率：`grep 'adaptive trigger' logs/*.log | grep '0\.20'`
   - 确认 profit_spacings 实际分布均值（RANGING bucket）
   - 若均值持续 <0.25：下界触发有效，继续观察
   - 若均值 0.4-0.8：系统处于正常状态，两级自适应处于待机

2. **P3候选（若avg<0.25频繁且ATR联动是根因）**：
   - `_update_tp` 中 `max(0.8, ...)` → `max(0.85, ...)`（ATR联动下界收紧，防止极低ATR时TP被压至0.64×）

3. **P3候选（若RANGING利润持续低）**：
   - RANGING的base_trigger从1.00→1.05（提高基础门槛，但需更多数据支撑）

## 系统评估
- **策略有效性**：9/10
  - 38轮迭代；全P0/P1已解决；P2/P3改进积累中
  - 两级自适应触发器：为极低利润场景提供额外保护
  - 所有P3待验证项需实盘日志，沙盒环境无法直接监控
- **当前主要风险**：
  1. 外部API网络受限（沙盒环境，无实时市场监控）
  2. ATR联动可能在低波动期将TP压缩过低（atr_ratio min=0.8 × RANGING 0.8 = 0.64×TP）
  3. 实盘日志无法访问（所有P3优化均未经实盘数据验证）
- **累计运行轮次**：38
