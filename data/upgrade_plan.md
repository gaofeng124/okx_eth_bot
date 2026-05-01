# ETH量化系统升级计划

## 本次（2026-05-01 第七十轮）完成

### grid_pro.py：_sz_scale 日志补充4档标签

**问题**：
`_sz_scale` 触发日志仅输出 `ATR=X.Xbps 仓位缩 ×Y.Y`，无法区分具体触发档位（28-35/35-50/50-70/>70bps）。
统计哪个区间最常触发对调参有价值，但没有档位标签无法从日志中直接分析。

**修复**：
新增 `_sz_tier` 局部变量，根据 `_atr_bps` 映射到 4 个字符串标签，嵌入 log.info 输出。
日志格式：`[grid][atr-scale] ATR=X.Xbps[28-35bps] 仓位缩 ×0.85（防高波动击穿止损）`

**效果预期**：
- 可从 logs 中 grep `[atr-scale]` 并统计 28-35bps/35-50bps 等档位出现频率
- 为后续档位阈值微调提供数据支撑

---

### grid_pro.py：loss_streak 冷静期按 regime 差异化

**问题**：
loss_streak 冷静期固定 900s（15min），不区分当前市场 regime。
- 逆势（多头+下跌 / 空头+上涨）：趋势对抗，连亏是必然，900s 后重开仍会被打
- 顺势（多头+上涨 / 空头+下跌）：连亏可能是临时回调，900s 可能太保守，错过趋势恢复

**修复（3档差异化）**：
| Regime | 旧值 | 新值 | 理由 |
|--------|------|------|------|
| 顺势 | 900s | 600s | 临时回调，快速恢复可抓趋势恢复 |
| RANGING 振荡 | 900s | 900s | 原值不变，振荡频繁止损是正常现象 |
| 逆势 | 900s | 1200s | 趋势对抗，等更久避免重复止损 |

同步更新 `record_analysis` 调用，新增 `cooldown_sec` 字段记录实际冷静时长。

**效果预期**：
- 逆势场景每次止损少重开1-2次（节省 300s 冷静期）
- 顺势场景止损后更快恢复（缩短 300s），理论上提升趋势日收益

---

## 历史完成（节选）

### 第六十九轮（2026-05-01）
- [x] grid_pro.py: _SLOW_BLEED_AGING_SEC从固定1800s升级为3档（顺势1800s/RANGING1500s/逆势1200s）
- [x] grid_pro.py: _sz_scale 补充28-35bps中间档sz=0.85

### 第六十八轮（2026-05-01）
- [x] detailed_daily_log.py: 修复文件句柄未显式关闭的Bug
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

- [ ] P1: round71：验证 data/logs/daily 目录是否正确初始化
  - analysis.jsonl 日志目录持续为空，需检查 detailed_daily_log.py 的 DATA_DIR 路径配置
  - 若路径配置正确，检查是否有实盘数据写入
- [ ] P2: round71：验证 loss_streak 3档冷静期效果
  - 顺势600s 是否过短？（需要实盘止损频率数据支撑）
  - 逆势1200s 是否与 _SLOW_BLEED_AGING_SEC 逆势1200s 形成双重保护？（TP aging 900s → slow_bleed 1200s → loss_streak 1200s 逻辑链是否一致）
- [ ] P2: round72：_sz_scale 频率统计
  - 积累足够日志后：分析各档位触发频率，评估 28-35bps 档是否需要进一步调整
- [ ] P3: 动态止盈 eff_tp_mult 灵敏度验证（需实盘数据）

## 下次优先行动

**round71：**
1. 检查 data/logs 目录初始化问题：`find data/ -name "*.jsonl" -o -name "*.log" 2>/dev/null`
2. 检查 detailed_daily_log.py 的 DATA_DIR 配置，确认路径是否与实际写入路径匹配
3. 验证 loss_streak 3档逻辑链一致性（TP aging 900s → slow_bleed 1200s → loss_streak 1200s）

## 系统评估
- **策略有效性**：9/10
  - 70轮迭代；三大止损机制（TP aging/slow_bleed/loss_streak）全部实现regime差异化
  - 仓位缩减日志完善（4档标签），为后续调参提供数据基础
- **当前主要风险**：
  1. 实盘日志仍为空，优化依赖理论推导未经实盘数据验证
  2. 顺势600s loss_streak 冷静期偏短，若市场快速震荡可能频繁触发重开
  3. 沙盒网络受限，无法实时验证外部API参数
- **累计运行轮次**：70
