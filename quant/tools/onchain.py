"""
Etherscan V2 API 链上数据工具集 —— 供 AI 因子研究使用。

典型用法：
    from quant.tools.onchain import (
        eth_price_usd, gas_oracle, exchange_net_flow_24h,
        whale_transfers_recent, exchange_balance,
    )

免费 tier：100k 调用/天，5 调用/秒。
API key 从环境变量 `ETHERSCAN_API_KEY` 读取。
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Any

import httpx

API_BASE = "https://api.etherscan.io/v2/api"
CHAIN_ID = 1  # Ethereum mainnet

# 主要交易所热钱包（公开数据）—— 监控流入/流出判断抛压/承接
EXCHANGE_WALLETS = {
    "Binance-14": "0x28C6c06298d514Db089934071355E5743bf21d60",
    "Binance-8": "0x4E9ce36E442e55EcD9025B9a6E0D88485d628A67",
    "Binance-Hot-20": "0xF977814e90dA44bFA03b6295A0616a897441aceC",
    "OKX-6": "0xA7EFAe728D2936e78BDA97dc267687568dD593f3",
    "OKX-Hot": "0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b",
    "Coinbase-10": "0x71660c4005BA85c37ccec55d0C4493E66Fe775d3",
    "Bybit-5": "0xa7A93fd0a276fc1C0197a5B5623eD117786eeD06",
    "Kraken-13": "0xDA9dfA130Df4dE4673b89022EE50ff26f6EA73Cf",
}


def _api_key() -> str:
    k = os.environ.get("ETHERSCAN_API_KEY", "").strip()
    if not k:
        raise RuntimeError("ETHERSCAN_API_KEY not set in env")
    return k


def _call(params: dict, timeout: float = 15.0) -> Any:
    """发起 Etherscan V2 调用，返回 result 字段。"""
    params = {"chainid": CHAIN_ID, **params, "apikey": _api_key()}
    r = httpx.get(API_BASE, params=params, timeout=timeout)
    data = r.json()
    if data.get("status") != "1":
        # 某些 action 失败返回 status=0，但 result 可能仍有值；抛出给调用者决定
        raise RuntimeError(f"Etherscan API error: {data.get('message')}: {data.get('result')}")
    return data.get("result")


def eth_price_usd() -> float:
    """ETH 现价（Etherscan 聚合价，不是 OKX）。"""
    r = _call({"module": "stats", "action": "ethprice"})
    return float(r["ethusd"])


def gas_oracle() -> dict:
    """Gas 价格档位。单位 gwei。
    返回 dict: safe / propose / fast / suggestBase
    网络拥堵 / 情绪高涨时 gas 飙升。"""
    r = _call({"module": "gastracker", "action": "gasoracle"})
    return {
        "safe": float(r["SafeGasPrice"]),
        "propose": float(r["ProposeGasPrice"]),
        "fast": float(r["FastGasPrice"]),
        "base_fee": float(r.get("suggestBaseFee", 0)),
    }


def exchange_balance(exchange: str = "Binance-14") -> float:
    """查某个交易所热钱包 ETH 余额。"""
    addr = EXCHANGE_WALLETS.get(exchange)
    if not addr:
        raise ValueError(f"unknown exchange tag {exchange}, known: {list(EXCHANGE_WALLETS)}")
    r = _call({"module": "account", "action": "balance", "address": addr, "tag": "latest"})
    return int(r) / 1e18


def all_exchange_balances() -> dict[str, float]:
    """批量查所有已知交易所钱包 ETH 余额。"""
    out = {}
    for name, addr in EXCHANGE_WALLETS.items():
        try:
            r = _call({"module": "account", "action": "balance", "address": addr, "tag": "latest"})
            out[name] = int(r) / 1e18
        except Exception as e:
            out[name] = None  # 保留错误不中断
        time.sleep(0.25)  # 4 req/s，避免超速
    return out


def wallet_txs_recent(address: str, limit: int = 100) -> list[dict]:
    """查地址最近 txlist。按时间倒序。"""
    r = _call({
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
    })
    if not isinstance(r, list):
        return []
    return r


def exchange_flow_recent(exchange: str = "Binance-14", block_window: int = 7200) -> dict:
    """统计交易所热钱包最近 N 块（~24h = 7200 块）的 ETH 流入/流出。

    返回：
      {inflow_eth, outflow_eth, net_flow_eth, inflow_count, outflow_count,
       large_inflow_count, large_outflow_count}
    large 判据：> 50 ETH。
    净流入 > 0 = 砸盘信号（用户打 ETH 进来要卖）
    净流出 > 0 = 买入信号
    """
    addr = EXCHANGE_WALLETS.get(exchange)
    if not addr:
        raise ValueError(f"unknown exchange tag {exchange}")
    # 先拿当前块号
    blk = int(_call({"module": "proxy", "action": "eth_blockNumber"}), 16)
    start_blk = max(0, blk - block_window)

    r = _call({
        "module": "account",
        "action": "txlist",
        "address": addr,
        "startblock": start_blk,
        "endblock": blk,
        "page": 1,
        "offset": 10000,  # 24h 内通常就 ~几百笔
        "sort": "desc",
    })
    if not isinstance(r, list):
        return {"error": str(r)}

    addr_lower = addr.lower()
    inflow = 0.0
    outflow = 0.0
    in_cnt = 0
    out_cnt = 0
    large_in = 0
    large_out = 0
    LARGE_THRESHOLD = 50.0

    for tx in r:
        value_eth = int(tx.get("value", 0)) / 1e18
        if value_eth <= 0:
            continue
        to = tx.get("to", "").lower()
        frm = tx.get("from", "").lower()
        if to == addr_lower:
            inflow += value_eth
            in_cnt += 1
            if value_eth >= LARGE_THRESHOLD:
                large_in += 1
        elif frm == addr_lower:
            outflow += value_eth
            out_cnt += 1
            if value_eth >= LARGE_THRESHOLD:
                large_out += 1

    return {
        "exchange": exchange,
        "window_blocks": block_window,
        "inflow_eth": round(inflow, 3),
        "outflow_eth": round(outflow, 3),
        "net_flow_eth": round(inflow - outflow, 3),
        "inflow_count": in_cnt,
        "outflow_count": out_cnt,
        "large_inflow_count": large_in,  # 抛压前兆
        "large_outflow_count": large_out,  # 囤币信号
    }


def summary_snapshot() -> dict:
    """一键取"链上全景"：价、gas、所有交易所余额。供 AI 每轮参考。"""
    snap = {}
    try:
        snap["eth_price_etherscan"] = eth_price_usd()
    except Exception as e:
        snap["eth_price_etherscan_err"] = str(e)
    try:
        snap["gas"] = gas_oracle()
    except Exception as e:
        snap["gas_err"] = str(e)
    try:
        snap["exchange_balances"] = all_exchange_balances()
    except Exception as e:
        snap["exchange_balances_err"] = str(e)
    return snap


if __name__ == "__main__":
    import json
    print(json.dumps(summary_snapshot(), indent=2, ensure_ascii=False))
