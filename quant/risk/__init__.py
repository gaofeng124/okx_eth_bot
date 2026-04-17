"""Merged risk package."""
from __future__ import annotations


# ----- balance.py -----

from typing import Any


def parse_balance_availability(data: dict[str, Any]) -> dict[str, float]:
    rows = data.get("data") or []
    if not rows or not isinstance(rows[0], dict):
        return {}
    details = rows[0].get("details") or []
    out: dict[str, float] = {}
    for d in details:
        if not isinstance(d, dict) or "ccy" not in d:
            continue
        ccy = str(d["ccy"])
        try:
            out[ccy] = float(
                d.get("availEq")
                or d.get("availBal")
                or d.get("cashBal")
                or 0.0
            )
        except (TypeError, ValueError):
            out[ccy] = 0.0
    return out


def split_inst_id(inst_id: str) -> tuple[str, str]:
    if "-" in inst_id:
        p = inst_id.split("-")
        if len(p) >= 2:
            return p[0], p[1]
    return "", ""

# ----- half_kelly.py -----

"""
永续 Half-Kelly：由最近若干笔成交的 fillPnl 估计 win_rate 与盈亏比，得到风险比例并映射为张数。

Kelly（二元/标量近似）：f* = p - (1-p)/b，其中 p=win_rate，b=avg_win/avg_loss（平均盈利/平均亏损幅度）。
Half-Kelly：f_half = 0.5 * max(0, f*)。

张数：将 f_half 视为权益中用于**保证金**的比例（与执行层 isolated 口径一致），则
  contracts ≈ equity × f_half × leverage / (buffer × mid × ctVal)。
"""

import math
from typing import Any


def _f(x: Any) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _ts_ms(r: dict[str, Any]) -> int:
    try:
        return int(r.get("ts") or r.get("fillTime") or 0)
    except (TypeError, ValueError):
        return 0


def take_recent_swap_fills(fills: list[dict[str, Any]], *, max_fills: int = 100) -> list[dict[str, Any]]:
    """按时间取最近 max_fills 条（升序排序后取尾部）。"""
    rows = [x for x in fills if isinstance(x, dict)]
    rows.sort(key=_ts_ms)
    if len(rows) <= max_fills:
        return rows
    return rows[-max_fills:]


def half_kelly_stats_from_fill_pnls(
    fills: list[dict[str, Any]],
    *,
    max_fills: int = 100,
) -> dict[str, Any]:
    """
    逐笔 fill 的 fillPnl（SWAP）作为一次独立结果：
    - win: fillPnl > 0
    - loss: fillPnl < 0
    忽略 fillPnl == 0（不计入 win/loss 计数，但可计入 n 需约定：此处 n 为用于统计的非零或全部）

    为稳健：仅对「有 fillPnl 字段且不全为 0」的样本估计；否则返回 insufficient。
    """
    recent = take_recent_swap_fills(fills, max_fills=max_fills)
    pnls: list[float] = []
    for r in recent:
        pnl = _f(r.get("fillPnl"))
        pnls.append(pnl)

    n_all = len(pnls)
    if n_all == 0:
        return {
            "n": 0,
            "n_used": 0,
            "win_rate": None,
            "avg_win": None,
            "avg_loss": None,
            "win_loss_ratio": None,
            "f_star": None,
            "risk_frac_half_kelly": None,
            "insufficient": True,
        }

    wins = [p for p in pnls if p > 1e-12]
    losses = [p for p in pnls if p < -1e-12]
    zeros = n_all - len(wins) - len(losses)

    # 胜率：盈利笔数 / 有明确盈亏方向的笔数（赢+输）；若全为 0 则无法估计
    n_dir = len(wins) + len(losses)
    if n_dir == 0:
        return {
            "n": n_all,
            "n_used": 0,
            "win_rate": None,
            "avg_win": None,
            "avg_loss": None,
            "win_loss_ratio": None,
            "f_star": None,
            "risk_frac_half_kelly": None,
            "insufficient": True,
            "zero_pnls": zeros,
        }

    win_rate = len(wins) / float(n_dir)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(-p for p in losses) / len(losses) if losses else 0.0

    if avg_loss > 1e-12:
        wl_ratio = avg_win / avg_loss
    else:
        wl_ratio = float("inf") if avg_win > 1e-12 else 0.0

    if wl_ratio == float("inf"):
        f_star = win_rate
    elif wl_ratio > 1e-12:
        f_star = win_rate - (1.0 - win_rate) / wl_ratio
    else:
        f_star = -1.0

    f_star = float(max(-1.0, min(1.0, f_star)))
    risk_half = 0.5 * max(0.0, f_star)

    return {
        "n": n_all,
        "n_used": n_dir,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "win_loss_ratio": wl_ratio if math.isfinite(wl_ratio) else None,
        "f_star": f_star,
        "risk_frac_half_kelly": risk_half,
        "insufficient": False,
        "zero_pnls": zeros,
    }


