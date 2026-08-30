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


def _as_bool(value: Any, default=None):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    try:
        raw = str(value).strip().lower()
    except Exception:
        return default
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    return default


def _first_present(d: Dict[str, Any], *keys, default=None):
    for key in keys:
        if key in d and d.get(key) is not None:
            return d.get(key)
    return default


def normalize_trade_event(data: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(data or {})

    # ------------------------------------------------------------
    # event_type
    # Accept:
    # - event_type
    # - eventType
    # - action
    # - event
    # Normalize aliases to one internal schema
    # ------------------------------------------------------------
    raw_event = (
        d.get("event_type")
        or d.get("eventType")
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

        "PENDING_SNAPSHOT": "PENDING_OPEN",
        "PENDINGSNAPSHOT": "PENDING_OPEN",

        "PENDING_MODIFY": "PENDING_MODIFY",
        "PENDINGMODIFY": "PENDING_MODIFY",
        "PENDING_UPDATE": "PENDING_MODIFY",
        "PENDINGUPDATE": "PENDING_MODIFY",

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
    d["eventtype"] = d["event_type"]

    # ------------------------------------------------------------
    # side
    # ------------------------------------------------------------
    if "side" not in d:
        inferred_side = _first_present(d, "type", "order_side", "orderSide")
        if inferred_side is not None:
            d["side"] = inferred_side

    if "side" in d and d["side"] is not None:
        d["side"] = str(d["side"]).strip().upper()

    # ------------------------------------------------------------
    # pending_type
    # Expected downstream:
    # limit | stop | stop_limit
    # ------------------------------------------------------------
    raw_pt = (
        d.get("pending_type")
        or d.get("pendingType")
        or d.get("order_type")
        or d.get("orderType")
        or d.get("pending_order_type")
        or d.get("pendingOrderType")
        or d.get("type_name")
        or d.get("typeName")
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

    if "pending_type" in d:
        d["pendingtype"] = d["pending_type"]

    # ------------------------------------------------------------
    # unify alternate key names before numeric cleanup
    # ------------------------------------------------------------
    alias_map = {
        "ticket": ("ticket",),
        "magic": ("magic",),
        "expiration_ms": ("expiration_ms", "expirationMs", "expirationms"),

        "volume": ("volume", "lots"),
        "sl": ("sl", "stop_loss", "stopLoss"),
        "tp": ("tp", "take_profit", "takeProfit"),

        "price": ("price",),
        "open_price": ("open_price", "openPrice"),
        "entry_price": ("entry_price", "entryPrice", "entryprice"),
        "stop_price": ("stop_price", "stopPrice", "stopprice"),
        "limit_price": ("limit_price", "limitPrice", "limitprice"),

        "mt5_contract_size": ("mt5_contract_size", "mt5ContractSize", "mt5contractsize"),
        "mt5_volume_min": ("mt5_volume_min", "mt5VolumeMin", "mt5volumemin"),
        "mt5_volume_max": ("mt5_volume_max", "mt5VolumeMax", "mt5volumemax"),
        "mt5_volume_step": ("mt5_volume_step", "mt5VolumeStep", "mt5volumestep"),
        "mt5_tick_size": ("mt5_tick_size", "mt5TickSize", "mt5ticksize", "tick_size", "tickSize"),
        "mt5_tick_value": ("mt5_tick_value", "mt5TickValue", "mt5tickvalue", "tick_value", "tickValue"),
        "point": ("point", "Point"),
        "digits": ("digits", "Digits"),
    }

    for canonical_key, aliases in alias_map.items():
        value = _first_present(d, *aliases, default=None)
        if value is not None and canonical_key not in d:
            d[canonical_key] = value

    # ------------------------------------------------------------
    # basic numeric cleanup
    # ------------------------------------------------------------
    for key in ("ticket", "magic", "expiration_ms", "digits"):
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
        "mt5_volume_max",
        "mt5_volume_step",
        "mt5_tick_size",
        "mt5_tick_value",
        "point",
    ):
        if key in d:
            d[key] = _as_float(d.get(key), d.get(key))

    # ------------------------------------------------------------
    # startup/recovery flags
    # ------------------------------------------------------------
    for key in ("startupSync", "startupRecovery", "isStartupSync", "recovery"):
        if key in d:
            d[key] = _as_bool(d.get(key), d.get(key))

    # ------------------------------------------------------------
    # fallback price normalization for pending orders
    # ------------------------------------------------------------
    if "entry_price" not in d or d.get("entry_price") is None:
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

    # ------------------------------------------------------------
    # add downstream-friendly aliases used elsewhere in the project
    # ------------------------------------------------------------
    if "entry_price" in d:
        d["entryprice"] = d["entry_price"]
    if "stop_price" in d:
        d["stopprice"] = d["stop_price"]
    if "limit_price" in d:
        d["limitprice"] = d["limit_price"]
    if "open_price" in d:
        d["openprice"] = d["open_price"]
    if "expiration_ms" in d:
        d["expirationms"] = d["expiration_ms"]
    if "mt5_contract_size" in d:
        d["mt5contractsize"] = d["mt5_contract_size"]
    if "mt5_volume_min" in d:
        d["mt5volumemin"] = d["mt5_volume_min"]
    if "mt5_volume_max" in d:
        d["mt5volumemax"] = d["mt5_volume_max"]
    if "mt5_volume_step" in d:
        d["mt5volumestep"] = d["mt5_volume_step"]
    if "mt5_tick_size" in d:
        d["mt5ticksize"] = d["mt5_tick_size"]
    if "mt5_tick_value" in d:
        d["mt5tickvalue"] = d["mt5_tick_value"]

    return d
