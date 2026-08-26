"""
Trade execution logic for copying MT5 orders to cTrader accounts.
Handles volume conversion and order placement.
"""

from config_loader import get_multi_account_config
from symbol_mapper import SymbolMapper
from app_state import logger, notify_error, notify_warning, notify_info


def _base_context(
    account_name=None,
    ticket=None,
    mt5_symbol=None,
    resolved_symbol=None,
    symbol_id=None,
    side=None,
    volume=None,
    magic=None,
    pending_type=None,
    adjusted_lots=None,
    volume_units=None,
    sl=None,
    tp=None,
    stop_price=None,
    limit_price=None,
    expiration_ms=None,
):
    ctx = {}
    if account_name is not None:
        ctx["account_name"] = account_name
    if ticket is not None:
        ctx["ticket"] = int(ticket)
    if mt5_symbol is not None:
        ctx["mt5_symbol"] = str(mt5_symbol)
    if resolved_symbol is not None:
        ctx["resolved_symbol"] = str(resolved_symbol)
    if symbol_id is not None:
        ctx["symbol_id"] = int(symbol_id)
    if side is not None:
        ctx["side"] = str(side)
    if volume is not None:
        ctx["volume"] = volume
    if magic is not None:
        ctx["magic"] = magic
    if pending_type is not None:
        ctx["pending_type"] = str(pending_type)
    if adjusted_lots is not None:
        ctx["adjusted_lots"] = float(adjusted_lots)
    if volume_units is not None:
        ctx["volume_units"] = int(volume_units)
    if sl is not None:
        ctx["sl"] = sl
    if tp is not None:
        ctx["tp"] = tp
    if stop_price is not None:
        ctx["stop_price"] = stop_price
    if limit_price is not None:
        ctx["limit_price"] = limit_price
    if expiration_ms is not None:
        ctx["expiration_ms"] = int(expiration_ms)
    return ctx


def _normalize_trade_side(side: str) -> str:
    side_norm = str(side or "").strip().upper()
    if side_norm in ("BUY", "LONG"):
        return "BUY"
    if side_norm in ("SELL", "SHORT"):
        return "SELL"
    raise ValueError(f"Unsupported trade side: {side}")


def _normalize_pending_type(pending_type: str) -> str:
    ptype = str(pending_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "limit": "limit",
        "stop": "stop",
        "stop_limit": "stop_limit",
        "stoplimit": "stop_limit",
        "buy_limit": "limit",
        "sell_limit": "limit",
        "buylimit": "limit",
        "selllimit": "limit",
        "buy_stop": "stop",
        "sell_stop": "stop",
        "buystop": "stop",
        "sellstop": "stop",
        "buy_stop_limit": "stop_limit",
        "sell_stop_limit": "stop_limit",
        "buystoplimit": "stop_limit",
        "sellstoplimit": "stop_limit",
    }
    return aliases.get(ptype, ptype)


def _snap_volume_units(
    volume_units: int,
    min_units: int,
    max_units: int,
    step_units: int,
) -> int:
    """
    Clamp and snap cTrader Open API volume (UNITS) to broker constraints.
    """
    v = int(volume_units or 0)

    if min_units and int(min_units) > 0:
        v = max(v, int(min_units))
    if max_units and int(max_units) > 0:
        v = min(v, int(max_units))

    if step_units and int(step_units) > 0:
        base = int(min_units) if (min_units and int(min_units) > 0) else 0
        steps = round((v - base) / float(step_units))
        v = base + int(steps) * int(step_units)

    if min_units and int(min_units) > 0:
        v = max(v, int(min_units))

    return int(v)


def _map_symbol_id(client, config, mt5_symbol: str):
    mapper = SymbolMapper(
        prefix=getattr(config, "symbol_prefix", ""),
        suffix=getattr(config, "symbol_suffix", ""),
        custom_map=getattr(config, "custom_symbols", {}),
        broker_symbol_map=client.symbol_name_to_id,
        strict=True,
    )
    return mapper.get_symbol_id(mt5_symbol)


def _resolve_ctrader_symbol_name(client, symbol_id: int, fallback: str = "") -> str:
    """
    Best-effort reverse lookup: symbolId -> cTrader symbol name.
    """
    try:
        symbol = client.symbol_details.get(int(symbol_id)) if hasattr(client, "symbol_details") else None
        name = getattr(symbol, "symbolName", None) if symbol is not None else None
        if name:
            return str(name)
    except Exception:
        pass

    try:
        broker_symbol_map = getattr(client, "symbol_name_to_id", {}) or {}
        for name, sid in broker_symbol_map.items():
            if int(sid) == int(symbol_id):
                return str(name)
    except Exception:
        pass

    return str(fallback or f"symbolId={symbol_id}")