def contracts_from_half_kelly_risk(
    *,
    risk_frac: float,
    equity_usdt: float,
    mid: float,
    ct_val: float,
    leverage: float,
    balance_buffer_pct: float,
    min_contracts: float,
    max_contracts: float,
) -> float:
    """
    risk_frac：Half-Kelly 得到的 [0,1] 权益保证金占比。
    与 _size_with_risk_budget 中 swap 分支一致：max_aff ≈ (qa * lev) / (buf * mid * ctVal)。
    此处用 equity×risk_frac 代替可用 qa 作为目标保证金上界。
    """
    if risk_frac <= 0 or equity_usdt <= 0 or mid <= 0 or ct_val <= 0 or leverage <= 0:
        return 0.0
    buf = 1.0 + max(0.0, float(balance_buffer_pct))
    margin_target = float(equity_usdt) * float(risk_frac)
    raw = (margin_target * float(leverage)) / (buf * float(mid) * float(ct_val))
    return max(float(min_contracts), min(float(max_contracts), raw))


def recommend_contracts(
    *,
    fills: list[dict[str, Any]],
    equity_usdt: float | None,
    mid: float,
    ct_val: float,
    leverage: float,
    balance_buffer_pct: float,
    min_contracts: float,
    max_contracts: float,
    lot_sz: float,
    conservative_contracts: float,
    min_samples: int,
    max_fills: int,
) -> tuple[float, dict[str, Any]]:
    """
    返回 (recommended_contracts, debug_dict)。
    样本不足 min_samples 或无法估计时，返回 conservative_contracts（已夹在 [min,max] 内）。
    """
    dbg: dict[str, Any] = {}
    st = half_kelly_stats_from_fill_pnls(fills, max_fills=max_fills)
    dbg["stats"] = st

    n_all = int(st.get("n") or 0)
    n_dir = int(st.get("n_used") or 0)
    if n_all < int(min_samples):
        dbg["mode"] = "conservative"
        dbg["reason"] = "fills_lt_min_samples"
        c = max(float(min_contracts), min(float(max_contracts), float(conservative_contracts)))
        return c, dbg
    if st.get("insufficient") or n_dir < 1:
        dbg["mode"] = "conservative"
        dbg["reason"] = "insufficient_pnl_signal"
        c = max(float(min_contracts), min(float(max_contracts), float(conservative_contracts)))
        return c, dbg

    rf = float(st.get("risk_frac_half_kelly") or 0.0)
    if equity_usdt is None or equity_usdt <= 0:
        dbg["mode"] = "conservative"
        dbg["reason"] = "no_equity"
        c = max(float(min_contracts), min(float(max_contracts), float(conservative_contracts)))
        return c, dbg

    raw_c = contracts_from_half_kelly_risk(
        risk_frac=rf,
        equity_usdt=float(equity_usdt),
        mid=float(mid),
        ct_val=float(ct_val),
        leverage=float(leverage),
        balance_buffer_pct=balance_buffer_pct,
        min_contracts=min_contracts,
        max_contracts=max_contracts,
    )
    dbg["mode"] = "half_kelly"
    dbg["contracts_raw"] = raw_c

    # lotSz 步长（与 lev5 _quantize_contracts 一致）
    lot_sz = float(lot_sz)
    if lot_sz > 0:
        k = int(round(raw_c / lot_sz))
        q = k * lot_sz
    else:
        q = raw_c
    q = max(float(min_contracts), min(float(max_contracts), q))
    dbg["contracts_quantized"] = q
    return float(q), dbg
