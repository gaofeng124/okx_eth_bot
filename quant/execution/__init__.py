"""Merged execution package: correlation_guard + service + pipeline."""
from __future__ import annotations

import time
from typing import Any

from quant.exchange.okx_err import (
    is_insufficient_margin_error,
    is_no_position_reduce_error,
    is_posside_parameter_error,
)
from quant.logging_config import get_logger
from quant.models import OrderIntent, is_priced_order
from quant.risk import RiskEngine

log = get_logger(__name__)
_CORR_NON_SWAP_WARNED = False


def _detailed_exec(event: str, **fields: Any) -> None:
    try:
        from quant.detailed_daily_log import record_execution

        record_execution(event, **fields)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 单笔意图：方向桶 + 名义
# ---------------------------------------------------------------------------


def exposure_side_opening_intent(intent: OrderIntent) -> str | None:
    """buy→long，sell→short；减仓类不计入。"""
    if intent.reduce_only:
        return None
    if getattr(intent, "reduce_only_sell", False):
        return None
    if intent.side == "buy":
        return "long"
    if intent.side == "sell":
        return "short"
    return None


def notional_usdt_for_guard(intent: OrderIntent, risk: RiskEngine) -> float | None:
    """与风控一致的名义；无法估算则 None（不拦截）。"""
    if not is_priced_order(intent.ord_type) or not intent.px:
        return None
    try:
        px = float(intent.px)
        sz = float(intent.sz)
    except (TypeError, ValueError):
        return None
    if px <= 0 or sz <= 0:
        return None
    return risk.notional_usdt_approx(intent, px, sz)


def _reduce_only_from_row(row: dict[str, Any]) -> bool:
    v = row.get("reduceOnly")
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes")


def _intent_from_okx_pending_row(
    row: dict[str, Any],
    *,
    inst_id: str,
    instrument_type: str,
) -> OrderIntent | None:
    rid = str(row.get("instId") or inst_id)
    side = str(row.get("side") or "").lower()
    if side not in ("buy", "sell"):
        return None
    sz = row.get("sz")
    px = row.get("px")
    if sz is None or px is None or str(px).strip() == "":
        return None
    return OrderIntent(
        inst_id=rid,
        side=side,  # type: ignore[arg-type]
        ord_type="limit",
        sz=str(sz),
        px=str(px),
        instrument_type="swap" if instrument_type == "swap" else "spot",  # type: ignore[arg-type]
        reduce_only=_reduce_only_from_row(row),
    )


def sync_exchange_corr_exposure(
    orders_pending_raw: dict[str, Any],
    *,
    inst_id: str,
    risk: RiskEngine,
    runtime: dict[str, Any],
) -> None:
    """
    由 GET orders-pending 结果更新 runtime：
    - corr_exchange_exposure_usdt: {long, short}
    - corr_exchange_clord_ids: 挂单 clOrdId 列表（与本地 pending 去重用）
    """
    global _CORR_NON_SWAP_WARNED
    inst_u = inst_id.upper()
    if not inst_u.endswith("-SWAP"):
        if not _CORR_NON_SWAP_WARNED:
            log.warning(
                "[corr] INST_ID 非 *-SWAP，跳过交易所挂单敞口同步（本管线仅支持线性永续）| inst_id=%s",
                inst_id,
            )
            _CORR_NON_SWAP_WARNED = True
        runtime["corr_exchange_exposure_usdt"] = {"long": 0.0, "short": 0.0}
        runtime["corr_exchange_clord_ids"] = []
        return
    inst_type = "swap"
    rows = orders_pending_raw.get("data") or []
    long_u = 0.0
    short_u = 0.0
    cl_ids: list[str] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("instId") or "").upper() != inst_u:
            continue
        cl = str(r.get("clOrdId") or "").strip()
        if cl:
            cl_ids.append(cl)
        oi = _intent_from_okx_pending_row(r, inst_id=inst_id, instrument_type=inst_type)
        if oi is None:
            continue
        side_tag = exposure_side_opening_intent(oi)
        nu = notional_usdt_for_guard(oi, risk)
        if side_tag is None or nu is None:
            continue
        if side_tag == "long":
            long_u += float(nu)
        else:
            short_u += float(nu)
    runtime["corr_exchange_exposure_usdt"] = {"long": long_u, "short": short_u}
    runtime["corr_exchange_clord_ids"] = cl_ids