def _should_copy(account_name, config, mt5_symbol, magic, volume):
    multi_config = get_multi_account_config()
    should_copy, reason = multi_config.should_copy_trade(config, mt5_symbol, magic, volume)
    if not should_copy:
        logger.info(
            f"[{account_name}] Skipping copy | symbol={mt5_symbol} magic={magic} volume={volume} reason={reason}"
        )
        notify_info(
            event="trade_copy_skipped",
            message=f"Trade copy skipped: {reason}",
            account_name=account_name,
            mt5_symbol=mt5_symbol,
            magic=magic,
            volume=volume,
            reason=reason,
        )
        return False
    return True


def _calc_volume_units(account_name, client, config, symbol_id: int, mt5_symbol: str, mt5_lots: float) -> int:
    """
    Convert MT5 lots -> cTrader volume UNITS using cTrader symbol lotSize, then snap.
    """
    symbol = client.symbol_details.get(symbol_id) if hasattr(client, "symbol_details") else None
    resolved_symbol = _resolve_ctrader_symbol_name(client, symbol_id, fallback=mt5_symbol)

    if symbol is None:
        msg = (
            f"Missing cTrader symbol_details for mt5_symbol={mt5_symbol} "
            f"resolved_symbol={resolved_symbol} symbolId={symbol_id}. "
            f"Wait for symbols to load before trading."
        )
        logger.error(f"[{account_name}] {msg}")
        notify_warning(
            event="volume_conversion_missing_symbol_details",
            message=msg,
            **_base_context(
                account_name=account_name,
                mt5_symbol=mt5_symbol,
                resolved_symbol=resolved_symbol,
                symbol_id=symbol_id,
            ),
        )
        return 0

    lot_size = int(getattr(symbol, "lotSize", 0) or 0)
    min_units = int(getattr(symbol, "minVolume", 0) or 0)
    max_units = int(getattr(symbol, "maxVolume", 0) or 0)
    step_units = int(getattr(symbol, "stepVolume", 0) or 0)

    if lot_size <= 0 or min_units <= 0 or step_units <= 0:
        msg = (
            f"Invalid cTrader symbol specs for mt5_symbol={mt5_symbol} "
            f"resolved_symbol={resolved_symbol} symbolId={symbol_id}: "
            f"lotSize={lot_size}, minVolume={min_units}, stepVolume={step_units}, maxVolume={max_units}"
        )
        logger.error(f"[{account_name}] {msg}")
        notify_warning(
            event="volume_conversion_invalid_symbol_specs",
            message=msg,
            **_base_context(
                account_name=account_name,
                mt5_symbol=mt5_symbol,
                resolved_symbol=resolved_symbol,
                symbol_id=symbol_id,
            ),
            lot_size=lot_size,
            min_units=min_units,
            max_units=max_units,
            step_units=step_units,
        )
        return 0

    raw_units = int(round(float(mt5_lots) * float(lot_size)))
    snapped = _snap_volume_units(raw_units, min_units, max_units, step_units)

    logger.info(
        f"[{account_name}] Volume conversion (cTrader specs): "
        f"symbol={resolved_symbol} symbolId={symbol_id}, mt5_symbol={mt5_symbol}, "
        f"mt5_lots={mt5_lots:.4f}, lotSize={lot_size}, "
        f"min={min_units}, max={max_units}, step={step_units} -> raw_units={raw_units}, units={snapped}"
    )
    return int(snapped)


