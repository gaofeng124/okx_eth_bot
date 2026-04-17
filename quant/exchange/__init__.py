"""Merged exchange package (flattened)."""
from __future__ import annotations


# ----- http_common.py -----

"""
欧易 HTTP 共用：私有签名 REST 与公共行情均走同一套代理与超时（见 settings）。
"""

from typing import Any

import httpx

from quant.settings import (
    OKX_REST_HTTP_PROXY,
    REST_HTTP_CONNECT_TIMEOUT_SEC,
    REST_HTTP_TIMEOUT_SEC,
)


def okx_http_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=REST_HTTP_CONNECT_TIMEOUT_SEC,
        read=REST_HTTP_TIMEOUT_SEC,
        write=min(60.0, REST_HTTP_TIMEOUT_SEC),
        pool=30.0,
    )


def okx_http_limits() -> httpx.Limits:
    """复用 TCP/TLS，降低每次请求的握手与调度开销（私有 REST 全走同一 Client）。"""
    return httpx.Limits(max_keepalive_connections=32, max_connections=64)


def okx_http_client_kwargs(*, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "timeout": okx_http_timeout(),
        "limits": okx_http_limits(),
        "trust_env": True,
    }
    if OKX_REST_HTTP_PROXY:
        kw["proxy"] = OKX_REST_HTTP_PROXY
    if extra:
        kw.update(extra)
    return kw

# ----- net_env.py -----

"""
与网络环境变量相关的工具。

部分环境会设置全局 SOCKS 代理（ALL_PROXY 等），底层库走 SOCKS 时需要安装
``python-socks``；若你希望「欧易公共 WS 直连」，可用 settings.OKX_WS_DIRECT=1，
在建立连接前临时移除这些变量（仅影响当前进程内的后续连接尝试，用完会还原）。
"""

import os

_PROXY_KEYS = (
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
)


def pop_proxy_env() -> dict[str, str]:
    """移除代理相关环境变量，返回被删键值以便恢复。"""
    saved: dict[str, str] = {}
    for k in _PROXY_KEYS:
        if k in os.environ:
            saved[k] = os.environ.pop(k)
    return saved


def restore_proxy_env(saved: dict[str, str]) -> None:
    if saved:
        os.environ.update(saved)

# ----- websockets_connection_patch.py -----

"""
Mitigate asyncio + TLS edge case: connection_lost() may run before connection_made()
during failed start_tls(), so recv_messages is missing — see python-websockets#1629.

Prevents asyncio "Exception in callback ... recv_messages" spam; does not fix the
underlying TLS reset (network/proxy/node).
"""

import functools
import logging

log = logging.getLogger(__name__)

_applied = False


def apply_websockets_connection_lost_patch() -> None:
    global _applied
    if _applied:
        return
    try:
        from websockets.asyncio.connection import Connection
    except ImportError:
        return

    if getattr(Connection.connection_lost, "_okx_tls_race_patch", False):
        _applied = True
        return

    _orig = Connection.connection_lost

    @functools.wraps(_orig)
    def _guarded(self, exc):  # type: ignore[no-untyped-def]
        if hasattr(self, "recv_messages"):
            return _orig(self, exc)
        log.debug(
            "[行情][WS] websockets: connection_lost before connection_made (TLS race); "
            "partial cleanup only"
        )
        try:
            self.protocol.receive_eof()
        except Exception:
            pass
        try:
            w = getattr(self, "connection_lost_waiter", None)
            if w is not None and not w.done():
                w.set_result(None)
        except Exception:
            pass
        return

    _guarded._okx_tls_race_patch = True  # type: ignore[attr-defined]
    Connection.connection_lost = _guarded  # type: ignore[method-assign]
    _applied = True

# ----- ws_errors.py -----

"""WebSocket 连接异常的可读格式化（部分库抛出的异常 str(e) 为空）。"""

import traceback


def format_ws_exception(exc: BaseException) -> str:
    parts: list[str] = [type(exc).__name__]
    s = str(exc).strip()
    if s:
        parts.append(s)
    else:
        parts.append(repr(exc))
    if isinstance(exc, OSError) and getattr(exc, "errno", None) is not None:
        parts.append(f"errno={exc.errno}")
    if exc.__cause__ is not None:
        parts.append(f"cause={type(exc.__cause__).__name__}:{exc.__cause__!r}")
    return " | ".join(parts)


def traceback_tail(exc: BaseException, *, max_chars: int = 1200) -> str:
    """当前异常栈的尾部文本，便于贴日志。"""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return text[-max_chars:]

# ----- okx_public.py -----

"""
欧易 **公共** 行情 REST（无需 API Key，与私有 REST 签名分离）。

用于 STRAT_DATA_SOURCE=rest：定时拉 ticker + K 线，避免 WebSocket 长连接。

注意：httpx 的 trust_env 只认标准 HTTP(S)_PROXY；若仅在 .env 里配了 OKX_WS_PROXY，
必须通过 settings.OKX_REST_HTTP_PROXY 显式传入 proxy=（已自动串联 OKX_WS_PROXY）。
"""