def _pending_bucket_sums(
    pending_orders: dict[str, Any],
    *,
    inst_id: str,
    exchange_clord_ids: set[str],
) -> tuple[float, float]:
    """本地 pending_orders（含未出现在交易所快照中的在途单）按方向累加名义。"""
    long_u = 0.0
    short_u = 0.0
    inst_u = inst_id.upper()
    for cid, e in pending_orders.items():
        if cid in exchange_clord_ids:
            continue
        if not isinstance(e, dict):
            continue
        if str(e.get("inst_id") or "").upper() != inst_u:
            continue
        side_tag = e.get("exposure_side")
        nu = e.get("notional_usdt")
        if not isinstance(nu, (int, float)):
            continue
        if side_tag == "long":
            long_u += float(nu)
        elif side_tag == "short":
            short_u += float(nu)
    return long_u, short_u


def aggregate_same_direction_exposure_usdt(
    *,
    inst_id: str,
    intent: OrderIntent,
    risk: RiskEngine,
    runtime: dict[str, Any] | None,
) -> tuple[str | None, float | None, float, float]:
    """
    返回 (新单方向 long|short, 新单名义, 同向当前总名义含挂单, 对向总名义)。

    同向总名义 = 交易所挂单快照（runtime）+ 本地 pending（去重 clOrdId）后与「新单同向」比较。
    """
    new_side = exposure_side_opening_intent(intent)
    new_n = notional_usdt_for_guard(intent, risk)
    if runtime is None:
        return new_side, new_n, 0.0, 0.0

    exch = runtime.get("corr_exchange_exposure_usdt")
    if not isinstance(exch, dict):
        exch = {}
    ex_long = float(exch.get("long") or 0.0)
    ex_short = float(exch.get("short") or 0.0)

    raw_ids = runtime.get("corr_exchange_clord_ids")
    if isinstance(raw_ids, list):
        ex_cl = {str(x) for x in raw_ids if x}
    else:
        ex_cl = set()

    po = runtime.get("pending_orders")
    if not isinstance(po, dict):
        po = {}
    pl, ps = _pending_bucket_sums(po, inst_id=inst_id, exchange_clord_ids=ex_cl)

    same_long = ex_long + pl
    same_short = ex_short + ps
    return new_side, new_n, same_long, same_short


def should_reject_correlated_intent(
    *,
    cap_usdt: float,
    inst_id: str,
    intent: OrderIntent,
    risk: RiskEngine,
    runtime: dict[str, Any] | None,
) -> tuple[bool, dict[str, Any]]:
    """
    若新单为开仓且同向累计（挂单+在途）+ 新单名义 > cap，则拒绝。

    返回 (reject, detail_dict)；detail 用于审计日志。
    """
    new_side, new_n, same_long, same_short = aggregate_same_direction_exposure_usdt(
        inst_id=inst_id,
        intent=intent,
        risk=risk,
        runtime=runtime,
    )
    detail: dict[str, Any] = {
        "new_side": new_side,
        "new_notional_usdt": new_n,
        "exchange_long_usdt": None,
        "exchange_short_usdt": None,
        "pending_extra_long_usdt": None,
        "pending_extra_short_usdt": None,
        "same_long_usdt": same_long,
        "same_short_usdt": same_short,
        "cap_usdt": cap_usdt,
    }
    if runtime and isinstance(runtime.get("corr_exchange_exposure_usdt"), dict):
        ce = runtime["corr_exchange_exposure_usdt"]
        detail["exchange_long_usdt"] = ce.get("long")
        detail["exchange_short_usdt"] = ce.get("short")

    if new_side is None or new_n is None:
        return False, detail

    if new_side == "long":
        total_same = same_long + float(new_n)
        other = same_short
    else:
        total_same = same_short + float(new_n)
        other = same_long
    detail["total_same_direction_usdt"] = total_same
    detail["opposite_direction_usdt"] = other

    if total_same > cap_usdt + 1e-9:
        detail["reason"] = "max_corr_notional_exceeded"
        return True, detail
    return False, detail


def enrich_pending_entry(
    intent: OrderIntent,
    risk: RiskEngine,
) -> dict[str, Any]:
    """写入 runtime pending_orders 单条所需的字段。"""
    nu = notional_usdt_for_guard(intent, risk)
    side = exposure_side_opening_intent(intent)
    return {
        "inst_id": intent.inst_id,
        "sz": intent.sz,
        "px": intent.px,
        "side": intent.side,
        "pos_side": intent.pos_side,
        "reduce_only": intent.reduce_only,
        "ord_type": intent.ord_type,
        "notional_usdt": nu,
        "exposure_side": side,
    }


def corr_notional_cap_usdt() -> float | None:
    """自 settings；未设置或非正数则关闭 CorrelationGuard。"""
    from quant import settings as S

    raw = str(getattr(S, "RISK_MAX_CORR_NOTIONAL_USDT", "") or "").strip()
    if not raw:
        return None
    try:
        x = float(raw)
    except ValueError:
        return None
    if x <= 0:
        return None
    return x


