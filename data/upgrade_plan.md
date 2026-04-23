# ETH量化系统升级计划

## 本次（2026-04-23 第二十七轮）完成

### grid_pro.py：_atr_baseline 持久化

**背景：**
第26轮修复了adaptive边界裁剪bug（trigger/offset的[lo,hi]边界与新base值不匹配）。
`_atr_baseline`是ATR基线慢速EMA（α=0.05），每次`_place_grid`调用时更新。
问题：重启后`_atr_baseline=0.0`，需要积累20次`_place_grid`调用才能稳定，
这段冷启动期内`_update_tp`的ATR比率联动（atr_ratio=[0.8,1.3]缩放eff_tp_mult）完全失效。

**修复内容：**
1. 新增 `_last_atr_save_ts: float = 0.0`（节流用时间戳）
2. 新增 `_save_atr_baseline()`：
   - 每5分钟最多写一次（避免高频磁盘IO）
   - 写入 `data/grid_atr_state.json`：`{"atr_baseline": <float>, "saved_ts": <epoch>}`
   - I/O异常静默忽略，不影响主交易循环
3. 新增 `_restore_atr_baseline()`：
   - 读取 `data/grid_atr_state.json`，若文件存在且未超过12小时则恢复
   - 恢复后打印日志：`[grid] 恢复 atr_baseline=0.002345（2.1h前保存），ATR联动立即可用`
4. `__init__` 中在 `_replay_tp_history()` 之后调用 `_restore_atr_baseline()`
5. `_place_grid` 中更新 `_atr_baseline` 后调用 `_save_atr_baseline()`

**效果预期：**
- 修复前：重启后冷启动期ATR联动完全无效（_atr_baseline=0），eff_tp_mult默认1.0，
  无论市场高低波动率都用同一格宽比，丧失ATR自适应能力
- 修复后：重启后立即读取最近保存的基线值，ATR联动从第1次_place_grid起就生效
- 日志可验证：搜索 `恢复 atr_baseline=` 行，确认文件年龄在合理范围内

---

## 历史完成

### 第二十六轮（2026-04-23）
- [x] grid_pro.py: 修复_adaptive_trail_trigger/_adaptive_trail_offset的Regime边界裁剪bug
  - RANGING trigger: [0.85,1.20], TRENDING trigger: [1.00,1.50]
  - RANGING offset: [0.35,0.65], TRENDING offset: [0.45,0.75]

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

- [ ] P3: 验证第26轮adaptive边界修复实际效果
  - 日志搜索：`adaptive trigger: base=... bounds=[0.85,1.20]` 或 `[1.00,1.50]`
  - 验证avg<0.4时adapted值是否在合理范围（不再被剪到0.50）
- [ ] P3: 验证第27轮atr_baseline持久化效果
  - 日志搜索：`恢复 atr_baseline=`，确认重启后立即恢复

---

## 下次优先行动

1. **P3: 检查实盘日志（若网络恢复）**
   - 验证 fill_tp 事件中 profit_spacings 字段是否存在（EWMA有数据）
   - 验证 adaptive trigger bounds 是否正确（[0.85,1.20] 或 [1.00,1.50]）
   - 验证 atr_baseline 是否在重启后恢复

2. **P3: 若发现新的真实问题，针对性修复**
   - 优先级：实盘验证反馈的bug > 理论改进

---

## 系统评估
- **策略有效性**：9/10
  - 27轮迭代；全P0/P1已解决；自适应层完整
  - 本轮完成最后一块"冷启动盲区"修复（ATR基线持久化）
  - 理论完备度：ATR联动TP + 分Regime EWMA + 时间衰减 + 边界修复 + 冷启动恢复
- **主要风险点**：
  1. 外部API网络受限（无法验证实盘运行状态，所有改进停留在代码层）
  2. 无实盘日志反馈：所有优化效果未经实盘数据验证
  3. 网络限制解除后，需优先检查实盘是否正常运行
- **累计运行轮次**：27
