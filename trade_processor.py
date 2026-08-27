"""
Trade event processing and handling logic.
Processes incoming MT5 trade events and routes them to appropriate handlers.
Supports one MT5 magic -> multiple cTrader destination accounts.
"""

import time
from threading import Lock

from app_state import (
    logger,
    PENDING_SLTP,
    MASTER_OPEN_LOTS,
    MASTER_CLOSED_LOTS,
    alert_trade_failure,
    alert_trade_warning,
    alert_trade_info,
)
from trade_executor import copy_open_to_account, copy_pending_to_account
from symbol_mapper import SymbolMapper


_PENDING_SLTP_MAX_AGE_MS = 5 * 60 * 1000
_PENDING_SLTP_BASE_RETRY_MS = 350
_PENDING_SLTP_MAX_RETRY_MS = 5000
_PENDING_SLTP_MAX_ATTEMPTS = 12

_PENDING_SLTP_LOCK = Lock()
_MASTER_LOTS_LOCK = Lock()


def _build_account_symbol_mapper(client, config) -> SymbolMapper:
    return SymbolMapper(
        prefix=getattr(config, "symbol_prefix", ""),
        suffix=getattr(config, "symbol_suffix", ""),
        custom_map=getattr(config, "custom_symbols", {}),
        broker_symbol_map=getattr(client, "symbol_name_to_id", {}),
        strict=True,
    )