def apply_optimistic_corr_after_submit(
    runtime: dict[str, Any],
    intent: OrderIntent,
    risk: RiskEngine,
) -> None:
    """
    REST 已受理、本地 pending 即将移除时，把本笔名义并入 corr_exchange_exposure_usdt，
    直到对账拉 orders-pending 覆盖为止。避免「仅挂在交易所、不在 pending」窗口内低估同向暴露。
    """
    if corr_notional_cap_usdt() is None:
        return
    side = exposure_side_opening_intent(intent)
    nu = notional_usdt_for_guard(intent, risk)
    if side is None or nu is None:
        return
    ex = runtime.setdefault("corr_exchange_exposure_usdt", {"long": 0.0, "short": 0.0})
    if not isinstance(ex, dict):
        return
    key = "long" if side == "long" else "short"
    ex[key] = float(ex.get(key) or 0.0) + float(nu)
    cid = str(intent.client_order_id or "").strip()
    if cid:
        lst = runtime.setdefault("corr_exchange_clord_ids", [])
        if isinstance(lst, list) and cid not in lst:
            lst.append(cid)
    runtime["corr_last_optimistic_bump_ts"] = time.time()


# ----- service.py -----

import math
from dataclasses import replace
from typing import Any

from quant.exchange import OKXRestClient, get_swap_instrument_spec
from quant.logging_config import get_logger
from quant.models import OrderIntent, is_priced_order
from quant.risk import parse_balance_availability, split_inst_id
from quant.risk import RiskEngine, RiskError
from quant.settings import (
    LEV5_MIN_CONTRACTS,
    LEV5_TREND_ORDER_TYPE_ADX_MIN,
    LEV5_TREND_ORDER_TYPE_OVERRIDE,
    RISK_BALANCE_BUFFER_PCT,
    RISK_CHECK_BALANCE,
)

log = get_logger(__name__)


def _quantize_to_lot(sz: float, lot_sz: float) -> float:
    """永续合约张数：向下对齐 lotSz（OKX 51121）。所有 swap 侧 sz 截断/对齐须经此函数。"""
    ls = float(lot_sz)
    if sz <= 0 or ls <= 0:
        return 0.0
    return math.floor(sz / ls + 1e-9) * ls