import logging
import random
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from quant.settings import (
    OKX_REST_HTTP_PROXY,
    OKX_REST_URL,
    OKX_REST_MAX_ATTEMPTS,
    OKX_REST_RETRY_BASE_SEC,
    REST_HTTP_CONNECT_TIMEOUT_SEC,
    REST_ORDER_BOOK_SZ,
)

log = logging.getLogger(__name__)

# 经代理或跨境网络时常见：对端未回 body 就断连（与 SSL EOF 同类，属瞬时故障）
_PUBLIC_REST_TRANSIENT: tuple[type[BaseException], ...] = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ProxyError,
)
_PUBLIC_REST_MAX_RETRIES = 3
_PUBLIC_REST_RETRY_BASE_SEC = 0.4


def mask_proxy_url(url: str) -> str:
    """日志用；含账号密码时打码。"""
    try:
        p = urlparse(url)
        if p.username is not None:
            port = f":{p.port}" if p.port else ""
            return f"{p.scheme}://***@{p.hostname or ''}{port}"
        return url
    except Exception:
        return url


def _timeout(read_sec: float) -> httpx.Timeout:
    r = float(read_sec)
    return httpx.Timeout(
        connect=REST_HTTP_CONNECT_TIMEOUT_SEC,
        read=r,
        write=min(60.0, r),
        pool=30.0,
    )


def _coerce_timeout(timeout: httpx.Timeout | float | None) -> httpx.Timeout:
    if isinstance(timeout, httpx.Timeout):
        return timeout
    if isinstance(timeout, (int, float)):
        return _timeout(float(timeout))
    return _timeout(90.0)


def _http_client(timeout: httpx.Timeout) -> httpx.Client:
    """
    若配置了 OKX_REST_HTTP_PROXY（含从 OKX_WS_PROXY 继承），显式走代理；
    否则仍 trust_env，以便仅用系统/终端里的 HTTPS_PROXY。
    """
    kw: dict[str, Any] = {"timeout": timeout, "trust_env": True}
    if OKX_REST_HTTP_PROXY:
        kw["proxy"] = OKX_REST_HTTP_PROXY
    return httpx.Client(**kw)


def _get_json(
    client: httpx.Client,
    url: str,
    params: dict[str, str],
) -> dict[str, Any]:
    """GET；429/5xx 退避重试（A/J：限频与 SSL 瞬断）。"""
    max_a = max(1, int(OKX_REST_MAX_ATTEMPTS))
    base = float(OKX_REST_RETRY_BASE_SEC)
    last_exc: BaseException | None = None
    for attempt in range(max_a):
        r = client.get(url, params=params)
        if r.status_code == 429:
            try:
                body = r.json()
            except Exception:
                body = r.text[:300]
            last_exc = RuntimeError(f"HTTP 429: {body}")
            ra = r.headers.get("Retry-After")
            try:
                wait_s = float(ra) if ra else min(8.0, base * (2**attempt))
            except (TypeError, ValueError):
                wait_s = min(8.0, base * (2**attempt))
            if attempt + 1 >= max_a:
                raise last_exc
            time.sleep(min(12.0, wait_s + random.random() * 0.06))
            continue
        if r.status_code >= 500:
            last_exc = RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            if attempt + 1 >= max_a:
                raise last_exc
            time.sleep(min(8.0, base * (2**attempt) + random.random() * 0.05))
            continue
        try:
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            last_exc = e
            if attempt + 1 >= max_a:
                raise
            time.sleep(min(8.0, base * (2**attempt) + random.random() * 0.08))
            continue
        if str(data.get("code", "0")) != "0":
            raise RuntimeError(f"OKX API 错误: {data}")
        return data
    raise RuntimeError(f"OKX public GET 失败: {last_exc}")


def get_ticker(
    inst_id: str,
    *,
    base_url: str = OKX_REST_URL,
    timeout: httpx.Timeout | float | None = None,
) -> dict[str, Any]:
    """GET /api/v5/market/ticker → data[0] 单行 ticker。"""
    bu = base_url.rstrip("/")
    to = _coerce_timeout(timeout)
    with _http_client(to) as client:
        url = f"{bu}/api/v5/market/ticker"
        data = _get_json(client, url, {"instId": inst_id})
    arr = data.get("data") or []
    if not arr:
        raise RuntimeError("ticker 空数据")
    return arr[0]


def get_candles(
    inst_id: str,
    *,
    bar: str = "1m",
    limit: int = 120,
    base_url: str = OKX_REST_URL,
    timeout: httpx.Timeout | float | None = None,
) -> list[list[str]]:
    """GET /api/v5/market/candles；返回行为 OKX 顺序（最新在前）。"""
    bu = base_url.rstrip("/")
    to = _coerce_timeout(timeout)
    with _http_client(to) as client:
        url = f"{bu}/api/v5/market/candles"
        data = _get_json(
            client,
            url,
            {"instId": inst_id, "bar": bar, "limit": str(limit)},
        )
    arr = data.get("data") or []
    return arr if isinstance(arr, list) else []


