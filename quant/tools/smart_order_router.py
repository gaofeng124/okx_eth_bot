"""
Smart Order Router —— 订单拆分 / Iceberg / VWAP

解决大单冲击成本：sz=1.0 一次性下单可能在 post_only 挂单时失败，
或 market order 滑点大。拆分减少 market impact。

提供的工具：
  1. split_and_place(side, total_sz, num_slices, interval_sec):
     把总量拆 N 片，每 interval 秒下一片
  2. place_iceberg(side, visible_sz, total_sz):
     只显示 visible_sz，成交后自动挂下一片
  3. vwap_execute(side, total_sz, duration_sec):
     按成交量加权分时间下单

由策略调用，当 total_sz > 2 张时建议使用。

用法（策略里）：
  from quant.tools.smart_order_router import SmartRouter
  r = SmartRouter()
  r.split_and_place("buy", total_sz=3.0, num_slices=3, interval_sec=20)
"""
from __future__ import annotations

import json
import os
import time
import hmac
import base64
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv("/root/okx_eth_bot/.env")
if not os.environ.get("OKX_API_KEY"):
    load_dotenv("/Users/gaofeng/Documents/okx_eth_bot/.env")

import httpx

CST = timezone(timedelta(hours=8))


def _sign(ts, m, p, body=""):
    secret = os.environ["OKX_SECRET_KEY"]
    return base64.b64encode(
        hmac.new(secret.encode(), f"{ts}{m}{p}{body}".encode(), hashlib.sha256).digest()
    ).decode()


def _api(method, path, body=""):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    h = {
        "OK-ACCESS-KEY": os.environ["OKX_API_KEY"],
        "OK-ACCESS-SIGN": _sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": os.environ["OKX_PASSPHRASE"],
        "Content-Type": "application/json",
    }
    if method == "GET":
        return httpx.get("https://www.okx.com" + path, headers=h, timeout=15).json()
    return httpx.post("https://www.okx.com" + path, headers=h, content=body, timeout=15).json()


class SmartRouter:
    """订单智能路由。"""

    def split_and_place(self, side: str, total_sz: float, num_slices: int = 3,
                        interval_sec: float = 20.0, ord_type: str = "market") -> list[dict]:
        """把大单拆成 num_slices 片，每 interval 秒下 1 片。"""
        slice_sz = round(total_sz / num_slices, 2)
        results = []
        for i in range(num_slices):
            this_sz = slice_sz if i < num_slices - 1 else (total_sz - slice_sz * (num_slices - 1))
            body = json.dumps({
                "instId": "ETH-USDT-SWAP",
                "tdMode": "isolated",
                "side": side,
                "ordType": ord_type,
                "sz": str(round(this_sz, 2)),
            })
            r = _api("POST", "/api/v5/trade/order", body)
            results.append({"slice": i + 1, "sz": this_sz, "response": r})
            if i < num_slices - 1:
                time.sleep(interval_sec)
        return results

    def place_iceberg(self, side: str, visible_sz: float, total_sz: float, px: float,
                      post_only: bool = True) -> list[dict]:
        """OKX 原生 iceberg order。"""
        body = json.dumps({
            "instId": "ETH-USDT-SWAP",
            "tdMode": "isolated",
            "side": side,
            "ordType": "iceberg",
            "sz": str(total_sz),
            "px": str(px),
            "szVisibleToPublic": str(visible_sz),
            **({"postOnly": "true"} if post_only else {}),
        })
        r = _api("POST", "/api/v5/trade/order-algo", body)
        return [{"type": "iceberg", "visible": visible_sz, "total": total_sz, "px": px, "response": r}]

    def vwap_execute(self, side: str, total_sz: float, duration_sec: float = 300.0,
                     slices: int = 10) -> list[dict]:
        """VWAP 分时执行：在 duration 内按指数递增 sz 分 slices 次下单。"""
        # 简化：等比时间 + 固定 sz（真 VWAP 需成交量曲线建模，留待下次）
        slice_sz = round(total_sz / slices, 2)
        interval = duration_sec / slices
        results = []
        for i in range(slices):
            this_sz = slice_sz if i < slices - 1 else (total_sz - slice_sz * (slices - 1))
            body = json.dumps({
                "instId": "ETH-USDT-SWAP",
                "tdMode": "isolated",
                "side": side,
                "ordType": "market",
                "sz": str(round(this_sz, 2)),
            })
            r = _api("POST", "/api/v5/trade/order", body)
            results.append({"slice": i + 1, "sz": this_sz, "ts": datetime.now(CST).strftime("%H:%M:%S"), "response": r})
            if i < slices - 1:
                time.sleep(interval)
        return results


if __name__ == "__main__":
    # 测试：不真实下单，只打印
    r = SmartRouter()
    print("SmartRouter 可用方法：split_and_place / place_iceberg / vwap_execute")
    print("模块加载成功，等策略调用")
