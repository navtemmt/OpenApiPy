"""
Trade event processing and handling logic.
Processes incoming MT5 trade events and routes them to appropriate handlers.
"""

import time

from app_state import logger, PENDING_SLTP, MASTER_OPEN_LOTS, MASTER_CLOSED_LOTS
from trade_executor import copy_open_to_account, copy_pending_to_account
from symbol_mapper import SymbolMapper


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
        "op_buylimit": "limit",
        "op_selllimit": "limit",
        "buy_stop": "stop",
        "sell_stop": "stop",
        "buystop": "stop",
        "sellstop": "stop",
        "op_buystop": "stop",
        "op_sellstop": "stop",
        "buy_stop_limit": "stop_limit",
        "sell_stop_limit": "stop_limit",
        "buystoplimit": "stop_limit",
        "sellstoplimit": "stop_limit",
    }
    return aliases.get(raw, raw)


def _lots_to_ctrader_cents(lots: float, mt5_contract_size: float) -> int:
    """
    MT5 lots -> underlying units -> cTrader cents-of-units.
    units = lots * contract_size
    cents = units * 100
    """
    units = float(lots) * float(mt5_contract_size or 0.0)
    return int(round(units * 100.0))


def _has_valid_sl(sl_value) -> bool:
    try:
        return float(sl_value or 0) > 0
    except Exception:
        return False


def _risk_mode(config) -> str:
    """
    Read risk_mode robustly even if config value accidentally includes inline comment fragments.
    """
    raw = str(getattr(config, "risk_mode", "SOURCE_VOLUME") or "SOURCE_VOLUME")
    raw = raw.split(";", 1)[0].split("#", 1)[0]
    return raw.strip().upper()


def _risk_reference(config) -> str:
    """
    What to use as base for PERCENT_EQUITY:
      - EQUITY (default)
      - BALANCE
    """
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
        return v if v >= 0 else 10.0
    except Exception:
        return 10.0


def _startup_pending_expiration_ms(config) -> int:
    try:
        v = int(float(getattr(config, "startup_pending_expiration_ms", 0) or 0))
        return v if v >= 0 else 0
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
    """
    Estimate money risk (in deposit currency) for 1.0 lot given entry and SL
    using cTrader symbol specs (tickValue + pipPosition/digits).

    Returns 0 if cannot compute.
    """
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
    """
    Estimate money risk per 1.0 MT5 lot using MT5 contract meta.

    Assumes MT5 account currency and cTrader deposit currency are effectively the same
    (e.g., both USD), so loss in quote currency ~= loss in account currency.

    Uses:
      - entry_price, sl_price
      - mt5_contract_size
    """
    try:
        entry = float(entry_price or 0.0)
        sl = float(sl_price or 0.0)
        if entry <= 0 or sl <= 0:
            return 0.0

        dist = abs(entry - sl)
        if dist <= 0:
            return 0.0

        mt5_contract_size = float(data.get("mt5_contract_size", 0) or 0.0)
        if mt5_contract_size <= 0:
            return 0.0

        return dist * mt5_contract_size
    except Exception:
        return 0.0


def _enforce_max_risk_on_fill(
    account_name,
    client,
    config,
    account_manager,
    position,
    symbol,
):
    """
    After a follower position is OPEN, trim excess volume if actual risk
    (based on fill price and SL) exceeds the intended risk for FIXED_USD / PERCENT_EQUITY.
    Uses only cTrader symbol/position data (no MT5 payload).
    """
    rm = _risk_mode(config)
    if rm not in ("FIXED_USD", "PERCENT_EQUITY"):
        return

    entry = float(getattr(position, "price", 0) or 0.0)
    sl = float(getattr(position, "stopLoss", 0) or 0.0)
    if entry <= 0 or sl <= 0:
        return

    risk_per_1lot = _estimate_risk_ccy_per_1lot_from_symbol(symbol, entry, sl)
    if risk_per_1lot <= 0:
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
    except Exception as e:
        logger.error(
            f"[{account_name}] Failed over-risk partial close for "
            f"positionId={position.positionId}: {e}"
        )