def _fetch_ticker_and_candles_once(
    inst_id: str,
    *,
    bar: str,
    limit: int,
    base_url: str,
    to: httpx.Timeout,
    book_sz: int,
) -> tuple[dict[str, Any], list[list[str]], dict[str, Any] | None]:
    with _http_client(to) as client:
        turl = f"{base_url}/api/v5/market/ticker"
        dj = _get_json(client, turl, {"instId": inst_id})
        arr = dj.get("data") or []
        if not arr:
            raise RuntimeError("ticker 空数据")
        row0 = arr[0]

        curl = f"{base_url}/api/v5/market/candles"
        cj = _get_json(
            client,
            curl,
            {"instId": inst_id, "bar": bar, "limit": str(limit)},
        )
        candles = cj.get("data") or []
        if not isinstance(candles, list):
            candles = []

        books: dict[str, Any] | None = None
        if book_sz > 0:
            try:
                burl = f"{base_url}/api/v5/market/books"
                bj = _get_json(
                    client,
                    burl,
                    {"instId": inst_id, "sz": str(min(400, max(1, book_sz)))},
                )
                barr = bj.get("data") or []
                if barr and isinstance(barr[0], dict):
                    books = {
                        "asks": barr[0].get("asks"),
                        "bids": barr[0].get("bids"),
                        "ts": barr[0].get("ts"),
                    }
            except Exception as e:
                log.debug("market/books 跳过: %s", e)
    return row0, candles, books


def fetch_ticker_and_candles(
    inst_id: str,
    *,
    bar: str,
    limit: int,
    base_url: str | None = None,
    timeout: httpx.Timeout | float | None = None,
    book_sz: int | None = None,
) -> tuple[dict[str, Any], list[list[str]], dict[str, Any] | None]:
    """
    同一 Client 连续请求 ticker + candles +（可选）order book，复用连接。

    books：GET /api/v5/market/books；book_sz=0 跳过。
    """
    bu = (base_url or OKX_REST_URL).rstrip("/")
    to = _coerce_timeout(timeout)
    bs = REST_ORDER_BOOK_SZ if book_sz is None else book_sz
    for attempt in range(_PUBLIC_REST_MAX_RETRIES):
        try:
            return _fetch_ticker_and_candles_once(
                inst_id,
                bar=bar,
                limit=limit,
                base_url=bu,
                to=to,
                book_sz=bs,
            )
        except httpx.ReadTimeout:
            raise
        except _PUBLIC_REST_TRANSIENT as e:
            if attempt + 1 >= _PUBLIC_REST_MAX_RETRIES:
                raise
            delay = _PUBLIC_REST_RETRY_BASE_SEC * (2**attempt)
            log.debug(
                "公共行情 REST 瞬时断连 %s（第 %s/%s 次）%.2fs 后重试",
                e,
                attempt + 1,
                _PUBLIC_REST_MAX_RETRIES,
                delay,
            )
            time.sleep(delay)


def get_funding_rate(
    inst_id: str,
    *,
    base_url: str = OKX_REST_URL,
    timeout: httpx.Timeout | float | None = None,
) -> float | None:
    bu = base_url.rstrip("/")
    to = _coerce_timeout(timeout)
    with _http_client(to) as client:
        url = f"{bu}/api/v5/public/funding-rate"
        data = _get_json(client, url, {"instId": inst_id})
    arr = data.get("data") or []
    if not arr or not isinstance(arr[0], dict):
        return None
    try:
        return float(arr[0].get("fundingRate"))
    except (TypeError, ValueError):
        return None


def get_funding_snapshot(
    inst_id: str,
    *,
    base_url: str = OKX_REST_URL,
    timeout: httpx.Timeout | float | None = None,
) -> dict[str, Any] | None:
    bu = base_url.rstrip("/")
    to = _coerce_timeout(timeout)
    with _http_client(to) as client:
        url = f"{bu}/api/v5/public/funding-rate"
        data = _get_json(client, url, {"instId": inst_id})
    arr = data.get("data") or []
    if not arr or not isinstance(arr[0], dict):
        return None
    row = arr[0]
    out: dict[str, Any] = {}
    try:
        out["fundingRate"] = float(row.get("fundingRate"))
    except (TypeError, ValueError):
        out["fundingRate"] = None
    try:
        out["nextFundingTime"] = int(row.get("nextFundingTime"))
    except (TypeError, ValueError):
        out["nextFundingTime"] = None
    return out