class ExecutionService:
    """
    执行层：顺序为「风控校验 → 调用 OKX REST」。
    更外层的 OMS/审计/熔断在 ExecutionPipeline 中。
    """

    def __init__(self, client: OKXRestClient, risk: RiskEngine) -> None:
        self._client = client
        self._risk = risk
        self._force_net_pos_side = False
        self._swap_instrument_spec_cache: dict[str, dict[str, Any]] = {}

    def _fetch_balance_maybe(self) -> dict[str, Any] | None:
        if not RISK_CHECK_BALANCE:
            return None
        try:
            return self._client.balance()
        except Exception as e:
            log.warning(
                "[风控] 拉取账户余额失败，仅做名义/数量上限校验: %s",
                e,
            )
            return None

    def _enforce_min_notional(self, intent: OrderIntent) -> None:
        """永续：名义由 RISK_MAX_*、lotSz/minSz 与交易所受理约束；不启用 STRAT_ORDER_NOTIONAL_MIN。"""
        return

    @staticmethod
    def _lot_decimals(lot_sz: float) -> int:
        s = f"{float(lot_sz):.12f}".rstrip("0").rstrip(".")
        if "." not in s:
            return 0
        return len(s.split(".")[-1])

    @staticmethod
    def _format_swap_sz_str(q: float, lot_sz: float) -> str:
        q = _quantize_to_lot(q, lot_sz)
        if q <= 0:
            return "0"
        dec = ExecutionService._lot_decimals(lot_sz)
        if dec <= 0:
            return str(int(q + 1e-12))
        return f"{q:.{dec}f}".rstrip("0").rstrip(".")

    def _get_swap_instrument_spec(self, inst_id: str) -> dict[str, Any]:
        if inst_id in self._swap_instrument_spec_cache:
            return self._swap_instrument_spec_cache[inst_id]
        spec: dict[str, Any] | None = None
        try:
            spec = get_swap_instrument_spec(inst_id)
        except Exception as e:
            log.warning("[执行] 拉取 instrument_spec 失败，使用默认 lotSz/minSz: %s", e)
        if not spec:
            spec = {
                "lotSz": float(LEV5_MIN_CONTRACTS),
                "minSz": float(LEV5_MIN_CONTRACTS),
                "maxLmtSz": 1e18,
            }
        self._swap_instrument_spec_cache[inst_id] = spec
        return spec

    def _quantize_swap_intent_sz(self, intent: OrderIntent) -> OrderIntent:
        """
        所有永续（开仓 / reduce_only）在提交前必须经 lotSz 对齐，避免 OKX 51121。
        """
        if intent.instrument_type != "swap":
            return intent
        try:
            clipped = float(intent.sz)
        except (TypeError, ValueError):
            return intent
        spec = self._get_swap_instrument_spec(intent.inst_id)
        lot_sz = float(spec.get("lotSz") or LEV5_MIN_CONTRACTS)
        if lot_sz <= 0:
            lot_sz = float(LEV5_MIN_CONTRACTS)
        min_sz = float(spec.get("minSz") or lot_sz)
        mx = self._as_float(spec.get("maxLmtSz"))
        max_sz = mx if mx is not None and mx > 0 else 1e18

        aligned = _quantize_to_lot(clipped, lot_sz)
        max_aligned = _quantize_to_lot(max_sz, lot_sz)
        if aligned > max_aligned + 1e-12:
            aligned = max_aligned
        if aligned < min_sz - 1e-12:
            if clipped + 1e-12 >= min_sz:
                k = math.ceil(min_sz / lot_sz - 1e-12)
                aligned = _quantize_to_lot(float(k) * lot_sz, lot_sz)
                if aligned > max_aligned + 1e-12:
                    aligned = max_aligned
            else:
                aligned = 0.0
        if aligned > max_aligned + 1e-12:
            aligned = max_aligned
        if aligned <= 0:
            raise RiskError(
                f"永续 sz 经 lotSz 对齐后无效（clipped={clipped} lotSz={lot_sz} minSz={min_sz}）"
            )
        out = self._format_swap_sz_str(aligned, lot_sz)
        return replace(intent, sz=out)

    def _clip_swap_open_sz_to_margin(
        self,
        intent: OrderIntent,
        balance_snapshot: dict[str, Any] | None,
    ) -> OrderIntent:
        """
        永续开仓：按「可用 quote / 杠杆」动态缩小 sz，避免 51008。
        reduce_only 平仓不做此截断。
        """
        if intent.instrument_type != "swap" or bool(intent.reduce_only):
            return intent
        if balance_snapshot is None:
            return intent
        if not is_priced_order(intent.ord_type) or not intent.px:
            return intent
        try:
            px = float(intent.px)
            sz = float(intent.sz)
        except (TypeError, ValueError):
            return intent
        if px <= 0 or sz <= 0:
            return intent
        lev = float(intent.leverage) if isinstance(intent.leverage, (int, float)) else 0.0
        if lev <= 0:
            return intent
        _, quote = split_inst_id(intent.inst_id)
        if not quote:
            return intent
        avail_map = parse_balance_availability(balance_snapshot)
        if quote not in avail_map:
            return intent
        got = float(avail_map[quote])
        buf = 1.0 + max(0.0, float(RISK_BALANCE_BUFFER_PCT))
        max_margin = got / buf
        max_notional = max_margin * lev
        # notional = px * base_qty；base_qty 由风控引擎统一换算（swap: sz*ctVal）
        unit_notional = px * self._risk.base_qty_for_risk(intent, 1.0)
        if unit_notional <= 0:
            return intent
        max_sz = max_notional / unit_notional
        if sz <= max_sz + 1e-12:
            return intent
        clipped = float(max_sz)
        spec = self._get_swap_instrument_spec(intent.inst_id)
        lot_sz = float(spec.get("lotSz") or LEV5_MIN_CONTRACTS)
        if lot_sz <= 0:
            lot_sz = float(LEV5_MIN_CONTRACTS)
        clipped = _quantize_to_lot(clipped, lot_sz)
        if clipped <= 0:
            raise RiskError(
                f"可用 {quote}={got:.8f} 在杠杆 {lev:.2f}x 下不足以开仓（请求 sz={sz:.8f}）"
            )
        out = self._format_swap_sz_str(clipped, lot_sz)
        log.warning(
            "[执行] 永续开仓数量由 %.8f 截断为 %s（可用 %s=%.8f，杠杆=%.2fx，缓冲 %.4f%%；"
            "已 lotSz 对齐，随后 _quantize_swap_intent_sz 再统一校验 minSz）",
            sz,
            out,
            quote,
            got,
            lev,
            RISK_BALANCE_BUFFER_PCT * 100.0,
        )
        return replace(intent, sz=out)

    def _apply_trend_order_type_override(
        self,
        intent: OrderIntent,
        runtime: dict[str, Any] | None,
    ) -> OrderIntent:
        # 仅对开仓 post_only 生效，避免干扰平仓/减仓执行路径。
        if (
            intent.ord_type != "post_only"
            or bool(intent.reduce_only)
            or bool(getattr(intent, "reduce_only_sell", False))
            or not isinstance(runtime, dict)
        ):
            return intent
        regime = str(runtime.get("regime") or "").strip().lower()
        adx_raw = runtime.get("regime_adx")
        try:
            adx = float(adx_raw)
        except (TypeError, ValueError):
            return intent
        if regime != "trending" or adx <= float(LEV5_TREND_ORDER_TYPE_ADX_MIN):
            return intent
        override = str(LEV5_TREND_ORDER_TYPE_OVERRIDE or "").strip().lower()
        if override not in ("limit", "post_only"):
            override = "limit"
        if override == intent.ord_type:
            return intent
        log.info("[执行] 强趋势模式：订单类型从 post_only 切换为 %s。", override)
        return replace(intent, ord_type=override)


    @staticmethod
    def _as_float(v: Any) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _swap_has_reducible_position(self, intent: OrderIntent) -> bool:
        """
        reduce_only 前，先看交易所是否真的有可平仓位，避免 51169。
        - 卖单 reduce_only: 期望平 long
        - 买单 reduce_only: 期望平 short
        """
        try:
            raw = self._client.positions_swap(intent.inst_id)
        except Exception as e:
            # 查询失败时宁可继续走下单（避免误杀可平仓单）
            log.warning("[交易所] positions 查询失败，跳过 precheck: %s", e)
            return True
        rows = raw.get("data") or []
        if not isinstance(rows, list) or not rows:
            return False
        close_long = intent.side == "sell"
        close_short = intent.side == "buy"
        eps = 1e-12

        for r in rows:
            if not isinstance(r, dict):
                continue
            ps = str(r.get("posSide") or "").strip().lower()
            pos = self._as_float(r.get("pos"))
            if pos is None:
                pos = self._as_float(r.get("availPos"))
            if pos is None:
                continue
            if abs(pos) <= eps:
                continue
            if ps == "long" and close_long:
                return True
            if ps == "short" and close_short:
                return True
            if ps in ("net", ""):
                # net 模式常见：pos 正=净多，负=净空
                if close_long and pos > eps:
                    return True
                if close_short and pos < -eps:
                    return True
        return False

    def prepare_submit(
        self,
        intent: OrderIntent,
        *,
        runtime: dict[str, Any] | None = None,
    ) -> tuple[OrderIntent, dict[str, Any] | None]:
        """拉余额并截断数量，供 Pipeline 在写审计前得到与交易所将提交的 sz（永续含保证金截断）。"""
        if intent.instrument_type != "swap":
            raise RiskError("仅支持 OKX 线性永续（instrument_type=swap）")
        intent = self._apply_trend_order_type_override(intent, runtime)
        # 与 submit() 中顺序一致：先截断再 lot 对齐，避免 orders 表 sz 与真实 REST 不一致。
        bal = self._fetch_balance_maybe() if not bool(intent.reduce_only) else None
        if not bool(intent.reduce_only):
            intent = self._clip_swap_open_sz_to_margin(intent, bal)
        intent = self._quantize_swap_intent_sz(intent)
        return intent, bal

    def submit(
        self,
        intent: OrderIntent,
        *,
        balance_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if intent.instrument_type != "swap":
            raise RiskError("仅支持 OKX 线性永续（instrument_type=swap）")
        bal = balance_snapshot if balance_snapshot is not None else self._fetch_balance_maybe()
        if not bool(intent.reduce_only):
            intent = self._clip_swap_open_sz_to_margin(intent, bal)
        intent = self._quantize_swap_intent_sz(intent)
        self._enforce_min_notional(intent)
        # 1) 风控：名义、数量、可选与交易所可用余额对比（限价）
        self._risk.check(intent, balance_snapshot=bal)
        log.info(
            "[风控] 校验通过 | inst=%s side=%s ord_type=%s px=%s sz=%s",
            intent.inst_id,
            intent.side,
            intent.ord_type,
            intent.px,
            intent.sz,
        )
        if bool(intent.reduce_only):
            if not self._swap_has_reducible_position(intent):
                raise RiskError("reduce_only 跳过：交易所该方向无可平仓位（预检查）")
        pos_side = None if self._force_net_pos_side else intent.pos_side
        log.info(
            "[交易所] POST /api/v5/trade/order 合约 | clOrdId=%s tdMode=%s posSide=%s",
            intent.client_order_id,
            intent.td_mode,
            pos_side,
        )
        try:
            return self._client.place_order_swap(
                inst_id=intent.inst_id,
                side=intent.side,
                px=intent.px,
                sz=intent.sz,
                ord_type=intent.ord_type,
                td_mode=intent.td_mode,
                pos_side=pos_side,
                reduce_only=bool(intent.reduce_only),
                cl_ord_id=intent.client_order_id,
            )
        except Exception as e:
            if is_no_position_reduce_error(e):
                raise RiskError(f"reduce_only but no position (51169): {e}") from e
            if is_insufficient_margin_error(e):
                raise RiskError(f"保证金不足（51008）跳过: {e}") from e
            if pos_side and (
                is_posside_parameter_error(e) or "posSide" in str(e)
            ):
                log.warning("[交易所] posSide 被拒绝，自动回退为 net 兼容下单")
                self._force_net_pos_side = True
                try:
                    return self._client.place_order_swap(
                        inst_id=intent.inst_id,
                        side=intent.side,
                        px=intent.px,
                        sz=intent.sz,
                        ord_type=intent.ord_type,
                        td_mode=intent.td_mode,
                        pos_side=None,
                        reduce_only=bool(intent.reduce_only),
                        cl_ord_id=intent.client_order_id,
                    )
                except Exception as e2:
                    if is_no_position_reduce_error(e2):
                        raise RiskError(f"reduce_only fallback but no position (51169): {e2}") from e2
                    if is_insufficient_margin_error(e2):
                        raise RiskError(f"保证金不足（51008）跳过: {e2}") from e2
                    log.error(
                        "[交易所] posSide 回退下单仍失败 | inst=%s side=%s ord_type=%s px=%s sz=%s tdMode=%s "
                        "orig_posSide=%s reduceOnly=%s | err1=%s | err2=%s",
                        intent.inst_id,
                        intent.side,
                        intent.ord_type,
                        intent.px,
                        intent.sz,
                        intent.td_mode,
                        intent.pos_side,
                        bool(intent.reduce_only),
                        e,
                        e2,
                    )
                    raise
            raise

# ----- pipeline.py -----

import json
from dataclasses import replace
from typing import Any

import httpx

from quant.logging_config import get_logger
from quant.logging_config import brief_okx_order_response
from quant.metrics import Metrics
from quant.models import OrderIntent
from quant.oms import OrderManager
from quant.risk import CircuitBreaker
from quant.risk import DailyDrawdownBreaker
from quant.risk import RiskError
from quant.store import AuditStore, NullAuditStore

log = get_logger(__name__)


def _is_transient_http_error(e: BaseException) -> bool:
    """G：基础设施瞬断/限频 — 不计入熔断，避免 SSL/429 误伤真实交易窗口。"""
    if isinstance(
        e,
        (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.ProxyError,
        ),
    ):
        return True
    msg = str(e).lower()
    for t in ("429", "503", "504", "502", "timeout", "ssl", "eof", "reset", "broken pipe"):
        if t in msg:
            return True
    return False


class ExecutionPipeline:
    """
    执行管道（一次真实下单的完整链路）：
    1) 熔断是否允许交易
    2) DailyDrawdownBreaker：UTC 日内亏损是否已触发（仅拦新开仓）
    3) CorrelationGuard：同向名义（挂单+在途+REST 成功后乐观并入）与 RISK_MAX_CORR_NOTIONAL_USDT
    4) OMS 生成 clOrdId（若策略未带）
    5) 审计「订单提交」行
    6) ExecutionService：风控 → REST
    7) 审计「订单结果」；乐观更新同向暴露（若启用 CorrelationGuard）；更新指标；返回带 latency 的响应副本
    """

    def __init__(
        self,
        *,
        inner: ExecutionService,
        audit: AuditStore | NullAuditStore,
        metrics: Metrics,
        oms: OrderManager,
        circuit: CircuitBreaker,
        run_id: str,
        runtime: dict[str, Any] | None = None,
    ) -> None:
        self._inner = inner
        self._audit = audit
        self._metrics = metrics
        self._oms = oms
        self._circuit = circuit
        self._run_id = run_id
        self._runtime = runtime

    def _update_exec_quality(
        self,
        *,
        intent: OrderIntent,
        ok: bool,
        latency_ms: float | None = None,
    ) -> None:
        if self._runtime is None:
            return
        eq = self._runtime.setdefault(
            "exec_quality",
            {
                "post_only": 0.55,
                "ioc": 0.55,
                "limit": 0.55,
                "updated_ts": time.time(),
            },
        )
        key = intent.ord_type if intent.ord_type in ("post_only", "ioc", "limit") else "limit"
        prev = float(eq.get(key, 0.55))
        sample = 0.85 if ok else 0.15
        if latency_ms is not None and latency_ms > 800.0:
            sample *= 0.9
        cur = 0.85 * prev + 0.15 * sample
        eq[key] = max(0.0, min(1.0, cur))
        eq["updated_ts"] = time.time()

    def submit(self, intent: OrderIntent) -> dict[str, Any] | None:
        if not self._circuit.allow():
            self._metrics.inc_circuit_open()
            log.warning(
                "[熔断] 处于冷却窗口，本笔跳过下单 | run_id=%s 约 %.0fs 后可恢复",
                self._run_id,
                self._circuit.seconds_until_open(),
            )
            _detailed_exec(
                "circuit_skip",
                run_id=self._run_id,
                seconds_until_open=self._circuit.seconds_until_open(),
            )
            return None

        if self._runtime is not None:
            br = self._runtime.get("daily_drawdown_breaker")
            if (
                isinstance(br, DailyDrawdownBreaker)
                and br.enabled()
                and not br.allows_opening_intent(intent)
            ):
                log.warning(
                    "[DailyDrawdown] 拒绝新开仓（日内亏损达上限）| inst=%s | ro=%s ro_sell=%s",
                    intent.inst_id,
                    bool(intent.reduce_only),
                    bool(intent.reduce_only_sell),
                )
                try:
                    self._audit.log_execution_guard(
                        self._run_id,
                        guard_type="daily_drawdown",
                        inst_id=intent.inst_id,
                        reason="max_daily_loss_pct",
                        detail={
                            **br.snapshot(),
                            "runtime_daily_dd": {
                                k: self._runtime.get(k)
                                for k in (
                                    "daily_dd_loss_pct",
                                    "daily_dd_day_open_equity_usdt",
                                    "daily_dd_cap_pct",
                                )
                                if k in self._runtime
                            },
                        },
                    )
                except Exception as ex:
                    log.warning(
                        "[审计] log_execution_guard(daily_drawdown) 失败 | %s",
                        ex,
                    )
                _detailed_exec(
                    "daily_drawdown_block",
                    run_id=self._run_id,
                    inst_id=intent.inst_id,
                    reduce_only=bool(intent.reduce_only),
                )
                return None

        cid = intent.client_order_id or self._oms.new_cl_ord_id()
        intent = replace(intent, client_order_id=cid)
        if self._runtime is not None:
            exp_edge = None
            entry_kind = None
            try:
                if intent.features_json:
                    fj = json.loads(intent.features_json)
                    if isinstance(fj, dict):
                        got = fj.get("expected_edge_bps")
                        if isinstance(got, (int, float)):
                            exp_edge = float(got)
                        ek = fj.get("entry_kind")
                        if isinstance(ek, str) and ek.strip():
                            entry_kind = ek.strip()
            except Exception as ex:
                log.debug("[OMS] 解析 features_json 失败（忽略）| clOrdId=%s | %s", cid, ex)
            po = self._runtime.setdefault("pending_orders", {})
            if isinstance(po, dict):
                base = enrich_pending_entry(intent, self._inner._risk)
                po[cid] = {
                    "ts": time.time(),
                    **base,
                    "expected_edge_bps": exp_edge,
                    "entry_kind": entry_kind,
                }
        log.info(
            "[OMS] 使用 clOrdId=%s | 策略意图=%s | 特征=%s",
            cid,
            intent.reason,
            (intent.features_json[:200] + "…")
            if intent.features_json and len(intent.features_json) > 200
            else intent.features_json,
        )
        try:
            intent, bal = self._inner.prepare_submit(intent, runtime=self._runtime)
        except RiskError as e:
            log.warning("[结果] 下单前跳过 | clOrdId=%s | %s", cid, e)
            if self._runtime is not None:
                po = self._runtime.get("pending_orders")
                if isinstance(po, dict):
                    po.pop(cid, None)
            _detailed_exec(
                "prepare_submit_risk_reject",
                cl_ord_id=cid,
                error=str(e),
            )
            return None
        cap = corr_notional_cap_usdt()
        if cap is not None and self._runtime is not None:
            rej, detail = should_reject_correlated_intent(
                cap_usdt=cap,
                inst_id=intent.inst_id,
                intent=intent,
                risk=self._inner._risk,
                runtime=self._runtime,
            )
            if rej:
                log.warning(
                    "[CorrelationGuard] 拒绝同向名义超上限 | cap=%.4f USDT | inst=%s | %s",
                    cap,
                    intent.inst_id,
                    detail,
                )
                try:
                    self._audit.log_execution_guard(
                        self._run_id,
                        guard_type="correlation",
                        inst_id=intent.inst_id,
                        reason=str(detail.get("reason") or "max_corr_notional_exceeded"),
                        detail=detail,
                    )
                except Exception as ex:
                    log.warning(
                        "[审计] log_execution_guard(correlation) 失败 | %s",
                        ex,
                    )
                po = self._runtime.get("pending_orders")
                if isinstance(po, dict):
                    po.pop(cid, None)
                _detailed_exec(
                    "correlation_guard_block",
                    run_id=self._run_id,
                    inst_id=intent.inst_id,
                    cap_usdt=cap,
                    detail=detail,
                )
                return None
        try:
            self._audit.log_order_submit(self._run_id, cid, intent)
        except Exception as ex:
            log.error("[审计] log_order_submit 失败 | clOrdId=%s | %s", cid, ex)
            if self._runtime is not None:
                po = self._runtime.get("pending_orders")
                if isinstance(po, dict):
                    po.pop(cid, None)
            raise
        log.info("[审计] 已写入 orders 表（提交）| clOrdId=%s", cid)
        _detailed_exec(
            "rest_submit_begin",
            cl_ord_id=cid,
            inst_id=intent.inst_id,
            side=intent.side,
            ord_type=intent.ord_type,
            px=intent.px,
            sz=intent.sz,
            reason=intent.reason,
        )

        t0 = time.perf_counter()
        try:
            out = self._inner.submit(intent, balance_snapshot=bal)
            self._circuit.record_success()
            self._metrics.inc_orders_ok()
            self._audit.log_order_result(cid, True, out, None)
            if self._runtime is not None:
                try:
                    apply_optimistic_corr_after_submit(
                        self._runtime,
                        intent,
                        self._inner._risk,
                    )
                except Exception as ex:
                    log.warning(
                        "[CorrelationGuard] 乐观敞口更新失败（已跳过）| clOrdId=%s | %s",
                        cid,
                        ex,
                    )
            dt_ms = (time.perf_counter() - t0) * 1000
            out2 = dict(out)
            out2["_latency_ms"] = round(dt_ms, 2)
            self._update_exec_quality(intent=intent, ok=True, latency_ms=dt_ms)
            if self._runtime is not None:
                po = self._runtime.get("pending_orders")
                if isinstance(po, dict):
                    po.pop(cid, None)
            log.info(
                "[结果] 交易所受理成功 | %s | latency_ms=%s",
                brief_okx_order_response(out2),
                out2["_latency_ms"],
            )
            log.info("[审计] 已更新 orders 表（成功）| clOrdId=%s", cid)
            _detailed_exec(
                "rest_submit_ok",
                cl_ord_id=cid,
                latency_ms=out2.get("_latency_ms"),
                response=out2,
            )
            return out2
        except RiskError as e:
            self._metrics.inc_orders_fail()
            self._update_exec_quality(intent=intent, ok=False, latency_ms=None)
            if self._runtime is not None:
                po = self._runtime.get("pending_orders")
                if isinstance(po, dict):
                    po.pop(cid, None)
            log.warning("[结果] 下单前风控拒绝 | clOrdId=%s | %s", cid, e)
            try:
                self._audit.log_order_result(cid, False, None, str(e)[:2000])
            except Exception as ex:
                log.warning("[审计] log_order_result(RiskError) 失败 | clOrdId=%s | %s", cid, ex)
            _detailed_exec(
                "rest_submit_risk_error",
                cl_ord_id=cid,
                error=str(e),
            )
            return None
        except Exception as e:
            self._metrics.inc_orders_fail()
            self._update_exec_quality(intent=intent, ok=False, latency_ms=None)
            err = f"{type(e).__name__}: {e}"
            self._audit.log_order_result(cid, False, None, err[:2000])
            if _is_transient_http_error(e):
                if self._runtime is not None:
                    tc = int(self._runtime.get("transient_order_fail_count") or 0) + 1
                    self._runtime["transient_order_fail_count"] = tc
                    self._runtime["transient_order_fail_last_ts"] = time.time()
                log.warning(
                    "[结果] 下单瞬时故障（不计熔断，保留 pending 待成交匹配）| clOrdId=%s | %s",
                    cid,
                    err,
                )
                log.info("[审计] 已更新 orders 表（失败）| clOrdId=%s", cid)
                _detailed_exec(
                    "rest_submit_transient_error",
                    cl_ord_id=cid,
                    error=err,
                )
                return None
            if self._runtime is not None:
                po = self._runtime.get("pending_orders")
                if isinstance(po, dict):
                    po.pop(cid, None)
            tripped = self._circuit.record_failure()
            log.error("[结果] 下单失败 | clOrdId=%s | %s", cid, err)
            log.info("[审计] 已更新 orders 表（失败）| clOrdId=%s", cid)
            _detailed_exec(
                "rest_submit_fail",
                cl_ord_id=cid,
                error=err,
                circuit_tripped=tripped,
            )
            if tripped:
                self._metrics.inc_circuit_open()
                log.warning("[熔断] 连续失败达到阈值，进入冷却")
            raise