def _resolve_open_volume_for_account(data: dict, config, *, account_name=None, client=None, account_manager=None):
    """
    Decide which lots to use for OPEN based on per-account risk settings.

    risk_mode:
      - SOURCE_VOLUME
      - FIXED_LOT
      - FIXED_USD
      - PERCENT_EQUITY

    Returns:
      (lots: float | None, decision: str)
    """
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
        if not (account_manager and client and account_name):
            return None, f"REJECT_{risk_mode}_MISSING_CONTEXT"

        mt5_symbol = data.get("symbol")

        symbol_id = _get_symbol_id_for_account(client, config, mt5_symbol)
        if symbol_id is None:
            return None, f"REJECT_{risk_mode}_NO_SYMBOL_ID"

        symbol = _get_symbol_details(client, int(symbol_id))
        if symbol is None:
            return None, f"REJECT_{risk_mode}_NO_SYMBOL_DETAILS"

        entry_price = float(data.get("entry_price", 0) or 0.0)
        if entry_price <= 0:
            return None, f"REJECT_{risk_mode}_NO_ENTRY_PRICE_FROM_MT5"

        risk_per_1lot = _estimate_risk_ccy_per_1lot_from_symbol(symbol, float(entry_price), float(sl))

        if risk_per_1lot <= 0:
            risk_per_1lot = _estimate_risk_ccy_per_1lot_from_mt5(data, float(entry_price), float(sl))

        if risk_per_1lot <= 0:
            return None, f"REJECT_{risk_mode}_CANNOT_PRICE_RISK"

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

        return lots, f"{risk_mode} usd={usd_risk:.2f} perLot={risk_per_1lot:.2f} entry={float(entry_price):.5f}"

    return src_lots, f"{risk_mode}_USING_SOURCE_VOLUME_FOR_NOW"


def _extract_open_entry_price(data: dict) -> float:
    for key in ("entry_price", "open_price", "price", "entry", "openPrice"):
        v = _to_float(data.get(key, 0), 0.0)
        if v > 0:
            return v
    return 0.0


def _is_startup_market_recovery(data: dict) -> bool:
    if _to_bool(data.get("startup_sync"), False):
        return True
    if _to_bool(data.get("startup_recovery"), False):
        return True
    if _to_bool(data.get("is_startup_sync"), False):
        return True
    if _to_bool(data.get("recovery"), False):
        return True

    sync_origin = str(
        data.get("sync_origin")
        or data.get("origin")
        or data.get("source")
        or data.get("reason")
        or ""
    ).strip().lower()

    return sync_origin in ("startup", "startup_sync", "startup_recovery", "recovery")


def _quote_value_from_obj(obj, *names):
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
        if hasattr(client, "symbol_quotes"):
            quote_obj = client.symbol_quotes.get(int(symbol_id))
    except Exception:
        quote_obj = None

    ask = _first_positive_float(
        _quote_value_from_obj(quote_obj, "ask", "askPrice", "bestAsk"),
        _quote_value_from_obj(symbol, "ask", "askPrice", "bestAsk"),
    )
    bid = _first_positive_float(
        _quote_value_from_obj(quote_obj, "bid", "bidPrice", "bestBid"),
        _quote_value_from_obj(symbol, "bid", "bidPrice", "bestBid"),
    )

    if side == "BUY":
        return ask if ask is not None else bid
    if side == "SELL":
        return bid if bid is not None else ask
    return ask if ask is not None else bid