def copy_open_to_account(
    account_name,
    client,
    config,
    ticket,
    mt5_symbol,
    side,
    volume,
    sl,
    tp,
    magic,
):
    """Execute a new market order on cTrader for a given account."""

    try:
        symbol_id = _map_symbol_id(client, config, mt5_symbol)
    except Exception as e:
        notify_error(
            event="map_symbol_market_open",
            message=str(e),
            exc=e,
            **_base_context(
                account_name=account_name,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                side=side,
                volume=volume,
                magic=magic,
            ),
        )
        raise

    if symbol_id is None:
        msg = f"Could not map MT5 symbol to cTrader symbolId | ticket={ticket} mt5_symbol={mt5_symbol}"
        logger.error(f"[{account_name}] {msg}")
        notify_warning(
            event="map_symbol_market_open_none",
            message=msg,
            **_base_context(
                account_name=account_name,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                side=side,
                volume=volume,
                magic=magic,
            ),
        )
        return

    resolved_symbol = _resolve_ctrader_symbol_name(client, symbol_id, fallback=mt5_symbol)

    if not _should_copy(account_name, config, mt5_symbol, magic, volume):
        return

    try:
        trade_side = _normalize_trade_side(side)
        adjusted_lots = getattr(config, "lot_multiplier", 1.0) * float(volume)
        adjusted_lots = max(
            float(getattr(config, "min_lot_size", 0.01)),
            min(adjusted_lots, float(getattr(config, "max_lot_size", 100.0))),
        )
    except Exception as e:
        notify_error(
            event="prepare_market_open",
            message=str(e),
            exc=e,
            **_base_context(
                account_name=account_name,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                resolved_symbol=resolved_symbol,
                symbol_id=symbol_id,
                side=side,
                volume=volume,
                magic=magic,
            ),
        )
        raise

    volume_to_send = _calc_volume_units(
        account_name=account_name,
        client=client,
        config=config,
        symbol_id=symbol_id,
        mt5_symbol=mt5_symbol,
        mt5_lots=float(adjusted_lots),
    )

    if volume_to_send <= 0:
        msg = (
            f"Skipping zero or negative volume | "
            f"ticket={ticket} symbol={resolved_symbol} symbolId={symbol_id}"
        )
        logger.warning(f"[{account_name}] {msg}")
        notify_warning(
            event="market_open_zero_volume",
            message=msg,
            **_base_context(
                account_name=account_name,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                resolved_symbol=resolved_symbol,
                symbol_id=symbol_id,
                adjusted_lots=adjusted_lots,
            ),
        )
        return

    logger.info(
        f"[{account_name}] Opening {trade_side} | "
        f"ticket={ticket} symbol={resolved_symbol} symbolId={symbol_id} mt5_symbol={mt5_symbol} | "
        f"Volume={volume_to_send} units ({adjusted_lots:.4f} lots before cTrader snap) | "
        f"SL={sl} TP={tp} | Label=MT5_{ticket}"
    )

    try:
        response = client.send_market_order(
            account_id=config.account_id,
            symbol_id=symbol_id,
            side=trade_side,
            volume=volume_to_send,
            sl=None,
            tp=None,
            label=f"MT5_{ticket}",
        )

        logger.info(
            f"[{account_name}] Order submitted | "
            f"ticket={ticket} symbol={resolved_symbol} symbolId={symbol_id} label=MT5_{ticket}"
        )
        notify_info(
            event="market_order_submitted",
            message="Market order submitted to cTrader",
            **_base_context(
                account_name=account_name,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                resolved_symbol=resolved_symbol,
                symbol_id=symbol_id,
                side=trade_side,
                volume_units=volume_to_send,
                adjusted_lots=adjusted_lots,
                sl=sl,
                tp=tp,
                magic=magic,
            ),
        )
        return response

    except Exception as e:
        notify_error(
            event="send_market_order",
            message=str(e),
            exc=e,
            **_base_context(
                account_name=account_name,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                resolved_symbol=resolved_symbol,
                symbol_id=symbol_id,
                side=trade_side,
                volume_units=volume_to_send,
                adjusted_lots=adjusted_lots,
                sl=sl,
                tp=tp,
                magic=magic,
            ),
        )
        raise