# ----- circuit.py -----

import time


class CircuitBreaker:
    """连续失败 N 次后冷却一段时间，防止异常刷屏与资金风险。"""

    def __init__(self, max_failures: int, cooldown_sec: float) -> None:
        self._max = max_failures
        self._cooldown = cooldown_sec
        self._failures = 0
        self._open_until = 0.0

    def allow(self) -> bool:
        return time.monotonic() >= self._open_until

    def seconds_until_open(self) -> float:
        return max(0.0, self._open_until - time.monotonic())

    def record_success(self) -> None:
        self._failures = 0

    def record_failure(self) -> bool:
        """返回 True 表示刚触发熔断（进入冷却）。"""
        self._failures += 1
        if self._failures >= self._max:
            self._open_until = time.monotonic() + self._cooldown
            self._failures = 0
            return True
        return False
# ----- daily_drawdown_breaker.py -----

"""
UTC 交易日日内亏损熔断：基准权益为当日 UTC 首次观测到的账户 totalEq，
若 (基准 − 当前权益) / 基准 ≥ RISK_DAILY_MAX_LOSS_PCT，则当日禁止新开仓，仅允许减仓类指令。
次日 UTC 00:00 后首次更新权益时自动重置。
"""

import math
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from quant.logging_config import get_logger
from quant.models import OrderIntent

log = get_logger(__name__)


def intent_is_reduce_only(intent: OrderIntent) -> bool:
    """永续 reduce_only 或现货减仓卖单 — 均视为平仓/减仓，不受日内熔断拦截。"""
    return bool(intent.reduce_only) or bool(getattr(intent, "reduce_only_sell", False))