def get_swap_instrument_spec(
    inst_id: str,
    *,
    base_url: str = OKX_REST_URL,
    timeout: httpx.Timeout | float | None = None,
) -> dict[str, Any] | None:
    bu = base_url.rstrip("/")
    to = _coerce_timeout(timeout)
    with _http_client(to) as client:
        url = f"{bu}/api/v5/public/instruments"
        data = _get_json(client, url, {"instType": "SWAP", "instId": inst_id})
    arr = data.get("data") or []
    if not arr or not isinstance(arr[0], dict):
        return None
    r0 = arr[0]
    out: dict[str, Any] = {}
    for k in ("ctVal", "lotSz", "minSz", "maxLmtSz"):
        v = r0.get(k)
        if v not in (None, ""):
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out or None


def get_open_interest(
    inst_id: str,
    *,
    base_url: str = OKX_REST_URL,
    timeout: httpx.Timeout | float | None = None,
) -> float | None:
    bu = base_url.rstrip("/")
    to = _coerce_timeout(timeout)
    with _http_client(to) as client:
        url = f"{bu}/api/v5/public/open-interest"
        data = _get_json(client, url, {"instType": "SWAP", "instId": inst_id})
    arr = data.get("data") or []
    if not arr or not isinstance(arr[0], dict):
        return None
    try:
        return float(arr[0].get("oi"))
    except (TypeError, ValueError):
        return None


def get_mark_price(
    inst_id: str,
    *,
    base_url: str = OKX_REST_URL,
    timeout: httpx.Timeout | float | None = None,
) -> float | None:
    bu = base_url.rstrip("/")
    to = _coerce_timeout(timeout)
    with _http_client(to) as client:
        url = f"{bu}/api/v5/public/mark-price"
        data = _get_json(client, url, {"instType": "SWAP", "instId": inst_id})
    arr = data.get("data") or []
    if not arr or not isinstance(arr[0], dict):
        return None
    try:
        return float(arr[0].get("markPx"))
    except (TypeError, ValueError):
        return None

# ----- okx_rest.py -----

"""
欧易 REST v5：签名 + 现货/合约下单。

同一 OKXRestClient 实例内复用 httpx.Client（连接池 + keep-alive），
请求路径加锁以保证同步 Client 在多线程（asyncio.to_thread）下安全。
"""

import base64
import hashlib
import hmac
import json
import logging
import random
import threading
import time as time_mod
from datetime import datetime, timezone
from typing import Any

import httpx

from quant.settings import (
    OKX_API_KEY,
    OKX_PASSPHRASE,
    OKX_REST_HTTP_PROXY,
    OKX_REST_URL,
    OKX_REST_MAX_ATTEMPTS,
    OKX_REST_RETRY_BASE_SEC,
    OKX_SECRET_KEY,
)

_rest_log = logging.getLogger(__name__ + ".rest")


# 本地时钟与 OKX 服务器时间的偏移量（秒），正值=本地超前，负值=本地滞后
# 当收到 401 Timestamp expired 时自动校准，减少后续签名失败
_clock_offset_sec: float = 0.0


def _iso_timestamp_ms() -> str:
    """生成签名用 ISO 时间戳，叠加已知的时钟偏移量。"""
    t = datetime.now(timezone.utc).timestamp() - _clock_offset_sec
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sync_clock_from_okx(base_url: str, proxy: str | None = None) -> None:
    """
    从 OKX 公共接口获取服务器时间，校准本地时钟偏移。
    仅在 401 时钟漂移时调用，失败不影响主流程。
    """
    global _clock_offset_sec
    try:
        kw: dict[str, Any] = {"timeout": httpx.Timeout(5.0)}
        if proxy:
            kw["proxy"] = proxy
        with httpx.Client(**kw) as c:
            r = c.get(f"{base_url.rstrip('/')}/api/v5/public/time")
            data = r.json()
        server_ms = int(data["data"][0]["ts"])
        local_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        _clock_offset_sec = (local_ms - server_ms) / 1000.0
        _rest_log.warning(
            "[clock] 时钟同步完成：本地超前 OKX %.3fs，后续签名已修正",
            _clock_offset_sec,
        )
    except Exception as e:
        _rest_log.warning("[clock] 时钟同步失败（忽略）: %s", e)


def sign(secret: str, ts: str, method: str, path: str, body: str) -> str:
    pre = ts + method.upper() + path + body
    mac = hmac.new(
        secret.encode("utf-8"),
        pre.encode("utf-8"),
        hashlib.sha256,
    )
    return base64.b64encode(mac.digest()).decode("utf-8")


