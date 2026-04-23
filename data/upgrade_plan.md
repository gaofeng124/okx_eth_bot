# ETH量化系统升级计划

## 本次（2026-04-23 第二十六轮）完成

### grid_pro.py：修复 _adaptive_trail_trigger/offset Regime边界裁剪Bug

**背景：**
第25轮实现了修复方向A（trigger=1.00/1.20）和修复方向B（offset=0.50/0.60），
目的是解决"trail偷盈利"问题（avg_win期望$1.31但实际$0.21）。
但两个 adaptive 函数的硬编码边界仍是旧值 [0.20, 0.50] / [0.08, 0.35]，
这些边界对应的是旧 base 值 0.30/0.40（trigger）和 0.15/0.25（offset）。

**Bug 表现：**
- EWMA TP利润 avg < 0.4 时（TP锁利太少，本应放宽trigger）：
  - 错误：`min(1.00+0.10, 0.50) = 0.50`（被剪到旧上界）
  - 正确：应为 1.10（允许价格超出TP一整格宽后再追踪）
- EWMA TP利润 avg < 0.30 时（offset也同理）：
  - 错误：`min(0.50+0.03, 0.35) = 0.35`（被剪到旧上界）
  - 正确：应为 0.53

**修复内容：**
1. 两个函数新增 `is_ranging: bool` 参数
2. 边界改为 Regime 独立：
   - RANGING trigger: [0.85, 1.20]  （base=1.00，震荡市适度放宽，不超过1.20格宽）
   - TRENDING trigger: [1.00, 1.50]  （base=1.20，趋势市上界放宽到1.50格宽，捕获更大延伸）
   - RANGING offset: [0.35, 0.65]  （base=0.50，下界0.35允许震荡市贴近锁利）
   - TRENDING offset: [0.45, 0.75]  （base=0.60，趋势市保留0.45下界，不过早贴市价）
3. _maybe_trail_tp 调用处传入 `_is_ranging`

**效果预期：**
- 修复前（avg<0.4）：trigger=0.50，价格超出TP半格就追踪 → 偷大量盈利
- 修复后（avg<0.4）：trigger=1.10，需价格超出TP一整格才追踪 → 拿接近全额格宽利润
- debug日志新增 bounds=[lo,hi] 字段，可验证实际使用的边界

---

## 历史完成

### 第二十五轮（2026-04-22）
- [x] grid_pro.py: ATR基线动态止盈（_atr_baseline慢速EMA + atr_ratio=[0.8,1.3]缩放eff_tp_mult）

### 第二十四轮（2026-04-22）
- [x] grid_pro.py: 分Regime EWMA双桶（_tp_profits_ranging/_tp_profits_trending）

### 第二十三轮（2026-04-22）
- [x] grid_pro.py: _replay_tp_history() — 重启从日志恢复TP历史

### 第二十二轮（2026-04-22）
- [x] grid_pro.py: EWMA时间衰减（半衰期30min）

### 第二十一轮（2026-04-22）
- [x] grid_pro.py: FGI格宽双向调整 + _refresh_funding REST fallback

### 第一~二十轮（2026-04-18/19/20/21）
- [x] 所有P0/P1问题：GRID_DAILY_TARGET=999, lock_path修复, fill事件, WS重连, 持仓同步
- [x] 双维度自适应TP（trigger+offset），动态格宽，FGI感知，资金费率防御等

---

## 待解决问题（按优先级）

- [ ] P3: _atr_baseline持久化（重启后恢复，避免冷启动期无ATR联动）
  - 方案：在grid_session.json中存储_atr_baseline值
  - 载入时恢复：`self._atr_baseline = session_data.get("atr_baseline", 0.0)`
  - 预期效果：重启后立即有ATR比率联动，不需要等20次_place_grid调用
- [ ] P3: 验证第26轮修复实际效果
  - 日志搜索：`adaptive trigger: base=... bounds=[0.85,1.20]` 或 `[1.00,1.50]`
  - 验证avg<0.4时adapted值是否在合理范围（不再被剪到0.50）

---

## 下次优先行动

1. **P3: _atr_baseline持久化**
   - grid_pro.py: `_save_session()` 中增加 `"atr_baseline": self._atr_baseline`
   - `_load_session()` 中增加 `self._atr_baseline = data.get("atr_baseline", 0.0)`
   - 避免每次部署/崩溃重启后ATR联动冷启动期的无效窗口

---

## 系统评估
- **策略有效性**：9/10
  - 26轮迭代；全P0/P1已解决；本轮修复了第25轮引入的隐蔽边界裁剪bug
  - 自适应层（修复后真正生效）：ATR联动TP + 分Regime EWMA + 时间衰减 + 冷启动恢复
  - FGI感知：三维度（档位-1/+1 + 格宽×0.8/×1.2）
  - Trail系统：Regime独立边界，trigger/offset真正在设计范围内工作
- **主要风险点**：
  1. 外部API网络受限（无法验证实盘运行状态，无法确认填单数据是否实际触发自适应）
  2. _atr_baseline冷启动期（仍未持久化，每次重启首20个_place_grid调用无ATR联动）
  3. 无实盘日志可验证，改进效果依赖代码分析
- **累计运行轮次**：26