@dataclass
class DailyDrawdownBreaker:
    max_loss_pct: float | None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _utc_date: date | None = None
    _day_open_equity_usdt: float | None = None
    _halted: bool = False

    def __post_init__(self) -> None:
        m = self.max_loss_pct
        if m is not None and (not math.isfinite(m) or m <= 0):
            self.max_loss_pct = None

    def enabled(self) -> bool:
        return self.max_loss_pct is not None

    def allows_opening_intent(self, intent: OrderIntent) -> bool:
        if not self.enabled():
            return True
        if intent_is_reduce_only(intent):
            return True
        with self._lock:
            return not self._halted

    def update_from_equity(
        self,
        equity_usdt: float | None,
        runtime: dict[str, Any] | None = None,
    ) -> None:
        """
        用最新账户权益（建议 OKX balance data[0].totalEq）更新日内亏损状态。
        每个 UTC 交易日第一次调用时把 equity 记为当日基准并重置 halted。
        """
        if not self.enabled():
            return
        if equity_usdt is None or not math.isfinite(float(equity_usdt)):
            return
        eq = float(equity_usdt)
        today = datetime.now(timezone.utc).date()

        with self._lock:
            if self._utc_date != today:
                self._utc_date = today
                self._day_open_equity_usdt = eq
                self._halted = False
                log.info(
                    "[DailyDrawdown] UTC 新交易日基准权益 totalEq≈%.4f USDT | date=%s",
                    eq,
                    today,
                )
                self._sync_runtime(runtime, loss_pct=0.0, drawdown_usdt=0.0)
                return

            base = self._day_open_equity_usdt
            if base is None or base <= 0:
                self._day_open_equity_usdt = eq
                self._sync_runtime(runtime, loss_pct=0.0, drawdown_usdt=0.0)
                return

            if self._halted:
                lp = max(0.0, (base - eq) / base) if base > 0 else 0.0
                self._sync_runtime(
                    runtime,
                    loss_pct=lp,
                    drawdown_usdt=max(0.0, base - eq),
                )
                return

            drawdown = base - eq
            loss_pct = max(0.0, drawdown / base) if drawdown > 0 else 0.0

            was_halted = self._halted
            if loss_pct >= float(self.max_loss_pct) - 1e-12:  # type: ignore[arg-type]
                self._halted = True

            self._sync_runtime(
                runtime,
                loss_pct=loss_pct,
                drawdown_usdt=max(0.0, drawdown),
            )

            if self._halted and not was_halted:
                log.error(
                    "[DailyDrawdown] 日内亏损达上限，禁止新开仓至 UTC 次日 | "
                    "loss_pct=%.4f cap=%.4f day_open≈%.4f equity≈%.4f",
                    loss_pct,
                    float(self.max_loss_pct),  # type: ignore[arg-type]
                    base,
                    eq,
                )

    def _sync_runtime(
        self,
        runtime: dict[str, Any] | None,
        *,
        loss_pct: float,
        drawdown_usdt: float,
    ) -> None:
        if runtime is None:
            return
        runtime["daily_dd_halted"] = bool(self._halted)
        runtime["daily_dd_utc_date"] = (
            str(self._utc_date) if self._utc_date is not None else None
        )
        if self._day_open_equity_usdt is not None:
            runtime["daily_dd_day_open_equity_usdt"] = self._day_open_equity_usdt
        runtime["daily_dd_loss_pct"] = loss_pct
        runtime["daily_dd_drawdown_usdt"] = drawdown_usdt
        cap = self.max_loss_pct
        runtime["daily_dd_cap_pct"] = float(cap) if cap is not None else None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "utc_date": str(self._utc_date) if self._utc_date else None,
                "day_open_equity_usdt": self._day_open_equity_usdt,
                "halted": self._halted,
                "max_loss_pct": self.max_loss_pct,
            }
# ----- engine.py -----

from dataclasses import dataclass
from typing import Any

from quant.logging_config import get_logger

log = get_logger(__name__)

from quant import settings as _settings
from quant.models import OrderIntent, is_priced_order

# --- 原 swap_margin.py：永续保证金与 instId 判断（合并以减少文件）---
SWAP_MARGIN_PRECHECK_SAFETY = 1.05


def is_swap_inst_id(inst_id: str) -> bool:
    return str(inst_id).upper().endswith("-SWAP")


def swap_open_initial_margin_usdt(
    *,
    sz_contracts: float,
    px: float,
    ct_val: float,
    leverage: float,
    safety: float = SWAP_MARGIN_PRECHECK_SAFETY,
) -> float:
    """开仓（多/空）初始保证金约 = 名义 / 杠杆 × safety。"""
    notional = abs(float(px)) * abs(float(sz_contracts)) * abs(float(ct_val))
    lev = max(abs(float(leverage)), 1e-9)
    return (notional / lev) * float(safety)


class RiskError(RuntimeError):
    """风控拒绝。"""


@dataclass(frozen=True)
class RiskConfig:
    max_notional_usdt: float | None = None
    max_order_base: float | None = None
    # 线性 USDT 永续：OKX 的 sz 为「张」；每张对应 ctVal 枚 base（如 ETH-USDT-SWAP 常為 0.01）。
    # 与 RISK_MAX_ORDER_BASE（币本位上限）比较时，用 base_qty = sz * swap_ct_val。
    swap_ct_val: float | None = None
    # 与交易所可用余额联动（限价单）；需 ExecutionService 传入 balance_snapshot
    check_balance: bool = False
    balance_buffer_pct: float = 0.0


