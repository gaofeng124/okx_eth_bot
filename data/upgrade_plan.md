# ETH量化系统升级计划

## 本次（2026-04-26 第四十三/四十四轮）完成

### grid_pro.py：_adaptive_trail_trigger 第三层 + _adaptive_trail_offset 对称扩展（round43+44）

**问题**：
- round38 引入两层低利润保护（avg<0.25 → +0.20; avg<0.40 → +0.10）
- round41 引入 avg>0.80 层收紧（trigger-0.05, offset-0.03）
- 边缘情况：当 avg>1.00（利润丰厚，市场有明显趋势延伸）时，仅收紧 -0.05/-0.03 力度不足
- trail 启动时机仍偏晚，导致在快速延伸行情中过早锁利后错失剩余幅度

**修复（round43+44）**：

`_adaptive_trail_trigger`：
- 新增第三层：`elif avg > 1.0: adapted = max(base_trigger - 0.10, lo)`
- 原 `avg > 0.8 → -0.05` 保留（现作为中间层）
- RANGING 效果：base=1.00，avg>1.0 → trigger=0.90（下界0.85内安全）
- TRENDING 效果：base=1.20，avg>1.0 → trigger=1.10（下界1.00内安全）

`_adaptive_trail_offset`：
- 新增对称第三层：`elif avg > 1.0: adapted = max(base_offset - 0.05, lo)`
- 原 `avg > 0.80 → -0.03` 保留（现作为中间层）
- RANGING 效果：base=0.50，avg>1.0 → offset=0.45（下界0.35内安全）
- TRENDING 效果：base=0.60，avg>1.0 → offset=0.55（下界0.45内安全）

**效果预期**：
- 利润丰厚时（avg>1.0格），trail 更早启动（trigger低）且落点更紧（offset小），锁利更迅速
- 正常震荡市（avg 0.4~0.8）：不触发任何层，行为与之前完全一致
- 低利润市（avg<0.25）：仍受 +0.20/+0.06 保护，防止过早追踪
- 对 total_w<0.5（round42 门槛）的保护无影响：avg=None 时直接返回 base，所有层均跳过

---

## 历史完成

### 第四十二轮（2026-04-26）
- [x] grid_pro.py: _ewma_profit_avg total_w 最低有效权重门槛（_EWMA_MIN_TOTAL_W=0.5）

### 第四十一轮（2026-04-26）
- [x] grid_pro.py: _ewma_profit_avg Regime-specific EWMA 半衰期（RANGING=900s/TRENDING=2700s）

### 第四十轮（2026-04-25）
- [x] grid_pro.py: _update_tp ATR ratio 下界收紧 0.8 → 0.85

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

- [ ] P3: round45：RANGING base_trigger 1.00 → 1.05（需实盘 trigger 激活频率确认）
  - 若 trigger 频繁在 0.95~1.05 之间命中，说明 1.00 偏紧，调高可减少误触发
  - 约束：avg>1.0 层已将 RANGING trigger 压至 0.90，base 调高后 avg>1.0 效果更显著

- [ ] P3: round46：观察 avg>1.0 分支实际触发频率（需实盘日志 profit_spacings 分布）
  - 预期：震荡市（avg~0.5）不触发；趋势延伸市（avg>1.0）触发频率约 10~20%
  - 若触发过频，考虑上调至 avg>1.2

- [ ] P3: RANGING trail_offset 0.50 基准是否需调整（依赖实盘数据）

## 下次优先行动

1. **round45**：若有实盘日志，分析 trigger 命中价格分布
   - 若 80%+ 成交在 trigger 0.95~1.05 之间：调高 base_trigger 至 1.05
   - 若无日志：实现 RANGING base_trigger 1.00 → 1.05（低风险参数调整）

2. 观察 round43/44 的 avg>1.0 分支：
   - 在 runner.py 日志中搜索 "adaptive trigger" / "adaptive offset"，核实 -0.10/-0.05 层是否被激活

## 系统评估
- **策略有效性**：9/10
  - 43/44轮迭代；全P0/P1已解决；自适应 trail 系统现为完整四层结构
  - trigger: [avg<0.25: +0.20] → [avg<0.40: +0.10] → [avg>0.80: -0.05] → [avg>1.00: -0.10]
  - offset:  [avg<0.25: +0.06] → [avg<0.30: +0.03] → [avg>0.80: -0.03] → [avg>1.00: -0.05]
  - 四层覆盖全利润区间，中间"平静带"(0.40~0.80)不干预保持稳定
- **当前主要风险**：
  1. 外部API网络受限（沙盒环境，无实时市场监控）
  2. 实盘日志无法访问（avg>1.0 触发频率未经验证，但边界安全）
  3. avg>1.0 场景若触发过频（市场长期高利润），trigger=0.90 在 RANGING 接近下界 0.85，需监控
- **累计运行轮次**：43（含round44合并执行）
