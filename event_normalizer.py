"""Normalization helpers for inbound MT4/MT5 trade-event payloads."""

from typing import Any, Dict


def _as_int(value: Any, default=None):
    try:
        return int(float(value))
    except Exception:
        return default


def _as_float(value: Any, default=None):
    try:
        return float(value)
    except Exception:
        return default


def normalize_trade_event(data: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(data or {})

    # ------------------------------------------------------------
    # event_type
    # Accept:
    # - event_type
    # - action
    # - event
    # Normalize aliases to one internal schema
    # ------------------------------------------------------------
    raw_event = (
        d.get("event_type")
        or d.get("action")
        or d.get("event")
        or ""
    )
    raw_event = str(raw_event).strip().upper().replace("-", "_").replace(" ", "_")

    event_aliases = {
        "OPEN": "OPEN",
        "MODIFY": "MODIFY",
        "CLOSE": "CLOSE",

        "PENDING_OPEN": "PENDING_OPEN",
        "PENDINGOPEN": "PENDING_OPEN",

        "PENDING_CLOSE": "PENDING_CANCEL",
        "PENDINGCLOSE": "PENDING_CANCEL",
        "PENDING_CANCEL": "PENDING_CANCEL",
        "PENDINGCANCEL": "PENDING_CANCEL",
        "PENDING_DELETE": "PENDING_CANCEL",
        "PENDINGDELETE": "PENDING_CANCEL",
        "ORDER_DELETE": "PENDING_CANCEL",
        "ORDERDELETE": "PENDING_CANCEL",
    }
    d["event_type"] = event_aliases.get(raw_event, raw_event)

    # ------------------------------------------------------------
    # side
    # ------------------------------------------------------------
    if "side" not in d and "type" in d:
        d["side"] = d["type"]

    if "side" in d and d["side"] is not None:
        d["side"] = str(d["side"]).strip().upper()

    # ------------------------------------------------------------
    # pending_type
    # Expected downstream:
    # limit | stop | stop_limit
    # ------------------------------------------------------------
    raw_pt = (
        d.get("pending_type")
        or d.get("order_type")
        or d.get("pending_order_type")
        or d.get("type_name")
        or ""
    )
    raw_pt = str(raw_pt).strip().upper().replace("-", "_").replace(" ", "_")

    pending_aliases = {
        "LIMIT": "limit",
        "STOP": "stop",
        "STOP_LIMIT": "stop_limit",
        "STOPLIMIT": "stop_limit",

        "BUY_LIMIT": "limit",
        "SELL_LIMIT": "limit",
        "BUYLIMIT": "limit",
        "SELLLIMIT": "limit",
        "OP_BUYLIMIT": "limit",
        "OP_SELLLIMIT": "limit",

        "BUY_STOP": "stop",
        "SELL_STOP": "stop",
        "BUYSTOP": "stop",
        "SELLSTOP": "stop",
        "OP_BUYSTOP": "stop",
        "OP_SELLSTOP": "stop",

        "BUY_STOP_LIMIT": "stop_limit",
        "SELL_STOP_LIMIT": "stop_limit",
        "BUYSTOPLIMIT": "stop_limit",
        "SELLSTOPLIMIT": "stop_limit",
    }

    if raw_pt:
        d["pending_type"] = pending_aliases.get(raw_pt, raw_pt.lower())

    # ------------------------------------------------------------
    # basic numeric cleanup
    # ------------------------------------------------------------
    for key in ("ticket", "magic", "expiration_ms"):
        if key in d:
            d[key] = _as_int(d.get(key), d.get(key))

    for key in (
        "volume",
        "lots",
        "sl",
        "tp",
        "price",
        "open_price",
        "entry_price",
        "stop_price",
        "limit_price",
        "mt5_contract_size",
        "mt5_volume_min",
        "mt5_volume_step",
    ):
        if key in d:
            d[key] = _as_float(d.get(key), d.get(key))

    # ------------------------------------------------------------
    # fallback price normalization for pending orders
    # ------------------------------------------------------------
    if "entry_price" not in d:
        if d.get("price") is not None:
            d["entry_price"] = d.get("price")
        elif d.get("open_price") is not None:
            d["entry_price"] = d.get("open_price")

    pt = str(d.get("pending_type") or "").lower()

    if pt == "limit" and d.get("limit_price") is None:
        if d.get("entry_price") is not None:
            d["limit_price"] = d.get("entry_price")

    if pt == "stop" and d.get("stop_price") is None:
        if d.get("entry_price") is not None:
            d["stop_price"] = d.get("entry_price")

    if pt == "stop_limit":
        if d.get("stop_price") is None and d.get("entry_price") is not None:
            d["stop_price"] = d.get("entry_price")
        if d.get("limit_price") is None and d.get("entry_price") is not None:
            d["limit_price"] = d.get("entry_price")

    return d