def copy_pending_to_account(
    account_name,
    client,
    config,
    ticket,
    mt5_symbol,
    side,
    volume,
    sl,
    tp,
    magic,
    pending_type: str,
    stop_price: float = 0.0,
    limit_price: float = 0.0,
    expiration_ms: int = 0,
):
    """
    Create a pending order on cTrader (LIMIT / STOP / STOP_LIMIT).

    pending_type: 'limit' | 'stop' | 'stop_limit'
    stop_price / limit_price: required depending on pending_type
    expiration_ms: optional (ms since epoch), 0 means no expiry
    """

    try:
        symbol_id = _map_symbol_id(client, config, mt5_symbol)
    except Exception as e:
        notify_error(
            event="map_symbol_pending_open",
            message=str(e),
            exc=e,
            **_base_context(
                account_name=account_name,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                side=side,
                volume=volume,
                magic=magic,
                pending_type=pending_type,
            ),
        )
        raise

    if symbol_id is None:
        msg = f"Could not map MT5 symbol to cTrader symbolId | ticket={ticket} mt5_symbol={mt5_symbol}"
        logger.error(f"[{account_name}] {msg}")
        notify_warning(
            event="map_symbol_pending_open_none",
            message=msg,
            **_base_context(
                account_name=account_name,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                side=side,
                volume=volume,
                magic=magic,
                pending_type=pending_type,
            ),
        )
        return

    resolved_symbol = _resolve_ctrader_symbol_name(client, symbol_id, fallback=mt5_symbol)

    if not _should_copy(account_name, config, mt5_symbol, magic, volume):
        return

    try:
        trade_side = _normalize_trade_side(side)
        ptype = _normalize_pending_type(pending_type)
        adjusted_lots = getattr(config, "lot_multiplier", 1.0) * float(volume)
        adjusted_lots = max(
            float(getattr(config, "min_lot_size", 0.01)),
            min(adjusted_lots, float(getattr(config, "max_lot_size", 100.0))),
        )
    except Exception as e:
        notify_error(
            event="prepare_pending_open",
            message=str(e),
            exc=e,
            **_base_context(
                account_name=account_name,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                resolved_symbol=resolved_symbol,
                symbol_id=symbol_id,
                side=side,
                volume=volume,
                magic=magic,
                pending_type=pending_type,
            ),
        )
        raise

    volume_to_send = _calc_volume_units(
        account_name=account_name,
        client=client,
        config=config,
        symbol_id=symbol_id,
        mt5_symbol=mt5_symbol,
        mt5_lots=float(adjusted_lots),
    )

    if volume_to_send <= 0:
        msg = (
            f"Skipping zero or negative pending volume | "
            f"ticket={ticket} symbol={resolved_symbol} symbolId={symbol_id}"
        )
        logger.warning(f"[{account_name}] {msg}")
        notify_warning(
            event="pending_open_zero_volume",
            message=msg,
            **_base_context(
                account_name=account_name,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                resolved_symbol=resolved_symbol,
                symbol_id=symbol_id,
                adjusted_lots=adjusted_lots,
                pending_type=ptype,
            ),
        )
        return

    try:
        sl_r = client.round_price_for_symbol(symbol_id, float(sl)) if sl and float(sl) > 0 else None
        tp_r = client.round_price_for_symbol(symbol_id, float(tp)) if tp and float(tp) > 0 else None
        stop_r = (
            client.round_price_for_symbol(symbol_id, float(stop_price))
            if stop_price and float(stop_price) > 0
            else 0.0
        )
        limit_r = (
            client.round_price_for_symbol(symbol_id, float(limit_price))
            if limit_price and float(limit_price) > 0
            else 0.0
        )
    except Exception as e:
        notify_error(
            event="round_pending_prices",
            message=str(e),
            exc=e,
            **_base_context(
                account_name=account_name,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                resolved_symbol=resolved_symbol,
                symbol_id=symbol_id,
                side=trade_side,
                pending_type=ptype,
                sl=sl,
                tp=tp,
                stop_price=stop_price,
                limit_price=limit_price,
            ),
        )
        raise

    logger.info(
        f"[{account_name}] Creating pending {ptype.upper()} {trade_side} | "
        f"ticket={ticket} symbol={resolved_symbol} symbolId={symbol_id} mt5_symbol={mt5_symbol} | "
        f"Volume={volume_to_send} units ({adjusted_lots:.4f} lots before cTrader snap) | "
        f"stop={stop_r} limit={limit_r} SL={sl_r} TP={tp_r} | "
        f"Label=MT5_{ticket} | expiry_ms={int(expiration_ms or 0)}"
    )

    try:
        resp = client.send_pending_order(
            account_id=config.account_id,
            symbol_id=symbol_id,
            side=trade_side,
            volume=volume_to_send,
            pending_type=ptype,
            stop_price=stop_r,
            limit_price=limit_r,
            sl=sl_r,
            tp=tp_r,
            label=f"MT5_{ticket}",
            expiration_ms=int(expiration_ms or 0),
        )
        logger.info(
            f"[{account_name}] Pending order submitted | "
            f"ticket={ticket} symbol={resolved_symbol} symbolId={symbol_id} label=MT5_{ticket}"
        )
        notify_info(
            event="pending_order_submitted",
            message="Pending order submitted to cTrader",
            **_base_context(
                account_name=account_name,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                resolved_symbol=resolved_symbol,
                symbol_id=symbol_id,
                side=trade_side,
                volume_units=volume_to_send,
                adjusted_lots=adjusted_lots,
                pending_type=ptype,
                stop_price=stop_r,
                limit_price=limit_r,
                sl=sl_r,
                tp=tp_r,
                expiration_ms=int(expiration_ms or 0),
                magic=magic,
            ),
        )
        return resp

    except Exception as e:
        notify_error(
            event="send_pending_order",
            message=str(e),
            exc=e,
            **_base_context(
                account_name=account_name,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                resolved_symbol=resolved_symbol,
                symbol_id=symbol_id,
                side=trade_side,
                volume_units=volume_to_send,
                adjusted_lots=adjusted_lots,
                pending_type=ptype,
                stop_price=stop_r,
                limit_price=limit_r,
                sl=sl_r,
                tp=tp_r,
                expiration_ms=int(expiration_ms or 0),
                magic=magic,
            ),
        )
        raise
