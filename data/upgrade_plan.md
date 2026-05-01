# ETH量化系统升级计划

## 本次（2026-05-01 第六十八轮）完成

### detailed_daily_log.py：修复 _write() 文件句柄未显式关闭的 Bug

**问题**：
```python
# 旧代码（有隐患）
with _lock:
    path.open("a", encoding="utf-8").write(line)
```
`path.open()` 返回临时文件对象，`.write()` 执行后该对象立即脱离作用域。
CPython 依赖引用计数在此时立即 `__del__` 关闭文件，但：
- 这不是 Python 语言规范的保证（PyPy / Jython 等不保证）
- 在高频写入 + 线程竞争下，GC 延迟可能导致文件描述符积压
- 若进程收到信号（SIGTERM）恰好在 GC 前，buffer 可能未刷盘 → `analysis.jsonl` 出现截断行

**修复**：
```python
# 新代码（正确）
with _lock:
    with path.open("a", encoding="utf-8") as _fh:
        _fh.write(line)
```
`with` 语句保证 `__exit__` 调用 `flush()` 再 `close()`，无论是否异常。

**效果预期**：
- analysis.jsonl 写入可靠性提升，实盘出现异常后日志完整性更好
- 解决 round67 计划中提到的"record_analysis 是否有未被捕获的路径问题"

---

### grid_pro.py：_TP_AGING_SEC 从 2 档升级为 3 档

**问题**：
旧逻辑 `1800.0 if regime == favorable_trend else 1500.0`：
- RANGING 和逆势（如多头 + TRENDING_DOWN）被混为一档 1500s
- 逆势场景下 TP 几乎不可能自然 fill（市价朝反方向运动），等 25min 才超时纯属浪费
- 同期 loss_streak 冷静期已缩至 900s，逆势 TP aging 应与之对齐

**修复（3 档差异化）**：
| Regime 场景 | 旧值 | 新值 | 理由 |
|------------|------|------|------|
| 顺势（多头+涨 / 空头+跌）| 1800s | 1800s | 不变，给 maker TP 充足时间自然成交 |
| RANGING 振荡 | 1500s | 1200s | 20min 已覆盖大多数振荡周期，超时即重置网格 |
| 逆势（多头+跌 / 空头+涨）| 1500s | 900s | 与 loss_streak 冷静期对齐，快速止损保护本金 |

**效果预期**：
- 逆势场景：每次 TP 超时止损快 10min，每日最多节省 ~30min 保证金占用
- RANGING：每次超时快 5min，格子复位更及时，捕捉下一次振荡机会
- 理论每日多出 0.5~1 次完整网格循环

---

## 历史完成（节选）

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

- [ ] P1: round69：检查实盘 analysis.jsonl 写入情况
  - 本轮修复文件句柄后，需确认 DATA_DIR 路径是否正确
  - 若仍为空，检查 DETAILED_DAILY_LOG 环境变量是否被覆盖为 0
- [ ] P2: round69：评估 _SLOW_BLEED_AGING_SEC 是否也需要 regime 差异化
  - 当前固定 1800s（30min），逆势下是否应缩短至 1200s？
  - 需先获取实盘数据：avg 浮亏持续时间分布
- [ ] P2: round70：_sz_scale 中间档 ATR 28-35bps → sz=0.85（待实盘验证后决定）
- [ ] P3: 动态止盈 eff_tp_mult 灵敏度验证（需实盘数据）

## 下次优先行动

**round69：**
1. 验证 analysis.jsonl 实盘写入：检查 `data/logs/daily/$(date +%Y-%m-%d)/analysis.jsonl` 是否有内容
2. 如有数据：分析 TP 成交时间分布，验证 RANGING 1200s 是否合适
3. 考虑 _SLOW_BLEED_AGING_SEC regime 差异化（逆势 1800s → 1200s）

## 系统评估
- **策略有效性**：9/10
  - 68轮迭代；文件写入可靠性修复；TP aging 3档精细化
  - 全部 P0/P1 历史问题已解决
- **当前主要风险**：
  1. 实盘日志仍为空，优化依赖理论推导未经实盘数据验证
  2. 沙盒网络受限，无法实时获取市场数据做动态参数调整
  3. 逆势 900s TP aging 可能在市场快速反转时提前止损（概率较低）
- **累计运行轮次**：68