def _get_symbol_id_for_account(client, config, mt5_symbol: str):
    try:
        mapper = _build_account_symbol_mapper(client, config)
        return mapper.get_symbol_id(mt5_symbol)
    except Exception:
        return None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def _to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _to_float_or_none(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    return default


def _pending_sltp_bucket(account_name: str) -> dict:
    with _PENDING_SLTP_LOCK:
        return PENDING_SLTP.setdefault(str(account_name), {})


def _next_pending_retry_delay_ms(attempts: int) -> int:
    attempts = max(0, int(attempts))
    delay = _PENDING_SLTP_BASE_RETRY_MS * (2 ** attempts)
    return min(delay, _PENDING_SLTP_MAX_RETRY_MS)


def _set_pending_sltp(account_name: str, ticket: int, symbol: str, sl: float, tp: float):
    ticket = int(ticket)
    now_ms = _now_ms()
    with _PENDING_SLTP_LOCK:
        bucket = PENDING_SLTP.setdefault(str(account_name), {})
        existing = bucket.get(ticket, {})
        bucket[ticket] = {
            "symbol": symbol,
            "sl": float(sl or 0.0),
            "tp": float(tp or 0.0),
            "created_ms": existing.get("created_ms", now_ms),
            "updated_ms": now_ms,
            "attempts": 0,
            "next_retry_ms": now_ms,
            "last_error": None,
            "last_position_id": existing.get("last_position_id"),
        }


def _get_pending_sltp(account_name: str, ticket: int):
    with _PENDING_SLTP_LOCK:
        return PENDING_SLTP.setdefault(str(account_name), {}).get(int(ticket))


def _clear_pending_sltp(account_name: str, ticket: int):
    with _PENDING_SLTP_LOCK:
        PENDING_SLTP.setdefault(str(account_name), {}).pop(int(ticket), None)


def _touch_pending_sltp_retry(account_name: str, ticket: int, error: str = None, position_id=None):
    with _PENDING_SLTP_LOCK:
        pending = PENDING_SLTP.setdefault(str(account_name), {}).get(int(ticket))
        if not pending:
            return

        attempts = int(pending.get("attempts", 0) or 0) + 1
        delay_ms = _next_pending_retry_delay_ms(attempts - 1)
        pending["attempts"] = attempts
        pending["next_retry_ms"] = _now_ms() + delay_ms
        pending["last_error"] = error
        pending["updated_ms"] = _now_ms()
        if position_id:
            pending["last_position_id"] = int(position_id)


def _pending_sltp_expired(pending: dict) -> bool:
    created_ms = _to_int(pending.get("created_ms", 0), 0)
    if created_ms <= 0:
        return False
    return (_now_ms() - created_ms) > _PENDING_SLTP_MAX_AGE_MS


def _pending_sltp_due(pending: dict) -> bool:
    return _now_ms() >= _to_int(pending.get("next_retry_ms", 0), 0)


def _canonical_event_type(data: dict) -> str:
    raw = str(
        data.get("event_type")
        or data.get("action")
        or data.get("event")
        or ""
    ).strip().upper().replace("-", "_").replace(" ", "_")

    aliases = {
        "PENDING_CLOSE": "PENDING_CANCEL",
        "PENDINGCLOSE": "PENDING_CANCEL",
        "PENDING_CANCEL": "PENDING_CANCEL",
        "PENDINGCANCEL": "PENDING_CANCEL",
        "PENDING_DELETE": "PENDING_CANCEL",
        "PENDINGDELETE": "PENDING_CANCEL",
        "ORDER_DELETE": "PENDING_CANCEL",
        "ORDERDELETE": "PENDING_CANCEL",
        "PENDING_OPEN": "PENDING_OPEN",
        "PENDINGOPEN": "PENDING_OPEN",
        "PENDING_MODIFY": "PENDING_MODIFY",
        "PENDINGMODIFY": "PENDING_MODIFY",
        "PENDING_UPDATE": "PENDING_MODIFY",
        "PENDINGUPDATE": "PENDING_MODIFY",
    }
    return aliases.get(raw, raw)


def _canonical_pending_type(data: dict) -> str:
    raw = str(
        data.get("pending_type")
        or data.get("order_type")
        or data.get("pending_order_type")
        or ""
    ).strip().lower().replace("-", "_").replace(" ", "_")

    aliases = {
        "limit": "limit",
        "stop": "stop",
        "stop_limit": "stop_limit",
        "stoplimit": "stop_limit",
        "buy_limit": "limit",
        "sell_limit": "limit",
        "buylimit": "limit",
        "selllimit": "limit",
        "op_buy_limit": "limit",
        "op_sell_limit": "limit",
        "buy_stop": "stop",
        "sell_stop": "stop",
        "buystop": "stop",
        "sellstop": "stop",
        "op_buy_stop": "stop",
        "op_sell_stop": "stop",
        "buy_stop_limit": "stop_limit",
        "sell_stop_limit": "stop_limit",
        "buystoplimit": "stop_limit",
        "sellstoplimit": "stop_limit",
    }
    return aliases.get(raw, raw)


def _lots_to_ctrader_cents(lots: float, mt5_contract_size: float) -> int:
    units = float(lots) * float(mt5_contract_size or 0.0)
    return int(round(units * 100.0))


def _has_valid_sl(sl_value) -> bool:
    try:
        return float(sl_value or 0) > 0
    except Exception:
        return False


def _risk_mode(config) -> str:
    raw = str(getattr(config, "risk_mode", "SOURCE_VOLUME") or "SOURCE_VOLUME")
    raw = raw.split(";", 1)[0].split("#", 1)[0]
    return raw.strip().upper()


def _risk_reference(config) -> str:
    raw = str(getattr(config, "risk_reference", "EQUITY") or "EQUITY")
    raw = raw.split(";", 1)[0].split("#", 1)[0]
    return raw.strip().upper()


def _startup_market_recovery_mode(config) -> str:
    raw = str(getattr(config, "startup_market_recovery_mode", "skip") or "skip")
    raw = raw.split(";", 1)[0].split("#", 1)[0].strip().lower()
    if raw not in ("market", "market_or_pending", "skip"):
        return "skip"
    return raw


def _startup_sync_market_orders_enabled(config) -> bool:
    return bool(getattr(config, "startup_sync_market_orders", False))


def _startup_market_max_distance_pips(config) -> float:
    try:
        v = float(getattr(config, "startup_market_max_distance_pips", 10.0) or 10.0)
        return v if v > 0 else 10.0
    except Exception:
        return 10.0


def _startup_pending_expiration_ms(config) -> int:
    try:
        v = int(float(getattr(config, "startup_pending_expiration_ms", 0) or 0))
        return v if v > 0 else 0
    except Exception:
        return 0


def _get_account_equity_or_balance(account_manager, account_name: str, config) -> float:
    ref = _risk_reference(config)

    try:
        if ref == "BALANCE" and hasattr(account_manager, "get_balance"):
            v = account_manager.get_balance(account_name)
        elif hasattr(account_manager, "get_equity"):
            v = account_manager.get_equity(account_name)
        else:
            v = None
    except Exception:
        v = None

    try:
        return float(v or 0.0)
    except Exception:
        return 0.0


def _get_symbol_details(client, symbol_id: int):
    try:
        return client.symbol_details.get(int(symbol_id)) if hasattr(client, "symbol_details") else None
    except Exception:
        return None


def _read_attr_or_key(obj, name, default=None):
    if obj is None:
        return default
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


def _first_positive_float(*values):
    for value in values:
        try:
            f = float(value)
            if f > 0:
                return f
        except Exception:
            pass
    return None


def _symbol_pip_size(symbol) -> float:
    try:
        pip_pos = _read_attr_or_key(symbol, "pipPosition", None)
        digits = _to_int(_read_attr_or_key(symbol, "digits", 0), 0)
        if pip_pos is not None:
            return float(10 ** (-int(pip_pos)))
        if digits > 0:
            return float(10 ** (-digits))
    except Exception:
        pass
    return 0.0


def _estimate_risk_ccy_per_1lot_from_symbol(symbol, entry_price: float, sl_price: float) -> float:
    try:
        entry = float(entry_price or 0.0)
        sl = float(sl_price or 0.0)
        if entry <= 0 or sl <= 0:
            return 0.0

        dist = abs(entry - sl)
        if dist <= 0:
            return 0.0

        pip_pos = getattr(symbol, "pipPosition", None)
        digits = int(getattr(symbol, "digits", 0) or 0)

        if pip_pos is not None:
            tick_size = 10 ** (-int(pip_pos))
        elif digits > 0:
            tick_size = 10 ** (-digits)
        else:
            return 0.0

        if tick_size <= 0:
            return 0.0

        ticks = dist / float(tick_size)
        if ticks <= 0:
            return 0.0

        tick_value = float(getattr(symbol, "tickValue", 0) or 0.0)
        if tick_value <= 0:
            return 0.0

        return float(ticks) * float(tick_value)
    except Exception:
        return 0.0


def _estimate_risk_ccy_per_1lot_from_mt5(data: dict, entry_price: float, sl_price: float) -> float:
    try:
        entry = float(entry_price or 0.0)
        sl = float(sl_price or 0.0)
        if entry <= 0 or sl <= 0:
            return 0.0

        dist = abs(entry - sl)
        if dist <= 0:
            return 0.0

        tick_size = _first_positive_float(
            data.get("mt5_tick_size"),
            data.get("tick_size"),
            data.get("tickSize"),
            data.get("point"),
            data.get("Point"),
            data.get("trade_tick_size"),
        )

        tick_value = _first_positive_float(
            data.get("mt5_tick_value"),
            data.get("tick_value"),
            data.get("tickValue"),
            data.get("trade_tick_value"),
            data.get("tradeTickValue"),
        )

        if tick_size is not None and tick_value is not None and tick_size > 0 and tick_value > 0:
            ticks = dist / float(tick_size)
            if ticks > 0:
                return float(ticks) * float(tick_value)

        mt5_contract_size = float(data.get("mt5_contract_size", 0) or 0.0)
        quote_to_deposit = _first_positive_float(
            data.get("quote_to_deposit_rate"),
            data.get("quote_to_account_rate"),
            data.get("conversion_rate"),
            data.get("fx_conversion_rate"),
        )

        if mt5_contract_size > 0 and quote_to_deposit is not None and quote_to_deposit > 0:
            return dist * mt5_contract_size * float(quote_to_deposit)

        return 0.0
    except Exception:
        return 0.0


def _enforce_max_risk_on_fill(
    account_name,
    client,
    config,
    account_manager,
    position,
    symbol,
    mt5_symbol=None,
    mt5_data=None,
):
    rm = _risk_mode(config)
    if rm not in ("FIXED_USD", "PERCENT_EQUITY"):
        return

    entry = float(getattr(position, "price", 0) or 0.0)
    sl = float(getattr(position, "stopLoss", 0) or 0.0)
    if entry <= 0 or sl <= 0:
        return

    if not isinstance(mt5_data, dict):
        msg = "Over-risk check skipped: missing mt5_data for strict MT5 risk mode"
        logger.warning(f"[{account_name}] {msg}")
        alert_trade_warning(
            account_name=account_name,
            action="overrisk_check_missing_mt5_data",
            ticket=0,
            message=msg,
            mt5_symbol=mt5_symbol,
        )
        return

    risk_per_1lot = _estimate_risk_ccy_per_1lot_from_mt5(mt5_data, entry, sl)
    logger.info(
        f"[{account_name}] Over-risk calc source=mt5_only, "
        f"symbol={mt5_symbol}, entry={entry:.5f}, sl={sl:.5f}, "
        f"mt5_tick_size={mt5_data.get('mt5_tick_size')}, "
        f"mt5_tick_value={mt5_data.get('mt5_tick_value')}, "
        f"quote_to_deposit_rate={mt5_data.get('quote_to_deposit_rate')}, "
        f"perLot={float(risk_per_1lot):.2f}"
    )
    if risk_per_1lot <= 0:
        msg = "Over-risk check skipped: cannot price MT5 risk strictly"
        logger.warning(f"[{account_name}] {msg}")
        alert_trade_warning(
            account_name=account_name,
            action="overrisk_check_cannot_price_risk",
            ticket=0,
            message=msg,
            mt5_symbol=mt5_symbol,
            entry=entry,
            sl=sl,
        )
        return

    if rm == "FIXED_USD":
        target_risk = float(getattr(config, "fixed_usd_risk", 0) or 0.0)
    else:
        pct = float(getattr(config, "risk_percent", 0) or 0.0)
        ref_amt = _get_account_equity_or_balance(account_manager, account_name, config)
        target_risk = (pct / 100.0) * float(ref_amt) if pct > 0 and ref_amt > 0 else 0.0

    if target_risk <= 0:
        return

    lot_size_cents = float(getattr(symbol, "lotSize", 0) or 0.0)
    follower_units = float(getattr(position.tradeData, "volume", 0) or 0.0)
    if lot_size_cents <= 0 or follower_units <= 0:
        return

    follower_lots = follower_units / lot_size_cents
    actual_risk = follower_lots * risk_per_1lot
    if actual_risk <= target_risk:
        return

    allowed_lots = target_risk / risk_per_1lot
    excess_lots = follower_lots - allowed_lots
    if excess_lots <= 0:
        return

    excess_units = int(round(excess_lots * lot_size_cents))
    if excess_units <= 0:
        return

    logger.info(
        f"[{account_name}] Over-risk detected on fill: rm={rm}, "
        f"actual_risk={actual_risk:.2f} target={target_risk:.2f}, "
        f"follower_lots={follower_lots:.4f}, trim_lots={excess_lots:.4f}, "
        f"trim_units={excess_units}"
    )

    try:
        client.close_position(
            account_id=config.account_id,
            position_id=position.positionId,
            volume=excess_units,
            symbol_id=symbol.symbolId,
        )
        logger.info(
            f"[{account_name}] Over-risk partial close sent: "
            f"positionId={position.positionId}, trim_units={excess_units}"
        )
        alert_trade_warning(
            account_name=account_name,
            action="overrisk_partial_close_sent",
            ticket=0,
            message="Over-risk trim executed after fill",
            mt5_symbol=mt5_symbol,
            position_id=position.positionId,
            trim_units=excess_units,
            actual_risk=actual_risk,
            target_risk=target_risk,
        )
    except Exception as e:
        alert_trade_failure(
            account_name=account_name,
            action="overrisk_partial_close_failed",
            ticket=0,
            exc=e,
            mt5_symbol=mt5_symbol,
            position_id=getattr(position, "positionId", None),
            trim_units=excess_units,
            actual_risk=actual_risk,
            target_risk=target_risk,
        )


def _resolve_open_volume_for_account(data: dict, config, *, account_name=None, client=None, account_manager=None):
    src_lots = float(data.get("volume", 0) or 0)
    sl = float(data.get("sl", 0) or 0)

    risk_mode = _risk_mode(config)
    reject_if_no_sl = bool(getattr(config, "reject_if_no_sl", False))
    source_volume_fallback = bool(getattr(config, "source_volume_fallback", True))

    if risk_mode == "FIXED_LOT":
        fixed_lot = float(getattr(config, "fixed_lot", 0) or 0)
        if fixed_lot > 0:
            return fixed_lot, "FIXED_LOT"
        return src_lots, "FIXED_LOT invalid -> SOURCE_VOLUME"

    if not _has_valid_sl(sl):
        if reject_if_no_sl:
            return None, "REJECT_NO_SL"
        if not source_volume_fallback:
            return None, "REJECT_NO_SL_FALLBACK_DISABLED"
        return src_lots, "NO_SL_FALLBACK_SOURCE_VOLUME"

    if risk_mode in ("FIXED_USD", "PERCENT_EQUITY"):
        if not (account_manager and account_name):
            return None, f"REJECT_{risk_mode}_MISSING_CONTEXT"

        mt5_symbol = data.get("symbol")
        entry_price = float(data.get("entry_price", 0) or 0.0)
        if entry_price <= 0:
            return None, f"REJECT_{risk_mode}_NO_ENTRY_PRICE_FROM_MT5"

        risk_per_1lot = _estimate_risk_ccy_per_1lot_from_mt5(data, float(entry_price), float(sl))

        logger.info(
            f"[{account_name}] Risk-per-lot calc: source=mt5_only, "
            f"symbol={mt5_symbol}, entry={float(entry_price):.5f}, sl={float(sl):.5f}, "
            f"mt5_tick_size={data.get('mt5_tick_size')}, "
            f"mt5_tick_value={data.get('mt5_tick_value')}, "
            f"quote_to_deposit_rate={data.get('quote_to_deposit_rate')}, "
            f"perLot={float(risk_per_1lot):.2f}"
        )

        if risk_per_1lot <= 0:
            return None, f"REJECT_{risk_mode}_CANNOT_PRICE_RISK_FROM_MT5"

        if risk_mode == "FIXED_USD":
            usd_risk = float(getattr(config, "fixed_usd_risk", 0) or 0)
            if usd_risk <= 0:
                return None, "REJECT_FIXED_USD_INVALID"
        else:
            pct = float(getattr(config, "risk_percent", 0) or 0)
            ref_amt = _get_account_equity_or_balance(account_manager, account_name, config)
            if pct <= 0:
                return None, "REJECT_PERCENT_EQUITY_INVALID_PCT"
            if ref_amt <= 0:
                return None, "REJECT_NO_EQUITY"
            usd_risk = (pct / 100.0) * float(ref_amt)

        lots = float(usd_risk) / float(risk_per_1lot)
        if lots <= 0:
            return None, f"REJECT_{risk_mode}_LOTS_NONPOSITIVE"

        return lots, (
            f"{risk_mode} mt5_only usd={usd_risk:.2f} "
            f"perLot={risk_per_1lot:.2f} entry={float(entry_price):.5f}"
        )

    return src_lots, f"{risk_mode}_USING_SOURCE_VOLUME_FOR_NOW"


def _extract_open_entry_price(data: dict) -> float:
    for key in ("entry_price", "open_price", "price", "entry", "openPrice"):
        v = _to_float(data.get(key, 0), 0.0)
        if v > 0:
            return v
    return 0.0


def _is_startup_market_recovery(data: dict) -> bool:
    if _to_bool(data.get("startup_sync", False), False):
        return True
    if _to_bool(data.get("startup_recovery", False), False):
        return True
    if _to_bool(data.get("is_startup_sync", False), False):
        return True
    if _to_bool(data.get("recovery", False), False):
        return True

    sync_origin = str(
        data.get("sync_origin")
        or data.get("origin")
        or data.get("source")
        or data.get("reason")
        or ""
    ).strip().lower()

    return sync_origin in ("startup", "startup_sync", "startup_recovery", "recovery")


def _quote_value_from_obj(obj, names):
    if obj is None:
        return None

    for name in names:
        v = _read_attr_or_key(obj, name, None)
        pv = _first_positive_float(v)
        if pv is not None:
            return pv

    nested = _read_attr_or_key(obj, "quote", None)
    if nested is not None:
        for name in names:
            v = _read_attr_or_key(nested, name, None)
            pv = _first_positive_float(v)
            if pv is not None:
                return pv

    return None


def _get_current_market_price(client, symbol_id: int, side: str):
    side = str(side or "").strip().upper()
    symbol = _get_symbol_details(client, symbol_id)

    quote_obj = None
    try:
        if hasattr(client, "spot_quotes"):
            quote_obj = client.spot_quotes.get(int(symbol_id))
    except Exception:
        quote_obj = None

    if quote_obj is None:
        try:
            if hasattr(client, "symbol_quotes"):
                quote_obj = client.symbol_quotes.get(int(symbol_id))
        except Exception:
            quote_obj = None

    ask = _first_positive_float(
        _quote_value_from_obj(quote_obj, ("ask", "askPrice", "bestAsk")),
        _quote_value_from_obj(symbol, ("ask", "askPrice", "bestAsk")),
    )
    bid = _first_positive_float(
        _quote_value_from_obj(quote_obj, ("bid", "bidPrice", "bestBid")),
        _quote_value_from_obj(symbol, ("bid", "bidPrice", "bestBid")),
    )

    if side == "BUY":
        return ask if ask is not None else bid
    if side == "SELL":
        return bid if bid is not None else ask
    return ask if ask is not None else bid


def _extract_mt_current_market_price(data: dict, side: str):
    side = str(side or "").strip().upper()

    ask = _first_positive_float(
        data.get("current_ask"),
        data.get("ask"),
        data.get("ask_price"),
        data.get("mt5_ask"),
        data.get("symbol_ask"),
    )
    bid = _first_positive_float(
        data.get("current_bid"),
        data.get("bid"),
        data.get("bid_price"),
        data.get("mt5_bid"),
        data.get("symbol_bid"),
    )
    last = _first_positive_float(
        data.get("current_price"),
        data.get("price_current"),
        data.get("market_price"),
        data.get("last_price"),
        data.get("last"),
    )

    if side == "BUY":
        return ask if ask is not None else (last if last is not None else bid)
    if side == "SELL":
        return bid if bid is not None else (last if last is not None else ask)
    return last if last is not None else (ask if ask is not None else bid)


def _extract_mt_pip_size(data: dict) -> float:
    direct = _first_positive_float(
        data.get("pip_size"),
        data.get("pipSize"),
        data.get("point"),
        data.get("Point"),
        data.get("tick_size"),
        data.get("tickSize"),
    )
    if direct is not None and direct > 0:
        return float(direct)

    pip_pos = data.get("pip_position", data.get("pipPosition", None))
    try:
        if pip_pos is not None and str(pip_pos) != "":
            return float(10 ** -int(float(pip_pos)))
    except Exception:
        pass

    digits = data.get("digits", data.get("mt5_digits", data.get("symbol_digits", None)))
    try:
        if digits is not None and str(digits) != "":
            d = int(float(digits))
            if d > 0:
                return float(10 ** -d)
    except Exception:
        pass

    return 0.0


def _build_startup_recovery_plan(client, config, mt5_symbol: str, side: str, entry_price: float, data: dict = None):
    data = data or {}
    mode = _startup_market_recovery_mode(config)

    if mode == "skip":
        return {"action": "skip", "reason": "startup_market_recovery_mode=skip"}

    if mode == "market":
        return {"action": "market", "reason": "startup_market_recovery_mode=market"}

    if float(entry_price or 0.0) <= 0:
        return {"action": "skip", "reason": "startup recovery missing entry_price"}

    symbol_id = _get_symbol_id_for_account(client, config, mt5_symbol)
    if symbol_id is None:
        return {"action": "skip", "reason": "startup recovery no symbol_id"}

    symbol = _get_symbol_details(client, int(symbol_id))

    current_price = _extract_mt_current_market_price(data, side)
    current_price_source = "mt5"

    if current_price is None or float(current_price) <= 0:
        current_price = _get_current_market_price(client, int(symbol_id), side)
        current_price_source = "ctrader"

    if current_price is None or float(current_price) <= 0:
        return {"action": "skip", "reason": "startup recovery current market price unavailable from MT5/cTrader"}

    pip_size = _symbol_pip_size(symbol) if symbol is not None else 0.0
    if pip_size <= 0:
        pip_size = _extract_mt_pip_size(data)

    if pip_size <= 0:
        return {"action": "skip", "reason": "startup recovery invalid pip size"}

    distance_pips = abs(float(current_price) - float(entry_price)) / float(pip_size)
    max_distance_pips = _startup_market_max_distance_pips(config)

    if distance_pips <= max_distance_pips:
        return {
            "action": "market",
            "reason": (
                f"startup recovery {current_price_source} quote "
                f"current={float(current_price):.5f}, "
                f"entry={float(entry_price):.5f}, "
                f"distance_pips={distance_pips:.2f} <= max_distance_pips={max_distance_pips:.2f}"
            ),
        }

    side = str(side or "").strip().upper()

    if side == "BUY":
        if float(current_price) <= float(entry_price):
            return {
                "action": "pending",
                "pending_type": "limit",
                "limit_price": float(entry_price),
                "stop_price": 0.0,
                "reason": (
                    f"startup recovery BUY -> LIMIT at entry; {current_price_source} quote "
                    f"current={float(current_price):.5f}, entry={float(entry_price):.5f}, "
                    f"distance_pips={distance_pips:.2f}"
                ),
            }
        return {
            "action": "pending",
            "pending_type": "stop",
            "stop_price": float(entry_price),
            "limit_price": 0.0,
            "reason": (
                f"startup recovery BUY -> STOP at entry; {current_price_source} quote "
                f"current={float(current_price):.5f}, entry={float(entry_price):.5f}, "
                f"distance_pips={distance_pips:.2f}"
            ),
        }

    if side == "SELL":
        if float(current_price) >= float(entry_price):
            return {
                "action": "pending",
                "pending_type": "limit",
                "limit_price": float(entry_price),
                "stop_price": 0.0,
                "reason": (
                    f"startup recovery SELL -> LIMIT at entry; {current_price_source} quote "
                    f"current={float(current_price):.5f}, entry={float(entry_price):.5f}, "
                    f"distance_pips={distance_pips:.2f}"
                ),
            }
        return {
            "action": "pending",
            "pending_type": "stop",
            "stop_price": float(entry_price),
            "limit_price": 0.0,
            "reason": (
                f"startup recovery SELL -> STOP at entry; {current_price_source} quote "
                f"current={float(current_price):.5f}, entry={float(entry_price):.5f}, "
                f"distance_pips={distance_pips:.2f}"
            ),
        }

    return {"action": "skip", "reason": f"startup recovery unsupported side={side!r}"}


def _resolve_target_accounts(data, account_manager):
    magic = _to_int(data.get("magic", 0), 0)
    if magic <= 0:
        msg = "No valid magic in event payload; cannot route event"
        logger.warning(msg)
        alert_trade_warning(
            account_name="router",
            action="resolve_target_accounts_invalid_magic",
            ticket=_to_int(data.get("ticket", 0), 0),
            message=msg,
            magic=magic,
        )
        return []

    matched = []
    seen = set()

    try:
        for account_name, (_, config) in account_manager.get_all_accounts().items():
            if not config:
                continue

            route_magic = getattr(config, "route_magic_number", None)
            magic_numbers = getattr(config, "magic_numbers", None)
            matched_here = False

            try:
                if route_magic is not None and int(route_magic) == int(magic):
                    matched_here = True
            except Exception:
                pass

            if not matched_here and magic_numbers:
                try:
                    if int(magic) in {int(x) for x in magic_numbers}:
                        matched_here = True
                except Exception:
                    pass

            if matched_here and account_name not in seen:
                matched.append(account_name)
                seen.add(account_name)
    except Exception as e:
        alert_trade_failure(
            account_name="router",
            action="resolve_target_accounts_exception",
            ticket=_to_int(data.get("ticket", 0), 0),
            exc=e,
            magic=magic,
        )
        return []

    if not matched:
        msg = f"No configured account routes for magic={magic}"
        logger.warning(msg)
        alert_trade_warning(
            account_name="router",
            action="resolve_target_accounts_no_match",
            ticket=_to_int(data.get("ticket", 0), 0),
            message=msg,
            magic=magic,
        )
        return []

    logger.info(f"Resolved magic={magic} -> target accounts: {matched}")
    return matched


def _get_target_account_contexts(data, account_manager):
    account_names = _resolve_target_accounts(data, account_manager)
    contexts = []

    for account_name in account_names:
        try:
            client = account_manager.get_client(account_name)
            config = account_manager.get_config(account_name)
        except Exception as e:
            alert_trade_failure(
                account_name=account_name,
                action="load_target_account_context",
                ticket=_to_int(data.get("ticket", 0), 0),
                exc=e,
            )
            continue

        if not client or not config:
            msg = f"Target account {account_name} is unavailable or not initialized"
            logger.warning(msg)
            alert_trade_warning(
                account_name=account_name,
                action="target_account_unavailable",
                ticket=_to_int(data.get("ticket", 0), 0),
                message=msg,
            )
            continue

        contexts.append((account_name, client, config))

    return contexts


def _safe_symbol_id_or_warn(account_name, client, config, ticket, mt5_symbol, action_name):
    symbol_id = _get_symbol_id_for_account(client, config, mt5_symbol)
    if symbol_id is None:
        msg = f"{action_name} ignored for ticket {ticket} (symbol mapping failed for {mt5_symbol})"
        logger.warning(f"[{account_name}] {msg}")
        alert_trade_warning(
            account_name=account_name,
            action=f"{action_name.lower()}_symbol_mapping_failed",
            ticket=int(ticket),
            message=msg,
            mt5_symbol=mt5_symbol,
        )
        return None
    return int(symbol_id)


def try_apply_pending_sltp(account_name, client, config, ticket, account_manager, force=False):
    pending = _get_pending_sltp(account_name, int(ticket))
    if not pending:
        return False

    if _pending_sltp_expired(pending):
        msg = f"Pending SL/TP expired for ticket {ticket}, dropping repair item"
        logger.warning(f"[{account_name}] {msg}")
        alert_trade_warning(
            account_name=account_name,
            action="pending_sltp_expired",
            ticket=int(ticket),
            message=msg,
            last_error=pending.get("last_error"),
        )
        _clear_pending_sltp(account_name, int(ticket))
        return False

    attempts = _to_int(pending.get("attempts", 0), 0)
    if attempts >= _PENDING_SLTP_MAX_ATTEMPTS:
        msg = f"Pending SL/TP exceeded retry limit for ticket {ticket}"
        logger.error(
            f"[{account_name}] {msg}, last_error={pending.get('last_error')}"
        )
        alert_trade_failure(
            account_name=account_name,
            action="pending_sltp_retry_limit_exceeded",
            ticket=int(ticket),
            exc=Exception(pending.get("last_error") or "retry limit exceeded"),
            attempts=attempts,
            mt5_symbol=pending.get("symbol"),
        )
        _clear_pending_sltp(account_name, int(ticket))
        return False

    if not force and not _pending_sltp_due(pending):
        return False

    position_id = account_manager.get_position_id(account_name, int(ticket))
    if not position_id:
        _touch_pending_sltp_retry(account_name, int(ticket), error="position_mapping_not_ready")
        return False

    mt5_symbol = pending.get("symbol")
    new_sl = float(pending.get("sl", 0) or 0)
    new_tp = float(pending.get("tp", 0) or 0)
    symbol_id = _get_symbol_id_for_account(client, config, mt5_symbol)

    logger.info(
        f"[{account_name}] Applying pending SL/TP for ticket {ticket} -> "
        f"positionId={position_id}, symbolId={symbol_id}, SL={new_sl}, TP={new_tp}, "
        f"attempt={attempts + 1}"
    )

    try:
        client.amend_position(
            account_id=config.account_id,
            position_id=position_id,
            symbol_id=symbol_id,
            stop_loss=new_sl if new_sl > 0 else None,
            take_profit=new_tp if new_tp > 0 else None,
        )
        logger.info(f"[{account_name}] Successfully applied pending SL/TP for ticket {ticket}")
        _clear_pending_sltp(account_name, int(ticket))
        return True
    except Exception as e:
        _touch_pending_sltp_retry(
            account_name,
            int(ticket),
            error=str(e),
            position_id=position_id,
        )
        alert_trade_failure(
            account_name=account_name,
            action="apply_pending_sltp",
            ticket=int(ticket),
            exc=e,
            mt5_symbol=mt5_symbol,
            position_id=position_id,
            symbol_id=symbol_id,
            sl=new_sl,
            tp=new_tp,
            attempt=attempts + 1,
        )
        return False


def drain_pending_sltp_repairs(account_manager, account_name=None, force=False):
    repaired = 0
    scanned_accounts = []

    try:
        if account_name is not None:
            scanned_accounts = [str(account_name)]
        else:
            with _PENDING_SLTP_LOCK:
                scanned_accounts = list(PENDING_SLTP.keys())
    except Exception:
        scanned_accounts = []

    for name in scanned_accounts:
        try:
            client = account_manager.get_client(name)
            config = account_manager.get_config(name)
            if not client or not config:
                continue

            with _PENDING_SLTP_LOCK:
                pending_bucket = dict(PENDING_SLTP.setdefault(name, {}))

            for ticket in list(pending_bucket.keys()):
                if try_apply_pending_sltp(
                    account_name=name,
                    client=client,
                    config=config,
                    ticket=int(ticket),
                    account_manager=account_manager,
                    force=force,
                ):
                    repaired += 1
        except Exception as e:
            alert_trade_failure(
                account_name=name,
                action="drain_pending_sltp_repairs",
                ticket=0,
                exc=e,
            )

    return repaired


def notify_position_update(account_name, ticket, account_manager):
    try:
        client = account_manager.get_client(account_name)
        config = account_manager.get_config(account_name)
        if not client or not config:
            return
        try_apply_pending_sltp(
            account_name=account_name,
            client=client,
            config=config,
            ticket=int(ticket),
            account_manager=account_manager,
            force=True,
        )
    except Exception as e:
        logger.debug(f"[{account_name}] notify_position_update failed: {e}")


def process_trade_event(data, account_manager):
    try:
        event_type = _canonical_event_type(data)
        ticket = _to_int(data.get("ticket", 0), 0)
        magic = _to_int(data.get("magic", 0), 0)

        logger.info(f"Processing event: {event_type} for ticket {ticket} (magic: {magic})")

        if event_type == "OPEN":
            handle_open_event(data, account_manager)
        elif event_type == "PENDING_OPEN":
            handle_pending_open_event(data, account_manager)
        elif event_type == "PENDING_MODIFY":
            handle_pending_modify_event(data, account_manager)
        elif event_type == "PENDING_CANCEL":
            handle_pending_cancel_event(data, account_manager)
        elif event_type == "MODIFY":
            handle_modify_event(data, account_manager)
        elif event_type == "CLOSE":
            handle_close_event(data, account_manager)
        else:
            msg = f"Unknown event type: {event_type}"
            logger.warning(msg)
            alert_trade_warning(
                account_name="router",
                action="unknown_event_type",
                ticket=ticket,
                message=msg,
                magic=magic,
            )

        drain_pending_sltp_repairs(account_manager)

    except Exception as e:
        alert_trade_failure(
            account_name="router",
            action="process_trade_event",
            ticket=_to_int(data.get("ticket", 0), 0),
            exc=e,
            event_type=data.get("event_type") or data.get("action") or data.get("event"),
            magic=_to_int(data.get("magic", 0), 0),
            symbol=data.get("symbol"),
        )
        raise


def handle_open_event(data, account_manager):
    ticket = _to_int(data.get("ticket"))
    mt5_symbol = data.get("symbol")
    side = str(data.get("side") or data.get("type") or "").strip().upper()
    src_volume = _to_float(data.get("volume", 0), 0.0)
    sl = _to_float(data.get("sl", 0), 0.0)
    tp = _to_float(data.get("tp", 0), 0.0)
    magic = _to_int(data.get("magic", 0), 0)
    entry_price = _extract_open_entry_price(data)
    is_startup_recovery = _is_startup_market_recovery(data)

    logger.info(
        f"OPEN event - Ticket: {ticket}, Symbol: {mt5_symbol}, "
        f"Side: {side}, Volume: {src_volume}, SL: {sl}, TP: {tp}, "
        f"EntryPrice: {entry_price}, StartupRecovery: {is_startup_recovery}"
    )

    if ticket <= 0:
        msg = "OPEN ignored: invalid ticket"
        logger.warning(msg)
        alert_trade_warning(
            account_name="router",
            action="open_invalid_ticket",
            ticket=ticket,
            message=msg,
            magic=magic,
            mt5_symbol=mt5_symbol,
        )
        return

    if src_volume > 0:
        with _MASTER_LOTS_LOCK:
            MASTER_OPEN_LOTS[int(ticket)] = float(src_volume)
            MASTER_CLOSED_LOTS[int(ticket)] = 0.0

    contexts = _get_target_account_contexts(data, account_manager)
    if not contexts:
        msg = f"OPEN ignored for ticket {ticket}: no target accounts for magic={magic}"
        logger.warning(msg)
        alert_trade_warning(
            account_name="router",
            action="open_no_target_accounts",
            ticket=ticket,
            message=msg,
            magic=magic,
            mt5_symbol=mt5_symbol,
        )
        return

    for account_name, client, config in contexts:
        try:
            existing_position_id = account_manager.get_position_id(account_name, int(ticket))
            if existing_position_id:
                logger.info(
                    f"[{account_name}] OPEN skip for ticket {ticket} "
                    f"(already mapped to positionId={existing_position_id})"
                )
                continue

            lots, decision = _resolve_open_volume_for_account(
                data,
                config,
                account_name=account_name,
                client=client,
                account_manager=account_manager,
            )

            if lots is None or float(lots) <= 0:
                msg = f"OPEN rejected for ticket {ticket}: {decision}"
                logger.warning(f"[{account_name}] {msg}")
                alert_trade_warning(
                    account_name=account_name,
                    action="open_rejected",
                    ticket=ticket,
                    message=msg,
                    mt5_symbol=mt5_symbol,
                    side=side,
                    volume=src_volume,
                    decision=decision,
                )
                continue

            logger.info(f"[{account_name}] OPEN sizing: {decision}, lots={float(lots):.4f}")

            if sl > 0 or tp > 0:
                _set_pending_sltp(account_name, ticket, mt5_symbol, sl, tp)
            else:
                _clear_pending_sltp(account_name, ticket)

            if is_startup_recovery:
                if not _startup_sync_market_orders_enabled(config):
                    logger.info(
                        f"[{account_name}] Startup recovery skipped for ticket {ticket} "
                        f"(startup_sync_market_orders=false)"
                    )
                    continue

                recovery_plan = _build_startup_recovery_plan(
                    client=client,
                    config=config,
                    mt5_symbol=mt5_symbol,
                    side=side,
                    entry_price=entry_price,
                    data=data,
                )

                logger.info(
                    f"[{account_name}] Startup recovery decision for ticket {ticket}: "
                    f"{recovery_plan.get('reason')} -> {recovery_plan.get('action')}"
                )

                if recovery_plan.get("action") == "skip":
                    _clear_pending_sltp(account_name, ticket)
                    continue

                if recovery_plan.get("action") == "pending":
                    copy_pending_to_account(
                        account_name=account_name,
                        client=client,
                        config=config,
                        ticket=ticket,
                        mt5_symbol=mt5_symbol,
                        side=side,
                        volume=float(lots),
                        sl=sl,
                        tp=tp,
                        magic=magic,
                        pending_type=recovery_plan.get("pending_type", "limit"),
                        stop_price=float(recovery_plan.get("stop_price", 0.0) or 0.0),
                        limit_price=float(recovery_plan.get("limit_price", 0.0) or 0.0),
                        expiration_ms=_startup_pending_expiration_ms(config),
                    )
                    continue

            copy_open_to_account(
                account_name=account_name,
                client=client,
                config=config,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                side=side,
                volume=float(lots),
                sl=sl,
                tp=tp,
                magic=magic,
            )

            try_apply_pending_sltp(
                account_name=account_name,
                client=client,
                config=config,
                ticket=int(ticket),
                account_manager=account_manager,
                force=False,
            )

        except Exception as e:
            alert_trade_failure(
                account_name=account_name,
                action="handle_open_event",
                ticket=ticket,
                exc=e,
                mt5_symbol=mt5_symbol,
                side=side,
                volume=src_volume,
                sl=sl,
                tp=tp,
                magic=magic,
            )


def handle_pending_open_event(data, account_manager):
    ticket = _to_int(data.get("ticket"))
    mt5_symbol = data.get("symbol")
    side = str(data.get("side") or data.get("type") or "").strip().upper()
    volume = _to_float(data.get("volume", 0), 0.0)
    sl = _to_float(data.get("sl", 0), 0.0)
    tp = _to_float(data.get("tp", 0), 0.0)
    magic = _to_int(data.get("magic", 0), 0)

    pending_type = _canonical_pending_type(data)
    entry_price = _to_float(data.get("entry_price", 0), 0.0)
    stop_price = _to_float(data.get("stop_price", 0), 0.0)
    limit_price = _to_float(data.get("limit_price", 0), 0.0)
    expiration_ms = _to_int(data.get("expiration_ms", 0), 0)

    if pending_type not in ("limit", "stop", "stop_limit"):
        msg = f"PENDING_OPEN ignored for ticket {ticket}: unsupported pending_type={pending_type!r}"
        logger.warning(msg)
        alert_trade_warning(
            account_name="router",
            action="pending_open_unsupported_type",
            ticket=ticket,
            message=msg,
            pending_type=pending_type,
            mt5_symbol=mt5_symbol,
        )
        return

    if pending_type == "limit" and limit_price <= 0:
        limit_price = entry_price
    if pending_type == "stop" and stop_price <= 0:
        stop_price = entry_price
    if pending_type == "stop_limit":
        if stop_price <= 0:
            stop_price = entry_price
        if limit_price <= 0:
            limit_price = entry_price

    logger.info(
        f"PENDING_OPEN event - Ticket: {ticket}, Symbol: {mt5_symbol}, Side: {side}, "
        f"Volume: {volume}, pending_type={pending_type}, "
        f"stop_price={stop_price}, limit_price={limit_price}, SL={sl}, TP={tp}, "
        f"expiration_ms={expiration_ms}"
    )

    pending_entry_price = 0.0
    if pending_type == "limit":
        pending_entry_price = float(limit_price or 0.0)
    elif pending_type == "stop":
        pending_entry_price = float(stop_price or 0.0)
    elif pending_type == "stop_limit":
        pending_entry_price = float(limit_price or 0.0) if float(limit_price or 0.0) > 0 else float(stop_price or 0.0)

    contexts = _get_target_account_contexts(data, account_manager)
    if not contexts:
        msg = f"PENDING_OPEN ignored for ticket {ticket}: no target accounts for magic={magic}"
        logger.warning(msg)
        alert_trade_warning(
            account_name="router",
            action="pending_open_no_target_accounts",
            ticket=ticket,
            message=msg,
            magic=magic,
            mt5_symbol=mt5_symbol,
        )
        return

    for account_name, client, config in contexts:
        try:
            rm = _risk_mode(config)
            sizing_volume = float(volume)

            if rm in ("FIXED_USD", "PERCENT_EQUITY"):
                sizing_data = dict(data)
                sizing_data["entry_price"] = float(pending_entry_price or 0.0)

                lots, decision = _resolve_open_volume_for_account(
                    sizing_data,
                    config,
                    account_name=account_name,
                    client=client,
                    account_manager=account_manager,
                )
                if lots is None or float(lots) <= 0:
                    msg = f"PENDING_OPEN rejected for ticket {ticket}: {decision}"
                    logger.warning(f"[{account_name}] {msg}")
                    alert_trade_warning(
                        account_name=account_name,
                        action="pending_open_rejected",
                        ticket=ticket,
                        message=msg,
                        mt5_symbol=mt5_symbol,
                        side=side,
                        volume=volume,
                        decision=decision,
                        pending_type=pending_type,
                    )
                    continue
                sizing_volume = float(lots)
                logger.info(f"[{account_name}] PENDING_OPEN sizing: {decision}, lots={float(lots):.4f}")

            copy_pending_to_account(
                account_name=account_name,
                client=client,
                config=config,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                side=side,
                volume=float(sizing_volume),
                sl=sl,
                tp=tp,
                magic=magic,
                pending_type=pending_type,
                stop_price=stop_price,
                limit_price=limit_price,
                expiration_ms=expiration_ms,
            )

        except Exception as e:
            alert_trade_failure(
                account_name=account_name,
                action="handle_pending_open_event",
                ticket=ticket,
                exc=e,
                mt5_symbol=mt5_symbol,
                side=side,
                volume=volume,
                sl=sl,
                tp=tp,
                magic=magic,
                pending_type=pending_type,
                stop_price=stop_price,
                limit_price=limit_price,
                expiration_ms=expiration_ms,
            )


def handle_pending_modify_event(data, account_manager):
    ticket = _to_int(data.get("ticket"), 0)
    mt5_symbol = data.get("symbol")
    side = str(data.get("side") or data.get("type") or "").strip().upper()
    volume = _to_float(data.get("volume", 0), 0.0)
    sl = _to_float(data.get("sl", 0), 0.0)
    tp = _to_float(data.get("tp", 0), 0.0)
    magic = _to_int(data.get("magic", 0), 0)

    pending_type = _canonical_pending_type(data)
    entry_price = _to_float(data.get("entry_price", 0), 0.0)
    stop_price = _to_float(data.get("stop_price", 0), 0.0)
    limit_price = _to_float(data.get("limit_price", 0), 0.0)
    expiration_ms = _to_int(data.get("expiration_ms", 0), 0)

    if pending_type not in ("limit", "stop", "stop_limit"):
        msg = f"PENDING_MODIFY ignored for ticket {ticket}: unsupported pending_type={pending_type!r}"
        logger.warning(msg)
        alert_trade_warning(
            account_name="router",
            action="pending_modify_unsupported_type",
            ticket=ticket,
            message=msg,
            pending_type=pending_type,
            mt5_symbol=mt5_symbol,
        )
        return

    if pending_type == "limit" and limit_price <= 0:
        limit_price = entry_price
    if pending_type == "stop" and stop_price <= 0:
        stop_price = entry_price
    if pending_type == "stop_limit":
        if stop_price <= 0:
            stop_price = entry_price
        if limit_price <= 0:
            limit_price = entry_price

    logger.info(
        f"PENDING_MODIFY event - Ticket: {ticket}, Symbol: {mt5_symbol}, Side: {side}, "
        f"Volume: {volume}, pending_type={pending_type}, "
        f"stop_price={stop_price}, limit_price={limit_price}, SL={sl}, TP={tp}, "
        f"expiration_ms={expiration_ms}"
    )

    contexts = _get_target_account_contexts(data, account_manager)
    if not contexts:
        msg = f"PENDING_MODIFY ignored for ticket {ticket}: no target accounts for magic={magic}"
        logger.warning(msg)
        alert_trade_warning(
            account_name="router",
            action="pending_modify_no_target_accounts",
            ticket=ticket,
            message=msg,
            magic=magic,
            mt5_symbol=mt5_symbol,
        )
        return

    for account_name, client, config in contexts:
        try:
            order_id = account_manager.get_order_id(account_name, int(ticket))
            if not order_id:
                msg = f"PENDING_MODIFY ignored for ticket {ticket} (no orderId mapping yet)"
                logger.warning(f"[{account_name}] {msg}")
                alert_trade_warning(
                    account_name=account_name,
                    action="pending_modify_missing_order_mapping",
                    ticket=ticket,
                    message=msg,
                    mt5_symbol=mt5_symbol,
                )
                continue

            symbol_id = _safe_symbol_id_or_warn(
                account_name, client, config, ticket, mt5_symbol, "pending_modify"
            )
            if symbol_id is None:
                continue

            client.amend_pending_order(
                account_id=config.account_id,
                order_id=int(order_id),
                symbol_id=int(symbol_id),
                side=side,
                volume=float(volume),
                pending_type=pending_type,
                stop_price=float(stop_price) if float(stop_price or 0) > 0 else None,
                limit_price=float(limit_price) if float(limit_price or 0) > 0 else None,
                stop_loss=float(sl) if float(sl or 0) > 0 else None,
                take_profit=float(tp) if float(tp or 0) > 0 else None,
                expiration_ms=int(expiration_ms) if int(expiration_ms or 0) > 0 else None,
            )
            logger.info(f"[{account_name}] Modified pending order {int(order_id)} for ticket {ticket}")

        except Exception as e:
            alert_trade_failure(
                account_name=account_name,
                action="handle_pending_modify_event",
                ticket=ticket,
                exc=e,
                mt5_symbol=mt5_symbol,
                order_id=order_id if "order_id" in locals() else None,
                side=side,
                volume=volume,
                sl=sl,
                tp=tp,
                pending_type=pending_type,
                stop_price=stop_price,
                limit_price=limit_price,
                expiration_ms=expiration_ms,
            )


def handle_pending_cancel_event(data, account_manager):
    ticket = _to_int(data.get("ticket", 0), 0)
    mt5_symbol = data.get("symbol")
    magic = _to_int(data.get("magic", 0), 0)

    logger.info(f"PENDING_CANCEL event - Ticket: {ticket}, Symbol: {mt5_symbol}")

    contexts = _get_target_account_contexts(data, account_manager)
    if not contexts:
        msg = f"PENDING_CANCEL ignored for ticket {ticket}: no target accounts for magic={magic}"
        logger.warning(msg)
        alert_trade_warning(
            account_name="router",
            action="pending_cancel_no_target_accounts",
            ticket=ticket,
            message=msg,
            magic=magic,
            mt5_symbol=mt5_symbol,
        )
        return

    for account_name, client, config in contexts:
        try:
            order_id = account_manager.get_order_id(account_name, int(ticket))
            if not order_id:
                msg = f"PENDING_CANCEL ignored for ticket {ticket} (no orderId mapping yet)"
                logger.warning(f"[{account_name}] {msg}")
                alert_trade_warning(
                    account_name=account_name,
                    action="pending_cancel_missing_order_mapping",
                    ticket=ticket,
                    message=msg,
                    mt5_symbol=mt5_symbol,
                )
                continue

            client.cancel_pending_order(account_id=config.account_id, order_id=int(order_id))
            logger.info(f"[{account_name}] Cancel sent: ticket {ticket} -> orderId {int(order_id)}")

        except Exception as e:
            alert_trade_failure(
                account_name=account_name,
                action="handle_pending_cancel_event",
                ticket=ticket,
                exc=e,
                mt5_symbol=mt5_symbol,
                order_id=order_id if "order_id" in locals() else None,
            )


def handle_modify_event(data, account_manager):
    ticket = _to_int(data.get("ticket"))
    mt5_symbol = data.get("symbol")
    new_sl = _to_float(data.get("sl", 0), 0.0)
    new_tp = _to_float(data.get("tp", 0), 0.0)
    magic = _to_int(data.get("magic", 0), 0)

    logger.info(
        f"MODIFY event - Ticket: {ticket}, Symbol: {mt5_symbol}, "
        f"New SL: {new_sl}, New TP: {new_tp}"
    )

    contexts = _get_target_account_contexts(data, account_manager)
    if not contexts:
        msg = f"MODIFY ignored for ticket {ticket}: no target accounts for magic={magic}"
        logger.warning(msg)
        alert_trade_warning(
            account_name="router",
            action="modify_no_target_accounts",
            ticket=ticket,
            message=msg,
            magic=magic,
            mt5_symbol=mt5_symbol,
        )
        return

    for account_name, client, config in contexts:
        try:
            position_id = account_manager.get_position_id(account_name, ticket)
            symbol_id = _safe_symbol_id_or_warn(
                account_name, client, config, ticket, mt5_symbol, "modify"
            )

            if position_id and symbol_id is not None:
                try:
                    client.amend_position(
                        account_id=config.account_id,
                        position_id=position_id,
                        symbol_id=symbol_id,
                        stop_loss=new_sl if new_sl > 0 else None,
                        take_profit=new_tp if new_tp > 0 else None,
                    )
                    logger.info(f"[{account_name}] Modified position {position_id} for ticket {ticket}")
                    _clear_pending_sltp(account_name, ticket)
                except Exception as amend_error:
                    msg = f"Immediate modify failed for ticket {ticket}, queueing repair: {amend_error}"
                    logger.warning(f"[{account_name}] {msg}")
                    alert_trade_warning(
                        account_name=account_name,
                        action="modify_immediate_failed_queue_repair",
                        ticket=ticket,
                        message=msg,
                        mt5_symbol=mt5_symbol,
                        position_id=position_id,
                        symbol_id=symbol_id,
                        sl=new_sl,
                        tp=new_tp,
                    )
                    _set_pending_sltp(account_name, ticket, mt5_symbol, new_sl, new_tp)
                    _touch_pending_sltp_retry(
                        account_name,
                        ticket,
                        error=str(amend_error),
                        position_id=position_id,
                    )
            else:
                msg = f"Position not found for ticket {ticket}, storing pending SL/TP"
                logger.warning(f"[{account_name}] {msg}")
                alert_trade_warning(
                    account_name=account_name,
                    action="modify_position_missing_store_pending_sltp",
                    ticket=ticket,
                    message=msg,
                    mt5_symbol=mt5_symbol,
                    sl=new_sl,
                    tp=new_tp,
                )
                _set_pending_sltp(account_name, ticket, mt5_symbol, new_sl, new_tp)

        except Exception as e:
            alert_trade_failure(
                account_name=account_name,
                action="handle_modify_event",
                ticket=ticket,
                exc=e,
                mt5_symbol=mt5_symbol,
                sl=new_sl,
                tp=new_tp,
                magic=magic,
            )


def handle_close_event(data, account_manager):
    ticket = _to_int(data.get("ticket"))
    mt5_symbol = data.get("symbol")
    close_lots = _to_float_or_none(data.get("volume", None))
    mt5_contract_size = _to_float(data.get("mt5_contract_size", 0), 0.0)
    magic = _to_int(data.get("magic", 0), 0)

    logger.info(f"CLOSE event - Ticket: {ticket}, Symbol: {mt5_symbol}, close_lots={close_lots}")

    with _MASTER_LOTS_LOCK:
        master_open_lots = float(MASTER_OPEN_LOTS.get(int(ticket), 0) or 0)
        master_closed_lots = float(MASTER_CLOSED_LOTS.get(int(ticket), 0) or 0)

    master_remaining_lots = max(0.0, master_open_lots - master_closed_lots)

    proportional_pct = None
    if close_lots is not None and master_remaining_lots > 0:
        proportional_pct = max(0.0, min(1.0, float(close_lots) / float(master_remaining_lots)))

    contexts = _get_target_account_contexts(data, account_manager)
    if not contexts:
        msg = f"CLOSE ignored for ticket {ticket}: no target accounts for magic={magic}"
        logger.warning(msg)
        alert_trade_warning(
            account_name="router",
            action="close_no_target_accounts",
            ticket=ticket,
            message=msg,
            magic=magic,
            mt5_symbol=mt5_symbol,
        )
        return

    for account_name, client, config in contexts:
        try:
            position_id = account_manager.get_position_id(account_name, ticket)
            if not position_id:
                logger.info(f"[{account_name}] CLOSE ignored for ticket {ticket} (no mapping)")
                _clear_pending_sltp(account_name, ticket)
                continue

            symbol_id = _safe_symbol_id_or_warn(
                account_name, client, config, ticket, mt5_symbol, "close"
            )
            if symbol_id is None:
                _clear_pending_sltp(account_name, ticket)
                continue

            rm = _risk_mode(config)
            follower_units = account_manager.get_position_volume(account_name, position_id)

            close_units = None

            if close_lots is not None and follower_units is not None and int(follower_units) > 0:
                if rm != "SOURCE_VOLUME" and proportional_pct is not None:
                    close_units = int(round(proportional_pct * float(follower_units)))
                    logger.info(
                        f"[{account_name}] Proportional CLOSE: risk_mode={rm}, "
                        f"master_close_lots={float(close_lots):.4f}, "
                        f"master_remaining_lots={master_remaining_lots:.4f}, "
                        f"pct={proportional_pct:.4f}, follower_units={int(follower_units)} -> close_units={close_units}"
                    )
                else:
                    if mt5_contract_size > 0:
                        close_units = _lots_to_ctrader_cents(float(close_lots), mt5_contract_size)
                    logger.info(
                        f"[{account_name}] Absolute CLOSE: risk_mode={rm}, close_lots={close_lots}, "
                        f"mt5_contract_size={mt5_contract_size} -> close_units={close_units}"
                    )

            if close_units is None or int(close_units) <= 0:
                close_units = follower_units

            if close_units is None or int(close_units) <= 0:
                msg = (
                    f"Cannot close ticket {ticket} (positionId={position_id}) "
                    f"because close volume is unknown/invalid."
                )
                logger.warning(f"[{account_name}] {msg}")
                alert_trade_warning(
                    account_name=account_name,
                    action="close_invalid_volume",
                    ticket=ticket,
                    message=msg,
                    mt5_symbol=mt5_symbol,
                    position_id=position_id,
                    follower_units=follower_units,
                    close_lots=close_lots,
                )
                _clear_pending_sltp(account_name, ticket)
                continue

            if follower_units is not None and int(follower_units) > 0:
                close_units = min(int(close_units), int(follower_units))

            client.close_position(
                account_id=config.account_id,
                position_id=position_id,
                volume=int(close_units),
                symbol_id=symbol_id,
            )

            logger.info(
                f"[{account_name}] Close sent for position {position_id} "
                f"(ticket {ticket}) close_units={int(close_units)}"
            )

            if follower_units is not None and int(close_units) >= int(follower_units):
                account_manager.remove_mapping(account_name, ticket)

            _clear_pending_sltp(account_name, ticket)

        except Exception as e:
            alert_trade_failure(
                account_name=account_name,
                action="handle_close_event",
                ticket=ticket,
                exc=e,
                mt5_symbol=mt5_symbol,
                position_id=position_id if "position_id" in locals() else None,
                symbol_id=symbol_id if "symbol_id" in locals() else None,
                close_lots=close_lots,
                follower_units=follower_units if "follower_units" in locals() else None,
                mt5_contract_size=mt5_contract_size,
                magic=magic,
            )

    if close_lots is not None:
        with _MASTER_LOTS_LOCK:
            MASTER_CLOSED_LOTS[int(ticket)] = master_closed_lots + float(close_lots)

    try:
        if close_lots is None:
            with _MASTER_LOTS_LOCK:
                MASTER_OPEN_LOTS.pop(int(ticket), None)
                MASTER_CLOSED_LOTS.pop(int(ticket), None)
    except Exception:
        pass