def _build_startup_recovery_plan(client, config, mt5_symbol: str, side: str, entry_price: float):
    mode = _startup_market_recovery_mode(config)

    if mode == "skip":
        return {"action": "skip", "reason": "startup_market_recovery_mode=skip"}

    if mode == "market":
        return {"action": "market", "reason": "startup_market_recovery_mode=market"}

    symbol_id = _get_symbol_id_for_account(client, config, mt5_symbol)
    if symbol_id is None:
        return {"action": "skip", "reason": "startup recovery: no symbol_id"}

    symbol = _get_symbol_details(client, int(symbol_id))
    if symbol is None:
        return {"action": "skip", "reason": "startup recovery: no symbol details"}

    if float(entry_price or 0.0) <= 0:
        return {"action": "skip", "reason": "startup recovery: missing entry_price"}

    current_price = _get_current_market_price(client, int(symbol_id), side)
    if current_price is None or float(current_price) <= 0:
        return {"action": "skip", "reason": "startup recovery: current market price unavailable"}

    pip_size = _symbol_pip_size(symbol)
    if pip_size <= 0:
        return {"action": "skip", "reason": "startup recovery: invalid pip size"}

    distance_pips = abs(float(current_price) - float(entry_price)) / float(pip_size)
    max_distance_pips = _startup_market_max_distance_pips(config)

    if distance_pips <= max_distance_pips:
        return {
            "action": "market",
            "reason": (
                f"startup recovery: current={float(current_price):.5f}, "
                f"entry={float(entry_price):.5f}, distance_pips={distance_pips:.2f} "
                f"<= max_distance_pips={max_distance_pips:.2f}"
            ),
        }

    side = str(side or "").strip().upper()

    if side == "BUY":
        if float(current_price) > float(entry_price):
            return {
                "action": "pending",
                "pending_type": "limit",
                "limit_price": float(entry_price),
                "stop_price": 0.0,
                "reason": (
                    f"startup recovery BUY -> LIMIT at entry: current={float(current_price):.5f}, "
                    f"entry={float(entry_price):.5f}, distance_pips={distance_pips:.2f}"
                ),
            }
        return {
            "action": "pending",
            "pending_type": "stop",
            "stop_price": float(entry_price),
            "limit_price": 0.0,
            "reason": (
                f"startup recovery BUY -> STOP at entry: current={float(current_price):.5f}, "
                f"entry={float(entry_price):.5f}, distance_pips={distance_pips:.2f}"
            ),
        }

    if side == "SELL":
        if float(current_price) < float(entry_price):
            return {
                "action": "pending",
                "pending_type": "limit",
                "limit_price": float(entry_price),
                "stop_price": 0.0,
                "reason": (
                    f"startup recovery SELL -> LIMIT at entry: current={float(current_price):.5f}, "
                    f"entry={float(entry_price):.5f}, distance_pips={distance_pips:.2f}"
                ),
            }
        return {
            "action": "pending",
            "pending_type": "stop",
            "stop_price": float(entry_price),
            "limit_price": 0.0,
            "reason": (
                f"startup recovery SELL -> STOP at entry: current={float(current_price):.5f}, "
                f"entry={float(entry_price):.5f}, distance_pips={distance_pips:.2f}"
            ),
        }

    return {"action": "skip", "reason": f"startup recovery: unsupported side={side!r}"}


