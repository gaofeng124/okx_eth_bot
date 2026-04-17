"""策略工厂（仅保留 grid_pro 网格策略）。"""
from __future__ import annotations

from quant.settings import (
    INST_ID,
    STRAT_MODE,
    STRAT_PRICE_DECIMALS,
    LEV5_CT_VAL_BASE,
    DATA_DIR,
    GRID_LEVELS,
    GRID_ATR_WINDOW,
    GRID_ATR_MULT,
    GRID_MIN_SPACING_PCT,
    GRID_MAX_SPACING_PCT,
    GRID_WHOLE_STOP_USDT,
    GRID_DAILY_STOP_USDT,
    GRID_RECENTER_MULT,
    GRID_ENTRY_TIMEOUT_SEC,
    GRID_COOLDOWN_SEC,
    GRID_SYNC_INTERVAL_SEC,
    GRID_ROUNDTRIP_FEE_BPS,
    GRID_LEVERAGE,
    GRID_TD_MODE,
    GRID_WARMUP_TICKS,
    GRID_DAILY_TARGET_USDT,
    GRID_DRAWDOWN_FROM_PEAK_USDT,
)
from quant.strategy.base import TickStrategy
from quant.strategy.grid_pro import GridProStrategy


def build_strategy() -> TickStrategy:
    mode = STRAT_MODE.strip().lower()
    if mode != "grid_pro":
        raise SystemExit(f"当前仅支持 STRAT_MODE='grid_pro'，收到: {STRAT_MODE!r}")
    return GridProStrategy(
        inst_id=INST_ID,
        leverage=GRID_LEVERAGE,
        td_mode=GRID_TD_MODE,
        price_decimals=STRAT_PRICE_DECIMALS,
        ct_val=LEV5_CT_VAL_BASE,
        grid_levels=GRID_LEVELS,
        atr_window=GRID_ATR_WINDOW,
        atr_mult=GRID_ATR_MULT,
        min_spacing_pct=GRID_MIN_SPACING_PCT,
        max_spacing_pct=GRID_MAX_SPACING_PCT,
        whole_stop_usdt=GRID_WHOLE_STOP_USDT,
        daily_stop_usdt=GRID_DAILY_STOP_USDT,
        recenter_mult=GRID_RECENTER_MULT,
        entry_timeout_sec=GRID_ENTRY_TIMEOUT_SEC,
        cooldown_sec=GRID_COOLDOWN_SEC,
        sync_interval_sec=GRID_SYNC_INTERVAL_SEC,
        roundtrip_fee_bps=GRID_ROUNDTRIP_FEE_BPS,
        data_dir=DATA_DIR,
        warmup_ticks=GRID_WARMUP_TICKS,
        daily_target_usdt=GRID_DAILY_TARGET_USDT,
        drawdown_from_peak_usdt=GRID_DRAWDOWN_FROM_PEAK_USDT,
    )
