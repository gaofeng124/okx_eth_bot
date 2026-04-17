"""
OKX 私有 REST 常见错误码 / 文案片段（用于分支，不替代完整 API 文档）。

说明：交易所可能调整文案；此处同时匹配 sCode/数字码与英文片段，并保留子串兜底。
"""
from __future__ import annotations

import re


# 常见 sCode（见 OKX 文档；以字符串形式出现在异常或响应体中）
CODE_INSUFFICIENT_MARGIN = "51008"
CODE_NO_POSITION_REDUCE = "51169"


def error_text(err: BaseException) -> str:
    """统一取可比较的异常文本（含类型名）。"""
    return f"{type(err).__name__}: {err}"


def _has_code(s: str, code: str) -> bool:
    return code in s


def is_insufficient_margin_error(err: BaseException) -> bool:
    s = error_text(err)
    if _has_code(s, CODE_INSUFFICIENT_MARGIN):
        return True
    low = s.lower()
    if "available usdt balance is insufficient" in low:
        return True
    if "available margin" in low and "insufficient" in low:
        return True
    if "balance" in low and "insufficient" in low and "margin" in low:
        return True
    return False


def is_no_position_reduce_error(err: BaseException) -> bool:
    s = error_text(err)
    if _has_code(s, CODE_NO_POSITION_REDUCE):
        return True
    low = s.lower()
    return "don't have any positions in this direction" in low


def is_posside_parameter_error(err: BaseException) -> bool:
    s = error_text(err)
    if "Parameter posSide error" in s:
        return True
    low = s.lower()
    if "posside" not in low and "pos_side" not in low:
        return False
    return any(
        w in low
        for w in ("error", "invalid", "reject", "mismatch", "not support", "illegal")
    )


# 响应 JSON 中 sCode 提取（若 place_order 封装把 body 放进异常）
_SCODE_RE = re.compile(r'"sCode"\s*:\s*"(\d+)"')


def response_scode_from_error(err: BaseException) -> str | None:
    m = _SCODE_RE.search(error_text(err))
    return m.group(1) if m else None
