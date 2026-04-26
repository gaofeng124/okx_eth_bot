# ETH量化系统升级计划

## 本次（2026-04-26 第四十一轮）完成

### grid_pro.py：_ewma_profit_avg Regime-specific EWMA 半衰期（P3）

**问题**：
- round38/39 两级自适应依赖 `_ewma_profit_avg()` 的 EWMA 利润平均值
- 原实现对所有 Regime 使用固定 1800s（30min）半衰期
- RANGING 震荡市：TP 成交频繁（每几分钟一次），1800s 半衰期导致响应迟钝
  - 30min 前的旧数据仍有 50% 权重，在震荡市可能拖慢自适应对利润变化的反应
- TRENDING 趋势市：TP 成交稀疏（每10-30min甚至更久），1800s 半衰期可能过短
  - 5次样本可能分布在几小时内，短半衰期会大幅降低较旧样本权重，样本有效性降低

**修复（round41）**：
- `lam = math.log(2.0) / self._PROFIT_HALF_LIFE` → `half_life = 900 if RANGING else 2700`
- RANGING  → 900s（15min）：震荡市快速适应，近15min数据权重≥50%
- TRENDING → 2700s（45min）：趋势市平滑，近45min数据权重≥50%，避免少量异常fill扰动

**额外清理**：
- 修正 `_maybe_trail_tp` docstring 中严重过时的旧参数值
  - RANGING trail_offset 文档值 0.15 → 实际值 0.50（round22 Direction A 修复后未更新）
  - RANGING trail_trigger 文档值 0.30 → 实际值 1.00

**效果预期**：
- RANGING 市：自适应 trigger/offset 对近期利润低谷/高峰响应速度提升 2x
- TRENDING 市：自适应更平滑，避免单次极端 fill 扭曲 EWMA
- avg<0.25 极低利润分支：在 RANGING 市下可更快识别并响应（900s vs 1800s）

---

## 历史完成

### 第四十轮（2026-04-25）
- [x] grid_pro.py: _update_tp ATR ratio 下界收紧 0.8 → 0.85（极低ATR时TP提升6.25%）

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

- [ ] P3: round42：评估 _tp_profits_ranging maxlen=20 在 RANGING 高频成交下是否充足
  - 场景：每小时成交6次 → 20条数据 = 3.3小时；900s半衰期有效窗口约1.5小时（4.5半衰期）
  - 若3.3小时内有≥20条数据，最旧数据已被剔除（超出bucket），但时间衰减已很低（<1%权重）
  - 评估：maxlen=20 对 RANGING 市是否需要提升到 25 或 30

- [ ] P3: round43：若有实盘日志，验证 avg<0.25 在 RANGING 下新半衰期（900s）命中频率
  - 方法：比较 round40 前（1800s）vs round41 后（900s）的 adaptive log 条目

- [ ] P3: RANGING base_trigger 1.00 → 1.05（需实盘数据确认 trail 触发率后决定）

- [ ] P3: _maybe_trail_tp 的 RANGING trail_offset 基准（0.50）是否过宽/过紧
  - 若 TP 被追踪后频繁被回撤吃掉，考虑 0.50 → 0.55
  - 需实盘 profit_spacings 分布数据

## 下次优先行动

1. **round42**：评估 `_tp_profits_ranging` maxlen 是否应从 20 升至 25
   - 逻辑：900s 半衰期 × 3 = 2700s 有效窗口；若 RANGING 下每15min一次TP = 3次/小时
   - 2700s内最多9条数据，maxlen=20 富余；若每5min一次 = 12次/小时，20条 = 100min，OK
   - 结论：当前 maxlen=20 在合理成交频率下足够；暂缓

2. **round42 候选**：检查 `_restore_atr_baseline` 逻辑，确认冷启动时 ATR 基线恢复正确
   - 若基线为0且当前格宽极低（DEAD regime），ratio=0.85（下界），TP偏低
   - 验证：启动时第一次 _place_grid 前 _atr_baseline 是否已正确恢复

## 系统评估
- **策略有效性**：9/10
  - 41轮迭代；全P0/P1已解决；P3精细化积累中
  - round41 Regime-specific EWMA 半衰期是自适应系统的底层参数优化
  - 所有P3待验证项需实盘日志，沙盒环境无法直接监控
- **当前主要风险**：
  1. 外部API网络受限（沙盒环境，无实时市场监控）
  2. 实盘日志无法访问（所有P3优化均未经实盘数据验证）
  3. RANGING 900s 半衰期在极低频成交市场（<5次/小时）可能导致 bucket 样本老化过快
- **累计运行轮次**：41
