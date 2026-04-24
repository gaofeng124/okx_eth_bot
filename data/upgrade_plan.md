# ETH量化系统升级计划

## 本次（2026-04-24 第三十三轮）完成

### runner.py：WS重连指数退避（P1）

**问题**：`_ws_price_feed_loop` 遇到任何异常均固定等待 5s 后重连。
- 瞬断（服务端 ping timeout）→ 等待 5s 才恢复，增加行情缺口时间
- 长时间断线（网络故障）→ 每 5s 重试一次，日志洪泛，无意义频繁请求

**修复**：指数退避 1s → 2s → 4s → 8s → 16s → 30s（上限），成功收到任意行情数据时重置为 1s。
**效果**：瞬断恢复更快（1s vs 5s）；长时故障减少无效重试 6 倍；行情静默窗口缩短。

---

### grid_pro.py：profit_spacings EWMA 上限帽（P3）

**问题**：EWMA 自适应系统从 TP 成交中计算 `profit_spacings = |fill - vwap| / spacing_abs`。
在强趋势行情（价格连续延伸 3-5 格宽）时，单次成交的 `profit_spacings` 可高达 4.0~6.0，
远超正常范围（0.4-0.8）。由于 EWMA 半衰期仅 30min，一次极端值会使均值长时间 > 0.8，
持续触发 trigger 收紧（-0.05/次），导致系统在趋势结束后进入过度防守状态 2-3h。

**修复**：
1. TP fill 时：`_ps = min(abs(...) / spacing_abs, 3.0)` — 极端值截断在 3.0
2. `_replay_tp_history` 读取：同步加 `min(float(ps), 3.0)` — 防重启后从历史注入旧极端值

**效果**：EWMA 依然感知"超常利润"（触发 trigger 收紧），但上限 3.0 防止单次极端事件
主导均值超过 2-3 小时，自适应系统在趋势回归正常后 ~30min 即恢复合理区间。

---

## 历史完成

### 第三十二轮（2026-04-24）
- [x] grid_pro.py: LONG路径_last_tp_trail_ts=now从if new_tp>tp_price内部移至触发条件外层（与SHORT对称）
- [x] grid_pro.py: 7e宽限期info日志加int(elapsed)%300<5节流（从每tick→每5分钟）

### 第三十一轮（2026-04-24）
- [x] grid_pro.py: 7e持仓硬超时盈利误强平：盈利>$0.10且TP挂单时延至2h
- [x] grid_pro.py: SHORT方向TP追踪节流时间戳移至触发条件外（部分修复，round 32补全LONG）

### 第三十轮（2026-04-23）
- [x] grid_pro.py: _recent_entries_ts补仓节流清理窗口60s→120s
- [x] grid_pro.py: 持仓硬超时7e节（基于fill_ts）

### 第二十九轮（2026-04-23）
- [x] grid_pro.py: _price_1h_cache失败后重试间隔1h→60s
- [x] grid_pro.py: status_summary增加fgi/atr_baseline_bps/eff_tp_mult字段

### 第一~二十八轮（2026-04-18~23）
- [x] 全部P0/P1问题：参数修复、WS重连、持仓同步、自适应TP、EWMA、FGI、资金费率等

---

## 待解决问题（按优先级）

- [ ] P3: 验证WS指数退避效果
  - 方法：grep '\[WS行情\]' logs/*.log | grep '后重连' 查看backoff序列是否递增
  - 预期：瞬断后出现 "1s 后重连"，多次后出现 "2s 后重连"、"4s 后重连"

- [ ] P3: 验证profit_spacings上限帽效果
  - 方法：grep 'adaptive trigger\|adaptive offset' logs/*.log 查看adapted值变化幅度
  - 预期：不再出现连续多次tighten后trigger值降至下限(0.85/1.00)的情况

- [ ] P3: 验证round 31-32修复效果
  - hard_hold_timeout触发减少：grep -c 'hard_hold_timeout' logs/*.log
  - 宽限日志频率：grep -c '宽限到' logs/*.log（应<30/小时）

- [ ] P3: 考虑为_maybe_trail_tp增加单元测试
  - 已历经4轮修改，测试覆盖LONG/SHORT路径、trigger不满足、new_tp不改善等情况

## 下次优先行动

1. **P3: 若能访问实盘日志**
   - 分析 profit_spacings 分布：`python3 -c "import json; [print(r['profit_spacings']) for r in [json.loads(l) for l in open('data/logs/daily/YYYY-MM-DD/analysis.jsonl')] if r.get('event')=='fill_tp' and r.get('profit_spacings')]"`
   - 验证 avg_win 是否收敛到合理区间（目标 0.5-1.5U per trade）

2. **P3: 监控自适应trigger/offset收敛**
   - grep 'adaptive trigger' logs/*.log | tail -20 — 查看最近20次adaptation方向

## 系统评估
- **策略有效性**：9/10
  - 33轮迭代；全P0/P1已解决；两项P3代码质量改进
  - WS退避改善断线恢复速度；profit_spacings帽提升EWMA稳健性
  - 主要待验证：实盘数据中profit_spacings分布是否健康
- **当前主要风险**：
  1. 外部API网络受限（沙箱限制，无实时市场监控）
  2. 实盘日志无法访问（所有优化均未经实盘数据验证）
  3. 自适应系统参数(trigger/offset)仍依赖EWMA收敛，首次冷启动5笔以上才生效
- **累计运行轮次**：33
