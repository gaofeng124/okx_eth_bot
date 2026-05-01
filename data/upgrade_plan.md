# ETH量化系统升级计划

## 本次（2026-05-01 第六十九轮）完成

### grid_pro.py：_SLOW_BLEED_AGING_SEC 从固定值升级为3档 regime 差异化

**问题**：
round68 已将 `_TP_AGING_SEC` 升级为3档（顺势1800s/RANGING1200s/逆势900s），
但同级别的慢出血超时 `_SLOW_BLEED_AGING_SEC` 仍固定 1800s。

**逻辑漏洞**：
- 逆势场景下：TP aging 在 900s 触发（需价格破位 OR 浮亏>0.5U）
- 慢出血阈值 -0.30U < -0.50U（低于TP aging触发值）
- 因此逆势+浮亏(-0.30U ~ -0.50U) 会绕过 TP aging，等满 1800s 才触发慢出血
- 实质上：逆势方向持仓 -0.35U 可以安心挂满 30min 不被系统处理！

**修复（3档差异化）**：
| Regime 场景 | 旧值 | 新值 | 理由 |
|------------|------|------|------|
| 顺势（多头+涨 / 空头+跌）| 1800s | 1800s | 不变，TP 仍在等自然成交 |
| RANGING 振荡 | 1800s | 1500s | 25min 已足够，适当收紧 |
| 逆势（多头+跌 / 空头+涨）| 1800s | 1200s | 与 TP aging 逆势档对齐，统一快速止损逻辑 |

**效果预期**：
- 逆势慢出血场景：每次最多少等 600s（节省 10min），快速清仓保护本金
- RANGING 场景：每次少等 300s（节省 5min），格子更快复位抓下次机会
- 与 TP aging 3档逻辑一致，两道防线现在统一成同一套 regime 框架

---

### grid_pro.py：_sz_scale 补充 28-35bps 中间档 sz=0.85

**问题**：
原 `_sz_scale` 档位：≤35bps → 1.0，35-50bps → 0.7，50-70bps → 0.5，>70bps → 0.3
28-35bps 区间（"轻微偏高"状态）没有缩减，直接跳到 1.0。
ETH ATR 28-35bps 是高频出现的边界状态，此时温和缩仓 15% 可降低止损击穿概率。

**修复**：新增 `elif _atr_bps > 28: _sz_scale = 0.85`（5档 → 5档，中间补充1档）

**效果预期**：
- 理论上 ~15% 仓位缩减降低 per_slot_stop 击穿概率
- ATR 28-35bps 时更平滑的仓位过渡（原1.0直接跳0.7较突兀）

---

## 历史完成（节选）

### 第六十八轮（2026-05-01）
- [x] detailed_daily_log.py: 修复 _write() 文件句柄未显式关闭的 Bug
- [x] grid_pro.py: _TP_AGING_SEC 从2档升级为3档（顺势1800s/RANGING1200s/逆势900s）

### 第六十七轮（2026-05-01）
- [x] grid_pro.py: _sync_tp 中 loss_streak 改为会话粒度（per-slot → session 统一）
- [x] grid_pro.py: loss_streak 冷静期 1800s → 900s

### 第六十六轮（2026-04-30）
- [x] grid_pro.py: _market_close_all 中 loss_streak 改为会话粒度

### 第六十五轮（2026-04-30）
- [x] settings.py: GRID_PER_SLOT_STOP_USDT 0.8 → 1.0

### 第一~六十四轮（2026-04-18~29）
- [x] 全部P0/P1问题；WS重连；持仓同步；自适应TP；EWMA；FGI；资金费率；1h gate等

---

## 待解决问题（按优先级）

- [ ] P1: round70：验证实盘 analysis.jsonl 写入情况
  - 若 data/logs/daily/$(date +%Y-%m-%d)/analysis.jsonl 仍为空，检查 DATA_DIR 配置
  - 本轮修复文件句柄（round68）后验证效果
- [ ] P2: round70：_sz_scale 日志改进
  - 当前仅 `< 1.0` 时记录一条日志，未区分具体触发档位
  - 改进：在 log 中输出 `档位:28-35bps/35-50bps/50-70bps/>70bps` 便于统计
- [ ] P2: round71：RANGING 振荡下 loss_streak 冷静期差异化
  - 当前 loss_streak 冷静期固定 900s（round67 统一改为会话粒度）
  - 顺势场景是否应延长至 1200s（避免频繁重开格被趋势拖走）？
- [ ] P3: 动态止盈 eff_tp_mult 灵敏度验证（需实盘数据）

## 下次优先行动

**round70：**
1. 检查 analysis.jsonl 实盘写入：`ls -la data/logs/daily/$(date +%Y-%m-%d)/`
2. 若有数据：分析慢出血触发分布，验证1200s/1500s差异化是否合理
3. 改进 _sz_scale 日志（区分28-35/35-50/50-70/>70 bps档位）

## 系统评估
- **策略有效性**：9/10
  - 69轮迭代；止损3档精细化形成完整regime框架；仓位5档平滑过渡
  - 全部 P0/P1 历史问题已解决
- **当前主要风险**：
  1. 实盘日志仍为空，优化依赖理论推导未经实盘数据验证
  2. 沙盒网络受限，无法实时获取市场数据做动态参数调整
  3. _SLOW_BLEED_AGING_SEC 逆势1200s 可能在市场快速反转时提前止损（低概率）
- **累计运行轮次**：69