def try_apply_pending_sltp(account_name, client, config, ticket, account_manager):
    pending = PENDING_SLTP.get(int(ticket))
    if not pending:
        return

    position_id = account_manager.get_position_id(account_name, int(ticket))
    if not position_id:
        return

    mt5_symbol = pending.get("symbol")
    new_sl = float(pending.get("sl", 0) or 0)
    new_tp = float(pending.get("tp", 0) or 0)
    symbol_id = _get_symbol_id_for_account(client, config, mt5_symbol)

    logger.info(
        f"[{account_name}] Applying pending SL/TP for ticket {ticket} -> "
        f"positionId={position_id}, symbolId={symbol_id}, SL={new_sl}, TP={new_tp}"
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
        del PENDING_SLTP[int(ticket)]
    except Exception as e:
        logger.error(f"[{account_name}] Failed to apply pending SL/TP for ticket {ticket}: {e}")


def notify_position_update(account_name, ticket, account_manager):
    """
    Call this when you learn ticket->positionId mapping (usually on ORDER_FILLED).
    It tries to apply pending SL/TP immediately.
    """
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
        elif event_type == "PENDING_CANCEL":
            handle_pending_cancel_event(data, account_manager)
        elif event_type == "MODIFY":
            handle_modify_event(data, account_manager)
        elif event_type == "CLOSE":
            handle_close_event(data, account_manager)
        else:
            logger.warning(f"Unknown event type: {event_type}")

    except Exception as e:
        logger.error(f"Error processing trade event: {e}")
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

    if src_volume > 0:
        MASTER_OPEN_LOTS[int(ticket)] = float(src_volume)
        MASTER_CLOSED_LOTS[int(ticket)] = 0.0

    if sl > 0 or tp > 0:
        PENDING_SLTP[int(ticket)] = {"symbol": mt5_symbol, "sl": float(sl), "tp": float(tp)}

    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            existing_position_id = account_manager.get_position_id(account_name, int(ticket))
            if existing_position_id:
                logger.info(
                    f"[{account_name}] OPEN skip for ticket {ticket}: "
                    f"already mapped to positionId={existing_position_id}"
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
                logger.warning(f"[{account_name}] OPEN rejected for ticket {ticket}: {decision}")
                continue

            logger.info(f"[{account_name}] OPEN sizing: {decision}, lots={float(lots):.4f}")

            if is_startup_recovery:
                if not _startup_sync_market_orders_enabled(config):
                    logger.info(
                        f"[{account_name}] Startup recovery skipped for ticket {ticket}: "
                        f"startup_sync_market_orders=false"
                    )
                    continue

                recovery_plan = _build_startup_recovery_plan(
                    client=client,
                    config=config,
                    mt5_symbol=mt5_symbol,
                    side=side,
                    entry_price=entry_price,
                )

                logger.info(
                    f"[{account_name}] Startup recovery decision for ticket {ticket}: "
                    f"{recovery_plan.get('reason', recovery_plan.get('action'))}"
                )

                if recovery_plan.get("action") == "skip":
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
        except Exception as e:
            logger.error(f"[{account_name}] Failed to copy OPEN event: {e}")


def handle_pending_open_event(data, account_manager):
    """
    Pending order open (LIMIT / STOP / STOP_LIMIT).

    Canonical expected payload after bridge normalization:
      pending_type: 'limit' | 'stop' | 'stop_limit'
      ticket, symbol, side, volume, sl, tp, magic
      entry_price / stop_price / limit_price
      expiration_ms (optional)
    """
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
        logger.warning(
            f"PENDING_OPEN ignored for ticket {ticket}: unsupported pending_type={pending_type!r}"
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
        pending_entry_price = (
            float(limit_price or 0.0)
            if float(limit_price or 0.0) > 0
            else float(stop_price or 0.0)
        )

    for account_name, (client, config) in account_manager.get_all_accounts().items():
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
                    logger.warning(
                        f"[{account_name}] PENDING_OPEN rejected for ticket {ticket}: {decision}"
                    )
                    continue

                sizing_volume = float(lots)
                logger.info(
                    f"[{account_name}] PENDING_OPEN sizing: {decision}, lots={float(lots):.4f}"
                )

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
            logger.error(f"[{account_name}] Failed to copy PENDING_OPEN event: {e}")


def handle_pending_cancel_event(data, account_manager):
    """
    Cancel pending order by master ticket.

    Uses AccountManager mapping: per-account ticket -> cTrader orderId.
    """
    ticket = _to_int(data.get("ticket", 0), 0)
    mt5_symbol = data.get("symbol")

    logger.info(f"PENDING_CANCEL event - Ticket: {ticket}, Symbol: {mt5_symbol}")

    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            order_id = account_manager.get_order_id(account_name, int(ticket))
            if not order_id:
                logger.warning(
                    f"[{account_name}] PENDING_CANCEL ignored for ticket {ticket} (no orderId mapping yet)"
                )
                continue

            client.cancel_pending_order(account_id=config.account_id, order_id=int(order_id))
            logger.info(f"[{account_name}] Cancel sent: ticket {ticket} -> orderId {int(order_id)}")

        except Exception as e:
            logger.error(f"[{account_name}] Failed to cancel pending for ticket {ticket}: {e}")


def handle_modify_event(data, account_manager):
    ticket = _to_int(data.get("ticket"))
    mt5_symbol = data.get("symbol")
    new_sl = _to_float(data.get("sl", 0), 0.0)
    new_tp = _to_float(data.get("tp", 0), 0.0)

    logger.info(
        f"MODIFY event - Ticket: {ticket}, Symbol: {mt5_symbol}, "
        f"New SL: {new_sl}, New TP: {new_tp}"
    )

    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            position_id = account_manager.get_position_id(account_name, ticket)
            symbol_id = _get_symbol_id_for_account(client, config, mt5_symbol)

            if position_id:
                client.amend_position(
                    account_id=config.account_id,
                    position_id=position_id,
                    symbol_id=symbol_id,
                    stop_loss=new_sl if new_sl > 0 else None,
                    take_profit=new_tp if new_tp > 0 else None,
                )
                logger.info(f"[{account_name}] Modified position {position_id} for ticket {ticket}")
            else:
                logger.warning(
                    f"[{account_name}] Position not found for ticket {ticket}, storing pending SL/TP"
                )
                PENDING_SLTP[int(ticket)] = {"symbol": mt5_symbol, "sl": new_sl, "tp": new_tp}

        except Exception as e:
            logger.error(f"[{account_name}] Failed to modify position for ticket {ticket}: {e}")


def handle_close_event(data, account_manager):
    ticket = _to_int(data.get("ticket"))
    mt5_symbol = data.get("symbol")

    close_lots = _to_float_or_none(data.get("volume", None))
    mt5_contract_size = _to_float(data.get("mt5_contract_size", 0), 0.0)

    logger.info(f"CLOSE event - Ticket: {ticket}, Symbol: {mt5_symbol}, close_lots={close_lots}")

    master_open_lots = float(MASTER_OPEN_LOTS.get(int(ticket), 0) or 0)
    master_closed_lots = float(MASTER_CLOSED_LOTS.get(int(ticket), 0) or 0)
    master_remaining_lots = max(0.0, master_open_lots - master_closed_lots)

    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            position_id = account_manager.get_position_id(account_name, ticket)
            if not position_id:
                logger.info(f"[{account_name}] CLOSE ignored for ticket {ticket} (no mapping)")
                continue

            symbol_id = _get_symbol_id_for_account(client, config, mt5_symbol)
            rm = _risk_mode(config)
            follower_units = account_manager.get_position_volume(account_name, position_id)

            close_units = None

            if close_lots is not None and follower_units is not None and int(follower_units) > 0:
                if rm != "SOURCE_VOLUME" and master_remaining_lots > 0:
                    pct = float(close_lots) / float(master_remaining_lots)
                    pct = max(0.0, min(1.0, pct))
                    close_units = int(round(pct * float(follower_units)))

                    logger.info(
                        f"[{account_name}] Proportional CLOSE: risk_mode={rm}, "
                        f"master_close_lots={float(close_lots):.4f}, "
                        f"master_remaining_lots={master_remaining_lots:.4f}, "
                        f"pct={pct:.4f}, follower_units={int(follower_units)} -> close_units={close_units}"
                    )

                    MASTER_CLOSED_LOTS[int(ticket)] = master_closed_lots + float(close_lots)
                    master_closed_lots = MASTER_CLOSED_LOTS[int(ticket)]
                    master_remaining_lots = max(0.0, master_open_lots - master_closed_lots)
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
                logger.warning(
                    f"[{account_name}] Cannot close ticket {ticket} (positionId={position_id}) "
                    f"because close volume is unknown/invalid."
                )
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

        except Exception as e:
            logger.error(f"[{account_name}] Failed to close position for ticket {ticket}: {e}")

    if int(ticket) in PENDING_SLTP:
        del PENDING_SLTP[int(ticket)]

    try:
        if close_lots is None:
            MASTER_OPEN_LOTS.pop(int(ticket), None)
            MASTER_CLOSED_LOTS.pop(int(ticket), None)
    except Exception:
        pass