class RiskEngine:
    """下单前校验：名义、数量上限、可选可用余额。"""

    def __init__(self, cfg: RiskConfig) -> None:
        self._cfg = cfg

    @staticmethod
    def _is_swap_intent(intent: OrderIntent) -> bool:
        """永续：以 instrument_type 或 instId 后缀为准，避免默认 spot 误走现货 base 校验。"""
        if getattr(intent, "instrument_type", None) == "swap":
            return True
        return is_swap_inst_id(intent.inst_id)

    def base_qty_for_risk(self, intent: OrderIntent, sz: float) -> float:
        """现货：sz 即 base 数量；线性永续：sz 为张数 × ctVal → base 数量。"""
        if (
            self._is_swap_intent(intent)
            and self._cfg.swap_ct_val is not None
            and float(self._cfg.swap_ct_val) > 0
        ):
            return sz * float(self._cfg.swap_ct_val)
        return sz

    def notional_usdt_approx(self, intent: OrderIntent, px: float, sz: float) -> float:
        """限价单名义（USDT）：现货 px×sz；线性永续 px×sz×ctVal。"""
        return px * self.base_qty_for_risk(intent, sz)

    def check(
        self,
        intent: OrderIntent,
        *,
        balance_snapshot: dict[str, Any] | None = None,
    ) -> None:
        try:
            sz = float(intent.sz)
        except ValueError as e:
            raise RiskError(f"非法 sz: {intent.sz}") from e
        if sz <= 0:
            raise RiskError(f"非法 sz（必须>0）: {intent.sz}")
        # reduce_only=True 表示平仓/减仓单，必须能执行（否则仓位卡死）
        # reduce_only_sell 是现货减仓的兼容标记
        relax = bool(getattr(intent, "reduce_only_sell", False)) or bool(intent.reduce_only)
        base_qty = self.base_qty_for_risk(intent, sz)
        ignore_swap_caps = intent.instrument_type == "swap" and bool(
            getattr(_settings, "RISK_SWAP_IGNORE_MAX_CAPS", True)
        )
        if not ignore_swap_caps:
            if (
                self._cfg.max_order_base is not None
                and base_qty > self._cfg.max_order_base + 1e-12
                and not relax
            ):
                extra = ""
                if self._is_swap_intent(intent) and self._cfg.swap_ct_val:
                    extra = (
                        f"（永续张数={sz} × ctVal={self._cfg.swap_ct_val} → base≈{base_qty:.6f}）"
                    )
                raise RiskError(
                    f"单笔 base 数量 {base_qty:.6f}{extra} 超过 "
                    f"RISK_MAX_ORDER_BASE={self._cfg.max_order_base}"
                )
            if is_priced_order(intent.ord_type):
                if not intent.px:
                    raise RiskError(f"{intent.ord_type} 单缺少 px")
                try:
                    px = float(intent.px)
                except ValueError as e:
                    raise RiskError(f"非法 px: {intent.px}") from e
                if px <= 0:
                    raise RiskError(f"非法 px（必须>0）: {intent.px}")
                notional = px * base_qty
                if (
                    self._cfg.max_notional_usdt is not None
                    and notional > self._cfg.max_notional_usdt + 1e-9
                    and not relax
                ):
                    raise RiskError(
                        f"单笔名义约 {notional:.4f} USDT 超过 "
                        f"RISK_MAX_NOTIONAL_USDT={self._cfg.max_notional_usdt}"
                    )
        elif is_priced_order(intent.ord_type):
            if not intent.px:
                raise RiskError(f"{intent.ord_type} 单缺少 px")
            try:
                px = float(intent.px)
            except ValueError as e:
                raise RiskError(f"非法 px: {intent.px}") from e
            if px <= 0:
                raise RiskError(f"非法 px（必须>0）: {intent.px}")
        # 市价名义依赖 tgt_ccy，此处略；需要时可按盘口中间价估算

        if not self._cfg.check_balance or balance_snapshot is None:
            return
        if not is_priced_order(intent.ord_type) or not intent.px:
            return
        avail_map = parse_balance_availability(balance_snapshot)
        base, quote = split_inst_id(intent.inst_id)
        buf = 1.0 + max(0.0, float(self._cfg.balance_buffer_pct))
        try:
            sz = float(intent.sz)
            px = float(intent.px)
        except (TypeError, ValueError):
            return
        # 永续：exchange 的交易量 sz 是“张”；风控需要把它换算成 base_qty 才能得到正确的名义/保证金量纲。
        base_qty = self.base_qty_for_risk(intent, sz)

        # 仅减仓意图通常不需要严格的余额覆盖（用于减少“关仓资金不足误拒单”）。
        relax = bool(getattr(intent, "reduce_only_sell", False))
        if relax:
            return
        # 永续平仓：不按「新开保证金」卡 USDT（避免误拒）；交由交易所与 reduce 逻辑。
        if self._is_swap_intent(intent) and bool(intent.reduce_only):
            return
        # 永续开仓（买=多、卖=空）：统一用 USDT 可用 vs 初始保证金（名义/杠杆×缓冲），不查现货 base
        if self._is_swap_intent(intent) and not bool(intent.reduce_only):
            if not quote or quote not in avail_map:
                return
            notional = px * base_qty
            lev = float(intent.leverage) if isinstance(intent.leverage, (int, float)) and intent.leverage else 0.0
            need = (notional / lev) * buf if lev > 0 else (notional * buf)
            got = float(avail_map[quote])
            if got < need:
                raise RiskError(
                    f"可用 {quote}={got:.8f} < 本笔约需保证金 {need:.8f}（swap 开多/开空；含缓冲 {self._cfg.balance_buffer_pct:.4%}）"
                )
            return
        if intent.side == "buy" and quote:
            if quote not in avail_map:
                return
            notional = px * base_qty
            need = (px * sz) * buf
            got = float(avail_map[quote])
            if got < need:
                raise RiskError(
                    f"可用 {quote}={got:.8f} < 本笔约需 {need:.8f}（含缓冲 {self._cfg.balance_buffer_pct:.4%}）"
                )
        if intent.side == "sell" and base:
            if base not in avail_map:
                return
            need = base_qty * buf
            got = float(avail_map[base])
            if got < need:
                # 快照与下单瞬间余额差 1e-6 量级、或仅减仓数量取整导致 dust
                slack = max(1e-10, 1e-5 * max(got, need))
                if need - got <= slack:
                    log.debug(
                        "[风控] 卖单余额差在容差内通过 | need=%.10f got=%.10f slack=%.2e",
                        need,
                        got,
                        slack,
                    )
                    return
                raise RiskError(
                    f"可用 {base}={got:.8f} < 本笔卖出 {need:.8f}（含缓冲 {self._cfg.balance_buffer_pct:.4%}）"
                )

    @staticmethod
    def recommend_half_kelly_swap_contracts(
        *,
        fills: list[dict[str, Any]],
        equity_usdt: float | None,
        mid: float,
        ct_val: float,
        leverage: float,
        balance_buffer_pct: float,
        min_contracts: float,
        max_contracts: float,
        lot_sz: float,
        conservative_contracts: float,
        min_samples: int,
        max_fills: int,
    ) -> tuple[float, dict[str, Any]]:
        """
        由最近成交 fill 的 fillPnl 估计 Half-Kelly 风险比例并映射为永续张数上界。
        样本不足时返回保守张数（调用方通常取 LEV5_MIN_CONTRACTS）。
        """
        return recommend_contracts(
            fills=fills,
            equity_usdt=equity_usdt,
            mid=mid,
            ct_val=ct_val,
            leverage=leverage,
            balance_buffer_pct=balance_buffer_pct,
            min_contracts=min_contracts,
            max_contracts=max_contracts,
            lot_sz=lot_sz,
            conservative_contracts=conservative_contracts,
            min_samples=min_samples,
            max_fills=max_fills,
        )