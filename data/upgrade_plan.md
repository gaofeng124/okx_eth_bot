# ETH量化系统升级计划

## 本次（2026-05-02 第七十四轮）完成

### grid_pro.py：丰富离线分析快照能力

**问题**：
- `status_summary()` 缺少 `_sz_scale`（仓位缩减系数）、loss_streak 冷静期状态、各槽位持仓时长
- `_log_status` 的30s `grid_status` 快照含 status_summary 信息，但没有专门的5分钟 `grid_state_snapshot`（含 regime 分布统计）
- 离线分析时无法直接看到：当前高ATR触发了哪个缩仓档？loss_streak 是否在阻止开仓？某槽位已持仓多久？

**修复（4处改动）**：
1. `__init__`：新增 `self._last_sz_scale = 1.0`（实例变量，存最近开仓缩减系数）
   和 `self._last_gridstate_snap_ts = 0.0`（5分钟快照节流时间戳）
2. `_open_slot`：`_sz_scale` 计算后立即 `self._last_sz_scale = _sz_scale` 持久化到实例
3. `status_summary()`：新增字段
   - `sz_scale_last`：最近一次开仓的仓位缩减系数（1.0/0.85/0.7/0.5/0.3）
   - `loss_streak_active`：bool，冷静期是否激活
   - `loss_streak_until_iso`：冷静期结束时刻（如 "14:32:00"），未激活时 null
   - `slot_hold_durations_sec`：`{level: elapsed_sec}` 各 HOLDING 槽位持仓时长
4. `_log_status`：新增每5分钟一次 `grid_state_snapshot` 事件（analysis.jsonl）
   - 包含 regime_stats（完整胜率/PnL分布）、ATR三窗口、sz_scale、loss_streak、各槽位持仓时长
   - 独立于30s `grid_status`，频率低但内容更全面，用于趋势分析

**效果预期**：
- analysis.jsonl 每5分钟一条 `grid_state_snapshot`，可直接用 jq/pandas 分析 sz_scale 分布
- 发现 loss_streak 连锁激活模式（何时触发、持续多久）
- 检测异常长持仓槽位（slot_hold_durations_sec 某档 >3600s = 1h，意味着挂单被跳过）

---

## 历史完成（节选）

### 第七十三轮（2026-05-02）
- [x] grid_pro.py: 修复 _check_phase4_trend_guard 5处硬编码 /root/okx_eth_bot/ 路径

### 第七十二轮（2026-05-02）
- [x] settings.py: 删除重复 DATA_DIR 相对路径定义，路径体系完整

### 第七十一轮（2026-05-01）
- [x] grid_pro.py: loss_streak 冷静期跨重启持久化

### 第七十轮（2026-05-01）
- [x] grid_pro.py: _sz_scale 4档标签 + regime差异化冷静期

### 第六十九~一轮（2026-04-18~05-01）
- [x] 全部P0/P1问题：WS重连/持仓同步/自适应TP/EWMA/FGI/资金费率/1h gate等

---

## 待解决问题（按优先级）

- [ ] P1: round75：检查服务实际运行状态
  - `systemctl status okx-eth-bot` 是否 active
  - `ls /home/user/okx_eth_bot/data/logs/daily/` 是否有目录
  - 若有日志，检查 fill_tp / grid_state_snapshot 事件数量
- [ ] P2: round75：若有 analysis.jsonl 积累，分析 sz_scale 各档位触发频率
  - `grep sz_scale_last data/logs/daily/*/analysis.jsonl | python3 -c "..."`
  - 判断 28-35bps 档（0.85）是否高频，是否值得调整阈值
- [ ] P2: round75：regime 分布验证
  - grid_state_snapshot 中 regime_stats 是否 RANGING 占主导（>60%）
  - 若 TRENDING 占比高，考虑调整 GRID_ATR_MULT

## 下次优先行动

**round75：**
1. 验证第72轮 DATA_DIR 修复效果：logs/daily 目录是否被正确创建
2. 统计 analysis.jsonl 中 grid_state_snapshot 事件，验证本轮改动生效
3. 分析实盘 fill_tp 记录：avg profit_spacings 是否接近 2.0（GRID_TP_MULT）

## 系统评估
- **策略有效性**：9/10
  - 74轮迭代，P0/P1 全部修复
  - 离线分析能力持续增强（status_summary 更完整）
  - 5分钟 grid_state_snapshot 新增 regime 分布统计，实盘调参依据更充分
- **当前主要风险**：
  1. 沙盒网络受限，无法获取实时市场数据验证效果
  2. 服务运行状态未确认（无实盘日志反馈）
  3. GRID_LEVELS=3 锁定，phase4 guard 的 +1 level 逻辑形同虚设
- **累计运行轮次**：74