class OKXRestClient:
    """长生命周期 REST 客户端：内部共享连接池，显著降低重复建连开销。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        secret_key: str | None = None,
        passphrase: str | None = None,
        simulated: bool | None = None,
    ) -> None:
        self.base_url = (base_url or OKX_REST_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else OKX_API_KEY
        self.secret_key = secret_key if secret_key is not None else OKX_SECRET_KEY
        self.passphrase = passphrase if passphrase is not None else OKX_PASSPHRASE
        # 已移除模拟盘：仅实盘 REST（见项目瘦身）
        self.simulated = False if simulated is None else bool(simulated)
        self._http: httpx.Client | None = None
        self._http_lock = threading.Lock()

    def close(self) -> None:
        """释放连接池；会话结束或短生命周期用完客户端时应调用。"""
        with self._http_lock:
            if self._http is not None:
                try:
                    self._http.close()
                finally:
                    self._http = None

    def __enter__(self) -> OKXRestClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _headers(self, method: str, path: str, body: str) -> dict[str, str]:
        ts = _iso_timestamp_ms()
        sig = sign(self.secret_key, ts, method, path, body)
        h: dict[str, str] = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        return h

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """签名 REST；429/5xx/SSL 类瞬断自动退避重试（每轮刷新时间戳）。"""
        body_str = "" if body is None else json.dumps(body, separators=(",", ":"))
        url = f"{self.base_url}{path}"
        max_a = max(1, int(OKX_REST_MAX_ATTEMPTS))
        base = float(OKX_REST_RETRY_BASE_SEC)
        _transient_http = (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.ProxyError,
        )
        last_exc: BaseException | None = None
        for attempt in range(max_a):
            headers = self._headers(method, path, body_str)
            try:
                with self._http_lock:
                    if self._http is None:
                        self._http = httpx.Client(**okx_http_client_kwargs())
                    http = self._http
                    if method.upper() == "GET":
                        r = http.request(method, url, headers=headers)
                    else:
                        r = http.request(method, url, content=body_str, headers=headers)
            except _transient_http as e:
                last_exc = e
                if attempt + 1 >= max_a:
                    raise RuntimeError(f"OKX REST 网络失败: {e}") from e
                delay = min(8.0, base * (2**attempt) + random.random() * 0.12)
                time_mod.sleep(delay)
                continue
            try:
                data = r.json()
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"非 JSON 响应 status={r.status_code} body={r.text[:500]}"
                ) from e
            if r.status_code == 429:
                last_exc = RuntimeError(f"HTTP 429: {data}")
                ra = r.headers.get("Retry-After")
                try:
                    wait_s = float(ra) if ra else min(8.0, base * (2**attempt))
                except (TypeError, ValueError):
                    wait_s = min(8.0, base * (2**attempt))
                if attempt + 1 >= max_a:
                    raise last_exc
                time_mod.sleep(min(12.0, wait_s + random.random() * 0.08))
                continue
            if r.status_code >= 500:
                last_exc = RuntimeError(f"HTTP {r.status_code}: {data}")
                if attempt + 1 >= max_a:
                    raise last_exc
                time_mod.sleep(min(8.0, base * (2**attempt) + random.random() * 0.05))
                continue
            if r.status_code == 401:
                # OKX error 50113 = Timestamp request expired（本地时钟漂移）
                okx_code = str(data.get("code", ""))
                if okx_code == "50113" and attempt + 1 < max_a:
                    _rest_log.warning("[clock] HTTP 401 时间戳过期，尝试时钟同步后重试 (%d/%d)", attempt + 1, max_a)
                    _sync_clock_from_okx(self.base_url, OKX_REST_HTTP_PROXY or None)
                    continue
                raise RuntimeError(f"HTTP 401: {data}")
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {data}")
            if str(data.get("code", "0")) != "0":
                raise RuntimeError(f"OKX API 错误: {data}")
            return data
        raise RuntimeError(f"OKX REST 失败: {last_exc}")

    def balance(self, ccy: str | None = None) -> dict[str, Any]:
        path = "/api/v5/account/balance"
        if ccy:
            path += f"?ccy={ccy}"
        return self.request("GET", path, None)

    def trade_fee_swap(self, inst_id: str) -> dict[str, Any]:
        """GET /api/v5/account/trade-fee — 合约 maker/taker 费率。"""
        family = inst_id.replace("-SWAP", "")
        path = f"/api/v5/account/trade-fee?instType=SWAP&instFamily={family}"
        return self.request("GET", path, None)

    def set_leverage(
        self,
        *,
        inst_id: str,
        lever: float,
        mgn_mode: str = "isolated",
        pos_side: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "instId": inst_id,
            "lever": str(lever),
            "mgnMode": mgn_mode,
        }
        if pos_side:
            body["posSide"] = pos_side
        return self.request("POST", "/api/v5/account/set-leverage", body)

    def place_order_swap(
        self,
        *,
        inst_id: str,
        side: str,
        sz: str,
        px: str | None = None,
        ord_type: str = "limit",
        td_mode: str = "isolated",
        pos_side: str | None = None,
        reduce_only: bool = False,
        cl_ord_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": sz,
        }
        if ord_type in ("limit", "post_only", "ioc", "fok"):
            if not px:
                raise ValueError(f"{ord_type} 需要 px")
            body["px"] = px
        if pos_side:
            body["posSide"] = pos_side
        if reduce_only:
            body["reduceOnly"] = "true"
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        return self.request("POST", "/api/v5/trade/order", body)

    def orders_pending(self, inst_id: str) -> dict[str, Any]:
        if not str(inst_id).upper().endswith("-SWAP"):
            raise ValueError("orders_pending 仅支持线性永续 instId（*-SWAP）")
        path = f"/api/v5/trade/orders-pending?instType=SWAP&instId={inst_id}"
        return self.request("GET", path, None)

    def cancel_order(
        self,
        *,
        inst_id: str,
        ord_id: str,
        td_mode: str = "cash",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "ordId": ord_id,
        }
        return self.request("POST", "/api/v5/trade/cancel-order", body)

    def fills_swap(
        self,
        inst_id: str,
        *,
        begin_ms: int | None = None,
        end_ms: int | None = None,
        limit: str = "100",
        after: str | None = None,
    ) -> dict[str, Any]:
        q = f"instType=SWAP&instId={inst_id}&limit={limit}"
        if begin_ms is not None:
            q += f"&begin={begin_ms}"
        if end_ms is not None:
            q += f"&end={end_ms}"
        if after:
            q += f"&after={after}"
        return self.request("GET", f"/api/v5/trade/fills?{q}", None)

    def positions_swap(self, inst_id: str) -> dict[str, Any]:
        path = f"/api/v5/account/positions?instType=SWAP&instId={inst_id}"
        return self.request("GET", path, None)

    def account_bills(
        self,
        *,
        inst_type: str | None = None,
        inst_id: str | None = None,
        ccy: str | None = None,
        bill_type: str | None = None,
        begin: str | None = None,
        end: str | None = None,
        after: str | None = None,
        before: str | None = None,
        limit: str = "100",
    ) -> dict[str, Any]:
        """
        GET /api/v5/account/bills（近 7 日账单）。
        bill_type：如 \"8\" 为资金费（见 Get bill types）。
        begin/end：毫秒时间戳字符串。
        """
        parts: list[str] = []
        if inst_type:
            parts.append(f"instType={inst_type}")
        if inst_id:
            parts.append(f"instId={inst_id}")
        if ccy:
            parts.append(f"ccy={ccy}")
        if bill_type is not None:
            parts.append(f"type={bill_type}")
        if begin is not None:
            parts.append(f"begin={begin}")
        if end is not None:
            parts.append(f"end={end}")
        if after:
            parts.append(f"after={after}")
        if before:
            parts.append(f"before={before}")
        parts.append(f"limit={limit}")
        q = "&".join(parts)
        return self.request("GET", f"/api/v5/account/bills?{q}", None)


def ensure_keys() -> None:
    if not (OKX_API_KEY and OKX_SECRET_KEY and OKX_PASSPHRASE):
        raise SystemExit(
            "缺少环境变量：请设置 OKX_API_KEY、OKX_SECRET_KEY、OKX_PASSPHRASE（可写入 .env 后 source）"
        )

# ----- ws_public.py -----

"""
欧易公共 WebSocket（行情侧，无需 API Key）：

