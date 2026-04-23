# ETH量化系统升级计划

## 本次（2026-04-23 第二十八轮）完成

### grid_pro.py：三项改进

**1. `_refresh_fgi` 失败后5分钟重试（原1小时）**

背景：
- 原代码：`self._last_fgi_ts = now` 在 try 块前无条件设置，失败后下次重试等1小时
- 问题：外网受限或API临时故障时，FGI缓存值（初始50=中性）长期不更新，
  FGI-based的格宽调整（FGI<25收窄/FGI>70扩宽）和档位调整（FGI极端减1档）失效

修复：失败时 `self._last_fgi_ts = now - 3300.0` → 5分钟后重试
效果：网络恢复后FGI数据在5分钟内同步，极端情绪时参数调整立即生效

**2. `_update_tp` 缓存 `_last_eff_tp_mult`**

新增 `self._last_eff_tp_mult: float = 1.0`（__init__中初始化）
`_update_tp()` 计算完 `_eff_tp_mult` 后写入 `self._last_eff_tp_mult`

**3. `fill_tp` 事件增加ATR诊断字段**

新字段：
- `grid_spacing_bps`：成交时的格宽（bps）
- `atr_baseline_bps`：成交时的ATR基线（bps）
- `eff_tp_mult`：实际使用的TP乘数（含Regime×0.8 + ATR联动）

效果：离线分析时可直接验证轮25-27的ATR联动是否正常工作，
无需在文本日志中搜索debug行

---

## 历史完成

### 第二十七轮（2026-04-23）
- [x] grid_pro.py: _atr_baseline持久化（_save_atr_baseline/_restore_atr_baseline）

### 第二十六轮（2026-04-23）
- [x] grid_pro.py: 修复_adaptive_trail_trigger/_adaptive_trail_offset的Regime边界裁剪bug

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

- [ ] P3: 网络恢复后验证实盘日志
  - fill_tp事件中是否有grid_spacing_bps/atr_baseline_bps/eff_tp_mult字段
  - FGI是否在网络恢复后5min内更新（日志搜索"恐贪指数更新"）
  - atr_baseline重启后是否恢复（日志搜索"恢复 atr_baseline="）

---

## 下次优先行动

1. **P3: 检查实盘日志（若网络恢复）**
   - grep "fill_tp" analysis.jsonl | python3 -c "import json,sys; [print(json.loads(l).get('eff_tp_mult')) for l in sys.stdin if 'fill_tp' in l]"
   - 验证 eff_tp_mult 是否在 [0.4, 2.0] 合理范围内变化

2. **P3: 若发现新的真实问题，针对性修复**
   - 优先级：实盘验证反馈的bug > 理论改进

---

## 系统评估
- **策略有效性**：9/10
  - 28轮迭代；全P0/P1已解决；自适应层完整
  - 本轮补强：FGI快速重试 + ATR联动可观测性
  - 理论完备度高：ATR联动TP + 分Regime EWMA + 时间衰减 + 边界修复 + 冷启动恢复 + 诊断字段
- **主要风险点**：
  1. 外部API网络受限（无法验证实盘运行状态）
  2. 无实盘日志反馈：所有优化效果未经实盘数据验证
  3. 网络限制解除后需优先检查实盘是否正常运行
- **累计运行轮次**：28
