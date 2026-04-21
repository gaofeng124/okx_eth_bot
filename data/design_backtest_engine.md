# 回测引擎 + 压力测试 — 设计文档

**背景**：所有改动都在实盘试错（已亏 ~$13 修 bug）。需要 offline 验证新想法再上线，风险降 10x。

**目标**：用**历史 tick 数据**在内存里"重放"交易日，跑 grid_pro 策略，对比预期 PnL。

**核心约束**：**零实盘影响**。回测进程独立，不连 OKX、不发单、只读历史数据。

---

## 一、数据源

### 1.1 已有
- `data/logs/daily/YYYY-MM-DD/market.jsonl`：每 tick 一条（ts, bid, ask, mid, spread, book）
- `data/logs/pnl_snapshots.jsonl`：每 ~50s 一条 PnL 快照
- `data/logs/daily/YYYY-MM-DD/analysis.jsonl`：策略决策 + regime

### 1.2 可补充
- OKX REST `/api/v5/market/history-candles` 拉 1m/5m/15m 历史 K 线
- OKX REST `/api/v5/market/history-trades` 拉历史成交（公开）
- 这些数据**离线拉一次存档**，不干扰实盘

---

## 二、架构（插件化 exchange adapter）

### 2.1 当前痛点
`grid_pro.py` 里**硬编码 OKX REST client**：
```python
self._rest.request("POST", "/api/v5/trade/order", {...})
```

### 2.2 重构目标
抽象一层 `ExchangeAdapter`：
```python
class ExchangeAdapter(Protocol):
    def place_order(self, inst_id, side, order_type, sz, px, **kw) -> OrderResult: ...
    def cancel_order(self, ord_id) -> bool: ...
    def get_positions(self) -> list: ...
    def get_balance(self) -> dict: ...
    def get_orders_pending(self) -> list: ...

class LiveOKXAdapter(ExchangeAdapter):
    """现有实盘实现（把 OKXRestClient 封装进来）"""

class BacktestAdapter(ExchangeAdapter):
    """回测用：模拟订单簿 + 模拟撮合"""
    def __init__(self, market_feed: Iterator[dict]):
        self._feed = market_feed
        self._pending_orders = {}  # 模拟挂单
        self._positions = {"long_sz": 0, "short_sz": 0, ...}
        self._cash = 42.0  # 初始资金
        self._fills_log = []
    def tick(self, market_state):
        """每 tick 检查挂单是否可成交"""
        for oid, order in list(self._pending_orders.items()):
            if self._would_fill(order, market_state):
                self._fill(oid, order, market_state)
```

### 2.3 主 grid_pro 改动（MVP）
- `grid_pro.py __init__` 加参数 `exchange_adapter` （默认 LiveOKXAdapter，向后兼容）
- 所有 `self._rest.request(...)` 换成 `self._exchange.place_order(...)` 等
- 不改业务逻辑，只抽象数据源

---

## 三、回测核心循环

```python
def run_backtest(
    strategy: GridProStrategy,
    market_feed: Iterator[dict],
    starting_capital: float = 42.0,
) -> BacktestReport:
    adapter = BacktestAdapter(starting_capital)
    strategy._exchange = adapter  # 注入
    
    for tick in market_feed:
        # 1. 喂市场数据给 strategy
        strategy.on_tick(
            last=tick["mid"],
            bid=tick["bid"],
            ask=tick["ask"],
            market_context={"ts": tick["ts"], ...},
        )
        # 2. adapter 检查是否有成交
        adapter.tick(tick)
    
    return BacktestReport(
        starting_capital=starting_capital,
        final_equity=adapter._cash + adapter._unrealized_upl,
        total_trades=len(adapter._fills_log),
        pnl_per_trade=[...],
        max_drawdown=...,
        sharpe=...,
    )
```

---

## 四、压力测试用例（验收策略）

### 4.1 用例列表
每个 case 是一段历史 tick 数据，+ 期望结果：

| # | 场景 | 数据时段 | 期望 |
|---|---|---|---|
| T1 | 震荡市（策略擅长）| 过去 3 天 ranging | 日收益 > +$1 |
| T2 | 温和下跌（-3%/d）| 历史任一 | 日亏 < -$2 |
| T3 | 剧烈下跌（-5%/d）| 2024.12 或 2025.01 crash | **日亏 < -$5**（关键考验） |
| T4 | 剧烈上涨（+5%/d）| 任一 pump 日 | 至少不亏 |
| T5 | 突然闪崩（10 分钟 -3%）| 手动构造 | per_slot_stop + whole_stop 都触发正确 |
| T6 | 极低波动率 | 假日等静态市 | grid 不开新格或开得很保守 |

### 4.2 通过标准
- T1 / T2 / T4 / T6：正常通过
- **T3 / T5：**关键 —— 即使亏也不能失控（不超过 whole_stop 的 120%）

---

## 五、实施路径

### Phase 1：数据持久化（~4h）
- [ ] `quant/backtest/data_loader.py`：从 market.jsonl + OKX history API 合并历史 tick
- [ ] 实现历史 book_imbalance 还原（如果当时有记录）

### Phase 2：Exchange Adapter（~6h）
- [ ] `quant/exchange/adapter.py` 抽象基类
- [ ] `LiveOKXAdapter` 封装现有 OKXRestClient
- [ ] `BacktestAdapter` 模拟成交（考虑 post_only 不越盘、maker 费率、滑点）

### Phase 3：grid_pro 注入（~2h）
- [ ] __init__ 加 exchange_adapter 参数
- [ ] 全局替换 self._rest 为 self._exchange
- [ ] 向后兼容：默认仍是 LiveOKXAdapter

### Phase 4：回测运行器 + 报告（~4h）
- [ ] `quant/backtest/runner.py` 主循环
- [ ] `BacktestReport` 结构化输出
- [ ] markdown 报告：PnL 曲线 / 回撤 / 胜率 / 每笔盈亏分布

### Phase 5：压力测试集成（~4h）
- [ ] `quant/backtest/stress_tests.py` 收集 T1-T6 用例
- [ ] 每次 grid_pro 改动前必须过这 6 个 case
- [ ] CI-like：跑一次，报告通过/失败

---

## 六、预期产出

**工期**：1.5-2 天（offline 开发，不碰实盘）

**交付**：
1. `quant/exchange/adapter.py`
2. `quant/backtest/{data_loader, runner, stress_tests}.py`
3. 一份压力测试报告：当前 grid_pro 在 T1-T6 的表现
4. 如果 T3 / T5 失败 → 明确下一步优化方向

**价值**：
- 所有未来改动（L2 重构、因子、ML）都可**先回测再上线**
- 风险降 10x
- 夜里可以自动跑"假设市况 X 系统表现"，第二天看报告