- 订阅 channel=tickers，持续收到 last / bidPx / askPx。
- 服务端可能发文本 "ping"，必须回 "pong"，否则会被断开。
- 断线后指数退避重连，避免打满连接。

下游（runner）用这些 tick 驱动策略 on_tick。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import websockets


apply_websockets_connection_lost_patch()

from quant.settings import OKX_WS_DIRECT, OKX_WS_PROXY

log = logging.getLogger(__name__)

# 首次失败时打一次完整栈，避免刷屏；之后只打摘要
_WS_FIRST_FAIL: bool = True
# 排查长提示只打一次，避免刷屏
_WS_HINT_LOGGED: bool = False
# 代理解析说明只打一次（websockets 在「自动」模式下可能得到无代理→直连）
_PROXY_DIAG_LOGGED: bool = False
# 本机代理端口拒绝连接（errno 61 等）的长提示只打一次
_PROXY_REFUSED_HINT_LOGGED: bool = False
# 已设 OKX_WS_PROXY 仍 TLS/对端 reset 的长提示只打一次
_TLS_THROUGH_PROXY_HINT_LOGGED: bool = False


def _mask_proxy_url(url: str) -> str:
    """本地代理 URL 打日志用；若含账号密码则打码。"""
    try:
        from urllib.parse import urlparse

        p = urlparse(url)
        if p.username is not None:
            port = f":{p.port}" if p.port else ""
            return f"{p.scheme}://***@{p.hostname or ''}{port}"
        return url
    except Exception:
        return url


