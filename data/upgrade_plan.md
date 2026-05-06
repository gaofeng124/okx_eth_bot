# ETH量化系统升级计划

## 本次（2026-05-06 第九十三轮）完成

### P2 修复：`record_tick` 节流竞争（`detailed_daily_log.py`）

**问题：**
`_last_tick_wall` 的检查与更新在 `_lock` 之外独立执行：
```python
# 原来：
if min_iv > 0.0 and (now - _last_tick_wall) < min_iv:
    return
_last_tick_wall = now  # ← 两行之间无锁保护
```
两个并发线程都能通过检查，都写入 tick，都更新 `_last_tick_wall`，导致节流失效、日志双写。

**修复：**
新增 `_tick_throttle_lock = threading.Lock()`，将检查+更新原子化：
```python
with _tick_throttle_lock:
    if (now - _last_tick_wall) < min_iv:
        return
    _last_tick_wall = now
```

---

### P2 修复：REST 阻塞主循环（`grid_pro.py`）

**问题：**
`_refresh_fgi`（timeout=5s，每1h）和 `_check_phase4_trend_guard`（timeout=2s，每10min）
直接在主 tick 循环中调用 `urllib.request.urlopen`，网络延迟/超时直接阻塞主循环。

**修复：**
两个函数均改为先原子更新时间戳（防重入），再 `threading.Thread(daemon=True)` 异步执行：
- 主循环零阻塞（即使 HTTP 超时也不受影响）
- 失败回退逻辑（`_last_fgi_ts = _now - 3300.0`）保留在线程内
- P4 降级的 subprocess/pkill 逻辑也在线程内执行

**效果预期：**
- 消除每1h最多5s的 FGI 阻塞窗口
- 消除每10min最多2s的 P4 守卫阻塞窗口
- 主循环吞吐量更稳定，tick 延迟更低

---

## 历史完成（节选）

### 第九十二轮（2026-05-06）
- [x] `_write()` 午夜边界 ts_wall 与目录不一致修复（`_ensure_day_dir` 移入锁内）

### 第九十一轮（2026-05-06）
- [x] `_replay_tp_history` regime 分别采样（ranging_recs[-20:] + trending_recs[-20:]）

### 第九十轮（2026-05-06）
- [x] `_position_sync_check` 正常路径每30次记录 `position_sync_ok` 事件

---

## 待解决问题（按优先级）

- [ ] P2: 全面扫描其他同步阻塞 HTTP 点（`_refresh_funding` 等是否也有阻塞路径）
- [ ] P3: 持续监控 position_sync_ok diff 是否存在趋势性漂移（等实盘数据）
- [ ] P3: 动态止盈——根据波动率调整每格利润目标（ATR 高时扩大 TP 距离）

## 下次优先行动

**round94：**
1. 扫描 `_refresh_funding` 及其他 REST fallback 是否也有同步阻塞，如有则同样改为后台线程
2. 检查 `position_sync_ok` 事件中 diff 字段的分布情况，判断是否存在系统性漂移

## 系统评估
- **策略有效性**：9/10
  - 93 轮迭代，主循环阻塞问题逐步消除
  - tick 节流线程安全性修复
  - FGI 和 P4 守卫完全异步化
- **当前主要风险**：
  1. 沙盒环境无法验证实盘 API 调用
  2. P4 守卫降级使用 pkill，在后台线程中执行，理论上主线程有短暂 dirty state
- **累计运行轮次**：93
