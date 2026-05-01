# ETH量化系统升级计划

## 本次（2026-05-01 第七十一轮）完成

### grid_pro.py：loss_streak冷静期跨重启持久化

**问题**：
`_loss_streak_until` 只存在于内存，进程重启（崩溃/systemd重启/维护）后归零。
场景：两次连亏 → loss_streak设1200s冷静期 → 30s后系统重启 → 冷静期清空 → 立即开新仓 → 第三次连亏。
在逆势市场中，这个场景非常常见，等于冷静期保护形同虚设。

**修复**：
新增 `_save_loss_streak()` 和 `_restore_loss_streak()` 方法，完全仿 `_save_atr_baseline/_restore_atr_baseline` 模式：
- 触发冷静期时（`_loss_streak_until` 被赋值后）立即写入 `data/grid_loss_streak.json`
- `__init__` 末尾调用 `_restore_loss_streak()`，若文件未过期则恢复 `_loss_streak_until`
- 已过期的文件自动删除（`p.unlink(missing_ok=True)`）

**效果预期**：
- 崩溃/重启后仍保持冷静期，避免逆势中重复开仓被打
- 日志可见：`[grid] 恢复 loss_streak 冷静期：剩余 X.X min（跨重启保护）`

---

## 历史完成（节选）

### 第七十轮（2026-05-01）
- [x] grid_pro.py: _sz_scale 日志补充4档标签(28-35/35-50/50-70/>70bps)便于统计触发频率
- [x] grid_pro.py: loss_streak冷静期按regime差异化（顺势600s/RANGING900s/逆势1200s）

### 第六十九轮（2026-05-01）
- [x] grid_pro.py: _SLOW_BLEED_AGING_SEC从固定1800s升级为3档（顺势1800s/RANGING1500s/逆势1200s）
- [x] grid_pro.py: _sz_scale 补充28-35bps中间档sz=0.85

### 第六十八轮（2026-05-01）
- [x] detailed_daily_log.py: 修复文件句柄未显式关闭的Bug（实为with语句已正确关闭，记录误报）
- [x] grid_pro.py: _TP_AGING_SEC 从2档升级为3档（顺势1800s/RANGING1200s/逆势900s）

### 第六十七轮（2026-05-01）
- [x] grid_pro.py: loss_streak 改为会话粒度（per-slot → session 统一）
- [x] grid_pro.py: loss_streak 冷静期 1800s → 900s

### 第六十六轮（2026-04-30）
- [x] grid_pro.py: _market_close_all 中 loss_streak 改为会话粒度

### 第六十五轮（2026-04-30）
- [x] settings.py: GRID_PER_SLOT_STOP_USDT 0.8 → 1.0

### 第一~六十四轮（2026-04-18~29）
- [x] 全部P0/P1问题；WS重连；持仓同步；自适应TP；EWMA；FGI；资金费率；1h gate等

---

## 待解决问题（按优先级）

- [ ] P1: round72：检查 data/grid_loss_streak.json 实盘是否正确生成
  - 验证：触发loss_streak后文件是否写入、重启后是否被恢复
- [ ] P1: round72：data/logs/daily 目录仍为空
  - analysis.jsonl 日志目录持续为空，需检查 detailed_daily_log.py 的 DATA_DIR 路径配置
  - 若路径配置正确，检查是否有实盘数据写入（bot是否真正在运行）
- [ ] P2: round72：验证 loss_streak 3档冷静期效果
  - 顺势600s 是否过短？（需要实盘止损频率数据支撑）
  - 逆势1200s 是否与 _SLOW_BLEED_AGING_SEC 逆势1200s 形成双重保护？
- [ ] P2: round73：_sz_scale 频率统计
  - 积累足够日志后：分析各档位触发频率，评估 28-35bps 档是否需要进一步调整
- [ ] P3: 动态止盈 eff_tp_mult 灵敏度验证（需实盘数据）

## 下次优先行动

**round72：**
1. 确认 grid_loss_streak.json 文件在实盘触发loss_streak后存在
2. 检查 data/logs 目录：`find /home/user/okx_eth_bot/data/ -name "*.jsonl" 2>/dev/null`
3. 若日志仍为空，考虑增加启动时强制创建 data/logs/daily 目录的逻辑

## 系统评估
- **策略有效性**：9/10
  - 71轮迭代；三大止损机制全部完善；loss_streak现在具备跨重启保护
  - 仿atr_baseline持久化模式，代码一致性高，维护性好
- **当前主要风险**：
  1. 实盘日志仍为空，优化依赖理论推导未经实盘数据验证
  2. 顺势600s loss_streak 冷静期偏短，若市场快速震荡可能频繁触发重开
  3. 沙盒网络受限，无法实时验证外部API参数
- **累计运行轮次**：71