def _log_proxy_diag_once(sample_wss_url: str) -> None:
    """说明 websockets 实际会不会走代理（IDE 无环境变量时常为「无」→直连）。"""
    global _PROXY_DIAG_LOGGED
    if _PROXY_DIAG_LOGGED or OKX_WS_DIRECT:
        return
    _PROXY_DIAG_LOGGED = True
    if OKX_WS_PROXY:
        log.info(
            "[行情][WS] 使用显式代理 OKX_WS_PROXY=%s",
            _mask_proxy_url(OKX_WS_PROXY),
        )
        return
    try:
        from websockets.proxy import get_proxy
        from websockets.uri import parse_uri

        auto = get_proxy(parse_uri(sample_wss_url))
        log.info(
            "[行情][WS] 代理解析（仅首次，样本 url=%s）："
            "websockets 自动 get_proxy → %s。"
            "若为「无」则当前进程在直连欧易，易出现 TLS handshake 后 reset；"
            "请在 .env 增加 OKX_WS_PROXY=http://127.0.0.1:7890（填你 Clash/V2Ray 的 HTTP 代理端口）",
            sample_wss_url,
            _mask_proxy_url(auto) if auto else "(无 — 将直连)",
        )
    except Exception as ex:
        log.debug("[行情][WS] 代理解析跳过: %s", ex)


def _normalize_ws_urls(ws_urls: str | Sequence[str]) -> list[str]:
    if isinstance(ws_urls, str):
        u = ws_urls.strip()
        return [u] if u else []
    return [x.strip() for x in ws_urls if str(x).strip()]


def _is_peer_reset(e: BaseException) -> bool:
    if isinstance(e, ConnectionResetError):
        return True
    if isinstance(e, OSError):
        en = getattr(e, "errno", None)
        # 54 macOS, 104 Linux ECONNRESET, 10054 Windows WSAECONNRESET
        if en in (54, 104, 10054):
            return True
    return False


def _is_local_connection_refused(e: BaseException) -> bool:
    """连本机代理端口失败（未启动或端口填错）。"""
    if isinstance(e, ConnectionRefusedError):
        return True
    if isinstance(e, OSError):
        en = getattr(e, "errno", None)
        # 61 macOS, 111 Linux ECONNREFUSED, 10061 Windows WSAECONNREFUSED
        if en in (61, 111, 10061):
            return True
    return False


