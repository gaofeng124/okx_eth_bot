"""Merged account package (flattened)."""
from __future__ import annotations


# ----- fills_fifo_pnl.py -----

"""
现货成交明细 → FIFO 已实现盈亏（USDT 口径）。

说明：
- 按 fill 的 ts 升序；每笔 buy 形成「批次」；(qty_base, cost_usdt)；
  sell 时按 FIFO 扣减批次，已实现 = 卖侧收到 USDT − 对应批次成本。
- fee 符号：欧易可能为负（返佣）；本模块按「fee 为增加成本 / 减少所得」处理：
  - 计价币 USDT 手续费：买单 cost += fee；卖单 proceeds += fee（fee 为负则降低成本、增加净得）。
- 手续费若为标的币（如 ETH），则从成交量中扣减后再算成本/卖量，并用成交价折算 USDT。

若窗口内净买入（未全部卖出），已实现只含已配对部分；剩余持仓的浮动盈亏需另用标价。
"""

from collections import deque
from dataclasses import dataclass
from typing import Any


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _ts_ms(r: dict[str, Any]) -> int:
    try:
        return int(r.get("ts") or r.get("fillTime") or 0)
    except (TypeError, ValueError):
        return 0


def _dedup_fills(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 fillId 去重；fillId 缺失时回退到关键字段组合键。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        fid = str(r.get("fillId") or "").strip()
        if fid:
            key = f"id:{fid}"
        else:
            key = (
                f"raw:{_ts_ms(r)}|{str(r.get('side') or '')}|"
                f"{str(r.get('fillPx') or '')}|{str(r.get('fillSz') or r.get('accFillSz') or '')}|"
                f"{str(r.get('fee') or '')}|{str(r.get('feeCcy') or '')}"
            )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _compress_same_order_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    订单级压缩：同一 ordId(+side+ts) 被撮合成多条成交时，合并为一条。

    说明：
    - 你在看板里关注的是「订单对应关系」，而 OKX 返回常是「成交明细」；
      同一订单在同一毫秒可能拆成多条，视觉上像“重复”。
    - 这里按 (ordId, side, ts, feeCcy) 聚合；无 ordId 的行保持原样。
    """
    bucket: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ord_id = str(r.get("ordId") or "").strip()
        side = str(r.get("side") or "").lower()
        ts = _ts_ms(r)
        fee_ccy = str(r.get("feeCcy") or "").upper()
        if not ord_id:
            out.append(r)
            continue
        k = (ord_id, side, ts, fee_ccy)
        try:
            sz = _f(r.get("fillSz") or r.get("accFillSz"))
            px = _f(r.get("fillPx"))
            fee = _f(r.get("fee"))
        except Exception:
            out.append(r)
            continue
        notional = px * sz
        if k not in bucket:
            one = dict(r)
            one["_sum_sz"] = sz
            one["_sum_notional"] = notional
            one["_sum_fee"] = fee
            bucket[k] = one
        else:
            one = bucket[k]
            one["_sum_sz"] = _f(one.get("_sum_sz")) + sz
            one["_sum_notional"] = _f(one.get("_sum_notional")) + notional
            one["_sum_fee"] = _f(one.get("_sum_fee")) + fee

    for one in bucket.values():
        sum_sz = _f(one.pop("_sum_sz", 0.0))
        sum_notional = _f(one.pop("_sum_notional", 0.0))
        sum_fee = _f(one.pop("_sum_fee", 0.0))
        vwap = (sum_notional / sum_sz) if sum_sz > 1e-18 else 0.0
        one["fillSz"] = f"{sum_sz:.18f}".rstrip("0").rstrip(".")
        one["fillPx"] = f"{vwap:.18f}".rstrip("0").rstrip(".")
        one["fee"] = f"{sum_fee:.18f}".rstrip("0").rstrip(".")
        out.append(one)
    return out


@dataclass
class FifoSpotPnl:
    """某交易对、某时段内成交的 FIFO 结果。"""

    realized_pnl_usdt: float
    buy_notional_usdt: float
    sell_notional_usdt: float
    fee_paid_usdt_equiv: float
    open_base_qty: float
    open_cost_usdt: float


@dataclass
class FifoSwapPnl:
    """SWAP 成交聚合（优先使用 OKX fillPnl 字段）。"""

    realized_pnl_usdt: float
    fee_paid_usdt_equiv: float
    buy_contracts: float
    sell_contracts: float


def _buy_leg(
    r: dict[str, Any],
    *,
    base: str,
    quote: str,
) -> tuple[float, float]:
    """返回 (净买入 base 数量, 花费 USDT 含费)."""
    px = _f(r.get("fillPx"))
    sz = _f(r.get("fillSz") or r.get("accFillSz"))
    fee = _f(r.get("fee"))
    fc = str(r.get("feeCcy") or "").upper()
    bu, qu = base.upper(), quote.upper()

    if fc == qu:
        # 手续费 USDT：正=多付；负=返佣减成本
        return sz, px * sz + fee
    if fc == bu:
        # 手续费扣在 base：净到账 base 减少
        net_b = sz - fee
        return max(net_b, 0.0), px * sz
    return sz, px * sz


def _sell_leg(
    r: dict[str, Any],
    *,
    base: str,
    quote: str,
) -> tuple[float, float]:
    """返回 (卖出 base 数量, 净收 USDT). 计价币手续费：净收 = px*sz - fee（fee 负值为返佣）。"""
    px = _f(r.get("fillPx"))
    sz = _f(r.get("fillSz") or r.get("accFillSz"))
    fee = _f(r.get("fee"))
    fc = str(r.get("feeCcy") or "").upper()
    bu, qu = base.upper(), quote.upper()

    if fc == qu:
        return sz, px * sz - fee
    if fc == bu:
        net_b = sz - fee
        return max(net_b, 0.0), px * max(net_b, 0.0)
    return sz, px * sz


def fifo_realized_spot_usdt(
    fills: list[dict[str, Any]],
    *,
    inst_id: str,
) -> FifoSpotPnl:
    """
    对单交易对现货成交做 FIFO 已实现盈亏（USDT）。

    仅处理 side 为 buy/sell 的 fill；其它忽略。
    """
    from quant.risk import split_inst_id

    base, quote = split_inst_id(inst_id)
    if not base or not quote:
        return FifoSpotPnl(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    rows = _dedup_fills([x for x in fills if isinstance(x, dict)])
    rows = _compress_same_order_rows(rows)
    rows.sort(key=_ts_ms)

    # deque of (qty_base, cost_usdt_total)
    lots: deque[tuple[float, float]] = deque()
    realized = 0.0
    buy_notional = 0.0
    sell_notional = 0.0
    fee_equiv = 0.0

    for r in rows:
        side = str(r.get("side") or "").lower()
        fee = _f(r.get("fee"))
        fc = str(r.get("feeCcy") or "").upper()
        if fc == quote.upper():
            fee_equiv += fee

        if side == "buy":
            qb, cost = _buy_leg(r, base=base, quote=quote)
            if qb > 1e-18:
                lots.append((qb, cost))
            buy_notional += _f(r.get("fillPx")) * _f(r.get("fillSz") or r.get("accFillSz"))

        elif side == "sell":
            q_sell, proceeds = _sell_leg(r, base=base, quote=quote)
            sell_notional += _f(r.get("fillPx")) * _f(r.get("fillSz") or r.get("accFillSz"))
            rem = q_sell
            net_proceeds_total = proceeds
            while rem > 1e-18 and lots:
                lq, lc = lots[0]
                take = min(rem, lq)
                unit = lc / lq if lq > 1e-18 else 0.0
                cost_part = take * unit
                proc_part = net_proceeds_total * (take / q_sell) if q_sell > 1e-18 else 0.0
                realized += proc_part - cost_part
                new_lq = lq - take
                if new_lq <= 1e-18:
                    lots.popleft()
                else:
                    lots[0] = (new_lq, lc - cost_part)
                rem -= take

    open_qty = sum(l[0] for l in lots)
    open_cost = sum(l[1] for l in lots)

    return FifoSpotPnl(
        realized_pnl_usdt=realized,
        buy_notional_usdt=buy_notional,
        sell_notional_usdt=sell_notional,
        fee_paid_usdt_equiv=fee_equiv,
        open_base_qty=open_qty,
        open_cost_usdt=open_cost,
    )


def realized_swap_usdt(
    fills: list[dict[str, Any]],
) -> FifoSwapPnl:
    """
    SWAP 已实现盈亏（USDT）：
    - 优先累计 fillPnl（交易所给出的逐笔已实现）
    - 手续费按 fee/feeCcy 汇总（USDT 口径）
    """
    rows = _dedup_fills([x for x in fills if isinstance(x, dict)])
    rows.sort(key=_ts_ms)
    realized = 0.0
    fee_usdt = 0.0
    buy_c = 0.0
    sell_c = 0.0
    for r in rows:
        side = str(r.get("side") or "").lower()
        sz = _f(r.get("fillSz") or r.get("accFillSz"))
        if side == "buy":
            buy_c += sz
        elif side == "sell":
            sell_c += sz
        try:
            realized += float(r.get("fillPnl") or 0.0)
        except (TypeError, ValueError):
            pass
        ccy = str(r.get("feeCcy") or "").upper()
        if ccy == "USDT":
            fee_usdt += _f(r.get("fee"))
    return FifoSwapPnl(
        realized_pnl_usdt=realized,
        fee_paid_usdt_equiv=fee_usdt,
        buy_contracts=buy_c,
        sell_contracts=sell_c,
    )


def fifo_match_events(
    fills: list[dict[str, Any]],
    *,
    inst_id: str,
) -> dict[str, Any]:
    """
    与 fifo_realized_spot_usdt 同一套 FIFO 规则，但输出「多买↔多卖」逐笔对应关系。

    - 每笔 **买入** fill 形成一个 lot（lot_id 递增）。
    - 每笔 **卖出** fill 可能从多个 lot 扣量；每个 lot 也可能被多笔卖单拆分消耗。
    - 界面可用 sell_events[].matches 展示：本笔卖单对应哪些 lot、各自数量、成本、分摊所得、贡献盈亏。

    返回 JSON 友好 dict（无 Decimal）。
    """
    from quant.risk import split_inst_id

    base, quote = split_inst_id(inst_id)
    if not base or not quote:
        return {
            "inst_id": inst_id,
            "buy_lots": [],
            "sell_events": [],
            "open_lots": [],
            "summary": {
                "realized_pnl_usdt": 0.0,
                "fee_paid_usdt_equiv": 0.0,
                "buy_notional_usdt": 0.0,
                "sell_notional_usdt": 0.0,
                "open_base_qty": 0.0,
                "open_cost_usdt": 0.0,
            },
        }

    rows = _dedup_fills([x for x in fills if isinstance(x, dict)])
    rows = _compress_same_order_rows(rows)
    rows.sort(key=_ts_ms)

    lots: deque[tuple[int, float, float]] = deque()
    next_lot_id = 1
    buy_lots: list[dict[str, Any]] = []
    sell_events: list[dict[str, Any]] = []
    lot_meta: dict[int, dict[str, Any]] = {}
    fee_equiv = 0.0
    buy_notional = 0.0
    sell_notional = 0.0
    buy_qty_base_gross = 0.0
    sell_qty_base_gross = 0.0
    matched_buy_cost_usdt = 0.0
    matched_sell_proceeds_usdt = 0.0

    def _fid(r: dict[str, Any]) -> str:
        return str(r.get("fillId") or r.get("billId") or "")

    for r in rows:
        side = str(r.get("side") or "").lower()
        fee = _f(r.get("fee"))
        fc = str(r.get("feeCcy") or "").upper()
        if fc == quote.upper():
            fee_equiv += fee

        if side == "buy":
            qb, cost = _buy_leg(r, base=base, quote=quote)
            if qb <= 1e-18:
                continue
            ts_ms = _ts_ms(r)
            fid = _fid(r)
            gross_sz = _f(r.get("fillSz") or r.get("accFillSz"))
            buy_qty_base_gross += gross_sz
            lid = next_lot_id
            next_lot_id += 1
            lots.append((lid, qb, cost))
            lot_meta[lid] = {"buy_ts_ms": ts_ms, "buy_fill_id": fid}
            buy_lots.append(
                {
                    "lot_id": lid,
                    "ts_ms": ts_ms,
                    "fill_id": fid,
                    "qty_base": qb,
                    "cost_usdt": cost,
                }
            )
            buy_notional += _f(r.get("fillPx")) * _f(
                r.get("fillSz") or r.get("accFillSz")
            )

        elif side == "sell":
            q_sell, proceeds = _sell_leg(r, base=base, quote=quote)
            sell_qty_base_gross += _f(r.get("fillSz") or r.get("accFillSz"))
            sell_notional += _f(r.get("fillPx")) * _f(
                r.get("fillSz") or r.get("accFillSz")
            )
            rem = q_sell
            net_proceeds_total = proceeds
            matches: list[dict[str, Any]] = []
            ev_pnl = 0.0
            while rem > 1e-18 and lots:
                lid, lq, lc = lots[0]
                take = min(rem, lq)
                unit = lc / lq if lq > 1e-18 else 0.0
                cost_part = take * unit
                proc_part = (
                    net_proceeds_total * (take / q_sell) if q_sell > 1e-18 else 0.0
                )
                pnl_part = proc_part - cost_part
                ev_pnl += pnl_part
                matched_buy_cost_usdt += cost_part
                matched_sell_proceeds_usdt += proc_part
                matches.append(
                    {
                        "lot_id": lid,
                        "buy_ts_ms": lot_meta.get(lid, {}).get("buy_ts_ms"),
                        "buy_fill_id": lot_meta.get(lid, {}).get("buy_fill_id"),
                        "qty_base": take,
                        "cost_usdt": cost_part,
                        "proceeds_usdt": proc_part,
                        "pnl_usdt": pnl_part,
                    }
                )
                new_lq = lq - take
                if new_lq <= 1e-18:
                    lots.popleft()
                else:
                    lots[0] = (lid, new_lq, lc - cost_part)
                rem -= take

            sell_events.append(
                {
                    "ts_ms": _ts_ms(r),
                    "fill_id": _fid(r),
                    "sell_qty_base": q_sell,
                    "sell_qty_base_gross": _f(r.get("fillSz") or r.get("accFillSz")),
                    "proceeds_usdt": proceeds,
                    "pnl_usdt": ev_pnl,
                    "matches": matches,
                }
            )

    open_lots = [
        {
            "lot_id": lid,
            "remaining_qty_base": lq,
            "remaining_cost_usdt": lc,
        }
        for lid, lq, lc in lots
    ]
    open_qty = sum(x["remaining_qty_base"] for x in open_lots)
    open_cost = sum(x["remaining_cost_usdt"] for x in open_lots)
    realized = sum(float(x["pnl_usdt"]) for x in sell_events)
    realized_check = matched_sell_proceeds_usdt - matched_buy_cost_usdt

    return {
        "inst_id": inst_id,
        "buy_lots": buy_lots,
        "sell_events": sell_events,
        "open_lots": open_lots,
        "summary": {
            "realized_pnl_usdt": realized,
            "realized_check_sell_minus_buy_usdt": realized_check,
            "fee_paid_usdt_equiv": fee_equiv,
            "buy_notional_usdt": buy_notional,
            "sell_notional_usdt": sell_notional,
            "buy_qty_base_gross": buy_qty_base_gross,
            "sell_qty_base_gross": sell_qty_base_gross,
            "matched_buy_cost_usdt": matched_buy_cost_usdt,
            "matched_sell_proceeds_usdt": matched_sell_proceeds_usdt,
            "open_base_qty": open_qty,
            "open_cost_usdt": open_cost,
        },
    }

# ----- funding_bills.py -----

"""
永续资金费账单：GET /api/v5/account/bills?type=8（资金费），按会话时间窗分页拉取。

与成交 fills 分离：资金费在结算点记入账户余额，用账单累计会话内 realized_funding_pnl。
"""

import time
from typing import Any

from quant.exchange import OKXRestClient
from quant.logging_config import get_logger

log = get_logger(__name__)


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fetch_funding_fee_bills_window(
    client: OKXRestClient,
    inst_id: str,
    begin_ms: int,
    end_ms: int,
    *,
    bill_type: str = "8",
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    """
    拉取 [begin_ms, end_ms] 内、指定合约、type=资金费 的账单（分页）。
    """
    out: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(max_pages):
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                raw = client.account_bills(
                    inst_type="SWAP",
                    inst_id=inst_id,
                    bill_type=bill_type,
                    begin=str(begin_ms),
                    end=str(end_ms),
                    after=after,
                    limit="100",
                )
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                time.sleep(0.35 * (2**attempt))
        else:
            log.warning(
                "[资金费账单] 拉取失败 inst_id=%s [%s,%s] after=%s err=%s",
                inst_id,
                begin_ms,
                end_ms,
                after,
                last_exc,
            )
            break

        batch = raw.get("data") or []
        if not batch:
            break
        for x in batch:
            if isinstance(x, dict):
                out.append(x)
        if len(batch) < 100:
            break
        last = batch[-1]
        if not isinstance(last, dict):
            break
        bid = last.get("billId")
        if not bid:
            break
        after = str(bid)
    return out


def ingest_new_funding_fees(
    rows: list[dict[str, Any]],
    *,
    inst_id: str,
    seen_bill_ids: set[str],
) -> tuple[float, int]:
    """
    仅统计本会话尚未见过的 billId；返回 (新增 USDT 资金费合计, 新入账条数)。
    使用 balChg（账户余额变动），且 instId 匹配、ccy=USDT。
    """
    inst_id_u = inst_id.upper()
    delta = 0.0
    n_new = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("instId") or "").upper() != inst_id_u:
            continue
        ccy = str(r.get("ccy") or "").upper()
        if ccy and ccy != "USDT":
            continue
        bid = str(r.get("billId") or "").strip()
        if not bid:
            continue
        if bid in seen_bill_ids:
            continue
        seen_bill_ids.add(bid)
        delta += _f(r.get("balChg"))
        n_new += 1
    return delta, n_new


def sum_funding_fee_bills_session_usdt(
    rows: list[dict[str, Any]],
    *,
    inst_id: str,
) -> float:
    """
    会话窗口内资金费账单（type=8）全额累计：按 billId 去重后对 balChg 求和（USDT）。
    与 ingest_new_funding_fees 不同：不依赖 seen 集合，用于报告时点全量重算。
    """
    inst_id_u = inst_id.upper()
    seen: set[str] = set()
    total = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("instId") or "").upper() != inst_id_u:
            continue
        ccy = str(r.get("ccy") or "").upper()
        if ccy and ccy != "USDT":
            continue
        bid = str(r.get("billId") or "").strip()
        if not bid:
            continue
        if bid in seen:
            continue
        seen.add(bid)
        total += _f(r.get("balChg"))
    return total

# ----- inventory.py -----

"""
现货库存快照：用于限制「超额持仓」时不再开新买单（卖单仍允许，优先减仓）。

盈利导向的工程含义：
- 未平仓 base 过高 → 价格波动主导 MTM，小步快跑易被库存拖累；
- 通过有效上沿 STRAT_INVENTORY_EFFECTIVE_MAX_BASE（见 settings：单值 / ref+上浮）+ 周期性拉 balance，抑制新开多，逼策略先卖。
- 「下不设限」：不强制最低持仓，卖单始终可按风控发出。
"""

from typing import Any

from quant.exchange import OKXRestClient
from quant.risk import parse_balance_availability, split_inst_id
from quant.settings import STRAT_INVENTORY_META


def build_inventory_snapshot(
    client: OKXRestClient,
    inst_id: str,
    max_base_position: float | None,
) -> dict[str, Any]:
    raw = client.balance()
    av = parse_balance_availability(raw)
    base, quote = split_inst_id(inst_id)
    ba = float(av.get(base, 0.0)) if base else 0.0
    qa = float(av.get(quote, 0.0)) if quote else 0.0
    buy_allowed = buy_allowed_for_base(ba, max_base_position)
    over = (
        max_base_position is not None
        and max_base_position > 0
        and ba > max_base_position
    )
    mode = "reduce_only" if over else "normal"
    cap = STRAT_INVENTORY_META
    return {
        "base_avail": ba,
        "quote_avail": qa,
        "buy_allowed": buy_allowed,
        "max_base_position": max_base_position,
        "over_max": over,
        "inventory_mode": mode,
        "inventory_ready": True,
        "inventory_ref": cap.get("ref"),
        "inventory_max_above": cap.get("above"),
        "inventory_cap_mode": cap.get("mode"),
    }


def buy_allowed_for_base(base_avail: float, max_base_position: float | None) -> bool:
    """是否允许新开买单（卖单不受此限制）。"""
    if max_base_position is None or max_base_position <= 0:
        return True
    return base_avail <= max_base_position


def default_inventory_context(
    max_base_position: float | None,
    *,
    conservative_pending: bool = False,
) -> dict[str, Any]:
    """尚未拉到余额前的占位。

    - 无上限或未配置 API：不拦截买单（与旧行为一致）。
    - 已设库存上沿且已连接交易所、但尚未拉取到首包余额时：
      conservative_pending=True → 禁止买（避免重启后首几个 tick 在「假空」状态下加仓）。
    """
    if max_base_position is None or max_base_position <= 0:
        return {
            "base_avail": None,
            "quote_avail": None,
            "buy_allowed": True,
            "max_base_position": max_base_position,
            "over_max": False,
            "inventory_mode": "normal",
            "inventory_ready": False,
        }
    if conservative_pending:
        return {
            "base_avail": None,
            "quote_avail": None,
            "buy_allowed": False,
            "max_base_position": max_base_position,
            "over_max": False,
            "inventory_mode": "unknown",
            "inventory_ready": False,
        }
    return {
        "base_avail": None,
        "quote_avail": None,
        "buy_allowed": True,
        "max_base_position": max_base_position,
        "over_max": False,
        "inventory_mode": "normal",
        "inventory_ready": False,
    }

# ----- snapshot.py -----

"""
欧易账户快照（需 API Key）：资金、现货挂单摘要。

现货 STRATEGY 无「合约持仓」概念，以余额 + 当前交易对挂单为主。
"""

from typing import Any

from quant.exchange import OKXRestClient
from quant.logging_config import get_logger

log = get_logger(__name__)


def _split_inst(inst_id: str) -> tuple[str, str]:
    if "-" in inst_id:
        parts = inst_id.split("-")
        if len(parts) >= 2:
            return parts[0], parts[1]
    return "", ""


def _fmt_bal_row(d: dict[str, Any]) -> str:
    ccy = d.get("ccy", "?")
    eq = d.get("eq", d.get("cashBal", "?"))
    avail = d.get("availEq", d.get("availBal", "?"))
    return f"{ccy}: eq={eq} avail={avail}"


def log_account_snapshot(client: OKXRestClient, inst_id: str) -> None:
    """
    拉取账户余额 + 本交易对挂单，打 INFO 日志（前缀 [账户]）。
    """
    base, quote = _split_inst(inst_id)
    raw = client.balance()
    data = raw.get("data") or []
    if not data:
        log.info("[账户] balance 返回空 data | raw=%s", raw)
        return

    row0 = data[0] if isinstance(data[0], dict) else {}
    total_eq = row0.get("totalEq", row0.get("adjEq", "-"))
    details = row0.get("details") or []
    log.info("[账户] ========== 账户快照 | instId=%s ==========", inst_id)
    log.info("[账户] 总权益 totalEq≈%s（USD 计价字段，以交易所为准）", total_eq)

    want = {base, quote} if base and quote else set()
    shown = 0
    for d in details:
        if not isinstance(d, dict):
            continue
        ccy = str(d.get("ccy", ""))
        if not want or ccy in want:
            log.info("[账户] 币种 | %s", _fmt_bal_row(d))
            shown += 1
    if not shown and details:
        for d in details[:8]:
            if isinstance(d, dict):
                log.info("[账户] 币种 | %s", _fmt_bal_row(d))

    try:
        pend = client.orders_pending(inst_id)
        orders = pend.get("data") or []
        log.info("[账户] 待成交挂单数（%s）= %s", inst_id, len(orders))
        for i, o in enumerate(orders[:10]):
            if not isinstance(o, dict):
                continue
            log.info(
                "[账户]   挂单%s | side=%s px=%s sz=%s state=%s",
                i + 1,
                o.get("side"),
                o.get("px"),
                o.get("sz"),
                o.get("state"),
            )
        if len(orders) > 10:
            log.info("[账户]   … 另有 %s 条未显示", len(orders) - 10)
    except Exception as e:
        log.warning("[账户] 拉取挂单失败: %s", e)

    log.info("[账户] =============================================")


def has_api_keys_configured() -> bool:
    from quant.settings import OKX_API_KEY, OKX_PASSPHRASE, OKX_SECRET_KEY

    return bool(
        OKX_API_KEY.strip() and OKX_SECRET_KEY.strip() and OKX_PASSPHRASE.strip()
    )

# ----- session_trade.py -----

"""
本会话交易统计：成交笔数（成交明细条数）、手续费（USDT 为主）、权益盈亏（现货标价）。

说明：
- 「成交笔数」来自 /api/v5/trade/fills 自 session_start_ms 起的明细条数（与 OKX 一致）。
- 日志里「REST下单成功累计」来自 Metrics.orders_ok（post 下单成功），与 fills 不是同一统计。
- 永续：资金费来自 /api/v5/account/bills?type=8（balChg），与 fills 分源累计为 realized_funding_pnl；
  净已实现 ≈ FIFO 交易已实现 + 资金费累计。
- 「权益盈亏」= 当前 (USDT可用 + 标的可用×mid) − 会话基准（首笔有效行情后建立），
  含未实现波动，不单列已实现；单币对现货下这是最直观的会话曲线。
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from quant.exchange import OKXRestClient
from quant.logging_config import get_logger
from quant.risk import parse_balance_availability, split_inst_id
from quant.risk import take_recent_swap_fills

log = get_logger(__name__)


@dataclass
class SessionTradeSnapshot:
    fills_count: int
    fees_usdt: float
    fees_other_ccy: bool
    realized_fifo_pnl_usdt: float | None
    open_base_after_fills: float | None
    open_cost_usdt_after_fills: float | None
    equity_now_usdt: float | None
    equity_base_usdt: float | None
    baseline_equity_usdt: float | None
    mtm_pnl_usdt: float | None
    baseline_ready: bool
    realized_funding_pnl_usdt: float | None
    net_realized_pnl_usdt: float | None


@dataclass
class SessionTradeStats:
    """线程安全；由 runner 在 tick 更新 mid，在指标循环里 refresh。"""

    inst_id: str
    session_start_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_mid: float = 0.0
    _last_mid_ts: float = 0.0
    _baseline_equity: float | None = None
    _baseline_usdt: float = 0.0
    _baseline_base: float = 0.0
    _fills_count: int = 0
    _fees_usdt: float = 0.0
    _fees_other: bool = False
    _open_cost_usdt: float | None = None
    _open_base_qty: float | None = None
    _last_mtm_usdt: float | None = None
    _last_upl_pct: float | None = None
    _last_fills_for_half_kelly: list[dict[str, Any]] = field(default_factory=list)
    _last_equity_usdt: float | None = None
    _seen_funding_bill_ids: set[str] = field(default_factory=set)
    _realized_funding_pnl_usdt: float = 0.0
    _last_funding_bills_fetch_ts: float = 0.0

    def _is_swap(self) -> bool:
        return self.inst_id.upper().endswith("-SWAP")

    def _spot_like_inst_id(self) -> str:
        return self.inst_id[:-5] if self._is_swap() else self.inst_id

    def note_mid(self, bid: float, ask: float, last: float) -> None:
        mid = (bid + ask) / 2.0 if bid and ask else last
        with self._lock:
            self._last_mid = mid
            self._last_mid_ts = time.time()

    def position_unrealized_pnl_pct(self) -> float | None:
        """相对 FIFO 持仓成本： (mark - cost) / cost；负数为浮亏。"""
        if self._is_swap():
            with self._lock:
                mtm = self._last_mtm_usdt
                beq = self._baseline_equity
            if (
                mtm is None
                or beq is None
                or not isinstance(beq, (int, float))
                or float(beq) <= 0
            ):
                return None
            return float(mtm) / float(beq)
        with self._lock:
            mid = self._last_mid
            oc = self._open_cost_usdt
            ob = self._open_base_qty
        if (
            oc is None
            or ob is None
            or oc <= 0
            or ob <= 1e-18
            or mid <= 0
        ):
            return None
        mark = ob * mid
        return (mark - oc) / oc

    def position_upl_pct(self) -> float | None:
        with self._lock:
            return self._last_upl_pct

    def last_fills_for_half_kelly(self) -> list[dict[str, Any]]:
        """最近窗口内用于 Half-Kelly 的成交副本（由 refresh 更新）。"""
        with self._lock:
            return list(self._last_fills_for_half_kelly)

    def last_equity_usdt(self) -> float | None:
        """最近一次 refresh 得到的账户 totalEq（SWAP）。"""
        with self._lock:
            return self._last_equity_usdt

    def _ensure_baseline(self, client: OKXRestClient) -> None:
        with self._lock:
            if self._baseline_equity is not None:
                return
            mid = self._last_mid
            if mid <= 0:
                return
        try:
            raw = client.balance()
        except Exception as e:
            log.debug("[盈亏] 基准权益未建立: balance 失败 %s", e)
            return
        if self._is_swap():
            rows = raw.get("data") or []
            if rows and isinstance(rows[0], dict):
                try:
                    eq = float(rows[0].get("totalEq"))
                except (TypeError, ValueError):
                    eq = 0.0
                if eq > 0:
                    with self._lock:
                        if self._baseline_equity is None:
                            self._baseline_equity = eq
                    log.info("[盈亏] 会话基准已建立 | SWAP totalEq≈%.4f USDT", eq)
                    return
        av = parse_balance_availability(raw)
        base, quote = split_inst_id(self._spot_like_inst_id())
        if not base or not quote:
            return
        u = float(av.get(quote, 0.0))
        b = float(av.get(base, 0.0))
        eq = u + b * mid
        with self._lock:
            if self._baseline_equity is not None:
                return
            self._baseline_usdt = u
            self._baseline_base = b
            self._baseline_equity = eq
        log.info(
            "[盈亏] 会话基准已建立 | mid≈%.8f | %s≈%.8f %s≈%.8f | 权益(标价)≈%.4f USDT",
            mid,
            quote,
            u,
            base,
            b,
            eq,
        )

    def refresh(self, client: OKXRestClient) -> SessionTradeSnapshot:
        """拉 fills、更新计数；若已有基准则算 MTM。"""
        self._ensure_baseline(client)
        now_ms = int(time.time() * 1000)
        rows = fetch_fills_window(
            client,
            self.inst_id,
            self.session_start_ms,
            now_ms,
            inst_type="SWAP" if self._is_swap() else "SPOT",
        )
        kelly_rows: list[dict[str, Any]] = []
        if self._is_swap():
            from quant import settings as _st_cfg

            w = int(getattr(_st_cfg, "RISK_HALF_KELLY_WINDOW", 100))
            kelly_rows = take_recent_swap_fills(rows, max_fills=max(10, w))
        fc, fusdt, fother = aggregate_fill_fees(rows)
        if self._is_swap():
            sw = realized_swap_usdt(rows)
            fifo_realized = sw.realized_pnl_usdt
            open_cost = None
            open_qty = None
        else:
            fifo = fifo_realized_spot_usdt(rows, inst_id=self._spot_like_inst_id())
            fifo_realized = fifo.realized_pnl_usdt
            open_cost = fifo.open_cost_usdt
            open_qty = fifo.open_base_qty

        with self._lock:
            self._fills_count = fc
            self._fees_usdt = fusdt
            self._fees_other = fother
            self._open_cost_usdt = open_cost
            self._open_base_qty = open_qty
            self._last_fills_for_half_kelly = kelly_rows
            mid = self._last_mid
            mid_ts = self._last_mid_ts
            beq = self._baseline_equity

        if self._is_swap():
            from quant import settings as _st_fb
            from quant.account import fetch_funding_fee_bills_window, ingest_new_funding_fees

            if getattr(_st_fb, "FUNDING_BILLS_ENABLE", True):
                interval = float(
                    getattr(_st_fb, "FUNDING_BILLS_FETCH_INTERVAL_SEC", 300.0)
                )
                now_t = time.time()
                should_fetch = False
                with self._lock:
                    if now_t - self._last_funding_bills_fetch_ts >= interval:
                        should_fetch = True
                if should_fetch:
                    try:
                        rows_fb = fetch_funding_fee_bills_window(
                            client,
                            self.inst_id,
                            self.session_start_ms,
                            now_ms,
                        )
                        with self._lock:
                            d, _ = ingest_new_funding_fees(
                                rows_fb,
                                inst_id=self.inst_id,
                                seen_bill_ids=self._seen_funding_bill_ids,
                            )
                            self._realized_funding_pnl_usdt += d
                            self._last_funding_bills_fetch_ts = now_t
                    except Exception as e:
                        log.debug("[资金费账单] 拉取失败: %s", e)
                        with self._lock:
                            # 失败也推进时间戳，避免每次 refresh 都重试打爆 API
                            self._last_funding_bills_fetch_ts = now_t

        eq_now: float | None = None
        mtm: float | None = None
        eq_base_part: float | None = None
        base, quote = split_inst_id(self._spot_like_inst_id())
        # 网络抖动时 REST 可能较久未更新 tick，mid 可能过旧。
        # mid 过旧会导致权益/MTM 标记与真实成交不一致，影响你对“买卖不对等”的判断。
        max_mid_age_sec = 120.0
        mid_age = time.time() - mid_ts if mid_ts else float("inf")
        if self._is_swap():
            try:
                bal = client.balance()
                d = bal.get("data") or []
                if d and isinstance(d[0], dict):
                    eq_now = float(d[0].get("totalEq"))
                    eq_base_part = None
                    if beq is not None:
                        mtm = eq_now - beq
                        self._last_mtm_usdt = mtm
                pos_raw = client.positions_swap(self.inst_id)
                ps = pos_raw.get("data") or []
                total_upl = 0.0
                total_notional = 0.0
                for p in ps:
                    if not isinstance(p, dict):
                        continue
                    try:
                        total_upl += float(p.get("upl") or 0.0)
                    except (TypeError, ValueError):
                        pass
                    try:
                        total_notional += abs(float(p.get("notionalUsd") or 0.0))
                    except (TypeError, ValueError):
                        pass
                with self._lock:
                    if total_notional > 1e-9:
                        self._last_upl_pct = total_upl / total_notional
                    else:
                        self._last_upl_pct = None
                    if eq_now is not None:
                        self._last_equity_usdt = eq_now
            except Exception as e:
                log.debug("[盈亏] SWAP 当前权益计算失败: %s", e)
        elif mid > 0 and base and quote and mid_age <= max_mid_age_sec:
            try:
                bal = client.balance()
                av = parse_balance_availability(bal)
                u = float(av.get(quote, 0.0))
                b = float(av.get(base, 0.0))
                eq_now = u + b * mid
                eq_base_part = b * mid
                if beq is not None:
                    mtm = eq_now - beq
                    self._last_mtm_usdt = mtm
            except Exception as e:
                log.debug("[盈亏] 当前权益计算失败: %s", e)

        rf_snap: float | None = None
        net_snap: float | None = None
        with self._lock:
            rf_val = self._realized_funding_pnl_usdt
        if self._is_swap():
            rf_snap = float(rf_val)
            tr = float(fifo_realized) if fifo_realized is not None else 0.0
            net_snap = tr + rf_snap
        else:
            net_snap = fifo_realized

        return SessionTradeSnapshot(
            fills_count=fc,
            fees_usdt=fusdt,
            fees_other_ccy=fother,
            realized_fifo_pnl_usdt=fifo_realized,
            open_base_after_fills=open_qty,
            open_cost_usdt_after_fills=open_cost,
            equity_now_usdt=eq_now,
            equity_base_usdt=eq_base_part,
            baseline_equity_usdt=beq,
            mtm_pnl_usdt=mtm,
            baseline_ready=beq is not None,
            realized_funding_pnl_usdt=rf_snap,
            net_realized_pnl_usdt=net_snap,
        )


def fetch_fills_window(
    client: OKXRestClient,
    inst_id: str,
    begin_ms: int,
    end_ms: int,
    *,
    inst_type: str = "SWAP",
) -> list[dict[str, Any]]:
    """分页拉取 [begin_ms, end_ms] 内永续成交（条数上限约 50×100）。"""
    out: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(50):
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                raw = client.fills_swap(
                    inst_id,
                    begin_ms=begin_ms,
                    end_ms=end_ms,
                    limit="100",
                    after=after,
                )
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                delay = 0.4 * (2**attempt)
                time.sleep(delay)
        else:
            # 仍失败：尽量返回已收集的 fills，但要告警，避免你以为计算完整。
            log.warning(
                "[盈亏] 拉取 fills 失败（返回部分结果）inst_id=%s [%s,%s] after=%s err=%s",
                inst_id,
                begin_ms,
                end_ms,
                after,
                str(last_exc),
            )
            break

        batch = raw.get("data") or []
        if not batch:
            break
        for x in batch:
            if isinstance(x, dict):
                out.append(x)
        if len(batch) < 100:
            break
        last = batch[-1]
        if not isinstance(last, dict):
            break
        fid = last.get("fillId")
        if not fid:
            break
        after = str(fid)
    # 去重：分页边界/网络重试时可能重复返回同一 fillId，直接返回会导致 FIFO 与手续费重复统计。
    seen_ids: set[str] = set()
    dedup: list[dict[str, Any]] = []
    for r in out:
        if not isinstance(r, dict):
            continue
        fid = str(r.get("fillId") or "").strip()
        if fid:
            if fid in seen_ids:
                continue
            seen_ids.add(fid)
        dedup.append(r)
    return dedup


def aggregate_fill_fees(rows: list[Any]) -> tuple[int, float, bool]:
    n = 0
    fusdt = 0.0
    other = False
    for r in rows:
        if not isinstance(r, dict):
            continue
        n += 1
        try:
            fee = float(r.get("fee") or 0.0)
        except (TypeError, ValueError):
            fee = 0.0
        ccy = str(r.get("feeCcy") or "").upper()
        if ccy == "USDT":
            fusdt += fee
        elif fee != 0 and ccy:
            other = True
    return n, fusdt, other


def aggregate_session_fill_pnl_fee_usdt(
    rows: list[dict[str, Any]],
) -> tuple[float, float, int]:
    """
    本会话窗口内 fills：fillPnl 合计、gross；USDT 手续费合计（与 aggregate_fill_fees 一致）；条数。
    total_fee 为交易所返回 fee 之和（含开仓/平仓，负数为返佣）。
    """
    gross = 0.0
    fee_usdt = 0.0
    n = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        n += 1
        try:
            gross += float(r.get("fillPnl") or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            fee = float(r.get("fee") or 0.0)
        except (TypeError, ValueError):
            fee = 0.0
        ccy = str(r.get("feeCcy") or "").upper()
        if ccy == "USDT":
            fee_usdt += fee
    return gross, fee_usdt, n


def avg_slippage_bps_vs_signal_refs(
    rows: list[dict[str, Any]],
    refs: dict[str, tuple[float | None, float | None, float | None]],
) -> float | None:
    """
    成交价相对「下单审计中记录的信号价」的平均偏差（bps）。
    参考价：优先 (ref_bid+ref_ask)/2，否则 ref_last；仅统计能关联 clOrdId 且参考价有效的成交。
    买方：(fillPx - ref_mid) / ref_mid * 1e4；卖方：(ref_mid - fillPx) / ref_mid * 1e4。
    """
    slips: list[float] = []

    def _mid(
        t: tuple[float | None, float | None, float | None],
    ) -> float | None:
        last, bid, ask = t
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (float(bid) + float(ask)) / 2.0
        if last is not None and float(last) > 0:
            return float(last)
        return None

    for r in rows:
        if not isinstance(r, dict):
            continue
        cid = str(r.get("clOrdId") or "").strip()
        if not cid or cid not in refs:
            continue
        rm = _mid(refs[cid])
        if rm is None or rm <= 0:
            continue
        try:
            px = float(r.get("fillPx") or 0.0)
        except (TypeError, ValueError):
            continue
        side = str(r.get("side") or "").lower()
        if side == "buy":
            slips.append((px - rm) / rm * 10000.0)
        elif side == "sell":
            slips.append((rm - px) / rm * 10000.0)
    if not slips:
        return None
    return sum(slips) / float(len(slips))


def avg_slippage_bps_fill_minus_signal_mid(
    rows: list[dict[str, Any]],
    refs: dict[str, tuple[float | None, float | None, float | None]],
) -> float | None:
    """
    每笔成交：(fillPx - signal_mid) / signal_mid × 10000；
    signal_mid 为审计 orders 中记录的 ref（与 load_order_signal_refs 一致），再取均值。
    仅统计能关联 clOrdId 且参考价有效的成交。
    """
    slips: list[float] = []

    def _mid(
        t: tuple[float | None, float | None, float | None],
    ) -> float | None:
        last, bid, ask = t
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (float(bid) + float(ask)) / 2.0
        if last is not None and float(last) > 0:
            return float(last)
        return None

    for r in rows:
        if not isinstance(r, dict):
            continue
        cid = str(r.get("clOrdId") or "").strip()
        if not cid or cid not in refs:
            continue
        sp = _mid(refs[cid])
        if sp is None or sp <= 0:
            continue
        try:
            px = float(r.get("fillPx") or 0.0)
        except (TypeError, ValueError):
            continue
        slips.append((px - sp) / sp * 10000.0)
    if not slips:
        return None
    return sum(slips) / float(len(slips))
