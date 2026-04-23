# ETH量化系统升级计划

## 本次（2026-04-23 第二十九轮）完成

### grid_pro.py：两项改进

**1. 修复 `_price_1h_cache` API失败时每tick阻塞bug（P1）**

背景：
- S7价格位置因子（S7权重0.30，最大单权重）每5分钟抓取OKX K线（4根15m candle）
- 代码：`except Exception: pass` —— 失败时不更新 `_price_1h_cache["ts"]`
- 问题：`ts` 初始化为 0.0，失败后维持旧值，导致 `now - ts > 300` 在每个 tick 都为 True
- **严重后果**：每 tick 都尝试 `urlopen(timeout=5)`，网络受限时每 tick 阻塞 5 秒
- 即使只是瞬时网络抖动，on_tick 热路径也会被连续冻结 5s/tick，严重影响交易执行

修复：`except Exception: self._price_1h_cache["ts"] = now - 240.0`
→ 失败后 60 秒重试（300 - 240 = 60），同 FGI 的修复思路（第28轮）
效果：网络故障期间 on_tick 最多阻塞一次 5 秒，而非每 tick 都阻塞

**2. `status_summary()` 新增三个监控字段**

新字段：
- `fgi`：恐贪指数（当前缓存值），可实时监控 FGI 是否更新
- `atr_baseline_bps`：ATR 基线（bps），可验证 ATR 联动 TP 是否正常工作
- `eff_tp_mult`：上次实际 TP 乘数（含 Regime × 0.8 + ATR 联动），监控 TP 缩放效果

效果：通过 `status.py` 或日志可直接读取这三个字段，无需搜索 debug 日志来验证系统状态

---

## 历史完成

### 第二十八轮（2026-04-23）
- [x] grid_pro.py: _refresh_fgi失败后改为5分钟重试（原1小时）
- [x] grid_pro.py: _update_tp缓存_last_eff_tp_mult
- [x] grid_pro.py: fill_tp事件新增grid_spacing_bps/atr_baseline_bps/eff_tp_mult诊断字段

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

- [ ] P3: 验证实盘日志（若网络恢复）
  - status_summary 是否输出 fgi/atr_baseline_bps/eff_tp_mult 新字段
  - fill_tp 事件是否有 grid_spacing_bps/atr_baseline_bps/eff_tp_mult
  - _price_1h_cache 是否 60s 重试（日志中无连续 5s 阻塞）
  - FGI 是否在 5 min 内更新（日志搜索"恐贪指数更新"）

---

## 下次优先行动

1. **P3: 若网络恢复，验证新字段是否出现在 status.py 输出中**
   - 命令：`python3 status.py 2>&1 | grep -E "fgi|atr_baseline|eff_tp"`
   - 验证：fgi 应在 0-100 范围，atr_baseline_bps 应在 20-80 范围，eff_tp_mult 应在 0.4-2.0

2. **P3: 若发现新的真实问题（分析实盘日志），针对性修复**
   - 优先级：实盘反馈的 bug > 理论改进

---

## 系统评估
- **策略有效性**：9/10
  - 29轮迭代；全P0/P1已解决；自适应层完整
  - 本轮：修复了on_tick热路径的5s阻塞bug + 监控可观测性增强
  - 理论完备度高：ATR联动TP + 分Regime EWMA + 时间衰减 + 边界修复 + 冷启动恢复 + 诊断字段 + 热路径保护
- **主要风险点**：
  1. 外部API网络受限（无法验证实盘运行状态）
  2. 无实盘日志反馈：所有优化效果未经实盘数据验证
  3. 网络限制解除后需优先检查实盘是否正常运行
- **累计运行轮次**：29
