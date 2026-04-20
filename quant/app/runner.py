"""
本地运行主入口（python run_strategy.py / run_strategy_live.py 会调到本模块）。

数据流（自上而下）：
1) [行情] WebSocket 订阅 tickers，或 STRAT_DATA_SOURCE=rest 时 HTTP 轮询 ticker+K 线（增强特征）。
2) [策略] 调用 strat.on_tick(..., market_context=?)，内部完成「分析 → 是否产生 OrderIntent」。
3) [审计] 若产生意图：写入 SQLite signals（含 reason / features_json）。
4) [执行] 若 STRAT_LIVE=1：经 ExecutionPipeline（DailyDrawdown→CorrelationGuard→OMS+熔断+风控+REST），并写 orders 表。
5) [指标] 后台任务周期性打印累计 tick/信号/下单成功笔数(orders_ok)/失败数。
6) [盈亏] 若配置了 API Key：拉 fills 汇总成交明细条数、手续费等；注意 orders_ok=REST 下单成功次数（挂单≠成交）。
7) [对账] 周期性拉挂单并打印最老挂单年龄；可选限时撤销超龄限价单（见 STRAT_STALE_ORDER_*）。
8) [库存] 有 API Key 时按间隔拉余额：超有效上沿则抑制新开买单（卖单仍下；上沿见 ref/上浮 或 STRAT_MAX_BASE_POSITION）。
9) [账户] 若配置了 API Key：启动时（及可选周期）拉余额与本交易对挂单。
10) [真实性] 有 Key 时启动即同步余额；超上限为「仅减仓」；主循环对买单再仲裁，防策略漏检。
11) [策略][决策] 无信号时按节流打印当前指标与距触发条件的差距（见 STRAT_DECISION_LOG）。

终端日志前缀便于 grep：[行情][策略][决策][审计][执行][结果][指标][盈亏][库存][真实性][对账][挂单][熔断][账户]。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import logging
import time
from functools import partial
from pathlib import Path
from typing import Any

from quant.account import (
    SessionTradeStats,
    aggregate_session_fill_pnl_fee_usdt,
    avg_slippage_bps_fill_minus_signal_mid,
    build_inventory_snapshot,
    default_inventory_context,
    fetch_fills_window,
    fetch_funding_fee_bills_window,
    has_api_keys_configured,
    log_account_snapshot,
    sum_funding_fee_bills_session_usdt,
)
from quant.analysis import (
    enrich_quote_context,
    fetch_rest_snapshot,
    parse_ticker_prices,
)
from quant.exchange import (
    OKXRestClient,
    ensure_keys,
    get_funding_rate,
    get_funding_snapshot,
    get_mark_price,
    get_open_interest,
    get_swap_instrument_spec,
    get_ticker,
    mask_proxy_url,
    stream_tickers,
)
from quant.execution import ExecutionPipeline, ExecutionService
from quant.logging_config import get_logger
from quant.metrics import Metrics
from quant.oms import OrderManager
from quant.reconcile import log_pending_orders
from quant.models import is_priced_order
from quant.risk import (
    CircuitBreaker,
    DailyDrawdownBreaker,
    RiskConfig,
    RiskEngine,
    is_swap_inst_id,
    parse_balance_availability,
    swap_open_initial_margin_usdt,
)
from quant.settings import (
    ACCOUNT_SNAPSHOT_INTERVAL_SEC,
    ACCOUNT_SNAPSHOT_ON_START,
    AUDIT_DB_NAME,
    AUDIT_ENABLED,
    CIRCUIT_COOLDOWN_SEC,
    CIRCUIT_MAX_FAILS,
    DATA_DIR,
    INST_ID,
    METRICS_INTERVAL_SEC,
    OKX_REST_HTTP_PROXY,
    OKX_WS_DIRECT,
    OKX_WS_PUBLIC_URL_LIST,
    RECONCILE_INTERVAL_SEC,
    REST_CANDLES_BAR,
    REST_CANDLES_LIMIT,
    REST_HTTP_CONNECT_TIMEOUT_SEC,
    REST_HTTP_TIMEOUT_SEC,
    REST_POLL_INTERVAL_SEC,
    REST_SNAPSHOT_SLOW_INTERVAL_SEC,
    RISK_BALANCE_BUFFER_PCT,
    RISK_CHECK_BALANCE,
    RISK_HALF_KELLY_ENABLE,
    RISK_HALF_KELLY_WINDOW,
    RISK_HALF_KELLY_MIN_SAMPLES,
    RISK_HALF_KELLY_CONSERVATIVE_CONTRACTS,
    RISK_ENABLED,
    RISK_MAX_NOTIONAL_USDT,
    RISK_MAX_ORDER_BASE,
    RISK_MAX_CORR_NOTIONAL_USDT,
    RISK_CORR_RESYNC_INTERVAL_SEC,
    RISK_MARGIN_PRECHECK_STRICT,
    RISK_DAILY_MAX_LOSS_PCT,
    RISK_SWAP_IGNORE_MAX_CAPS,
    STRAT_DATA_SOURCE,
    STRAT_LIVE,
    STRAT_MARKET_TGT_CCY,
    STRAT_MODE,
    STRAT_ORDER_NOTIONAL_MAX_USDT,
    STRAT_ORDER_NOTIONAL_MIN_USDT,
    STRAT_ORDER_NOTIONAL_USDT,
    STRAT_ORDER_SZ,
    STRAT_ORDER_TYPE,
    STRAT_MAX_BASE_POSITION,
    STRAT_INVENTORY_EFFECTIVE_MAX_BASE,
    STRAT_INVENTORY_META,
    STRAT_BALANCE_POLL_SEC,
    STRAT_INVENTORY_LOG_SEC,
    STRAT_STALE_ORDER_CANCEL_SEC,
    STRAT_STALE_ORDER_POLL_SEC,
    LEV5_ENABLED,
    LEV5_GUARD_POLL_SEC,
    LEV5_DAILY_DD_LIMIT_PCT,
    LEV5_HALT_COOLDOWN_SEC,
    LEV5_LEVERAGE,
    LEV5_MICRO_POLL_SEC,
    LEV5_MAX_CONSEC_LOSS,
    LEV5_POS_MODE,
    LEV5_SELFCHECK_LOG_SEC,
    LEV5_TUNE_EDGE_STEP_BPS,
    LEV5_TUNE_FREEZE_PREMIUM,
    LEV5_TUNE_FREEZE_VOL,
    LEV5_TUNE_INTERVAL_SEC,
    LEV5_TUNE_MAX_STEP_SCALE,
    LEV5_TUNE_Z_STEP,
    LEV5_MODEL_DIAG_INTERVAL_SEC,
    LEV5_MODEL_DIAG_MIN_SAMPLES,
    LEV5_MODEL_DIAG_SAFETY_STEP_BPS,
    LEV5_STOP_LOSS_MTM_PCT,
    LEV5_STOP_LOSS_UPL_PCT,
    LEV5_AGGRESSIVE_MODE,
    LEV5_DYNAMIC_ACTIVITY_TARGET,
    LEV5_ACTIVITY_VOL_REF,
    LEV5_ACTIVITY_MIN_SIGNALS_PER_HOUR,
    LEV5_ACTIVITY_MAX_SIGNALS_PER_HOUR,
    LEV5_DIRECTIONAL_PERF_BIAS_ENABLE,
    LEV5_DIRECTIONAL_PERF_EMA_ALPHA,
    LEV5_DIRECTIONAL_PERF_RELAX_Z,
    LEV5_DIRECTIONAL_PERF_RELAX_EDGE_BPS,
    LEV5_DIRECTIONAL_PERF_PUSH_Z,
    LEV5_DIRECTIONAL_PERF_PUSH_EDGE_BPS,
    LEV5_DIRECTIONAL_BIAS_VOL_GATE_ENABLE,
    LEV5_DIRECTIONAL_BIAS_VOL_LOW,
    LEV5_DIRECTIONAL_BIAS_VOL_HIGH,
    LEV5_DIRECTIONAL_BIAS_MIN_SCALE,
    LEV5_DIRECTIONAL_BIAS_MAX_SCALE,
    LEV5_DIRECTIONAL_PERF_DIFF_BASE,
    LEV5_DIRECTIONAL_PERF_DIFF_SAMPLE_REF,
    LEV5_DIRECTIONAL_PERF_DIFF_MIN_SCALE,
    LEV5_DIRECTIONAL_PERF_DIFF_MAX_SCALE,
    LEV5_DIRECTIONAL_PERF_DIFF_EMA_ALPHA,
    LEV5_DIRECTIONAL_PERF_HYST_ENABLE,
    LEV5_DIRECTIONAL_PERF_HYST_ENTER_MUL,
    LEV5_DIRECTIONAL_PERF_HYST_EXIT_MUL,
    LEV5_DIRECTIONAL_PERF_STATE_MIN_HOLD_SEC,
    LEV5_DIRECTIONAL_PERF_HOLD_DYNAMIC_ENABLE,
    LEV5_DIRECTIONAL_PERF_HOLD_VOL_LOW,
    LEV5_DIRECTIONAL_PERF_HOLD_VOL_HIGH,
    LEV5_DIRECTIONAL_PERF_HOLD_MIN_SEC,
    LEV5_DIRECTIONAL_PERF_HOLD_MAX_SEC,
    LEV5_DIRECTIONAL_WINRATE_WEIGHT,
    LEV5_DIRECTIONAL_PAYOFF_WEIGHT,
    LEV5_DIRECTIONAL_SCORE_MIN_SAMPLES,
    LEV5_TARGET_SIGNALS_PER_HOUR,
    LEV5_LOW_ACTIVITY_RELAX_Z,
    LEV5_LOW_ACTIVITY_RELAX_EDGE_BPS,
    LEV5_FORCED_RELAX_NO_SIGNAL_SEC_1,
    LEV5_FORCED_RELAX_NO_SIGNAL_SEC_2,
    LEV5_FALLBACK_MIN_NET_EDGE_BPS_BASE,
    LEV5_FALLBACK_MIN_NET_EDGE_BPS_MAX,
    LEV5_TD_MODE,
    LEV5_ACCOUNT_POS_POLL_SEC,
    LEV5_POS_RECONCILE_SEC,
    LEV5_SOFT_FEE_TO_EQUITY_WARN,
    LEV5_CH_AUTOTUNE_ENABLE,
    LEV5_CH_AUTOTUNE_STEP,
    LEV5_CH_PERF_EMA_ALPHA,
    LEV5_CH_W_ADJ_MAX,
    LEV5_CH_W_ADJ_MIN,
    LEV5_AUTOTUNE_Z_ADD_FLOOR,
    LEV5_AUTOTUNE_Z_ADD_FLOOR_FORCED,
    LEV5_AUTOTUNE_EDGE_ADD_HARD_FLOOR,
    LEV5_AUTOTUNE_EDGE_ADD_HARD_FLOOR_FORCED,
    LEV5_FEE_EQUITY_SCALE_RATIO,
    LEV5_FEE_PRESSURE_SIZE_MUL,
    LEV5_FEE_EQUITY_BLOCK_RATIO,
    LEV5_FEE_BLOCK_COOLDOWN_SEC,
    LEV5_PREDICTION_CALIBRATION_ENABLE,
    LEV5_PREDICTION_CALIBRATION_METHOD,
    LEV5_PREDICTION_CALIBRATION_MIN_SAMPLES,
    LEV5_PREDICTION_CALIBRATION_ECE_INTERVAL,
    LEV5_RUNNER_FEE_GATE_MIN_NET_EDGE_BPS,
    LEV5_CT_VAL_BASE,
    LEV5_MIN_CONTRACTS,
    LEV5_MAX_CONTRACTS,
    GRID_LEVERAGE,
    GRID_TD_MODE,
    new_run_id,
)
from quant.store import (
    AuditStore,
    NullAuditStore,
    append_pnl_snapshot,
    append_runtime_checkpoint,
)
from quant.strategy.factory import build_strategy

log = get_logger(__name__)

# 非 lev5 模式下仍节流打印「手续费/权益」软提示
_FEE_RATIO_WARN_TS = 0.0

# 持仓止损日志节流（秒），避免每 tick 刷屏
_POSITION_SL_LOG_INTERVAL_SEC = 60.0
_position_sl_log_state: dict[str, float] = {"t": 0.0}


async def _inventory_bootstrap(
    client: OKXRestClient,
    inst_id: str,
    inv_state: dict[str, Any],
) -> None:
    """启动即拉一次真实余额，避免首几个 tick 仍按「未同步」状态加仓。"""
    try:
        snap = await asyncio.to_thread(
            build_inventory_snapshot,
            client,
            inst_id,
            STRAT_INVENTORY_EFFECTIVE_MAX_BASE,
        )
        inv_state["snapshot"] = snap
        inv_state["last_poll"] = time.monotonic()
        mode_zh = "仅减仓" if snap.get("inventory_mode") == "reduce_only" else "正常"
        log.info(
            "[真实性] 冷启动已同步账户 | base=%.8f | quote≈%.4f | max_base=%s | 模式=%s | 允许买=%s",
            snap["base_avail"],
            snap["quote_avail"],
            snap["max_base_position"],
            mode_zh,
            snap["buy_allowed"],
        )
    except Exception as e:
        log.warning(
            "[真实性] 冷启动库存同步失败: %s（若已设 max，在首包余额成功前将保守禁止买）",
            e,
        )


async def _refresh_inventory_throttled(
    client: OKXRestClient,
    inst_id: str,
    inv_state: dict[str, Any],
) -> None:
    """周期性拉余额，供策略抑制超额买单 + [库存] 日志。"""
    now = time.monotonic()
    lp = inv_state.get("last_poll")
    if lp is not None and now - lp < STRAT_BALANCE_POLL_SEC:
        return
    inv_state["last_poll"] = now
    try:
        snap = await asyncio.to_thread(
            build_inventory_snapshot,
            client,
            inst_id,
            STRAT_INVENTORY_EFFECTIVE_MAX_BASE,
        )
        inv_state["snapshot"] = snap
        if now - inv_state.get("last_inv_log", 0.0) >= STRAT_INVENTORY_LOG_SEC:
            inv_state["last_inv_log"] = now
            mode_zh = "仅减仓" if snap.get("inventory_mode") == "reduce_only" else "正常"
            log.info(
                "[库存] base=%.8f | quote≈%.4f | max_base=%s | 模式=%s | 允许买=%s",
                snap["base_avail"],
                snap["quote_avail"],
                snap["max_base_position"],
                mode_zh,
                snap["buy_allowed"],
            )
    except Exception as e:
        log.warning("[库存] 刷新失败: %s", e)


def _swap_intent_is_perp(intent: object) -> bool:
    inst = getattr(intent, "inst_id", "") or ""
    return getattr(intent, "instrument_type", None) == "swap" or is_swap_inst_id(str(inst))


def _swap_strategy_runtime_base() -> dict[str, Any]:
    """永续共用 runtime（scalp 与 lev5）：持仓摘要、合约规格、盘口/资金费/ Half-Kelly 等。"""
    return {
        "usdt_avail_swap": None,
        "swap_position_summary": None,
        "swap_pending_count": None,
        "instrument_spec": None,
        "last_mid": None,
        "order_book": None,
        "book_imbalance": None,
        "funding_rate": None,
        "next_funding_time_ms": None,
        "micro_ts": None,
        "premium_pct": None,
        "open_interest": None,
        "oi_delta_pct": None,
        "rest_fail_streak": 0,
        "last_candle_snapshot_wall_ts": None,
        "swap_taker_fee_rate": None,
        "account_maker_fee_rate": None,
        "account_taker_fee_rate": None,
        "roundtrip_taker_fee_bps": None,
        "fee_rt_bps_live": None,
        "force_net_pos_side": False,
        "regime_block_entries": False,
        "equity_usdt": None,
        "mtm_usdt": None,
        "half_kelly_contract_cap": None,
        "half_kelly_debug": None,
    }


def _maybe_block_swap_insufficient_margin(
    intent: object,
    *,
    lev5_runtime: dict[str, Any] | None,
    mid_px: float,
    audit: AuditStore | NullAuditStore,
    run_id: str,
    last: float,
    bid: float,
    ask: float,
) -> bool:
    """
    永续开仓（买/卖，非 reduce_only）：用 mid 价估算初始保证金
    required = sz×ctVal×mid/杠杆×1.05；若 USDT 可用 < required，写 signals.reason=
    insufficient_margin_precheck 并丢弃意图（不发 REST）。
    无 usdt_avail_swap 快照时不拦截（交执行层/交易所），除非 RISK_MARGIN_PRECHECK_STRICT=1。
    """
    if not _swap_intent_is_perp(intent):
        return False
    ro = bool(getattr(intent, "reduce_only", False)) or bool(
        getattr(intent, "reduce_only_sell", False)
    )
    if ro:
        return False
    if not is_priced_order(getattr(intent, "ord_type", "")) or not getattr(intent, "px", None):
        return False
    try:
        sz = float(getattr(intent, "sz"))
        px = float(getattr(intent, "px"))
    except (TypeError, ValueError):
        return False
    ct = float(LEV5_CT_VAL_BASE)
    lev = (
        float(getattr(intent, "leverage"))
        if isinstance(getattr(intent, "leverage", None), (int, float))
        else float(LEV5_LEVERAGE)
    )
    usdt_avail: float | None = None
    if isinstance(lev5_runtime, dict):
        spec = lev5_runtime.get("instrument_spec")
        if isinstance(spec, dict):
            try:
                ctv = float(spec.get("ctVal") or 0)
                if ctv > 0:
                    ct = ctv
            except (TypeError, ValueError):
                pass
        ua = lev5_runtime.get("usdt_avail_swap")
        if isinstance(ua, (int, float)):
            usdt_avail = float(ua)
    if usdt_avail is None:
        if RISK_MARGIN_PRECHECK_STRICT:
            detail = {
                "strict_no_balance_snapshot": True,
                "mid_px": round(mid_px, 8),
                "limit_px": round(px, 8),
            }
            fj_raw = getattr(intent, "features_json", None) or "{}"
            try:
                base = json.loads(fj_raw) if isinstance(fj_raw, str) else {}
                if not isinstance(base, dict):
                    base = {}
                fj_out = json.dumps({**base, "margin_precheck": detail}, ensure_ascii=False)
            except Exception:
                fj_out = json.dumps({"margin_precheck": detail}, ensure_ascii=False)
            audit.log_signal(
                run_id,
                intent,  # type: ignore[arg-type]
                last=last,
                bid=bid,
                ask=ask,
                reason="insufficient_margin_precheck_strict",
                features_json=fj_out,
            )
            log.warning(
                "[策略] 严格保证金预检：无 USDT 可用快照，丢弃开仓意图 | inst=%s | "
                "（若余额接口长期失败请关闭 RISK_MARGIN_PRECHECK_STRICT）",
                getattr(intent, "inst_id", ""),
            )
            return True
        return False
    need = swap_open_initial_margin_usdt(
        sz_contracts=sz,
        px=float(mid_px),
        ct_val=ct,
        leverage=lev,
    )
    if usdt_avail + 1e-9 >= need:
        return False
    detail = {
        "need_margin_usdt": round(need, 8),
        "usdt_avail_swap": round(usdt_avail, 8),
        "ct_val": ct,
        "leverage": lev,
        "mid_px": round(mid_px, 8),
        "limit_px": round(px, 8),
    }
    fj_raw = getattr(intent, "features_json", None) or "{}"
    try:
        base = json.loads(fj_raw) if isinstance(fj_raw, str) else {}
        if not isinstance(base, dict):
            base = {}
        merged = {**base, "margin_precheck": detail}
        fj_out = json.dumps(merged, ensure_ascii=False)
    except Exception:
        fj_out = json.dumps({"margin_precheck": detail}, ensure_ascii=False)
    audit.log_signal(
        run_id,
        intent,  # type: ignore[arg-type]
        last=last,
        bid=bid,
        ask=ask,
        reason="insufficient_margin_precheck",
        features_json=fj_out,
    )
    log.warning(
        "[策略] 保证金预检未通过，丢弃意图（不进入执行管道）| inst=%s side=%s need≈%.6f avail≈%.6f USDT",
        getattr(intent, "inst_id", ""),
        getattr(intent, "side", ""),
        need,
        usdt_avail,
    )
    log.info(
        "[审计] 已写入 signals 表 | run_id=%s | reason=insufficient_margin_precheck",
        run_id,
    )
    return True


_DEFAULT_SWAP_TAKER_FEE_RATE = 0.0005


def _parse_taker_fee_rate_from_trade_fee_api(raw: dict[str, Any]) -> float | None:
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, list) or not data:
        return None
    row = data[0]
    if not isinstance(row, dict):
        return None
    for k in ("taker", "takerU"):
        v = row.get(k)
        if v is None or str(v).strip() == "":
            continue
        try:
            x = float(v)
            if x >= 0:
                return x
        except (TypeError, ValueError):
            continue
    return None


def _net_edge_bps_from_intent_features(intent: object) -> float | None:
    fj = getattr(intent, "features_json", None)
    if not fj or not isinstance(fj, str):
        return None
    try:
        d = json.loads(fj)
        if not isinstance(d, dict):
            return None
        v = d.get("net_edge_bps")
        if v is None:
            return None
        return float(v)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _maybe_reject_fee_gate(
    intent: object,
    *,
    lev5_runtime: dict[str, Any] | None,
    audit: AuditStore | NullAuditStore,
    run_id: str,
    last: float,
    bid: float,
    ask: float,
    metrics: Metrics,
) -> bool:
    """
    仅 lev5：若 LEV5_RUNNER_FEE_GATE_MIN_NET_EDGE_BPS > 0，且开仓意图的 features_json
    中 net_edge_bps 低于该阈值，则丢弃意图，写 signals reason=fee_gate_rejected，
    并增加 fee_gate_rejected 计数（不计入 signals 成功数）。
    阈值为 0（默认）时关闭本层门控；策略内仍用 LEV5_MIN_NET_EDGE_BPS 等。
    """
    # lev5 strategy has been removed; keep this hook disabled for grid-only mode.
    return False


def _detailed_tick(
    outcome: str,
    *,
    last: float,
    bid: float,
    ask: float,
    qh: dict[str, Any],
    mctx: dict[str, Any],
    intent: Any,
    extra: dict[str, Any] | None = None,
) -> None:
    try:
        from quant.detailed_daily_log import record_tick

        record_tick(
            outcome=outcome,
            last=last,
            bid=bid,
            ask=ask,
            qh=qh,
            mctx=mctx,
            intent=intent,
            extra=extra,
        )
    except Exception:
        pass


def _detailed_decision(event: str, **fields: Any) -> None:
    try:
        from quant.detailed_daily_log import record_decision

        record_decision(event, **fields)
    except Exception:
        pass


async def _dispatch_tick(
    *,
    strat: object,
    metrics: Metrics,
    audit: AuditStore | NullAuditStore,
    run_id: str,
    pipeline: ExecutionPipeline | None,
    loop: asyncio.AbstractEventLoop,
    last: float,
    bid: float,
    ask: float,
    market_context: dict | None,
    session_trade: SessionTradeStats | None = None,
    account_client: OKXRestClient | None = None,
    inv_state: dict[str, Any] | None = None,
    lev5_guard: dict[str, Any] | None = None,
    lev5_runtime: dict[str, Any] | None = None,
) -> None:
    """单步：tick → 策略 → 审计 →（可选）执行。"""
    metrics.inc_ticks()
    if session_trade is not None:
        session_trade.note_mid(bid, ask, last)
    if account_client is not None and inv_state is not None:
        await _refresh_inventory_throttled(account_client, INST_ID, inv_state)
        snap = inv_state.get("snapshot")
        if snap is None:
            snap = default_inventory_context(
                STRAT_INVENTORY_EFFECTIVE_MAX_BASE,
                conservative_pending=(
                    STRAT_INVENTORY_EFFECTIVE_MAX_BASE is not None
                    and STRAT_INVENTORY_EFFECTIVE_MAX_BASE > 0
                ),
            )
    else:
        snap = default_inventory_context(
            STRAT_INVENTORY_EFFECTIVE_MAX_BASE,
            conservative_pending=False,
        )
    if session_trade is not None:
        pnl_pct = session_trade.position_unrealized_pnl_pct()
        upl_pct = session_trade.position_upl_pct()
        # 仅永续：账户级 MTM/UPL 止损（与 settings 中 LEV5_STOP_LOSS_* 一致）
        hit_swap_mtm = (
            pnl_pct is not None and pnl_pct <= -LEV5_STOP_LOSS_MTM_PCT
        )
        hit_swap_upl = (
            upl_pct is not None and upl_pct <= -LEV5_STOP_LOSS_UPL_PCT
        )
        if hit_swap_mtm or hit_swap_upl:
            snap = {
                **snap,
                "buy_allowed": False,
                "inventory_mode": "reduce_only",
                "position_stop_loss": True,
            }
            now_m = time.monotonic()
            if (
                now_m - _position_sl_log_state["t"]
                >= _POSITION_SL_LOG_INTERVAL_SEC
            ):
                _position_sl_log_state["t"] = now_m
                log.warning(
                    "[止损] 触发 | mode=%s mtm=%.2f%% upl=%.2f%% | "
                    "阈值 swap_mtm=%.2f%% swap_upl=%.2f%% | 仅减仓",
                    STRAT_MODE,
                    (pnl_pct * 100.0) if pnl_pct is not None else float("nan"),
                    (upl_pct * 100.0) if upl_pct is not None else float("nan"),
                    LEV5_STOP_LOSS_MTM_PCT * 100.0,
                    LEV5_STOP_LOSS_UPL_PCT * 100.0,
                )
    if market_context:
        mctx_merged: dict[str, Any] = {**market_context, "inventory": snap}
    else:
        mctx_merged = {"inventory": snap}
    mid_px = (bid + ask) / 2.0 if bid and ask else last
    ts_raw = market_context.get("ticker_ts") if isinstance(market_context, dict) else None
    ob_raw = market_context.get("order_book") if isinstance(market_context, dict) else None
    candle_wall = None
    if lev5_runtime is not None:
        candle_wall = lev5_runtime.get("last_candle_snapshot_wall_ts")
    qh = enrich_quote_context(
        bid=float(bid),
        ask=float(ask),
        mid=float(mid_px),
        ticker_ts=ts_raw,
        order_book=ob_raw if isinstance(ob_raw, dict) else None,
        candle_snapshot_wall_ts=float(candle_wall)
        if isinstance(candle_wall, (int, float))
        else None,
    )
    mctx_merged = {**mctx_merged, **qh}
    if lev5_runtime is not None:
        lev5_runtime["last_mid"] = last
        if market_context and isinstance(market_context.get("order_book"), dict):
            lev5_runtime["order_book"] = market_context.get("order_book")
        mctx_merged["strategy_runtime"] = lev5_runtime
        mctx_merged["lev5_runtime"] = lev5_runtime
    intent = strat.on_tick(  # type: ignore[union-attr]
        last=last,
        bid=bid,
        ask=ask,
        market_context=mctx_merged,
    )
    if intent is None:
        _detailed_tick(
            "no_intent",
            last=last,
            bid=bid,
            ask=ask,
            qh=qh,
            mctx=mctx_merged,
            intent=None,
        )
        return
    if (
        lev5_runtime is not None
        and lev5_runtime.get("force_net_pos_side") is True
        and intent.instrument_type == "swap"
        and intent.pos_side is not None
    ):
        intent = replace(intent, pos_side=None)

    if _maybe_block_swap_insufficient_margin(
        intent,
        lev5_runtime=lev5_runtime,
        mid_px=float(mid_px),
        audit=audit,
        run_id=run_id,
        last=last,
        bid=bid,
        ask=ask,
    ):
        _detailed_tick(
            "margin_precheck_block",
            last=last,
            bid=bid,
            ask=ask,
            qh=qh,
            mctx=mctx_merged,
            intent=intent,
            extra={"mid_px": mid_px},
        )
        _detailed_decision("margin_precheck_block", mid_px=mid_px)
        return

    ro = bool(intent.reduce_only) or bool(intent.reduce_only_sell)
    if (
        lev5_guard is not None
        and lev5_guard.get("halt_new_entries") is True
        and not ro
    ):
        log.warning(
            "[杠杆风控] 丢弃开仓信号：已触发停机 | reason=%s",
            lev5_guard.get("reason"),
        )
        _detailed_tick(
            "lev5_halt_new_entries",
            last=last,
            bid=bid,
            ask=ask,
            qh=qh,
            mctx=mctx_merged,
            intent=intent,
            extra={"reason": lev5_guard.get("reason")},
        )
        _detailed_decision(
            "lev5_halt_new_entries",
            reason=lev5_guard.get("reason"),
        )
        return

    if (
        lev5_runtime is not None
        and lev5_runtime.get("daily_dd_halted")
        and not ro
    ):
        log.warning(
            "[DailyDrawdown] 丢弃开仓信号：日内亏损已达上限（UTC 次日重置）| "
            "loss_pct=%s cap=%s day_open=%s",
            lev5_runtime.get("daily_dd_loss_pct"),
            lev5_runtime.get("daily_dd_cap_pct"),
            lev5_runtime.get("daily_dd_day_open_equity_usdt"),
        )
        _detailed_tick(
            "daily_drawdown_halt",
            last=last,
            bid=bid,
            ask=ask,
            qh=qh,
            mctx=mctx_merged,
            intent=intent,
            extra={
                "loss_pct": lev5_runtime.get("daily_dd_loss_pct"),
                "cap_pct": lev5_runtime.get("daily_dd_cap_pct"),
                "day_open": lev5_runtime.get("daily_dd_day_open_equity_usdt"),
            },
        )
        _detailed_decision(
            "daily_drawdown_halt",
            loss_pct=lev5_runtime.get("daily_dd_loss_pct"),
            cap_pct=lev5_runtime.get("daily_dd_cap_pct"),
        )
        return

    if _maybe_reject_fee_gate(
        intent,
        lev5_runtime=lev5_runtime,
        audit=audit,
        run_id=run_id,
        last=last,
        bid=bid,
        ask=ask,
        metrics=metrics,
    ):
        _detailed_tick(
            "fee_gate_reject",
            last=last,
            bid=bid,
            ask=ask,
            qh=qh,
            mctx=mctx_merged,
            intent=intent,
        )
        _detailed_decision("fee_gate_reject")
        return

    metrics.inc_signals()
    if lev5_runtime is not None:
        lev5_runtime["no_signal_since_ts"] = time.time()
        lev5_runtime["forced_relax_level"] = 0
        lev5_runtime["forced_relax_no_signal_sec"] = 0.0
        if not ro:
            lev5_runtime["entry_signals_after_last_fill"] = (
                int(lev5_runtime.get("entry_signals_after_last_fill", 0)) + 1
            )
            if intent.pos_side == "long":
                lev5_runtime["signals_long_total"] = int(lev5_runtime.get("signals_long_total", 0)) + 1
            elif intent.pos_side == "short":
                lev5_runtime["signals_short_total"] = int(lev5_runtime.get("signals_short_total", 0)) + 1
    audit.log_signal(run_id, intent, last=last, bid=bid, ask=ask)

    log.info(
        "[策略] 产生订单意图 | inst=%s side=%s pos_side=%s reduce_only=%s ord_type=%s px=%s sz=%s tgt_ccy=%s | "
        "last=%s bid=%s ask=%s",
        intent.inst_id,
        intent.side,
        intent.pos_side,
        bool(intent.reduce_only),
        intent.ord_type,
        intent.px,
        intent.sz,
        intent.tgt_ccy,
        last,
        bid,
        ask,
    )
    log.info("[分析] reason=%s", intent.reason)
    if intent.features_json:
        log.info("[分析] features_json=%s", intent.features_json)
    log.info("[审计] 已写入 signals 表 | run_id=%s", run_id)
    try:
        from quant.detailed_daily_log import intent_dict

        _detailed_decision("signal_recorded", intent=intent_dict(intent), run_id=run_id)
    except Exception:
        pass

    if not STRAT_LIVE or pipeline is None:
        log.info(
            "[执行] 本会话未发单（STRAT_LIVE=0）；若需下单请改用 run_strategy_live.py 并配置 .env"
        )
        _detailed_tick(
            "dry_run_no_live_submit",
            last=last,
            bid=bid,
            ask=ask,
            qh=qh,
            mctx=mctx_merged,
            intent=intent,
            extra={"STRAT_LIVE": STRAT_LIVE, "pipeline_is_none": pipeline is None},
        )
        _detailed_decision("dry_run_no_live_submit", STRAT_LIVE=STRAT_LIVE)
        return

    intent = replace(
        intent,
        signal_last=float(last),
        signal_bid=float(bid),
        signal_ask=float(ask),
    )
    _detailed_tick(
        "submitting_to_executor",
        last=last,
        bid=bid,
        ask=ask,
        qh=qh,
        mctx=mctx_merged,
        intent=intent,
    )
    _detailed_decision("pipeline_submit_enqueued")
    try:
        await loop.run_in_executor(None, partial(pipeline.submit, intent))
    except Exception as e:
        log.exception("[结果] 下单链路异常: %s", e)
        _detailed_decision("pipeline_submit_exception", error=str(e))


def _risk_engine(*, swap_ct_val: float | None = None) -> RiskEngine:
    max_n = (
        float(RISK_MAX_NOTIONAL_USDT) if RISK_MAX_NOTIONAL_USDT.strip() else None
    )
    max_b = float(RISK_MAX_ORDER_BASE) if RISK_MAX_ORDER_BASE.strip() else None
    return RiskEngine(
        RiskConfig(
            max_notional_usdt=max_n,
            max_order_base=max_b,
            swap_ct_val=swap_ct_val,
            check_balance=RISK_CHECK_BALANCE,
            balance_buffer_pct=RISK_BALANCE_BUFFER_PCT,
        )
    )


def _log_session_banner(
    *,
    run_id: str,
    audit_path: Path | None,
) -> None:
    """启动时打一块总览，方便对照终端与 .env。"""
    log.info("========== 会话开始 ==========")
    log.info("[配置] run_id=%s", run_id)
    log.info(
        "[配置] 交易对 INST_ID=%s | 策略 STRAT_MODE=%s | 发单类型 STRAT_ORDER_TYPE=%s"
        "%s",
        INST_ID,
        STRAT_MODE,
        STRAT_ORDER_TYPE,
        (
            f" | STRAT_MARKET_TGT_CCY={STRAT_MARKET_TGT_CCY!r}"
            if STRAT_ORDER_TYPE == "market"
            else ""
        ),
    )
    if (
        STRAT_ORDER_NOTIONAL_MIN_USDT is not None
        and STRAT_ORDER_NOTIONAL_MAX_USDT is not None
    ):
        log.info(
            "[配置] 单笔名义 USDT（随机）[%s, %s]",
            STRAT_ORDER_NOTIONAL_MIN_USDT,
            STRAT_ORDER_NOTIONAL_MAX_USDT,
        )
    elif STRAT_ORDER_NOTIONAL_USDT is not None:
        log.info("[配置] 单笔名义 USDT（固定）=%s", STRAT_ORDER_NOTIONAL_USDT)
    else:
        log.info("[配置] 单笔数量 STRAT_ORDER_SZ=%s（未启用 USDT 名义）", STRAT_ORDER_SZ)
    log.info(
        "[配置] 库存 cap mode=%s ref=%s 上浮=%s legacy_STRAT_MAX=%r effective_max=%s | "
        "余额刷新=%ss | 库存日志=%ss | 超时撤单=%ss（轮询=%ss，0=关）",
        STRAT_INVENTORY_META.get("mode"),
        STRAT_INVENTORY_META.get("ref"),
        STRAT_INVENTORY_META.get("above"),
        STRAT_MAX_BASE_POSITION,
        STRAT_INVENTORY_EFFECTIVE_MAX_BASE,
        STRAT_BALANCE_POLL_SEC,
        STRAT_INVENTORY_LOG_SEC,
        STRAT_STALE_ORDER_CANCEL_SEC,
        STRAT_STALE_ORDER_POLL_SEC,
    )
    log.info(
        "[配置] STRAT_LIVE=%s（是否真发 REST）| RISK_ENABLED=%s | OKX_WS_DIRECT=%s",
        STRAT_LIVE,
        RISK_ENABLED,
        OKX_WS_DIRECT,
    )
    log.info(
        "[配置] 熔断 CIRCUIT_MAX_FAILS=%s COOLDOWN_SEC=%s | 对账间隔 RECONCILE_SEC=%s",
        CIRCUIT_MAX_FAILS,
        CIRCUIT_COOLDOWN_SEC,
        RECONCILE_INTERVAL_SEC,
    )
    log.info(
        "[配置] CorrelationGuard 快照间隔 RISK_CORR_RESYNC_INTERVAL_SEC=%s | "
        "严格保证金预检 RISK_MARGIN_PRECHECK_STRICT=%s",
        RISK_CORR_RESYNC_INTERVAL_SEC,
        RISK_MARGIN_PRECHECK_STRICT,
    )
    log.info(
        "[配置] 永续 RISK_SWAP_IGNORE_MAX_CAPS=%s（为真时不卡 RISK_MAX_*，单笔看可用 USDT×杠杆）",
        RISK_SWAP_IGNORE_MAX_CAPS,
    )
    log.info(
        "[配置] 风控上限 RISK_MAX_NOTIONAL_USDT=%r RISK_MAX_ORDER_BASE=%r（RISK_SWAP_IGNORE_MAX_CAPS=0 时生效）",
        RISK_MAX_NOTIONAL_USDT,
        RISK_MAX_ORDER_BASE,
    )
    log.info(
        "[配置] CorrelationGuard RISK_MAX_CORR_NOTIONAL_USDT=%r（空=关；同向挂单+在途+本笔名义上限）",
        RISK_MAX_CORR_NOTIONAL_USDT,
    )
    log.info(
        "[配置] DailyDrawdownBreaker RISK_DAILY_MAX_LOSS_PCT=%.4f（0=关；相对 UTC 当日基准权益 max 亏损含浮亏）",
        float(RISK_DAILY_MAX_LOSS_PCT),
    )
    log.info(
        "[配置] 余额风控 RISK_CHECK_BALANCE=%s 缓冲=%.4f%%（发单前对比 OKX 可用余额）",
        RISK_CHECK_BALANCE,
        RISK_BALANCE_BUFFER_PCT * 100.0,
    )
    log.info(
        "[配置] REST 轮询=%.2fs（仅 STRAT_DATA_SOURCE=rest）",
        REST_POLL_INTERVAL_SEC,
    )
    log.warning(
        "[配置][网格] leverage=%.2fx td_mode=%s | guard_poll=%.1fs dd_limit=%.2f%%",
        GRID_LEVERAGE,
        GRID_TD_MODE,
        LEV5_GUARD_POLL_SEC,
        LEV5_DAILY_DD_LIMIT_PCT * 100.0,
    )
    if AUDIT_ENABLED and audit_path is not None:
        log.info("[配置] 审计 AUDIT_ENABLED=1 | SQLite=%s", audit_path)
    else:
        log.info("[配置] 审计 AUDIT_ENABLED=0（不写库）")


def _apply_half_kelly_to_lev5_runtime(
    lev5_runtime: dict[str, Any],
    session_trade: SessionTradeStats,
) -> None:
    """在 session_trade.refresh 之后调用：用最新 fills / 权益更新 Half-Kelly 张数上界。"""
    if not RISK_HALF_KELLY_ENABLE:
        return
    if STRAT_MODE != "grid_pro" or not INST_ID.upper().endswith("-SWAP"):
        return
    try:
        last_mid = lev5_runtime.get("last_mid")
        if not isinstance(last_mid, (int, float)) or float(last_mid) <= 0:
            return
        mid_px = float(last_mid)
        spec = lev5_runtime.get("instrument_spec")
        if not isinstance(spec, dict):
            spec = {}
        ct_val = float(spec.get("ctVal") or LEV5_CT_VAL_BASE or 0.01)
        lot_sz = float(spec.get("lotSz") or LEV5_MIN_CONTRACTS)
        max_c = float(LEV5_MAX_CONTRACTS)
        min_c = float(LEV5_MIN_CONTRACTS)
        mns = spec.get("minSz")
        mxs = spec.get("maxLmtSz")
        if mns is not None:
            try:
                min_c = max(min_c, float(mns))
            except (TypeError, ValueError):
                pass
        if mxs is not None:
            try:
                max_c = min(max_c, float(mxs))
            except (TypeError, ValueError):
                pass
        _lev = float(GRID_LEVERAGE)
        cap, dbg = RiskEngine.recommend_half_kelly_swap_contracts(
            fills=session_trade.last_fills_for_half_kelly(),
            equity_usdt=session_trade.last_equity_usdt(),
            mid=mid_px,
            ct_val=ct_val,
            leverage=_lev,
            balance_buffer_pct=float(RISK_BALANCE_BUFFER_PCT),
            min_contracts=min_c,
            max_contracts=max_c,
            lot_sz=lot_sz,
            conservative_contracts=float(RISK_HALF_KELLY_CONSERVATIVE_CONTRACTS),
            min_samples=int(RISK_HALF_KELLY_MIN_SAMPLES),
            max_fills=int(RISK_HALF_KELLY_WINDOW),
        )
        lev5_runtime["half_kelly_contract_cap"] = cap
        lev5_runtime["half_kelly_debug"] = dbg
    except Exception as e:
        log.debug("[风控] Half-Kelly 张数上界计算失败: %s", e)


def _parse_tick(row: dict) -> tuple[float, float, float] | None:
    try:
        last = float(row["last"])
        bid = float(row["bidPx"])
        ask = float(row["askPx"])
        return last, bid, ask
    except (KeyError, TypeError, ValueError):
        return None


def _lev5_regime_metrics_suffix(lev5_runtime: dict[str, Any] | None) -> str:
    if not isinstance(lev5_runtime, dict):
        return "regime=n/a | adx=n/a"
    r = str(lev5_runtime.get("regime") or "n/a")
    adx = lev5_runtime.get("regime_adx")
    try:
        adx_f = float(adx)
        adx_s = f"{adx_f:.1f}" if adx_f == adx_f else "n/a"
    except (TypeError, ValueError):
        adx_s = "n/a"
    return f"regime={r} | adx={adx_s}"


async def _metrics_loop(
    metrics: Metrics,
    client: OKXRestClient | None,
    session_trade: SessionTradeStats | None,
    run_id: str,
    audit: AuditStore | NullAuditStore | None = None,
    lev5_runtime: dict[str, Any] | None = None,
) -> None:
    global _FEE_RATIO_WARN_TS
    while True:
        await asyncio.sleep(METRICS_INTERVAL_SEC)
        snap = metrics.snapshot()
        fills_n = "n/a"
        net_pnl_fee_s = "n/a"
        st_refresh: Any = None
        if client is not None and session_trade is not None:
            try:
                st_refresh = await asyncio.to_thread(session_trade.refresh, client)
                fills_n = str(st_refresh.fills_count)
                if st_refresh.realized_fifo_pnl_usdt is not None:
                    net_pnl_fee_s = f"{float(st_refresh.realized_fifo_pnl_usdt) + float(st_refresh.fees_usdt):+.6f}"
                if isinstance(lev5_runtime, dict) and st_refresh is not None:
                    _apply_half_kelly_to_lev5_runtime(lev5_runtime, session_trade)
            except Exception:
                fills_n = "err"
                net_pnl_fee_s = "err"
        regime_suffix = _lev5_regime_metrics_suffix(lev5_runtime)
        sig_struct: dict[str, int] | None = None
        if audit is not None:
            try:
                sig_struct = audit.signal_reason_breakdown(run_id)
            except Exception:
                sig_struct = None
        sig_total = int(snap["signals"])
        ord_ok = int(snap["orders_ok"])
        fills_i = None
        try:
            fills_i = int(fills_n)
        except (TypeError, ValueError):
            fills_i = None
        conv_so = (ord_ok / sig_total) if sig_total > 0 else 0.0
        conv_of = (
            (int(fills_i) / ord_ok) if (fills_i is not None and ord_ok > 0) else 0.0
        )
        log.info(
            "[指标] 累计 | ticks=%s signals=%s fee_gate_rejected=%s orders_ok=%s orders_fail=%s "
            "circuit_skip=%s uptime_sec=%s | fills_api=%s（REST 成交明细条数，与 orders_ok 不同）"
            " | 转化 signals->orders=%.2f%% orders->fills=%.2f%%"
            " | 信号结构 open=%s close=%s stale=%s"
            " | net_pnl_after_fee=%s（fillPnl 累计+fee 累计，fee 为负） | %s",
            snap["ticks"],
            snap["signals"],
            snap.get("fee_gate_rejected_count", 0),
            snap["orders_ok"],
            snap["orders_fail"],
            snap["circuit_open_count"],
            snap["uptime_sec"],
            fills_n,
            conv_so * 100.0,
            conv_of * 100.0,
            (sig_struct or {}).get("open", "n/a"),
            (sig_struct or {}).get("close", "n/a"),
            (sig_struct or {}).get("stale_exit", "n/a"),
            net_pnl_fee_s,
            regime_suffix,
        )
        if client is not None and session_trade is not None:
            try:
                st = st_refresh if st_refresh is not None else await asyncio.to_thread(
                    session_trade.refresh, client
                )
                fee_note = "（含非 USDT 手续费）" if st.fees_other_ccy else ""
                _na = "n/a"
                eq_s = (
                    f"{st.equity_now_usdt:.4f}"
                    if st.equity_now_usdt is not None
                    else _na
                )
                mtm_s = (
                    f"{st.mtm_pnl_usdt:+.4f}"
                    if st.mtm_pnl_usdt is not None
                    else _na
                )
                base_s = (
                    f"{st.baseline_equity_usdt:.4f}"
                    if st.baseline_equity_usdt is not None
                    else _na
                )
                fifo_s = (
                    f"{st.realized_fifo_pnl_usdt:+.6f}"
                    if st.realized_fifo_pnl_usdt is not None
                    else _na
                )
                funding_s = (
                    f"{st.realized_funding_pnl_usdt:+.6f}"
                    if st.realized_funding_pnl_usdt is not None
                    else _na
                )
                net_s = (
                    f"{st.net_realized_pnl_usdt:+.6f}"
                    if st.net_realized_pnl_usdt is not None
                    else _na
                )
                fee_cover_s = _na
                try:
                    fee_abs = abs(float(st.fees_usdt))
                    if fee_abs > 1e-12 and st.realized_fifo_pnl_usdt is not None:
                        fee_cover_s = f"{float(st.realized_fifo_pnl_usdt) / fee_abs:.2f}x"
                except Exception:
                    fee_cover_s = _na
                open_s = (
                    f"{st.open_base_after_fills:.8f}"
                    if st.open_base_after_fills is not None
                    else _na
                )
                log.info(
                    "[盈亏] 本会话 | inst=%s | "
                    "REST下单成功累计=%s（限价单=已挂单，未必成交；与网页「成交」不是同一概念）| "
                    "[指标]策略意图signals=%s | "
                    "成交明细条数(fills API)=%s | "
                    "手续费(USDT)≈%.8f%s | 交易已实现/FIFO(USDT)≈%s | fee覆盖率≈%s | "
                    "资金费累计(USDT)≈%s | 净已实现(交易+资金费 USDT)≈%s | 未平仓base≈%s | "
                    "权益(标价)≈%s USDT | 会话盈亏(MTM)≈%s USDT | 基准权益≈%s USDT | %s",
                    INST_ID,
                    snap["orders_ok"],
                    snap["signals"],
                    st.fills_count,
                    st.fees_usdt,
                    fee_note,
                    fifo_s,
                    fee_cover_s,
                    funding_s,
                    net_s,
                    open_s,
                    eq_s,
                    mtm_s,
                    base_s,
                    _lev5_regime_metrics_suffix(lev5_runtime),
                )
                try:
                    if isinstance(lev5_runtime, dict) and st.equity_now_usdt is not None:
                        eq = float(st.equity_now_usdt)
                        if eq > 1e-6:
                            r = float(st.fees_usdt) / eq
                            lev5_runtime["session_fee_to_equity_ratio"] = r
                            if r >= float(LEV5_FEE_EQUITY_BLOCK_RATIO):
                                lev5_runtime["fee_pressure_block_entries_until"] = (
                                    time.time() + float(LEV5_FEE_BLOCK_COOLDOWN_SEC)
                                )
                                lev5_runtime["fee_pressure_size_mul"] = 1.0
                            elif r >= float(LEV5_FEE_EQUITY_SCALE_RATIO):
                                lev5_runtime["fee_pressure_size_mul"] = float(LEV5_FEE_PRESSURE_SIZE_MUL)
                            else:
                                lev5_runtime["fee_pressure_size_mul"] = 1.0
                            blk = float(lev5_runtime.get("fee_pressure_block_entries_until") or 0.0)
                            if r < float(LEV5_FEE_EQUITY_BLOCK_RATIO) and time.time() >= blk:
                                lev5_runtime["fee_pressure_block_entries_until"] = 0.0
                except Exception:
                    pass
                try:
                    eqv = st.equity_now_usdt
                    if (
                        eqv is not None
                        and eqv > 1e-6
                        and st.fees_usdt > 0
                        and (st.fees_usdt / float(eqv)) >= float(LEV5_SOFT_FEE_TO_EQUITY_WARN)
                    ):
                        lw = (
                            float(lev5_runtime.get("_last_fee_ratio_warn_ts") or 0.0)
                            if isinstance(lev5_runtime, dict)
                            else _FEE_RATIO_WARN_TS
                        )
                        if time.time() - lw > 900.0:
                            if isinstance(lev5_runtime, dict):
                                lev5_runtime["_last_fee_ratio_warn_ts"] = time.time()
                            else:
                                _FEE_RATIO_WARN_TS = time.time()
                            log.warning(
                                "[盈亏][费用] 手续费/权益≈%.2f%%（软提示：换手成本偏高；对照 fills 与 prediction）",
                                (st.fees_usdt / float(eqv)) * 100.0,
                            )
                except Exception:
                    pass
                try:
                    eq = lev5_runtime.get("exec_quality") if isinstance(lev5_runtime, dict) else None
                    append_pnl_snapshot(
                        {
                            "orders_ok": snap["orders_ok"],
                            "signals": snap["signals"],
                            "fills_count": st.fills_count,
                            "fees_usdt": st.fees_usdt,
                            "realized_fifo_usdt": st.realized_fifo_pnl_usdt,
                            "realized_funding_pnl_usdt": st.realized_funding_pnl_usdt,
                            "net_realized_pnl_usdt": st.net_realized_pnl_usdt,
                            "open_base": st.open_base_after_fills,
                            "mtm_usdt": st.mtm_pnl_usdt,
                            "inst_id": INST_ID,
                            "run_id": run_id,
                            "okx_simulated": False,
                            "adaptive_z_add": (
                                lev5_runtime.get("adaptive_z_add")
                                if isinstance(lev5_runtime, dict)
                                else None
                            ),
                            "adaptive_edge_bps_add": (
                                lev5_runtime.get("adaptive_edge_bps_add")
                                if isinstance(lev5_runtime, dict)
                                else None
                            ),
                            "funding_rate": (
                                lev5_runtime.get("funding_rate")
                                if isinstance(lev5_runtime, dict)
                                else None
                            ),
                            "premium_pct": (
                                lev5_runtime.get("premium_pct")
                                if isinstance(lev5_runtime, dict)
                                else None
                            ),
                            "oi_delta_pct": (
                                lev5_runtime.get("oi_delta_pct")
                                if isinstance(lev5_runtime, dict)
                                else None
                            ),
                            "book_imbalance": (
                                lev5_runtime.get("book_imbalance")
                                if isinstance(lev5_runtime, dict)
                                else None
                            ),
                            "exec_q_post_only": eq.get("post_only") if isinstance(eq, dict) else None,
                            "exec_q_ioc": eq.get("ioc") if isinstance(eq, dict) else None,
                            "exec_q_limit": eq.get("limit") if isinstance(eq, dict) else None,
                            "edge_k_z_mul": (
                                lev5_runtime.get("edge_k_z_mul")
                                if isinstance(lev5_runtime, dict)
                                else None
                            ),
                            "edge_k_imb_mul": (
                                lev5_runtime.get("edge_k_imb_mul")
                                if isinstance(lev5_runtime, dict)
                                else None
                            ),
                            "edge_k_prem_mul": (
                                lev5_runtime.get("edge_k_prem_mul")
                                if isinstance(lev5_runtime, dict)
                                else None
                            ),
                            "diag_expected_edge_bps": (
                                lev5_runtime.get("diag_expected_edge_bps")
                                if isinstance(lev5_runtime, dict)
                                else None
                            ),
                            "diag_adverse_slip_bps": (
                                lev5_runtime.get("diag_adverse_slip_bps")
                                if isinstance(lev5_runtime, dict)
                                else None
                            ),
                        }
                    )
                    if isinstance(lev5_runtime, dict):
                        append_runtime_checkpoint(
                            "metrics_pnl_snapshot",
                            {
                                "run_id": run_id,
                                "inst_id": INST_ID,
                                "okx_simulated": False,
                                "orders_ok": snap["orders_ok"],
                                "signals": snap["signals"],
                                "fills_count": st.fills_count,
                                "mtm_usdt": st.mtm_pnl_usdt,
                                "realized_fifo_usdt": st.realized_fifo_pnl_usdt,
                                "realized_funding_pnl_usdt": st.realized_funding_pnl_usdt,
                                "net_realized_pnl_usdt": st.net_realized_pnl_usdt,
                                "fees_usdt": st.fees_usdt,
                                "equity_usdt": st.equity_now_usdt,
                                "adaptive_z_add": lev5_runtime.get("adaptive_z_add"),
                                "adaptive_edge_bps_add": lev5_runtime.get("adaptive_edge_bps_add"),
                                "target_signals_1h": lev5_runtime.get("target_signals_1h"),
                                "signals_1h": lev5_runtime.get("signals_1h"),
                                "edge_score_long": lev5_runtime.get("edge_score_long"),
                                "edge_score_short": lev5_runtime.get("edge_score_short"),
                                "directional_perf_bias_state": lev5_runtime.get("directional_perf_bias_state"),
                            },
                        )
                except Exception:
                    pass
            except Exception as e:
                log.warning("[盈亏] 汇总失败: %s", e)


async def _lev5_guard_loop(
    client: OKXRestClient,
    session_trade: SessionTradeStats,
    guard: dict[str, Any],
    runtime: dict[str, Any],
) -> None:
    while True:
        await asyncio.sleep(LEV5_GUARD_POLL_SEC)
        try:
            st = await asyncio.to_thread(session_trade.refresh, client)
            mtm = st.mtm_pnl_usdt
            base = st.baseline_equity_usdt
            dd = None
            if (
                mtm is not None
                and base is not None
                and isinstance(base, (int, float))
                and float(base) > 0
            ):
                dd = float(mtm) / float(base)
            runtime["equity_usdt"] = st.equity_now_usdt
            runtime["mtm_usdt"] = st.mtm_pnl_usdt
            br_dd = runtime.get("daily_drawdown_breaker")
            if isinstance(br_dd, DailyDrawdownBreaker) and br_dd.enabled():
                br_dd.update_from_equity(st.equity_now_usdt, runtime)
            r_now = st.realized_fifo_pnl_usdt
            r_prev = guard.get("last_realized")
            if isinstance(r_now, (int, float)) and isinstance(r_prev, (int, float)):
                delta = float(r_now) - float(r_prev)
                if delta < -1e-9:
                    guard["consec_loss"] = int(guard.get("consec_loss", 0)) + 1
                elif delta > 1e-9:
                    guard["consec_loss"] = 0
            if isinstance(r_now, (int, float)):
                guard["last_realized"] = float(r_now)

            try:
                bal_raw = await asyncio.to_thread(client.balance, None)
                av = parse_balance_availability(bal_raw)
                runtime["usdt_avail_swap"] = av.get("USDT")
            except Exception:
                pass
            now_ts = time.time()
            try:
                last_tf = float(runtime.get("_last_swap_taker_fee_fetch_ts") or 0.0)
                if (
                    INST_ID.upper().endswith("-SWAP")
                    and now_ts - last_tf >= 3600.0
                ):
                    fee_raw = await asyncio.to_thread(client.trade_fee_swap, INST_ID)
                    tr = _parse_taker_fee_rate_from_trade_fee_api(fee_raw)
                    if tr is not None:
                        runtime["swap_taker_fee_rate"] = tr
                    runtime["_last_swap_taker_fee_fetch_ts"] = now_ts
            except Exception:
                pass
            last_p = runtime.get("_last_pos_poll_ts")
            last_pv = float(last_p) if isinstance(last_p, (int, float)) else 0.0
            if (
                INST_ID.upper().endswith("-SWAP")
                and now_ts - last_pv >= float(LEV5_ACCOUNT_POS_POLL_SEC)
            ):
                runtime["_last_pos_poll_ts"] = now_ts
                try:
                    pos_raw = await asyncio.to_thread(client.positions_swap, INST_ID)
                    runtime["swap_position_summary"] = _summarize_swap_positions(pos_raw)
                except Exception:
                    pass
                try:
                    pend_raw = await asyncio.to_thread(client.orders_pending, INST_ID)
                    runtime["swap_pending_count"] = (
                        len(pend_raw.get("data") or [])
                        if isinstance(pend_raw, dict)
                        else 0
                    )
                except Exception:
                    pass

            halt = False
            reason = None
            if isinstance(dd, float) and dd <= -abs(LEV5_DAILY_DD_LIMIT_PCT):
                halt = True
                reason = (
                    "daily_drawdown_limit "
                    f"dd={dd:.4f} limit={-abs(LEV5_DAILY_DD_LIMIT_PCT):.4f}"
                )
            if int(guard.get("consec_loss", 0)) >= int(LEV5_MAX_CONSEC_LOSS):
                halt = True
                reason = (
                    "consecutive_losses "
                    f"count={guard.get('consec_loss')} limit={LEV5_MAX_CONSEC_LOSS}"
                )
            if halt and not guard.get("halt_new_entries"):
                guard["halt_new_entries"] = True
                guard["reason"] = reason
                guard["halt_since"] = time.monotonic()
                log.error(
                    "[杠杆风控] 触发停机：禁止新开仓，仅允许 reduce_only 平仓 | %s",
                    reason,
                )
                append_runtime_checkpoint(
                    "guard_halt",
                    {
                        "inst_id": INST_ID,
                        "reason": reason,
                        "mtm_usdt": st.mtm_pnl_usdt,
                        "equity_usdt": st.equity_now_usdt,
                        "baseline_equity_usdt": st.baseline_equity_usdt,
                        "consec_loss": guard.get("consec_loss"),
                    },
                )
            if guard.get("halt_new_entries"):
                hs = guard.get("halt_since")
                if isinstance(hs, (int, float)):
                    cd = max(30.0, float(LEV5_HALT_COOLDOWN_SEC))
                    if time.monotonic() - float(hs) >= cd:
                        guard["halt_new_entries"] = False
                        guard["reason"] = None
                        guard["consec_loss"] = 0
                        guard["halt_since"] = None
                        log.warning(
                            "[杠杆风控] 冷却结束，恢复开仓 | cooldown=%.0fs",
                            cd,
                        )
                        append_runtime_checkpoint(
                            "guard_resume",
                            {
                                "inst_id": INST_ID,
                                "cooldown_sec": cd,
                                "mtm_usdt": st.mtm_pnl_usdt,
                                "equity_usdt": st.equity_now_usdt,
                            },
                        )
        except Exception as e:
            log.warning("[杠杆风控] 守护轮询失败: %s", e)


async def _lev5_funding_loop(runtime: dict[str, Any]) -> None:
    while True:
        await asyncio.sleep(60.0)
        try:
            fs = await asyncio.to_thread(get_funding_snapshot, INST_ID)
            if isinstance(fs, dict):
                runtime["funding_rate"] = fs.get("fundingRate")
                runtime["next_funding_time_ms"] = fs.get("nextFundingTime")
            else:
                fr = await asyncio.to_thread(get_funding_rate, INST_ID)
                runtime["funding_rate"] = fr
        except Exception as e:
            log.debug("[杠杆] funding 拉取失败: %s", e)


async def _lev5_instrument_loop(runtime: dict[str, Any]) -> None:
    while True:
        await asyncio.sleep(300.0)
        try:
            spec = await asyncio.to_thread(get_swap_instrument_spec, INST_ID)
            if isinstance(spec, dict):
                runtime["instrument_spec"] = spec
        except Exception as e:
            log.debug("[杠杆] instruments 拉取失败: %s", e)


async def _lev5_microstructure_loop(runtime: dict[str, Any]) -> None:
    prev_oi: float | None = None
    while True:
        await asyncio.sleep(max(1.0, LEV5_MICRO_POLL_SEC))
        try:
            oi = await asyncio.to_thread(get_open_interest, INST_ID)
            mark = await asyncio.to_thread(get_mark_price, INST_ID)
            last = runtime.get("last_mid")
            books = runtime.get("order_book")
            if isinstance(mark, (int, float)) and isinstance(last, (int, float)) and float(last) > 0:
                runtime["premium_pct"] = (float(mark) - float(last)) / float(last)
            if isinstance(books, dict):
                bids = books.get("bids") or []
                asks = books.get("asks") or []
                bsum = 0.0
                asum = 0.0
                for x in bids[:5]:
                    try:
                        bsum += float(x[1])
                    except Exception:
                        pass
                for x in asks[:5]:
                    try:
                        asum += float(x[1])
                    except Exception:
                        pass
                den = bsum + asum
                if den > 1e-12:
                    runtime["book_imbalance"] = (bsum - asum) / den
            if isinstance(oi, (int, float)):
                runtime["open_interest"] = float(oi)
                if isinstance(prev_oi, (int, float)) and prev_oi > 0:
                    runtime["oi_delta_pct"] = (float(oi) - float(prev_oi)) / float(prev_oi)
                prev_oi = float(oi)
            runtime["micro_ts"] = time.time()
        except Exception as e:
            log.debug("[杠杆] microstructure 拉取失败: %s", e)


async def _ws_price_feed_loop(runtime: dict[str, Any]) -> None:
    """后台 WebSocket 实时行情写入 runtime，供 _eval_exit 使用最新价格。"""
    while True:
        try:
            async for row in stream_tickers(OKX_WS_PUBLIC_URL_LIST, INST_ID):
                p = _parse_tick(row)
                if p:
                    _last, _bid, _ask = p
                    runtime["ws_last"] = _last
                    runtime["ws_bid"] = _bid
                    runtime["ws_ask"] = _ask
                    runtime["ws_ts"] = time.time()
        except Exception as e:
            log.warning("[WS行情] 连接断开: %s | 5s 后重连", e)
            await asyncio.sleep(5.0)


async def _ws_stall_watchdog_loop(
    *,
    strat: object,
    metrics: Any,
    audit: Any,
    run_id: str,
    pipeline: Any,
    loop: asyncio.AbstractEventLoop,
    session_trade: Any,
    account_client: Any,
    inv_state: Any,
    lev5_guard: Any,
    lev5_runtime: dict[str, Any],
    stall_sec: float = 30.0,
) -> None:
    """WS 静默保底：行情 dispatch 超过 stall_sec 无新 tick 时注入 synthetic tick。
    解决场景：冷静期结束后 WS 网络抖动导致 on_tick 长时间不被调用，网格无法自动恢复。
    """
    await asyncio.sleep(60.0)  # 初始等待，给 WS 建连和首个 tick 到来的时间
    while True:
        await asyncio.sleep(5.0)
        now = time.time()
        last_dispatch = float(lev5_runtime.get("ws_dispatch_ts") or 0.0)
        if now - last_dispatch < stall_sec:
            continue
        synth_last = float(lev5_runtime.get("ws_last") or 0.0)
        if synth_last <= 0:
            continue
        synth_bid = float(lev5_runtime.get("ws_bid") or synth_last)
        synth_ask = float(lev5_runtime.get("ws_ask") or synth_last)
        log.warning(
            "[WS][保底] 行情 dispatch 静默 %.0fs，注入 synthetic tick | last=%.2f",
            now - last_dispatch,
            synth_last,
        )
        synth_ctx: dict[str, Any] = {
            "source": "ws_synthetic",
            "instId": INST_ID,
            "candle_features": lev5_runtime.get("candle_ctx") or {},
        }
        try:
            await _dispatch_tick(
                strat=strat,
                metrics=metrics,
                audit=audit,
                run_id=run_id,
                pipeline=pipeline,
                loop=loop,
                last=synth_last,
                bid=synth_bid,
                ask=synth_ask,
                market_context=synth_ctx,
                session_trade=session_trade,
                account_client=account_client,
                inv_state=inv_state,
                lev5_guard=lev5_guard,
                lev5_runtime=lev5_runtime,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("[WS][保底] synthetic tick dispatch 失败: %s", e)
        finally:
            lev5_runtime["ws_dispatch_ts"] = time.time()


async def _candle_refresh_loop(runtime: dict[str, Any]) -> None:
    """后台定期拉取 K 线特征缓存到 runtime，供 WS 模式 market_context 注入。
    WS ticker 速度快（200ms），但没有 K 线数据；此任务每 15s 刷新一次补充特征。
    """
    while True:
        try:
            snapshot = await asyncio.to_thread(
                fetch_rest_snapshot,
                INST_ID,
                bar=REST_CANDLES_BAR,
                limit=REST_CANDLES_LIMIT,
                http_timeout_sec=REST_HTTP_TIMEOUT_SEC,
            )
            _prices, mctx = snapshot
            cf = mctx.get("candle_features")
            ob = mctx.get("order_book")
            if isinstance(cf, dict) and not cf.get("insufficient"):
                runtime["candle_ctx"] = cf
                runtime["candle_ctx_ts"] = time.time()
            if isinstance(ob, dict):
                runtime["order_book"] = ob
        except Exception as e:
            log.debug("[K线刷新] 拉取失败: %s", e)
        await asyncio.sleep(15.0)


def _summarize_swap_positions(raw: dict[str, Any]) -> dict[str, Any]:
    long_sz = 0.0
    short_sz = 0.0
    long_upl = 0.0
    short_upl = 0.0
    long_ct_ms: int | None = None
    short_ct_ms: int | None = None
    for row in raw.get("data") or []:
        if not isinstance(row, dict):
            continue
        ps = str(row.get("posSide") or "").lower()
        try:
            p = float(row.get("pos") or 0.0)
        except (TypeError, ValueError):
            continue
        try:
            upl = float(row.get("upl") or 0.0)
        except (TypeError, ValueError):
            upl = 0.0
        ctm = row.get("cTime") or row.get("openTime") or row.get("createdTime")
        try:
            cms = int(float(ctm)) if ctm is not None else None
        except (TypeError, ValueError):
            cms = None
        if ps == "long":
            long_sz += abs(p)
            long_upl += upl
            if abs(p) > 1e-12 and cms is not None:
                long_ct_ms = cms if long_ct_ms is None else min(long_ct_ms, cms)
        elif ps == "short":
            short_sz += abs(p)
            short_upl += upl
            if abs(p) > 1e-12 and cms is not None:
                short_ct_ms = cms if short_ct_ms is None else min(short_ct_ms, cms)
        elif ps == "net":
            if p > 0:
                long_sz += abs(p)
                long_upl += upl
                if cms is not None:
                    long_ct_ms = cms if long_ct_ms is None else min(long_ct_ms, cms)
            elif p < 0:
                short_sz += abs(p)
                short_upl += upl
                if cms is not None:
                    short_ct_ms = cms if short_ct_ms is None else min(short_ct_ms, cms)
    return {
        "long_sz": long_sz,
        "short_sz": short_sz,
        "long_upl": long_upl,
        "short_upl": short_upl,
        "has_any": long_sz > 1e-12 or short_sz > 1e-12,
        # OKX 持仓创建时间(ms)→秒，用于真实持仓时长（修正本地 enter_ts 漂移）
        "long_open_ts_sec": (long_ct_ms / 1000.0) if long_ct_ms else None,
        "short_open_ts_sec": (short_ct_ms / 1000.0) if short_ct_ms else None,
    }


async def _swap_cold_start_sync(client: OKXRestClient, runtime: dict[str, Any]) -> None:
    if not INST_ID.upper().endswith("-SWAP"):
        return
    try:
        bal_raw = await asyncio.to_thread(client.balance, None)
        av = parse_balance_availability(bal_raw)
        runtime["usdt_avail_swap"] = av.get("USDT")
    except Exception as e:
        log.warning("[冷启动][swap] 余额解析失败: %s", e)
    try:
        pos_raw = await asyncio.to_thread(client.positions_swap, INST_ID)
        runtime["swap_position_summary"] = _summarize_swap_positions(pos_raw)
    except Exception as e:
        log.warning("[冷启动][swap] 永续持仓拉取失败: %s", e)
    npen = None
    try:
        pend_raw = await asyncio.to_thread(client.orders_pending, INST_ID)
        npen = len(pend_raw.get("data") or []) if isinstance(pend_raw, dict) else 0
        runtime["swap_pending_count"] = npen
        try:
            from quant.execution import corr_notional_cap_usdt, sync_exchange_corr_exposure

            if corr_notional_cap_usdt() is not None and isinstance(pend_raw, dict):
                ct0 = runtime.get("swap_ct_val")
                sv0 = float(ct0) if isinstance(ct0, (int, float)) else None
                sync_exchange_corr_exposure(
                    pend_raw,
                    inst_id=INST_ID,
                    risk=_risk_engine(swap_ct_val=sv0),
                    runtime=runtime,
                )
        except Exception as ex:
            log.debug("[冷启动][swap] corr 敞口同步跳过: %s", ex)
    except Exception as e:
        log.warning("[冷启动][swap] 永续挂单拉取失败: %s", e)
    summ = runtime.get("swap_position_summary")
    try:
        fee_raw = await asyncio.to_thread(client.trade_fee_swap, INST_ID)
        tr = _parse_taker_fee_rate_from_trade_fee_api(fee_raw)
        if tr is not None:
            runtime["swap_taker_fee_rate"] = tr
            log.info("[冷启动][swap] 账户 taker 费率=%s（用于 FeeGate）", tr)
    except Exception as e:
        log.debug("[冷启动][swap] trade-fee 拉取失败，FeeGate 用默认 taker: %s", e)
    log.warning(
        "[冷启动][swap] 交易所状态 | USDT_avail≈%s | positions=%s | pending_orders=%s",
        runtime.get("usdt_avail_swap"),
        summ,
        npen,
    )
    try:
        append_runtime_checkpoint(
            "swap_cold_start",
            {
                "inst_id": INST_ID,
                "usdt_avail_swap": runtime.get("usdt_avail_swap"),
                "swap_position_summary": summ,
                "swap_pending_count": npen,
            },
        )
    except Exception:
        pass


async def _lev5_selfcheck_loop(runtime: dict[str, Any]) -> None:
    while True:
        await asyncio.sleep(max(10.0, LEV5_SELFCHECK_LOG_SEC))
        micro_ts = runtime.get("micro_ts")
        age = time.time() - float(micro_ts) if isinstance(micro_ts, (int, float)) else None
        no_sig_since = runtime.get("no_signal_since_ts")
        if isinstance(no_sig_since, (int, float)):
            runtime["forced_relax_no_signal_sec"] = round(max(0.0, time.time() - float(no_sig_since)), 1)
        log.info(
            "[自检][lev5] runtime | funding=%s equity=%s premium=%s oi_delta=%s micro_age=%s "
            "adapt_z=%s adapt_edge_bps=%s edge_mul(z/imb/prem)=%s/%s/%s frozen=%s "
            "signals_1h=%s target_1h=%s long=%s/%s short=%s/%s perf=%s/%s vol_bias_scale=%s "
            "thr=%s raw_thr=%s hyst=%s hold=%s min_hold=%s n=%s/%s forced=%s no_sig_sec=%s",
            runtime.get("funding_rate"),
            runtime.get("equity_usdt"),
            runtime.get("premium_pct"),
            runtime.get("oi_delta_pct"),
            f"{age:.1f}s" if isinstance(age, (int, float)) else "n/a",
            runtime.get("adaptive_z_add"),
            runtime.get("adaptive_edge_bps_add"),
            runtime.get("edge_k_z_mul"),
            runtime.get("edge_k_imb_mul"),
            runtime.get("edge_k_prem_mul"),
            runtime.get("autotune_frozen"),
            runtime.get("signals_1h"),
            runtime.get("target_signals_1h"),
            runtime.get("signals_long_1h"),
            runtime.get("target_signals_long_1h"),
            runtime.get("signals_short_1h"),
            runtime.get("target_signals_short_1h"),
            runtime.get("edge_score_long"),
            runtime.get("edge_score_short"),
            runtime.get("directional_bias_vol_scale"),
            runtime.get("directional_perf_diff_threshold"),
            runtime.get("directional_perf_diff_threshold_raw"),
            runtime.get("directional_perf_bias_state"),
            runtime.get("directional_perf_state_hold_sec"),
            runtime.get("directional_perf_min_hold_sec"),
            runtime.get("edge_perf_samples_long"),
            runtime.get("edge_perf_samples_short"),
            runtime.get("forced_relax_level"),
            runtime.get("forced_relax_no_signal_sec"),
        )
        log.info(
            "[自检][lev5] account | usdt_avail=%s positions=%s pending=%s entry_sig_since_fill=%s "
            "ch_w_adj_long=%s ch_w_adj_short=%s",
            runtime.get("usdt_avail_swap"),
            runtime.get("swap_position_summary"),
            runtime.get("swap_pending_count"),
            runtime.get("entry_signals_after_last_fill"),
            runtime.get("ch_w_adj_long"),
            runtime.get("ch_w_adj_short"),
        )
        log.info(
            "[自检][lev5] exec_health | slip_adverse_ewm=%s fee/eq=%s fee_block_until=%s "
            "fee_size_mul=%s transient_fails=%s rest_fail_streak=%s",
            runtime.get("slip_adverse_ewm"),
            runtime.get("session_fee_to_equity_ratio"),
            runtime.get("fee_pressure_block_entries_until"),
            runtime.get("fee_pressure_size_mul"),
            runtime.get("transient_order_fail_count"),
            runtime.get("rest_fail_streak"),
        )
        try:
            append_runtime_checkpoint(
                "lev5_selfcheck",
                {
                    "inst_id": INST_ID,
                    "funding_rate": runtime.get("funding_rate"),
                    "equity_usdt": runtime.get("equity_usdt"),
                    "mtm_usdt": runtime.get("mtm_usdt"),
                    "premium_pct": runtime.get("premium_pct"),
                    "oi_delta_pct": runtime.get("oi_delta_pct"),
                    "book_imbalance": runtime.get("book_imbalance"),
                    "adaptive_z_add": runtime.get("adaptive_z_add"),
                    "adaptive_edge_bps_add": runtime.get("adaptive_edge_bps_add"),
                    "signals_1h": runtime.get("signals_1h"),
                    "target_signals_1h": runtime.get("target_signals_1h"),
                    "edge_score_long": runtime.get("edge_score_long"),
                    "edge_score_short": runtime.get("edge_score_short"),
                    "directional_perf_bias_state": runtime.get("directional_perf_bias_state"),
                    "directional_perf_diff_threshold": runtime.get("directional_perf_diff_threshold"),
                    "usdt_avail_swap": runtime.get("usdt_avail_swap"),
                    "swap_position_summary": runtime.get("swap_position_summary"),
                    "swap_pending_count": runtime.get("swap_pending_count"),
                    "entry_signals_after_last_fill": runtime.get("entry_signals_after_last_fill"),
                    "ch_w_adj_long": runtime.get("ch_w_adj_long"),
                    "ch_w_adj_short": runtime.get("ch_w_adj_short"),
                },
            )
        except Exception:
            pass
        eq = runtime.get("exec_quality")
        if isinstance(eq, dict):
            log.info(
                "[自检][lev5] exec_q | post_only=%.3f ioc=%.3f limit=%.3f",
                float(eq.get("post_only", 0.0)),
                float(eq.get("ioc", 0.0)),
                float(eq.get("limit", 0.0)),
            )


async def _lev5_autotune_loop(runtime: dict[str, Any]) -> None:
    while True:
        await asyncio.sleep(max(30.0, LEV5_TUNE_INTERVAL_SEC))
        try:
            eq_usdt = runtime.get("equity_usdt")
            mtm = runtime.get("mtm_usdt")
            q = runtime.get("exec_quality")
            ioc_q = float((q or {}).get("ioc", 0.55)) if isinstance(q, dict) else 0.55
            post_q = float((q or {}).get("post_only", 0.55)) if isinstance(q, dict) else 0.55
            z_add = float(runtime.get("adaptive_z_add") or 0.0)
            edge_add = float(runtime.get("adaptive_edge_bps_add") or 0.0)
            kz = float(runtime.get("edge_k_z_mul") or 1.0)
            kimb = float(runtime.get("edge_k_imb_mul") or 1.0)
            kprem = float(runtime.get("edge_k_prem_mul") or 1.0)
            rel_vol = runtime.get("rel_vol")
            premium = runtime.get("premium_pct")
            book_imb = runtime.get("book_imbalance")
            sig_1h = runtime.get("signals_1h")
            sig_long_1h = runtime.get("signals_long_1h")
            sig_short_1h = runtime.get("signals_short_1h")
            pnl_pct = None
            if isinstance(eq_usdt, (int, float)) and isinstance(mtm, (int, float)) and float(eq_usdt) > 0:
                pnl_pct = float(mtm) / float(eq_usdt)
            freeze = False
            if isinstance(rel_vol, (int, float)) and float(rel_vol) >= float(LEV5_TUNE_FREEZE_VOL):
                freeze = True
            if isinstance(premium, (int, float)) and abs(float(premium)) >= float(LEV5_TUNE_FREEZE_PREMIUM):
                freeze = True
            runtime["autotune_frozen"] = freeze
            now_ts = time.time()
            regime_now = str(runtime.get("regime") or "n/a")
            regime_prev = str(runtime.get("_regime_state_name") or "")
            if regime_now != regime_prev:
                runtime["_regime_state_name"] = regime_now
                runtime["_regime_state_since_ts"] = now_ts
            regime_since = runtime.get("_regime_state_since_ts")
            regime_dur_min = (
                max(0.0, (now_ts - float(regime_since)) / 60.0)
                if isinstance(regime_since, (int, float))
                else 0.0
            )
            runtime["regime_duration_min"] = round(regime_dur_min, 2)
            if regime_now == "ranging_hard":
                runtime["ranging_hard_duration_min"] = round(regime_dur_min, 2)
            else:
                runtime["ranging_hard_duration_min"] = 0.0
            no_sig_since = runtime.get("no_signal_since_ts")
            no_sig_sec = (
                max(0.0, time.time() - float(no_sig_since))
                if isinstance(no_sig_since, (int, float))
                else 0.0
            )
            runtime["forced_relax_no_signal_sec"] = round(no_sig_sec, 1)
            fr1 = max(12.0, float(LEV5_FORCED_RELAX_NO_SIGNAL_SEC_1))
            fr2 = max(fr1 + 28.0, float(LEV5_FORCED_RELAX_NO_SIGNAL_SEC_2))
            if regime_now == "ranging_hard":
                # 极度震荡时避免频繁放松门槛刷低质量尝试。
                fr1 = max(fr1, 300.0)
                fr2 = max(fr2, 600.0)
            forced_relax_level = 0
            if no_sig_sec >= fr2:
                forced_relax_level = 2
            elif no_sig_sec >= fr1:
                forced_relax_level = 1
            runtime["forced_relax_level"] = forced_relax_level
            if not freeze:
                step_scale = max(0.2, min(2.0, float(LEV5_TUNE_MAX_STEP_SCALE)))
                z_step = float(LEV5_TUNE_Z_STEP) * step_scale
                e_step = float(LEV5_TUNE_EDGE_STEP_BPS) * step_scale
                kz_step = 0.02 * step_scale
                k_step = 0.015 * step_scale
                if isinstance(pnl_pct, float) and (pnl_pct < -0.01 or min(ioc_q, post_q) < 0.38):
                    z_add += z_step
                    edge_add += e_step
                    kz += kz_step
                    kimb += k_step
                    kprem += k_step
                elif isinstance(pnl_pct, float) and pnl_pct > 0.01 and min(ioc_q, post_q) > 0.58:
                    z_add -= z_step
                    edge_add -= e_step
                    kz -= kz_step
                    kimb -= k_step
                    kprem -= k_step
                target_sig = float(LEV5_TARGET_SIGNALS_PER_HOUR)
                if (
                    LEV5_DYNAMIC_ACTIVITY_TARGET
                    and isinstance(rel_vol, (int, float))
                    and float(LEV5_ACTIVITY_VOL_REF) > 1e-9
                ):
                    scale = float(rel_vol) / float(LEV5_ACTIVITY_VOL_REF)
                    target_sig = target_sig * max(0.6, min(2.0, scale))
                target_sig = max(
                    float(LEV5_ACTIVITY_MIN_SIGNALS_PER_HOUR),
                    min(float(LEV5_ACTIVITY_MAX_SIGNALS_PER_HOUR), target_sig),
                )
                if regime_now == "ranging_hard":
                    target_sig = 1.0
                runtime["target_signals_1h"] = target_sig
                bias_long = 0.5
                perf_long = runtime.get("edge_score_long")
                perf_short = runtime.get("edge_score_short")
                if not isinstance(perf_long, (int, float)):
                    perf_long = runtime.get("edge_perf_long")
                if not isinstance(perf_short, (int, float)):
                    perf_short = runtime.get("edge_perf_short")
                perf_diff = 0.0
                vol_bias_scale = 1.0
                if isinstance(premium, (int, float)) and isinstance(book_imb, (int, float)):
                    if float(premium) > 0 and float(book_imb) > 0:
                        bias_long = 0.62
                    elif float(premium) < 0 and float(book_imb) < 0:
                        bias_long = 0.38
                if (
                    LEV5_DIRECTIONAL_PERF_BIAS_ENABLE
                    and isinstance(perf_long, (int, float))
                    and isinstance(perf_short, (int, float))
                ):
                    perf_diff = float(perf_long) - float(perf_short)
                    if (
                        LEV5_DIRECTIONAL_BIAS_VOL_GATE_ENABLE
                        and isinstance(rel_vol, (int, float))
                        and float(LEV5_DIRECTIONAL_BIAS_VOL_HIGH) > float(LEV5_DIRECTIONAL_BIAS_VOL_LOW)
                    ):
                        v = float(rel_vol)
                        vlo = float(LEV5_DIRECTIONAL_BIAS_VOL_LOW)
                        vhi = float(LEV5_DIRECTIONAL_BIAS_VOL_HIGH)
                        frac = (v - vlo) / (vhi - vlo)
                        frac = max(0.0, min(1.0, frac))
                        vol_bias_scale = float(LEV5_DIRECTIONAL_BIAS_MIN_SCALE) + frac * (
                            float(LEV5_DIRECTIONAL_BIAS_MAX_SCALE) - float(LEV5_DIRECTIONAL_BIAS_MIN_SCALE)
                        )
                    runtime["directional_bias_vol_scale"] = vol_bias_scale
                    bias_long += max(-0.12, min(0.12, (perf_diff / 4.0) * vol_bias_scale))
                bias_long = max(0.25, min(0.75, bias_long))
                runtime["edge_perf_diff"] = perf_diff
                target_long = max(1.0, target_sig * bias_long)
                target_short = max(1.0, target_sig - target_long)
                runtime["target_signals_long_1h"] = target_long
                runtime["target_signals_short_1h"] = target_short
                if LEV5_AGGRESSIVE_MODE and isinstance(sig_1h, (int, float)) and float(sig_1h) < target_sig:
                    z_add -= float(LEV5_LOW_ACTIVITY_RELAX_Z)
                    edge_add -= float(LEV5_LOW_ACTIVITY_RELAX_EDGE_BPS)
                z_add_long = float(runtime.get("adaptive_z_add_long") or 0.0)
                z_add_short = float(runtime.get("adaptive_z_add_short") or 0.0)
                edge_add_long = float(runtime.get("adaptive_edge_bps_add_long") or 0.0)
                edge_add_short = float(runtime.get("adaptive_edge_bps_add_short") or 0.0)
                if LEV5_AGGRESSIVE_MODE and isinstance(sig_long_1h, (int, float)) and float(sig_long_1h) < target_long:
                    z_add_long -= float(LEV5_LOW_ACTIVITY_RELAX_Z) * 0.8
                    edge_add_long -= float(LEV5_LOW_ACTIVITY_RELAX_EDGE_BPS) * 0.8
                if LEV5_AGGRESSIVE_MODE and isinstance(sig_short_1h, (int, float)) and float(sig_short_1h) < target_short:
                    z_add_short -= float(LEV5_LOW_ACTIVITY_RELAX_Z) * 0.8
                    edge_add_short -= float(LEV5_LOW_ACTIVITY_RELAX_EDGE_BPS) * 0.8
                if LEV5_DIRECTIONAL_PERF_BIAS_ENABLE and isinstance(perf_long, (int, float)) and isinstance(perf_short, (int, float)):
                    long_n = int(runtime.get("edge_perf_samples_long", 0))
                    short_n = int(runtime.get("edge_perf_samples_short", 0))
                    n_eff = max(1, min(long_n, short_n))
                    sample_ref = max(1.0, float(LEV5_DIRECTIONAL_PERF_DIFF_SAMPLE_REF))
                    sample_scale = (sample_ref / float(n_eff)) ** 0.5
                    sample_scale = max(
                        float(LEV5_DIRECTIONAL_PERF_DIFF_MIN_SCALE),
                        min(float(LEV5_DIRECTIONAL_PERF_DIFF_MAX_SCALE), sample_scale),
                    )
                    vol_thr_scale = max(0.65, min(1.45, 1.0 / max(0.2, vol_bias_scale)))
                    perf_thr_raw = float(LEV5_DIRECTIONAL_PERF_DIFF_BASE) * sample_scale * vol_thr_scale
                    prev_thr = runtime.get("directional_perf_diff_threshold")
                    prev_thr_v = float(prev_thr) if isinstance(prev_thr, (int, float)) else perf_thr_raw
                    thr_alpha = max(0.05, min(0.7, float(LEV5_DIRECTIONAL_PERF_DIFF_EMA_ALPHA)))
                    perf_thr = (1.0 - thr_alpha) * prev_thr_v + thr_alpha * perf_thr_raw
                    runtime["directional_perf_diff_threshold"] = perf_thr
                    runtime["directional_perf_diff_threshold_raw"] = perf_thr_raw
                    enter_mul = max(0.6, min(1.6, float(LEV5_DIRECTIONAL_PERF_HYST_ENTER_MUL)))
                    exit_mul = max(0.2, min(1.2, float(LEV5_DIRECTIONAL_PERF_HYST_EXIT_MUL)))
                    enter_thr = perf_thr * enter_mul
                    exit_thr = perf_thr * min(enter_mul, exit_mul)
                    now_ts = time.time()
                    min_hold = max(0.0, float(LEV5_DIRECTIONAL_PERF_STATE_MIN_HOLD_SEC))
                    if (
                        LEV5_DIRECTIONAL_PERF_HOLD_DYNAMIC_ENABLE
                        and isinstance(rel_vol, (int, float))
                        and float(LEV5_DIRECTIONAL_PERF_HOLD_VOL_HIGH) > float(LEV5_DIRECTIONAL_PERF_HOLD_VOL_LOW)
                    ):
                        v = float(rel_vol)
                        vlo = float(LEV5_DIRECTIONAL_PERF_HOLD_VOL_LOW)
                        vhi = float(LEV5_DIRECTIONAL_PERF_HOLD_VOL_HIGH)
                        frac = (v - vlo) / (vhi - vlo)
                        frac = max(0.0, min(1.0, frac))
                        hold_max = max(10.0, float(LEV5_DIRECTIONAL_PERF_HOLD_MAX_SEC))
                        hold_min = max(5.0, min(hold_max, float(LEV5_DIRECTIONAL_PERF_HOLD_MIN_SEC)))
                        # 高波动缩短持有（更激进），低波动延长持有（防抖）
                        min_hold = hold_max - frac * (hold_max - hold_min)
                    runtime["directional_perf_min_hold_sec"] = min_hold
                    state = str(runtime.get("directional_perf_bias_state") or "none")
                    state_since = runtime.get("directional_perf_bias_state_since")
                    since_ts = float(state_since) if isinstance(state_since, (int, float)) else now_ts
                    hold_sec = max(0.0, now_ts - since_ts) if state != "none" else 0.0
                    runtime["directional_perf_state_hold_sec"] = hold_sec
                    active = "none"
                    if LEV5_DIRECTIONAL_PERF_HYST_ENABLE:
                        if state == "long":
                            if perf_diff >= exit_thr or hold_sec < min_hold:
                                active = "long"
                            else:
                                active = "none"
                        elif state == "short":
                            if perf_diff <= -exit_thr or hold_sec < min_hold:
                                active = "short"
                            else:
                                active = "none"
                        else:
                            if perf_diff >= enter_thr:
                                active = "long"
                            elif perf_diff <= -enter_thr:
                                active = "short"
                            else:
                                active = "none"
                    else:
                        if perf_diff >= perf_thr:
                            active = "long"
                        elif perf_diff <= -perf_thr:
                            active = "short"
                    if active != state:
                        runtime["directional_perf_bias_state_since"] = now_ts
                    runtime["directional_perf_bias_state"] = active
                    relax_z = float(LEV5_DIRECTIONAL_PERF_RELAX_Z) * vol_bias_scale
                    relax_edge = float(LEV5_DIRECTIONAL_PERF_RELAX_EDGE_BPS) * vol_bias_scale
                    push_z = float(LEV5_DIRECTIONAL_PERF_PUSH_Z) * max(0.5, vol_bias_scale * 0.9)
                    push_edge = float(LEV5_DIRECTIONAL_PERF_PUSH_EDGE_BPS) * max(0.5, vol_bias_scale * 0.9)
                    if active == "long":
                        z_add_long -= relax_z
                        edge_add_long -= relax_edge
                        z_add_short += push_z
                        edge_add_short += push_edge
                    elif active == "short":
                        z_add_short -= relax_z
                        edge_add_short -= relax_edge
                        z_add_long += push_z
                        edge_add_long += push_edge
                if (not STRAT_LIVE) and forced_relax_level > 0:
                    # Low-activity emergency channel: temporary extra relax until signals recover.
                    z_add -= 0.04 * float(forced_relax_level)
                    edge_add -= 0.35 * float(forced_relax_level)
                    z_add_long -= 0.02 * float(forced_relax_level)
                    z_add_short -= 0.02 * float(forced_relax_level)
                    edge_add_long -= 0.15 * float(forced_relax_level)
                    edge_add_short -= 0.15 * float(forced_relax_level)

                # 自学习：动态调节 fallback 的最低 net_edge 门槛。
                # 亏损/执行质量差时收紧（减少噪声与手续费蚕食），
                # 信号不足但执行质量好时微放松（维持节奏）。
                fb_floor = float(
                    runtime.get("fallback_min_net_edge_bps")
                    or LEV5_FALLBACK_MIN_NET_EDGE_BPS_BASE
                )
                fb_step = 0.08
                if (
                    isinstance(pnl_pct, float) and pnl_pct < -0.004
                ) or min(ioc_q, post_q) < 0.46:
                    fb_floor += fb_step
                elif (
                    isinstance(sig_1h, (int, float))
                    and float(sig_1h)
                    < float(
                        runtime.get("target_signals_1h")
                        or LEV5_TARGET_SIGNALS_PER_HOUR
                    )
                    * 0.75
                    and min(ioc_q, post_q) > 0.58
                ):
                    fb_floor -= fb_step * 0.5
                fb_floor = max(
                    float(LEV5_FALLBACK_MIN_NET_EDGE_BPS_BASE),
                    min(float(LEV5_FALLBACK_MIN_NET_EDGE_BPS_MAX), fb_floor),
                )
                runtime["fallback_min_net_edge_bps"] = fb_floor
                if LEV5_CH_AUTOTUNE_ENABLE:
                    valid_ch = (
                        "pullback",
                        "continuation",
                        "micro",
                        "vol_break",
                        "funding",
                        "fallback",
                    )
                    step = float(LEV5_CH_AUTOTUNE_STEP)
                    lo = float(LEV5_CH_W_ADJ_MIN)
                    hi = float(LEV5_CH_W_ADJ_MAX)
                    adj_l = runtime.setdefault("ch_w_adj_long", {})
                    adj_s = runtime.setdefault("ch_w_adj_short", {})
                    if not isinstance(adj_l, dict):
                        adj_l = {}
                        runtime["ch_w_adj_long"] = adj_l
                    if not isinstance(adj_s, dict):
                        adj_s = {}
                        runtime["ch_w_adj_short"] = adj_s
                    for ch in valid_ch:
                        for _side, adjd in (("long", adj_l), ("short", adj_s)):
                            ck = f"ch_perf_{ch}_{_side}"
                            perf = runtime.get(ck)
                            if not isinstance(perf, (int, float)):
                                continue
                            cur = float(adjd.get(ch, 1.0))
                            pv = float(perf)
                            if pv < -0.012:
                                cur *= 1.0 - step
                            elif pv > 0.018:
                                cur *= 1.0 + step * 0.65
                            adjd[ch] = max(lo, min(hi, cur))
                runtime["adaptive_z_add_long"] = max(-0.18, min(0.30, z_add_long))
                runtime["adaptive_z_add_short"] = max(-0.18, min(0.30, z_add_short))
                runtime["adaptive_edge_bps_add_long"] = max(-1.4, min(3.0, edge_add_long))
                runtime["adaptive_edge_bps_add_short"] = max(-1.4, min(3.0, edge_add_short))

            z_floor = (
                float(LEV5_AUTOTUNE_Z_ADD_FLOOR_FORCED)
                if (not STRAT_LIVE) and forced_relax_level > 0
                else float(LEV5_AUTOTUNE_Z_ADD_FLOOR)
            )
            edge_floor = (
                float(LEV5_AUTOTUNE_EDGE_ADD_HARD_FLOOR_FORCED)
                if (not STRAT_LIVE) and forced_relax_level > 0
                else float(LEV5_AUTOTUNE_EDGE_ADD_HARD_FLOOR)
            )
            runtime["adaptive_z_add"] = max(z_floor, min(0.30, z_add))
            runtime["adaptive_edge_bps_add"] = max(edge_floor, min(3.0, edge_add))
            runtime["edge_k_z_mul"] = max(0.7, min(1.6, kz))
            runtime["edge_k_imb_mul"] = max(0.7, min(1.6, kimb))
            runtime["edge_k_prem_mul"] = max(0.7, min(1.6, kprem))
        except Exception as e:
            log.debug("[自检][lev5] autotune 失败: %s", e)


def _fmt_report_num(x: float | int | None) -> str:
    if x is None:
        return "null"
    if isinstance(x, float):
        return f"{x:.8f}".rstrip("0").rstrip(".")
    return str(x)


async def _lev5_hourly_report_loop(
    runtime: dict[str, Any],
    metrics: Metrics,
    *,
    account_client: OKXRestClient | None,
    session_trade: SessionTradeStats | None,
    audit: AuditStore | NullAuditStore,
    run_id: str,
    inst_id: str,
) -> None:
    prev_signals = 0
    prev_orders = 0
    prev_long = 0
    prev_short = 0
    prev_fee_gate_rejected = 0
    while True:
        await asyncio.sleep(3600.0)
        snap = metrics.snapshot()
        eq = runtime.get("equity_usdt")
        mtm = runtime.get("mtm_usdt")
        pnl_pct = None
        if isinstance(eq, (int, float)) and isinstance(mtm, (int, float)) and float(eq) > 0:
            pnl_pct = float(mtm) / float(eq)
        sig_h = int(snap.get("signals", 0)) - int(prev_signals)
        ord_h = int(snap.get("orders_ok", 0)) - int(prev_orders)
        long_total = int(runtime.get("signals_long_total", 0))
        short_total = int(runtime.get("signals_short_total", 0))
        sig_long_h = long_total - prev_long
        sig_short_h = short_total - prev_short
        fg_tot = int(snap.get("fee_gate_rejected_count", 0))
        fee_gate_h = fg_tot - int(prev_fee_gate_rejected)
        runtime["signals_1h"] = sig_h
        runtime["orders_ok_1h"] = ord_h
        runtime["signals_long_1h"] = sig_long_h
        runtime["signals_short_1h"] = sig_short_h
        runtime["fee_gate_rejected_count_1h"] = fee_gate_h
        prev_signals = int(snap.get("signals", 0))
        prev_orders = int(snap.get("orders_ok", 0))
        prev_long = long_total
        prev_short = short_total
        prev_fee_gate_rejected = fg_tot

        signals_total, orders_rest_ok = audit.session_flow_counts(run_id)
        fee_gate_rejected_db = audit.count_fee_gate_rejected_signals(run_id)
        gross_pnl: float | None = None
        total_fee: float | None = None
        funding_collected: float | None = None
        net_pnl: float | None = None
        orders_filled: int | None = None
        avg_slip: float | None = None

        now_ms = int(time.time() * 1000)
        if account_client is not None and session_trade is not None:
            try:
                fill_rows = await asyncio.to_thread(
                    fetch_fills_window,
                    account_client,
                    inst_id,
                    session_trade.session_start_ms,
                    now_ms,
                    inst_type="SWAP",
                )
                g, fee, n_fill = aggregate_session_fill_pnl_fee_usdt(fill_rows)
                gross_pnl = g
                total_fee = fee
                orders_filled = n_fill
                refs = audit.load_order_signal_refs(run_id)
                avg_slip = avg_slippage_bps_fill_minus_signal_mid(fill_rows, refs)
            except Exception as e:
                log.warning("[报告][lev5][1h] 成交/手续费聚合失败（fills API）: %s", e)

            try:
                fb_rows = await asyncio.to_thread(
                    fetch_funding_fee_bills_window,
                    account_client,
                    inst_id,
                    session_trade.session_start_ms,
                    now_ms,
                )
                funding_collected = sum_funding_fee_bills_session_usdt(
                    fb_rows, inst_id=inst_id
                )
            except Exception as e:
                funding_collected = None
                log.warning("[报告][lev5][1h] 资金费账单拉取失败（/account/bills type=8）: %s", e)
        else:
            funding_collected = None

        if gross_pnl is not None and total_fee is not None and funding_collected is not None:
            net_pnl = gross_pnl + total_fee + funding_collected
        else:
            net_pnl = None

        k_part = str(orders_filled) if orders_filled is not None else "null"
        trade_stats = (
            f"信号{signals_total}笔 / REST接受{orders_rest_ok}笔 / "
            f"实际成交{k_part}笔 / 手续费门控拒绝{fee_gate_rejected_db}笔"
        )

        log.warning(
            "[报告][lev5][1h] gross_pnl=%s total_fee=%s funding_collected=%s net_pnl=%s "
            "trade_stats=%s avg_slippage_bps=%s",
            _fmt_report_num(gross_pnl),
            _fmt_report_num(total_fee),
            _fmt_report_num(funding_collected),
            _fmt_report_num(net_pnl),
            trade_stats,
            _fmt_report_num(avg_slip),
        )
        regime_now = str(runtime.get("regime") or "n/a")
        if regime_now == "ranging_hard":
            dmin = runtime.get("ranging_hard_duration_min")
            try:
                dmin_s = f"{float(dmin):.1f}"
            except (TypeError, ValueError):
                dmin_s = "n/a"
            log.warning(
                "[市场状态] regime=ranging_hard 持续时间=%s分钟，建议等待趋势恢复（ADX>20）后再评估信号质量。",
                dmin_s,
            )

        if net_pnl is not None:
            hist = runtime.setdefault("_lev5_hourly_net_pnl_hist", [])
            hist.append(float(net_pnl))
            if len(hist) > 3:
                hist[:] = hist[-3:]
            if len(hist) == 3 and all(float(x) < 0.0 for x in hist):
                log.warning(
                    "[警告] 净亏损连续3小时，建议检查策略参数或暂停实盘。"
                )
        else:
            runtime["_lev5_hourly_net_pnl_hist"] = []
        log.warning(
            "[报告][lev5][1h] mtm=%s pnl_pct=%s orders_ok=%s fail=%s signals=%s "
            "fee_gate_rejected=%s long=%s short=%s target=%s long_t=%s short_t=%s adapt_z=%s adapt_edge=%s "
            "edge_mul=%s/%s/%s diag_edge=%s diag_slip=%s",
            mtm,
            f"{pnl_pct:.4%}" if isinstance(pnl_pct, (int, float)) else "n/a",
            ord_h,
            snap.get("orders_fail"),
            sig_h,
            fee_gate_h,
            sig_long_h,
            sig_short_h,
            runtime.get("target_signals_1h"),
            runtime.get("target_signals_long_1h"),
            runtime.get("target_signals_short_1h"),
            runtime.get("adaptive_z_add"),
            runtime.get("adaptive_edge_bps_add"),
            runtime.get("edge_k_z_mul"),
            runtime.get("edge_k_imb_mul"),
            runtime.get("edge_k_prem_mul"),
            runtime.get("diag_expected_edge_bps"),
            runtime.get("diag_adverse_slip_bps"),
        )
        orders_attempted = int(snap.get("orders_ok", 0)) + int(snap.get("orders_fail", 0) or 0)
        try:
            append_runtime_checkpoint(
                "lev5_hourly_report",
                {
                    "inst_id": INST_ID,
                    "gross_pnl": gross_pnl,
                    "total_fee": total_fee,
                    "funding_collected": funding_collected,
                    "net_pnl": net_pnl,
                    "trade_stats": trade_stats,
                    "orders_attempted": orders_attempted,
                    "orders_ok": orders_rest_ok,
                    "orders_filled": orders_filled,
                    "fee_gate_rejected_signals": fee_gate_rejected_db,
                    "avg_slippage_bps": avg_slip,
                    "mtm_usdt": mtm,
                    "pnl_pct": pnl_pct,
                    "orders_ok_1h": ord_h,
                    "signals_1h": sig_h,
                    "signals_long_1h": sig_long_h,
                    "signals_short_1h": sig_short_h,
                    "fee_gate_rejected_count_1h": fee_gate_h,
                    "target_signals_1h": runtime.get("target_signals_1h"),
                    "target_signals_long_1h": runtime.get("target_signals_long_1h"),
                    "target_signals_short_1h": runtime.get("target_signals_short_1h"),
                    "edge_score_long": runtime.get("edge_score_long"),
                    "edge_score_short": runtime.get("edge_score_short"),
                    "directional_perf_bias_state": runtime.get("directional_perf_bias_state"),
                },
            )
        except Exception:
            pass


async def _lev5_model_diag_loop(runtime: dict[str, Any]) -> None:
    while True:
        await asyncio.sleep(max(60.0, LEV5_MODEL_DIAG_INTERVAL_SEC))
        try:
            rows = runtime.get("edge_diag_samples")
            if not isinstance(rows, list):
                continue
            xs = [x for x in rows if isinstance(x, dict)]
            if len(xs) < int(LEV5_MODEL_DIAG_MIN_SAMPLES):
                continue
            exp_vals = []
            adv_vals = []
            for r in xs:
                e = r.get("expected_edge_bps")
                a = r.get("adverse_slip_bps")
                if isinstance(e, (int, float)) and isinstance(a, (int, float)):
                    exp_vals.append(float(e))
                    adv_vals.append(float(a))
            if len(exp_vals) < int(LEV5_MODEL_DIAG_MIN_SAMPLES):
                continue
            exp_avg = sum(exp_vals) / len(exp_vals)
            adv_avg = sum(adv_vals) / len(adv_vals)
            edge_add = float(runtime.get("adaptive_edge_bps_add") or 0.0)
            if exp_avg < adv_avg + 0.8:
                edge_add += float(LEV5_MODEL_DIAG_SAFETY_STEP_BPS)
            elif exp_avg > adv_avg + 2.0:
                edge_add -= float(LEV5_MODEL_DIAG_SAFETY_STEP_BPS) * 0.5
            runtime["adaptive_edge_bps_add"] = max(
                float(LEV5_AUTOTUNE_EDGE_ADD_HARD_FLOOR), min(3.0, edge_add)
            )
            runtime["diag_expected_edge_bps"] = round(exp_avg, 4)
            runtime["diag_adverse_slip_bps"] = round(adv_avg, 4)
        except Exception as e:
            log.debug("[自检][lev5] model_diag 失败: %s", e)


def _update_exec_quality_from_fill(
    runtime: dict[str, Any],
    *,
    ord_type: str,
    adverse_slip_bps: float,
) -> None:
    eq = runtime.setdefault(
        "exec_quality",
        {
            "post_only": 0.55,
            "ioc": 0.55,
            "limit": 0.55,
            "updated_ts": time.time(),
        },
    )
    key = ord_type if ord_type in ("post_only", "ioc", "limit") else "limit"
    prev = float(eq.get(key, 0.55))
    sample = max(0.1, min(0.95, 1.0 - max(0.0, adverse_slip_bps) / 12.0))
    eq[key] = max(0.0, min(1.0, 0.80 * prev + 0.20 * sample))
    eq["updated_ts"] = time.time()


async def _lev5_fills_quality_loop(client: OKXRestClient, runtime: dict[str, Any]) -> None:
    after: str | None = None
    begin_ms = int(time.time() * 1000) - 10 * 60 * 1000
    while True:
        await asyncio.sleep(5.0)
        try:
            raw = await asyncio.to_thread(
                client.fills_swap,
                INST_ID,
                begin_ms=begin_ms,
                limit="100",
                after=after,
            )
            rows = raw.get("data") or []
            if not rows:
                continue
            pending = runtime.get("pending_orders")
            if not isinstance(pending, dict):
                continue
            fill_matched_any = False
            for r in rows:
                if not isinstance(r, dict):
                    continue
                cid = str(r.get("clOrdId") or "").strip()
                if not cid or cid not in pending:
                    continue
                meta = pending.pop(cid, None)
                if not isinstance(meta, dict):
                    continue
                fill_matched_any = True
                if bool(meta.get("reduce_only")):
                    continue
                try:
                    fp = float(r.get("fillPx"))
                    ep = float(meta.get("px"))
                except (TypeError, ValueError):
                    continue
                if ep <= 0 or fp <= 0:
                    continue
                side = str(meta.get("side") or "").lower()
                if side == "buy":
                    slip_bps = (ep - fp) / ep * 10000.0
                else:
                    slip_bps = (fp - ep) / ep * 10000.0
                adverse = max(0.0, -slip_bps)
                slip_ring = runtime.setdefault("slip_recent_bps", [])
                if isinstance(slip_ring, list):
                    slip_ring.append(round(adverse, 3))
                    if len(slip_ring) > 120:
                        del slip_ring[:-120]
                slip_a = 0.22
                prev_s = float(runtime.get("slip_adverse_ewm") or 0.0)
                runtime["slip_adverse_ewm"] = (1.0 - slip_a) * prev_s + slip_a * adverse
                expected = meta.get("expected_edge_bps")
                expected_edge = float(expected) if isinstance(expected, (int, float)) else 0.8
                _update_exec_quality_from_fill(
                    runtime,
                    ord_type=str(meta.get("ord_type") or "limit"),
                    adverse_slip_bps=adverse,
                )
                pos_side = str(meta.get("pos_side") or "").lower()
                if pos_side not in ("long", "short"):
                    pos_side = "long" if side == "buy" else "short"
                perf_key = "edge_perf_long" if pos_side == "long" else "edge_perf_short"
                prev_perf = runtime.get(perf_key)
                prev_perf_v = float(prev_perf) if isinstance(prev_perf, (int, float)) else 0.0
                alpha = max(0.05, min(0.5, float(LEV5_DIRECTIONAL_PERF_EMA_ALPHA)))
                sample_perf = expected_edge - adverse
                runtime[perf_key] = (1.0 - alpha) * prev_perf_v + alpha * sample_perf
                runtime["edge_perf_updated_ts"] = time.time()
                n_key = "edge_perf_samples_long" if pos_side == "long" else "edge_perf_samples_short"
                runtime[n_key] = min(2000, int(runtime.get(n_key, 0)) + 1)
                wr_key = "edge_winrate_long" if pos_side == "long" else "edge_winrate_short"
                wg_key = "edge_wingain_long" if pos_side == "long" else "edge_wingain_short"
                lm_key = "edge_lossmag_long" if pos_side == "long" else "edge_lossmag_short"
                prev_wr = runtime.get(wr_key)
                prev_wg = runtime.get(wg_key)
                prev_lm = runtime.get(lm_key)
                wr_v = float(prev_wr) if isinstance(prev_wr, (int, float)) else 0.5
                wg_v = float(prev_wg) if isinstance(prev_wg, (int, float)) else 0.6
                lm_v = float(prev_lm) if isinstance(prev_lm, (int, float)) else 0.6
                win = 1.0 if sample_perf > 0 else 0.0
                wr_v = (1.0 - alpha) * wr_v + alpha * win
                if sample_perf > 0:
                    wg_v = (1.0 - alpha) * wg_v + alpha * min(6.0, sample_perf)
                elif sample_perf < 0:
                    lm_v = (1.0 - alpha) * lm_v + alpha * min(6.0, -sample_perf)
                runtime[wr_key] = wr_v
                runtime[wg_key] = wg_v
                runtime[lm_key] = lm_v
                payoff = wg_v / max(0.05, lm_v)
                payoff_norm = (payoff - 1.0) / (payoff + 1.0)
                win_norm = (wr_v - 0.5) * 2.0
                score = runtime[perf_key]
                if int(runtime.get(n_key, 0)) >= int(LEV5_DIRECTIONAL_SCORE_MIN_SAMPLES):
                    score = (
                        float(runtime[perf_key])
                        + float(LEV5_DIRECTIONAL_WINRATE_WEIGHT) * win_norm
                        + float(LEV5_DIRECTIONAL_PAYOFF_WEIGHT) * payoff_norm
                    )
                score_key = "edge_score_long" if pos_side == "long" else "edge_score_short"
                runtime[score_key] = score
                ek = str(meta.get("entry_kind") or "").strip().lower()
                valid_ch = {
                    "pullback",
                    "continuation",
                    "micro",
                    "vol_break",
                    "funding",
                    "fallback",
                }
                if ek in valid_ch:
                    ck = f"ch_perf_{ek}_{pos_side}"
                    prev_c = runtime.get(ck)
                    prev_cv = float(prev_c) if isinstance(prev_c, (int, float)) else 0.0
                    alpha_c = max(0.05, min(0.5, float(LEV5_CH_PERF_EMA_ALPHA)))
                    runtime[ck] = (1.0 - alpha_c) * prev_cv + alpha_c * sample_perf
                    runtime["ch_perf_updated_ts"] = time.time()
                runtime["entry_signals_after_last_fill"] = 0
                try:
                    append_runtime_checkpoint(
                        "fill_quality_update",
                        {
                            "inst_id": INST_ID,
                            "cl_ord_id": cid,
                            "pos_side": pos_side,
                            "ord_type": meta.get("ord_type"),
                            "entry_kind": meta.get("entry_kind"),
                            "expected_edge_bps": expected_edge,
                            "adverse_slip_bps": adverse,
                            "sample_perf": sample_perf,
                            "edge_score_long": runtime.get("edge_score_long"),
                            "edge_score_short": runtime.get("edge_score_short"),
                            "directional_perf_bias_state": runtime.get("directional_perf_bias_state"),
                        },
                    )
                except Exception:
                    pass
                samples = runtime.setdefault("edge_diag_samples", [])
                if isinstance(samples, list):
                    samples.append(
                        {
                            "ts": time.time(),
                            "expected_edge_bps": meta.get("expected_edge_bps"),
                            "adverse_slip_bps": adverse,
                            "pos_side": pos_side,
                        }
                    )
                    if len(samples) > 300:
                        del samples[:-300]
            if fill_matched_any:
                br_dd = runtime.get("daily_drawdown_breaker")
                if isinstance(br_dd, DailyDrawdownBreaker) and br_dd.enabled():
                    try:
                        bal = await asyncio.to_thread(client.balance, None)
                        d = bal.get("data") or []
                        eq_dd = None
                        if d and isinstance(d[0], dict):
                            try:
                                eq_dd = float(d[0].get("totalEq"))
                            except (TypeError, ValueError):
                                eq_dd = None
                        br_dd.update_from_equity(eq_dd, runtime)
                    except Exception:
                        pass
            last = rows[-1]
            if isinstance(last, dict) and last.get("fillId"):
                after = str(last.get("fillId"))
        except Exception as e:
            log.debug("[杠杆] fills 质量更新失败: %s", e)


async def _corr_exposure_resync_loop(
    client: OKXRestClient,
    inst_id: str,
    runtime: dict[str, Any] | None,
) -> None:
    """
    CorrelationGuard 启用时：高频用 orders-pending 覆盖交易所侧挂单名义，
    缩短「REST 受理后乐观并入」与真实挂单快照之间的漂移窗口。
    """
    from quant.execution import corr_notional_cap_usdt, sync_exchange_corr_exposure

    while True:
        iv = float(RISK_CORR_RESYNC_INTERVAL_SEC)
        if iv <= 0:
            await asyncio.sleep(600.0)
            continue
        await asyncio.sleep(iv)
        if corr_notional_cap_usdt() is None or not isinstance(runtime, dict):
            continue
        try:
            raw = await asyncio.to_thread(client.orders_pending, inst_id)
            ct = runtime.get("swap_ct_val")
            sv = float(ct) if isinstance(ct, (int, float)) else None
            sync_exchange_corr_exposure(
                raw,
                inst_id=inst_id,
                risk=_risk_engine(swap_ct_val=sv),
                runtime=runtime,
            )
        except Exception as e:
            log.debug("[corr] 敞口 resync 失败: %s", e)


async def _reconcile_loop(
    client: OKXRestClient,
    inst_id: str,
    runtime: dict[str, Any] | None = None,
) -> None:
    from quant.execution import sync_exchange_corr_exposure

    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_SEC)
        try:
            raw = await asyncio.to_thread(log_pending_orders, client, inst_id)
            if isinstance(runtime, dict):
                ct = runtime.get("swap_ct_val")
                sv = float(ct) if isinstance(ct, (int, float)) else None
                risk = _risk_engine(swap_ct_val=sv)
                sync_exchange_corr_exposure(raw, inst_id=inst_id, risk=risk, runtime=runtime)
        except Exception as e:
            log.warning("reconcile failed: %s", e)


async def _stale_order_loop(client: OKXRestClient, inst_id: str) -> None:
    from quant.reconcile import cancel_stale_pending_orders

    while True:
        await asyncio.sleep(STRAT_STALE_ORDER_POLL_SEC)
        if STRAT_STALE_ORDER_CANCEL_SEC <= 0:
            continue
        try:
            await asyncio.to_thread(
                cancel_stale_pending_orders,
                client,
                inst_id,
                max_age_sec=STRAT_STALE_ORDER_CANCEL_SEC,
            )
        except Exception as e:
            log.warning("[挂单] 超时扫描失败: %s", e)


async def _account_snapshot_loop(
    client: OKXRestClient,
    inst_id: str,
    interval_sec: float,
) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        try:
            await asyncio.to_thread(log_account_snapshot, client, inst_id)
        except Exception as e:
            log.warning("[账户] 周期快照失败: %s", e)


def _config_fingerprint() -> str:
    """I：运行配置指纹，写入 runtime_checkpoints 便于归因。"""
    lev = float(GRID_LEVERAGE)
    raw = (
        f"{INST_ID}|{STRAT_MODE}|0|{int(STRAT_LIVE)}|"
        f"{lev}|{REST_POLL_INTERVAL_SEC}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def _startup_exchange_selfcheck(client: OKXRestClient) -> None:
    """J：启动时验证私有 REST 可读（权限 / 网络 / 模拟盘头）。"""

    def _probe() -> None:
        client.balance()
        if INST_ID.upper().endswith("-SWAP"):
            client.positions_swap(INST_ID)

    try:
        await asyncio.to_thread(_probe)
        log.info("[自检][API] 私有 REST：balance / positions 探测成功")
    except Exception as e:
        log.warning("[自检][API] 私有 REST 探测失败（权限/代理/网络）: %s", e)


async def _lev5_position_reconcile_loop(client: OKXRestClient, runtime: dict[str, Any]) -> None:
    """B：交易所持仓为真 — 周期性快照与变化告警（不自动乱平仓，盈利优先）。"""
    while True:
        await asyncio.sleep(max(12.0, float(LEV5_POS_RECONCILE_SEC)))
        try:
            raw = await asyncio.to_thread(client.positions_swap, INST_ID)
            summ = _summarize_swap_positions(raw)
            runtime["exchange_position_truth"] = summ
            runtime["swap_position_summary"] = summ
            sig = (round(float(summ["long_sz"]), 8), round(float(summ["short_sz"]), 8))
            prev = runtime.get("_exchange_pos_sig_prev")
            if prev is not None and prev != sig:
                log.warning(
                    "[对账][持仓] 交易所仓位变化 | prev=%s → now=%s | 请以交易所为准",
                    prev,
                    sig,
                )
            runtime["_exchange_pos_sig_prev"] = sig
        except Exception as e:
            log.debug("[对账][持仓] 拉取失败: %s", e)


async def _lev5_pending_orders_sweep_loop(runtime: dict[str, Any]) -> None:
    """B：清理过期 pending_orders（瞬时下单失败时保留 clOrdId 供成交匹配）。"""
    while True:
        await asyncio.sleep(18.0)
        try:
            po = runtime.get("pending_orders")
            if not isinstance(po, dict) or not po:
                continue
            nowt = time.time()
            stale: list[str] = []
            for cid, meta in list(po.items()):
                if not isinstance(meta, dict):
                    stale.append(str(cid))
                    continue
                try:
                    ts = float(meta.get("ts") or 0.0)
                except (TypeError, ValueError):
                    ts = 0.0
                if nowt - ts > 240.0:
                    stale.append(str(cid))
            for cid in stale:
                po.pop(cid, None)
                log.warning(
                    "[OMS] 清理过期 pending | clOrdId=%s（>240s TTL，含已成交未即时移除的残留）",
                    cid,
                )
        except Exception as e:
            log.debug("[OMS] pending 扫描失败: %s", e)


async def run() -> None:
    # 避免 httpx/httpcore 对每个 GET 打 INFO（刷屏）；需看底层请求时用 LOG_LEVEL=DEBUG
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    run_id = new_run_id()
    try:
        from quant.detailed_daily_log import init_session

        _daily = init_session(run_id=run_id)
        if _daily is not None:
            log.info("[日志] 详细离线日志根: %s", _daily.parent.resolve())
            log.info("[日志] 今日目录: %s（market/analysis/decisions/execution/system）", _daily.resolve())
    except Exception as ex:
        log.warning("[日志] 详细离线日志初始化失败（忽略）| %s", ex)
    metrics = Metrics()
    audit: AuditStore | NullAuditStore
    db_path: Path | None = None
    if AUDIT_ENABLED:
        db_path = Path(DATA_DIR) / AUDIT_DB_NAME
        audit = AuditStore(db_path)
    else:
        audit = NullAuditStore()
    if STRAT_DATA_SOURCE not in ("ws", "rest"):
        raise SystemExit(
            f"未知 STRAT_DATA_SOURCE={STRAT_DATA_SOURCE!r}，请使用 ws 或 rest（见 .env.example）"
        )
    if not str(INST_ID).upper().endswith("-SWAP"):
        raise SystemExit(
            "本项目仅支持 OKX 线性永续：INST_ID 必须以 -SWAP 结尾（例如 ETH-USDT-SWAP），"
            f"当前为 {INST_ID!r}"
        )

    audit.start_run(
        run_id,
        INST_ID,
        {
            "strat_live": STRAT_LIVE,
            "engine": "quant.app.runner",
            "strat_mode": STRAT_MODE,
            "strat_data_source": STRAT_DATA_SOURCE,
        },
    )

    strat = build_strategy()
    _log_session_banner(run_id=run_id, audit_path=db_path)
    try:
        append_runtime_checkpoint(
            "session_start",
            {
                "run_id": run_id,
                "inst_id": INST_ID,
                "strat_mode": STRAT_MODE,
                "strat_live": STRAT_LIVE,
                "okx_simulated": False,
                "data_dir": DATA_DIR,
                "audit_db": str(db_path) if db_path is not None else None,
                "config_fp": _config_fingerprint(),
            },
        )
    except Exception:
        pass
    log.info(
        "[配置] STRAT_DATA_SOURCE=%s | REST_POLL=%ss bar=%s limit=%s | "
        "HTTP 读超时=%ss 连接超时=%ss",
        STRAT_DATA_SOURCE,
        REST_POLL_INTERVAL_SEC,
        REST_CANDLES_BAR,
        REST_CANDLES_LIMIT,
        REST_HTTP_TIMEOUT_SEC,
        REST_HTTP_CONNECT_TIMEOUT_SEC,
    )
    log.info(
        "[配置] REST 显式代理（未设则仅靠 HTTPS_PROXY；OKX_WS_PROXY 已自动参与）: %s",
        mask_proxy_url(OKX_REST_HTTP_PROXY)
        if OKX_REST_HTTP_PROXY
        else "(无)",
    )
    log.warning(
        "[环境] 实盘 REST | config_fp=%s | %s",
        _config_fingerprint(),
        "请确认 API 权限与资金",
    )

    circuit = CircuitBreaker(CIRCUIT_MAX_FAILS, CIRCUIT_COOLDOWN_SEC)
    pipeline: ExecutionPipeline | None = None
    client: OKXRestClient | None = None
    exec_runtime: dict[str, Any] | None = None

    if STRAT_MODE != "grid_pro":
        raise SystemExit(f"当前仅支持 STRAT_MODE='grid_pro'，收到: {STRAT_MODE!r}")
    lev5_runtime: dict[str, Any] | None = None
    if INST_ID.upper().endswith("-SWAP"):
        lev5_runtime = _swap_strategy_runtime_base()

    if STRAT_LIVE:
        if not RISK_ENABLED:
            raise SystemExit(
                "STRAT_LIVE=1 时必须 RISK_ENABLED=1（余额与风控引擎校验）。"
            )
        if not RISK_SWAP_IGNORE_MAX_CAPS and (
            not RISK_MAX_NOTIONAL_USDT.strip() or not RISK_MAX_ORDER_BASE.strip()
        ):
            raise SystemExit(
                "STRAT_LIVE=1 且 RISK_SWAP_IGNORE_MAX_CAPS=0 时请在 .env 设置 "
                "RISK_MAX_NOTIONAL_USDT 与 RISK_MAX_ORDER_BASE。"
            )
        ensure_keys()
        client = OKXRestClient()
        exec_runtime = lev5_runtime if isinstance(lev5_runtime, dict) else {}
        dd_br = DailyDrawdownBreaker(max_loss_pct=float(RISK_DAILY_MAX_LOSS_PCT))
        if dd_br.enabled():
            exec_runtime["daily_drawdown_breaker"] = dd_br
        log.info("[执行] OKX REST 客户端已启用连接池复用（httpx keep-alive + 线程安全）")
        if INST_ID.upper().endswith("-SWAP"):
            try:
                try:
                    await asyncio.to_thread(
                        client.set_leverage,
                        inst_id=INST_ID,
                        lever=GRID_LEVERAGE,
                        mgn_mode=GRID_TD_MODE,
                        pos_side="long",
                    )
                    await asyncio.to_thread(
                        client.set_leverage,
                        inst_id=INST_ID,
                        lever=GRID_LEVERAGE,
                        mgn_mode=GRID_TD_MODE,
                        pos_side="short",
                    )
                except Exception as e:
                    if "posSide" in str(e) or "Parameter posSide error" in str(e):
                        log.warning(
                            "[杠杆] 账户当前不接受 posSide（可能是 net 模式），回退为不带 posSide 设置杠杆"
                        )
                        await asyncio.to_thread(
                            client.set_leverage,
                            inst_id=INST_ID,
                            lever=GRID_LEVERAGE,
                            mgn_mode=GRID_TD_MODE,
                        )
                        if isinstance(lev5_runtime, dict):
                            lev5_runtime["force_net_pos_side"] = True
                    else:
                        raise
                log.warning(
                    "[杠杆] 已设置合约杠杆 | inst=%s | leverage=%.2fx | td_mode=%s | pos_mode=%s",
                    INST_ID,
                    GRID_LEVERAGE,
                    GRID_TD_MODE,
                    (
                        "net_compat"
                        if isinstance(lev5_runtime, dict) and lev5_runtime.get("force_net_pos_side")
                        else "long_short"
                    ),
                )
            except Exception as e:
                raise SystemExit(f"设置杠杆失败，请检查账户权限/品种/模式: {e}") from e
        oms = OrderManager(run_id)
        swap_ct_val: float | None = None
        if INST_ID.upper().endswith("-SWAP"):
            try:
                spec = await asyncio.to_thread(get_swap_instrument_spec, INST_ID)
                if isinstance(spec, dict):
                    exec_runtime["instrument_spec"] = spec
                if isinstance(spec, dict) and spec.get("ctVal"):
                    swap_ct_val = float(spec["ctVal"])
                    log.info(
                        "[风控] 永续合约 %s ctVal=%s（张→base：base_qty = sz × ctVal）",
                        INST_ID,
                        swap_ct_val,
                    )
                else:
                    log.warning(
                        "[风控] 无法从 instruments 读取 %s 的 ctVal，"
                        "永续风控暂用 ctVal=0.01（请确认与交易所一致）",
                        INST_ID,
                    )
                    swap_ct_val = 0.01
            except Exception as e:
                log.warning(
                    "[风控] 拉取 instruments 失败: %s；永续风控暂用 ctVal=0.01",
                    e,
                )
                swap_ct_val = 0.01
        inner = ExecutionService(client, _risk_engine(swap_ct_val=swap_ct_val))
        exec_runtime["swap_ct_val"] = swap_ct_val
        pipeline = ExecutionPipeline(
            inner=inner,
            audit=audit,
            metrics=metrics,
            oms=oms,
            circuit=circuit,
            run_id=run_id,
            runtime=exec_runtime,
        )
        log.warning(
            "[执行] STRAT_LIVE=1：将通过 REST 发单（实盘）；"
            "链路=熔断→OMS→审计→风控→交易所。Ctrl+C 停止。"
        )
    else:
        log.info(
            "[执行] STRAT_LIVE=0：只做「行情+策略+审计/指标」，不调用下单 REST。"
            "若要发单：STRAT_LIVE=1 且 RISK_ENABLED=1，使用 run_strategy_live.py。"
        )
        if isinstance(lev5_runtime, dict):
            exec_runtime = lev5_runtime

    account_client: OKXRestClient | None = client
    if account_client is None and has_api_keys_configured():
        try:
            ensure_keys()
            account_client = OKXRestClient()
        except Exception as e:
            log.warning("[账户] 无法创建 REST 客户端（检查密钥与网络）: %s", e)
            account_client = None
    elif not has_api_keys_configured():
        log.info("[账户] 未配置 OKX API Key，跳过资金/挂单快照（仅公共行情仍可运行）")

    if ACCOUNT_SNAPSHOT_ON_START and account_client is not None:
        try:
            await asyncio.to_thread(log_account_snapshot, account_client, INST_ID)
        except Exception as e:
            log.warning("[账户] 启动快照失败: %s", e)

    if account_client is not None:
        try:
            raw = await asyncio.to_thread(account_client.trade_fee_swap, INST_ID)
            rows = raw.get("data") or []
            if rows and isinstance(rows[0], dict):
                r0 = rows[0]
                log.info(
                    "[账户] OKX trade-fee | inst=%s maker=%s taker=%s category=%s",
                    INST_ID,
                    r0.get("maker"),
                    r0.get("taker"),
                    r0.get("category"),
                )
                if lev5_runtime is not None:
                    try:
                        maker = abs(float(r0.get("maker") or 0.0))
                        taker = abs(float(r0.get("taker") or 0.0))
                        # 永续往返按 taker×2（IOC/吃单平仓与入场对齐）；注入策略 net_edge / 门槛比较
                        rt_bps = taker * 2.0 * 10000.0
                        lev5_runtime["account_maker_fee_rate"] = maker
                        lev5_runtime["account_taker_fee_rate"] = taker
                        lev5_runtime["roundtrip_taker_fee_bps"] = rt_bps
                        lev5_runtime["fee_rt_bps_live"] = rt_bps
                    except (TypeError, ValueError):
                        pass
        except Exception as e:
            log.debug("[账户] trade-fee 拉取跳过: %s", e)

    if (
        account_client is not None
        and lev5_runtime is not None
        and INST_ID.upper().endswith("-SWAP")
    ):
        await _swap_cold_start_sync(account_client, lev5_runtime)

    session_trade: SessionTradeStats | None = None
    if account_client is not None:
        session_trade = SessionTradeStats(inst_id=INST_ID)
        await _startup_exchange_selfcheck(account_client)

    inv_state: dict[str, Any] | None = None
    if account_client is not None:
        inv_state = {"last_poll": None, "last_inv_log": 0.0, "snapshot": None}

    loop = asyncio.get_running_loop()
    lev5_guard: dict[str, Any] | None = {
        "halt_new_entries": False,
        "reason": None,
        "consec_loss": 0,
        "last_realized": 0.0,
        "halt_since": None,
    }
    bg: list[asyncio.Task[None]] = [
        asyncio.create_task(
            _metrics_loop(
                metrics,
                account_client,
                session_trade,
                run_id,
                audit=audit,
                lev5_runtime=lev5_runtime,
            )
        )
    ]
    if (
        account_client is not None
        and session_trade is not None
        and lev5_guard is not None
        and lev5_runtime is not None
    ):
        bg.append(
            asyncio.create_task(
                _lev5_guard_loop(account_client, session_trade, lev5_guard, lev5_runtime)
            )
        )
    if (
        lev5_runtime is not None
        and INST_ID.upper().endswith("-SWAP")
    ):
        bg.append(asyncio.create_task(_lev5_funding_loop(lev5_runtime)))
        bg.append(asyncio.create_task(_lev5_instrument_loop(lev5_runtime)))
        bg.append(asyncio.create_task(_lev5_microstructure_loop(lev5_runtime)))
    if (
        account_client is not None
        and lev5_runtime is not None
        and INST_ID.upper().endswith("-SWAP")
    ):
        bg.append(
            asyncio.create_task(_lev5_position_reconcile_loop(account_client, lev5_runtime))
        )
        bg.append(asyncio.create_task(_lev5_pending_orders_sweep_loop(lev5_runtime)))
    if account_client is not None:
        bg.append(
            asyncio.create_task(
                _reconcile_loop(account_client, INST_ID, exec_runtime or {})
            )
        )
    if (
        STRAT_LIVE
        and account_client is not None
        and RISK_CORR_RESYNC_INTERVAL_SEC > 0
        and INST_ID.upper().endswith("-SWAP")
    ):
        bg.append(
            asyncio.create_task(
                _corr_exposure_resync_loop(
                    account_client,
                    INST_ID,
                    exec_runtime if isinstance(exec_runtime, dict) else {},
                )
            )
        )
    if account_client is not None and STRAT_STALE_ORDER_CANCEL_SEC > 0:
        bg.append(asyncio.create_task(_stale_order_loop(account_client, INST_ID)))
    if ACCOUNT_SNAPSHOT_INTERVAL_SEC > 0 and account_client is not None:
        bg.append(
            asyncio.create_task(
                _account_snapshot_loop(
                    account_client,
                    INST_ID,
                    ACCOUNT_SNAPSHOT_INTERVAL_SEC,
                )
            )
        )
    if lev5_runtime is not None:
        bg.append(
            asyncio.create_task(
                _lev5_hourly_report_loop(
                    lev5_runtime,
                    metrics,
                    account_client=account_client,
                    session_trade=session_trade,
                    audit=audit,
                    run_id=run_id,
                    inst_id=INST_ID,
                )
            )
        )

    try:
        if account_client is not None and inv_state is not None:
            await _inventory_bootstrap(account_client, INST_ID, inv_state)

        if STRAT_DATA_SOURCE == "rest":
            log.info(
                "[行情][REST] 使用 HTTP 轮询 + K 线特征（无 WebSocket）；"
                "间隔=%ss | bar=%s | limit=%s | 读超时=%ss | 连接超时=%ss",
                REST_POLL_INTERVAL_SEC,
                REST_CANDLES_BAR,
                REST_CANDLES_LIMIT,
                REST_HTTP_TIMEOUT_SEC,
                REST_HTTP_CONNECT_TIMEOUT_SEC,
            )
            log.info(
                "[行情][REST] httpx 代理: %s（errno=60 时检查 OKX_REST_PROXY / "
                "HTTPS_PROXY 与网络）",
                mask_proxy_url(OKX_REST_HTTP_PROXY)
                if OKX_REST_HTTP_PROXY
                else "(无显式代理，仅靠 trust_env)",
            )
            log.info(
                "[行情][REST] 已关闭逐请求 HTTP 日志。接下来请看："
                "约每 %ss 的 [指标] 与 [盈亏]（有 API Key 时）；"
                "仅当策略触发时出现 [策略]/[执行]/[结果]。无信号时终端会较安静。",
                METRICS_INTERVAL_SEC,
            )
            backoff = 1.0
            next_slow_ts = 0.0
            cached_feats: dict[str, Any] | None = None
            cached_books: dict[str, Any] | None = None
            while True:
                try:
                    now_ts = time.time()
                    if now_ts >= next_slow_ts or cached_feats is None:
                        (last, bid, ask), mctx = await asyncio.to_thread(
                            fetch_rest_snapshot,
                            INST_ID,
                            bar=REST_CANDLES_BAR,
                            limit=REST_CANDLES_LIMIT,
                            http_timeout_sec=REST_HTTP_TIMEOUT_SEC,
                        )
                        cf = mctx.get("candle_features")
                        if isinstance(cf, dict):
                            cached_feats = cf
                        ob = mctx.get("order_book")
                        if isinstance(ob, dict):
                            cached_books = ob
                        if lev5_runtime is not None:
                            lev5_runtime["last_candle_snapshot_wall_ts"] = time.time()
                        next_slow_ts = now_ts + max(2.0, float(REST_SNAPSHOT_SLOW_INTERVAL_SEC))
                    else:
                        t = await asyncio.to_thread(
                            get_ticker,
                            INST_ID,
                            timeout=REST_HTTP_TIMEOUT_SEC,
                        )
                        p = parse_ticker_prices(t)
                        if p is None:
                            raise RuntimeError("ticker 行缺少 last/bidPx/askPx")
                        last, bid, ask = p
                        mctx = {
                            "source": "rest",
                            "instId": INST_ID,
                            "bar": REST_CANDLES_BAR,
                            "ticker_ts": t.get("ts"),
                            "candle_features": cached_feats or {},
                        }
                        if cached_books:
                            mctx["order_book"] = cached_books
                        for k in ("open24h", "high24h", "low24h", "vol24h", "sodUtc8"):
                            if k in t:
                                mctx[k] = t[k]
                    backoff = 1.0
                    if lev5_runtime is not None:
                        lev5_runtime["rest_fail_streak"] = 0
                except Exception as e:
                    log.warning(
                        "[行情][REST] 拉取失败: %s | %.1fs 后重试",
                        e,
                        backoff,
                    )
                    if lev5_runtime is not None:
                        lev5_runtime["rest_fail_streak"] = int(
                            lev5_runtime.get("rest_fail_streak") or 0
                        ) + 1
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.5, 30.0)
                    continue
                await _dispatch_tick(
                    strat=strat,
                    metrics=metrics,
                    audit=audit,
                    run_id=run_id,
                    pipeline=pipeline,
                    loop=loop,
                    last=last,
                    bid=bid,
                    ask=ask,
                    market_context=mctx,
                    session_trade=session_trade,
                    account_client=account_client,
                    inv_state=inv_state,
                    lev5_guard=lev5_guard,
                    lev5_runtime=lev5_runtime,
                )
                await asyncio.sleep(REST_POLL_INTERVAL_SEC)
        else:
            logging.getLogger("websockets").setLevel(logging.WARNING)
            if lev5_runtime is not None:
                # _ws_price_feed_loop: 独立 WS 连接，实时写 ws_last/ws_ts 供 synthetic tick 使用
                bg.append(asyncio.create_task(_ws_price_feed_loop(lev5_runtime)))
                bg.append(asyncio.create_task(_ws_stall_watchdog_loop(
                    strat=strat,
                    metrics=metrics,
                    audit=audit,
                    run_id=run_id,
                    pipeline=pipeline,
                    loop=loop,
                    session_trade=session_trade,
                    account_client=account_client,
                    inv_state=inv_state,
                    lev5_guard=lev5_guard,
                    lev5_runtime=lev5_runtime,
                )))
            async for row in stream_tickers(OKX_WS_PUBLIC_URL_LIST, INST_ID):
                p = _parse_tick(row)
                if not p:
                    log.debug("[行情] 跳过一条无法解析的 ticker 行")
                    continue
                last, bid, ask = p
                # 注入 K 线特征缓存（由 _candle_refresh_loop 每 15s 刷新一次）
                _cached_cf = lev5_runtime.get("candle_ctx") if lev5_runtime else None
                ws_ctx: dict[str, Any] = {
                    "source": "ws",
                    "instId": INST_ID,
                    "ticker_ts": row.get("ts") if isinstance(row, dict) else None,
                    "candle_features": _cached_cf or {},
                }
                try:
                    await _dispatch_tick(
                        strat=strat,
                        metrics=metrics,
                        audit=audit,
                        run_id=run_id,
                        pipeline=pipeline,
                        loop=loop,
                        last=last,
                        bid=bid,
                        ask=ask,
                        market_context=ws_ctx,
                        session_trade=session_trade,
                        account_client=account_client,
                        inv_state=inv_state,
                        lev5_guard=lev5_guard,
                        lev5_runtime=lev5_runtime,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as _dispatch_exc:
                    log.error("[行情][WS] dispatch_tick 异常（跳过本tick）: %s", _dispatch_exc)
                    continue
                # 记录本次 dispatch 时间，供 _ws_stall_watchdog_loop 判断 WS 是否静默
                if lev5_runtime is not None:
                    lev5_runtime["ws_dispatch_ts"] = time.time()
    finally:
        for t in bg:
            t.cancel()
        await asyncio.gather(*bg, return_exceptions=True)
        audit.close()
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if account_client is not None and account_client is not client:
            try:
                account_client.close()
            except Exception:
                pass
        log.info("[系统] 会话结束 | run_id=%s | 审计连接已关闭", run_id)


def main() -> None:
    # 支持通过环境变量设置最大运行时间（小时），超时后干净退出由外部脚本重启
    # 默认 24 小时：降低重启频率，减少因重启导致的网格中心漂移和未成交订单丢失
    import os as _os
    max_hours = float(_os.environ.get("BOT_MAX_SESSION_HOURS", "24"))
    max_sec = max_hours * 3600.0
    import signal as _signal, threading as _threading

    def _timeout_shutdown():
        import time as _time
        _time.sleep(max_sec)
        log.warning(
            "[系统] 已运行 %.1f 小时，触发计划重启 → 进程退出（由外部脚本自动重启）",
            max_hours,
        )
        _signal.raise_signal(_signal.SIGTERM)

    t = _threading.Thread(target=_timeout_shutdown, daemon=True)
    t.start()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