async def stream_tickers(
    ws_urls: str | Sequence[str], inst_id: str
) -> AsyncIterator[dict[str, Any]]:
    """持续产出单条 ticker 数据 dict（含 last / bidPx / askPx / ts）。"""
    urls = _normalize_ws_urls(ws_urls)
    if not urls:
        raise ValueError("ws_urls 不能为空")
    url_idx = 0
    sub = {"op": "subscribe", "args": [{"channel": "tickers", "instId": inst_id}]}
    backoff = 1.0
    if OKX_WS_DIRECT:
        log.info(
            "[行情][WS] OKX_WS_DIRECT=1：已临时忽略 HTTP/SOCKS 代理环境变量，直连欧易 WS"
        )
    else:
        log.info(
            "[行情][WS] OKX_WS_DIRECT=0：公共 WS 走代理"
            "（显式 OKX_WS_PROXY，或 HTTPS_PROXY/ALL_PROXY 等自动解析）"
        )
        _log_proxy_diag_once(urls[0])

    while True:
        current_url = urls[url_idx]
        saved_proxy: dict[str, str] = {}
        try:
            if OKX_WS_DIRECT:
                saved_proxy = pop_proxy_env()
            try:
                # websockets>=11 默认 proxy=True 会读系统代理；OKX_WS_DIRECT=1 时强制不走代理
                # ping_interval=None：禁用 websockets 库的 binary ping/pong 帧。
                # HTTP 代理通常不转发 binary WS ping，导致 keepalive ping timeout。
                # OKX 使用应用层 text ping/pong（服务端发 "ping"→客户端回 "pong"），
                # 我们也主动每 25s 发一次 "ping" 维持连接。
                conn_kw: dict[str, Any] = {
                    "ping_interval": None,
                    "ping_timeout": None,
                    "close_timeout": 10,
                    "open_timeout": 30,
                }
                if OKX_WS_DIRECT:
                    conn_kw["proxy"] = None
                else:
                    # 显式 URL 优先；否则 True=从环境解析（可能得到 None→直连）
                    conn_kw["proxy"] = OKX_WS_PROXY if OKX_WS_PROXY else True
                async with websockets.connect(current_url, **conn_kw) as ws:
                    await ws.send(json.dumps(sub))
                    log.info(
                        "[行情][WS] 已订阅 tickers | instId=%s | url=%s",
                        inst_id,
                        current_url,
                    )
                    backoff = 1.0
                    _last_ping_ts = asyncio.get_event_loop().time()
                    _HEARTBEAT_INTERVAL = 25.0  # OKX 要求 30s 内至少发一次 ping
                    async for raw in ws:
                        # 主动心跳：每 25s 发一次 text "ping"
                        _now = asyncio.get_event_loop().time()
                        if _now - _last_ping_ts >= _HEARTBEAT_INTERVAL:
                            await ws.send("ping")
                            _last_ping_ts = _now
                        if isinstance(raw, str) and raw.strip() in ("ping", "pong"):
                            if raw.strip() == "ping":
                                await ws.send("pong")
                            continue
                        if not isinstance(raw, str):
                            continue
                        msg = json.loads(raw)
                        if msg.get("event") == "error":
                            log.error("[行情][WS] 通道错误: %s", msg)
                            continue
                        if msg.get("event") == "subscribe":
                            continue
                        arg = msg.get("arg") or {}
                        if arg.get("channel") == "tickers" and msg.get("data"):
                            yield msg["data"][0]
            finally:
                restore_proxy_env(saved_proxy)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            global _WS_FIRST_FAIL, _WS_HINT_LOGGED, _PROXY_REFUSED_HINT_LOGGED
            global _TLS_THROUGH_PROXY_HINT_LOGGED
            detail = format_ws_exception(e)
            log.warning(
                "[行情][WS] 连接中断: %s | %.1fs 后重连（退避上限 30s）",
                detail,
                backoff,
            )
            if _WS_FIRST_FAIL:
                _WS_FIRST_FAIL = False
                log.error(
                    "[行情][WS] 首次失败详情（含堆栈，便于排查网络/代理/SSL）\n%s",
                    traceback_tail(e),
                )
            if _is_peer_reset(e) and len(urls) > 1:
                url_idx = (url_idx + 1) % len(urls)
                log.info(
                    "[行情][WS] 对端重置连接，切换下一 WS 地址: %s",
                    urls[url_idx],
                )
            proxy_refused = bool(OKX_WS_PROXY) and _is_local_connection_refused(e)
            if proxy_refused and not _PROXY_REFUSED_HINT_LOGGED:
                _PROXY_REFUSED_HINT_LOGGED = True
                log.warning(
                    "[行情][WS] 本机代理端口拒绝连接（连不上 %s）："
                    "该地址上当前没有进程监听。请 ① 打开并运行 Clash / V2Ray / Surge 等；"
                    "② 在客户端「端口设置」里查看**混合端口 / HTTP 端口**（7890 只是常见示例，"
                    "你机器上可能是别的数字），把 .env 里 OKX_WS_PROXY 改成一致；"
                    "③ 若只开了 SOCKS 未开 HTTP，请用 OKX_WS_PROXY=socks5h://127.0.0.1:端口"
                    "（需已 pip install python-socks）。",
                    _mask_proxy_url(OKX_WS_PROXY),
                )
            elif (
                bool(OKX_WS_PROXY)
                and _is_peer_reset(e)
                and not _TLS_THROUGH_PROXY_HINT_LOGGED
            ):
                _TLS_THROUGH_PROXY_HINT_LOGGED = True
                _WS_HINT_LOGGED = True
                log.warning(
                    "[行情][WS] 排查建议（代理已通，但 TLS/对端仍 reset）："
                    "部分 HTTP 代理对目标端口 8443 的 CONNECT+TLS 不稳定。"
                    "可试 ① 将 OKX_WS_PROXY 改为 SOCKS（Clash「SOCKS 端口」），例如 "
                    "socks5h://127.0.0.1:7898（端口以你客户端为准），需 python-socks；"
                    "② Clash 开启 TUN / 增强模式，让流量系统级走代理；"
                    "③ 换可用节点。堆栈若含 start_tls，多为握手阶段被中断。"
                )
            elif "SOCKS" in detail or "python-socks" in detail:
                log.warning(
                    "[行情][WS] 提示：SOCKS 代理需安装 python-socks（pip install -r requirements.txt）；"
                    "若本机用代理上网，请保持 OKX_WS_DIRECT=0"
                )
            elif not proxy_refused and not _WS_HINT_LOGGED:
                _WS_HINT_LOGGED = True
                if OKX_WS_DIRECT:
                    log.warning(
                        "[行情][WS] 排查建议（直连失败）：若你平时靠本机代理软件上网，"
                        "请把 .env 里 OKX_WS_DIRECT 改为 0，并在终端/系统里设置 "
                        "ALL_PROXY 或 HTTPS_PROXY 指向代理（如 http://127.0.0.1:7890）；"
                        "SOCKS 需 python-socks。也可试 OKX_WS_PUBLIC_URL 备用 "
                        "wss://wsaws.okx.com:8443/ws/v5/public"
                    )
                elif OKX_WS_PROXY:
                    log.warning(
                        "[行情][WS] 排查建议（已设 OKX_WS_PROXY 仍失败）："
                        "核对代理类型与端口；试 socks5h://、TUN、换节点。"
                        "若仅为偶发超时，可忽略或增大 open_timeout。"
                    )
                else:
                    log.warning(
                        "[行情][WS] 排查建议（走代理仍失败）：在 .env 设 "
                        "OKX_WS_PROXY=http://127.0.0.1:端口（本机 Clash HTTP/混合端口），"
                        "避免 IDE 子进程未继承终端导致 get_proxy 为空、实际直连。"
                        "SOCKS 用 socks5h://127.0.0.1:端口 且需 python-socks。"
                    )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30.0)
