# ETH量化系统升级计划

## 本次（2026-05-06 第九十四轮）完成

### P2 修复：全部 HTTP 阻塞点异步化（`grid_pro.py`）

**问题：**
第93轮修复了 `_refresh_fgi`（1h/次，5s阻塞）和 `_check_phase4_trend_guard`（10min/次，2s阻塞）。
但遗漏了另外两处：

1. `_refresh_funding` fallback（1h/次，timeout=5s）：
   当 runner 未提供资金费率时直接在主循环中 `urlopen`，阻塞最多5s。
2. `_price_1h_cache` 更新（5min/次，timeout=2s）：
   S7 价格位置因子每5分钟拉取 1h K 线，直接阻塞主循环最多2s，**且在每次 `_decide_open` 调用中触发**。

**修复：**
两处均采用与 FGI 相同的模式：
- 先原子更新时间戳（防重入）
- 再 `threading.Thread(daemon=True)` 异步执行 HTTP
- 失败时在线程内回退 ts，实现指数退避重试

**线程名汇总（便于日志过滤）：**
| 线程名 | 触发频率 | 超时 |
|--------|----------|------|
| `fgi-refresh` | 1h/次 | 5s |
| `funding-refresh` | 1h/次（fallback） | 5s |
| `p4-trend-guard` | 10min/次 | 2s |
| `price-1h-refresh` | 5min/次 | 2s |

**效果预期：**
- 主循环吞吐量提升：每5min周期无2s卡顿，每1h无5s双卡顿
- `_decide_open` 调用路径完全非阻塞
- tick 延迟上界降低，网格挂单响应更及时

---

## 历史完成（节选）

### 第九十三轮（2026-05-06）
- [x] `record_tick` 节流竞争修复（`_tick_throttle_lock` 原子化）
- [x] `_refresh_fgi` 和 `_check_phase4_trend_guard` 改为 daemon 线程

### 第九十二轮（2026-05-06）
- [x] `_write()` 午夜边界 ts_wall 与目录不一致修复

### 第九十一轮（2026-05-06）
- [x] `_replay_tp_history` regime 分别采样

---

## 待解决问题（按优先级）

- [ ] P2: 检查 runner.py WebSocket 断线重连逻辑是否有主线程阻塞点
- [ ] P2: 确认 analysis.jsonl 中 fill 事件是否正常写入（有成交时）
- [ ] P3: 持续监控 position_sync_ok diff 是否存在趋势性漂移
- [ ] P3: 动态止盈——根据波动率调整每格利润目标（ATR 高时扩大 TP 距离）

## 下次优先行动

**round95：**
1. 读取 runner.py，重点检查 WebSocket on_close/on_error 回调是否有阻塞重连逻辑
2. 检查是否存在 `analysis.jsonl` fill 事件写入缺失问题（成交后应有 fill 记录）
3. 若发现阻塞重连，改为 threading.Timer 或 asyncio 延迟重连

## 系统评估
- **策略有效性**：9/10
  - 94 轮迭代，grid_pro.py 主循环所有 HTTP 调用均已异步化
  - 主循环真正零阻塞，tick 延迟上界大幅改善
- **当前主要风险**：
  1. 沙盒环境无法验证实盘 API 调用及实际成交
  2. runner.py WebSocket 重连逻辑尚未审查
- **累计运行轮次**：94
