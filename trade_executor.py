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
    reason=None,
    order_id=None,
):
    ctx = {}
    if account_name is not None:
        ctx["account_name"] = str(account_name)
    if ticket is not None:
        try:
            ctx["ticket"] = int(ticket)
        except Exception:
            ctx["ticket"] = ticket
    if mt5_symbol is not None:
        ctx["mt5_symbol"] = str(mt5_symbol)
    if resolved_symbol is not None:
        ctx["resolved_symbol"] = str(resolved_symbol)
    if symbol_id is not None:
        try:
            ctx["symbol_id"] = int(symbol_id)
        except Exception:
            ctx["symbol_id"] = symbol_id
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
        try:
            ctx["volume_units"] = int(volume_units)
        except Exception:
            ctx["volume_units"] = volume_units
    if sl is not None:
        ctx["sl"] = sl
    if tp is not None:
        ctx["tp"] = tp
    if stop_price is not None:
        ctx["stop_price"] = stop_price
    if limit_price is not None:
        ctx["limit_price"] = limit_price
    if expiration_ms is not None:
        try:
            ctx["expiration_ms"] = int(expiration_ms)
        except Exception:
            ctx["expiration_ms"] = expiration_ms
    if reason is not None:
        ctx["reason"] = str(reason)
    if order_id is not None:
        try:
            ctx["order_id"] = int(order_id)
        except Exception:
            ctx["order_id"] = order_id
    return ctx


def _to_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _to_int(value, default=0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


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
    normalized = aliases.get(ptype, ptype)
    if normalized not in ("limit", "stop", "stop_limit"):
        raise ValueError(f"Unsupported pending type: {pending_type}")
    return normalized


def _clamp_lots(config, lots: float) -> float:
    raw_lots = float(lots or 0.0)
    min_lot = float(getattr(config, "min_lot_size", 0.01) or 0.01)
    max_lot = float(getattr(config, "max_lot_size", 100.0) or 100.0)

    if max_lot < min_lot:
        max_lot = min_lot

    return max(min_lot, min(raw_lots, max_lot))


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
    if max_units and int(max_units) > 0:
        v = min(v, int(max_units))

    return int(v)


def _map_symbol_id(client, config, mt5_symbol: str):
    mapper = SymbolMapper(
        prefix=getattr(config, "symbol_prefix", ""),
        suffix=getattr(config, "symbol_suffix", ""),
        custom_map=getattr(config, "custom_symbols", {}),
        broker_symbol_map=getattr(client, "symbol_name_to_id", {}) or {},
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


def _get_symbol_details(client, symbol_id: int):
    try:
        return client.symbol_details.get(int(symbol_id)) if hasattr(client, "symbol_details") else None
    except Exception:
        return None


def _round_price_or_none(client, symbol_id: int, price):
    price_f = _to_float(price, 0.0)
    if price_f <= 0:
        return None
    return client.round_price_for_symbol(int(symbol_id), float(price_f))


def _normalize_expiration_ms(expiration_ms) -> int:
    value = _to_int(expiration_ms, 0)
    return value if value > 0 else 0


def _extract_pending_order_id(response) -> int:
    try:
        if response is None:
            return 0

        direct = getattr(response, "orderId", None)
        if direct is not None and int(direct) > 0:
            return int(direct)

        order = getattr(response, "order", None)
        if order is not None:
            nested = getattr(order, "orderId", None)
            if nested is not None and int(nested) > 0:
                return int(nested)

        extracted = None
        try:
            from ctrader_open_api import Protobuf
            extracted = Protobuf.extract(response)
        except Exception:
            extracted = None

        if extracted is not None:
            direct = getattr(extracted, "orderId", None)
            if direct is not None and int(direct) > 0:
                return int(direct)

            order = getattr(extracted, "order", None)
            if order is not None:
                nested = getattr(order, "orderId", None)
                if nested is not None and int(nested) > 0:
                    return int(nested)
    except Exception:
        pass

    return 0


def _store_pending_mapping_immediately(account_name, ticket, order_id, pending_type=None):
    try:
        if int(order_id or 0) <= 0: return
        from account_manager import get_account_manager
        manager=get_account_manager()
        if manager is not None and hasattr(manager,"register_pending"):
            manager.register_pending(account_name,int(ticket),pending_type,int(order_id))
    except Exception:
        logger.debug("[%s] Failed to immediately store pending mapping for ticket %s",account_name,ticket,exc_info=True)


def _clear_stale_position_mapping(account_name, ticket):
    """
    Clear only position mapping before creating a pending order.

    This is safe because a pending order should not depend on an old positionId,
    but we intentionally do NOT clear any order mapping here. Clearing the order
    mapping during startup replay can cause duplicate pending orders to be sent
    before the next accepted orderId is persisted.
    """
    try:
        from account_manager import get_account_manager
        manager = get_account_manager()
        if manager is None:
            return
        if hasattr(manager, "_remove_position_mapping"):
            manager._remove_position_mapping(account_name, int(ticket))
    except Exception:
        logger.debug(
            "[%s] Failed to clear stale position mapping for ticket %s",
            account_name,
            ticket,
            exc_info=True,
        )


def _get_existing_order_mapping(account_name, ticket) -> int:
    try:
        from account_manager import get_account_manager
        manager = get_account_manager()
        if manager is None:
            return 0
        if hasattr(manager, "get_order_id"):
            return int(manager.get_order_id(account_name, int(ticket)) or 0)
        if hasattr(manager, "getorderid"):
            return int(manager.getorderid(account_name, int(ticket)) or 0)
        if hasattr(manager, "get_orderid"):
            return int(manager.get_orderid(account_name, int(ticket)) or 0)
    except Exception:
        logger.debug(
            "[%s] Failed to read existing order mapping for ticket %s",
            account_name,
            ticket,
            exc_info=True,
        )
    return 0


def _has_active_pending_order(client, order_id: int):
    """Return True/False when observable; None means UNKNOWN."""
    try:
        if int(order_id or 0)<=0: return False
        containers=[]
        for attr in ("pending_orders","pendingOrders","orders","open_orders","openOrders"):
            obj=getattr(client,attr,None)
            if obj is not None: containers.append(obj)
        if not containers: return None
        for container in containers:
            try:
                values=container.values() if hasattr(container,"values") else container
                if isinstance(container,dict) and int(order_id) in [int(k) for k in container.keys()]: return True
                for value in values:
                    if getattr(value,"orderId",None) is not None and int(value.orderId)==int(order_id): return True
            except Exception: continue
        return False
    except Exception:
        logger.debug("Failed active pending-order check for orderId=%s",order_id,exc_info=True)
        return None


def _should_skip_duplicate_pending(account_name, client, ticket) -> int:
    order_id=_get_existing_order_mapping(account_name,ticket)
    if order_id<=0: return 0
    active=_has_active_pending_order(client,order_id)
    if active is True: return int(order_id)
    try:
        from account_manager import get_account_manager
        manager=get_account_manager(); state=manager.get_pending_state(account_name,int(ticket)) if manager else None
        if active is None and state in ("PENDING","CANCEL_REQUESTED","UNKNOWN"): return int(order_id)
    except Exception: pass
    return 0


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
    symbol = _get_symbol_details(client, symbol_id)
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


def transition_pending_to_market(
    account_name, client, config, ticket, mt5_symbol, side, volume, sl, tp, magic,
    account_manager, pending_type=None,
):
    ticket=int(ticket)
    ptype=pending_type or account_manager.get_pending_type(account_name,ticket)
    if ptype: ptype=_normalize_pending_type(ptype)
    pending_pid=account_manager.get_pending_position_id(account_name,ticket)
    market_pid=account_manager.get_market_position_id(account_name,ticket)
    order_id=account_manager.get_order_id(account_name,ticket)
    state=account_manager.get_pending_state(account_name,ticket)

    if pending_pid:
        try:
            client.amend_position(account_id=config.account_id,position_id=int(pending_pid),sl=float(sl or 0) or None,tp=float(tp or 0) or None)
        except Exception as exc: logger.warning("[%s] Pending-origin SL/TP amend failed ticket=%s: %s",account_name,ticket,exc)
        return pending_pid

    if ptype in ("stop","stop_limit"):
        logger.warning("[%s] %s activation ticket=%s has no cTrader pending-origin fill yet; NO market fallback",account_name,ptype.upper(),ticket)
        account_manager.set_pending_state(account_name,ticket,"UNKNOWN")
        account_manager.request_reconcile(account_name)
        return None

    if ptype != "limit":
        logger.warning("[%s] Unknown pending type for ticket=%s; refusing market fallback",account_name,ticket)
        account_manager.set_pending_state(account_name,ticket,"UNKNOWN")
        account_manager.request_reconcile(account_name)
        return None

    if market_pid:
        return account_manager.get_position_id(account_name,ticket)
    if state in ("UNKNOWN","CANCEL_REQUESTED"):
        account_manager.request_reconcile(account_name)
        return None

    if order_id:
        active=_has_active_pending_order(client,order_id)
        if active is False:
            account_manager.set_pending_state(account_name,ticket,"UNKNOWN"); account_manager.request_reconcile(account_name); return None
        if active is None and state not in ("PENDING",):
            account_manager.set_pending_state(account_name,ticket,"UNKNOWN"); account_manager.request_reconcile(account_name); return None
        if not account_manager.has_market_fallback_submitted(account_name,ticket):
            response=copy_open_to_account(account_name=account_name,client=client,config=config,ticket=ticket,mt5_symbol=mt5_symbol,side=side,volume=float(volume),sl=sl,tp=tp,magic=magic,is_fallback=True)
            account_manager.register_market_order(account_name,ticket,_extract_pending_order_id(response),fallback=True)
        try:
            client.cancel_pending_order(account_id=config.account_id,order_id=int(order_id))
            account_manager.set_pending_state(account_name,ticket,"CANCEL_REQUESTED")
        except Exception as exc:
            text=str(exc).upper(); account_manager.set_pending_state(account_name,ticket,"UNKNOWN"); account_manager.request_reconcile(account_name)
            if "ORDER_NOT_FOUND" in text or "ORDER NOT FOUND" in text:
                logger.warning("[%s] LIMIT cancel ORDER_NOT_FOUND is UNKNOWN ticket=%s orderId=%s",account_name,ticket,order_id)
            else: logger.warning("[%s] LIMIT cancel failed ticket=%s orderId=%s: %s",account_name,ticket,order_id,exc)
        return None

    if state == "CANCELLED" and not account_manager.has_market_fallback_submitted(account_name,ticket):
        response=copy_open_to_account(account_name=account_name,client=client,config=config,ticket=ticket,mt5_symbol=mt5_symbol,side=side,volume=float(volume),sl=sl,tp=tp,magic=magic,is_fallback=True)
        account_manager.register_market_order(account_name,ticket,_extract_pending_order_id(response),fallback=True)
        return response
    account_manager.request_reconcile(account_name)
    return None


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
    is_fallback=False,
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
        multiplier = float(getattr(config, "lot_multiplier", 1.0) or 1.0)
        adjusted_lots = _clamp_lots(config, multiplier * float(volume))
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

        try:
            from account_manager import get_account_manager
            manager=get_account_manager()
            manager.register_market_order(account_name,int(ticket),_extract_pending_order_id(response),fallback=bool(is_fallback))
        except Exception:
            logger.debug("[%s] Failed to register market order ticket=%s",account_name,ticket,exc_info=True)

        logger.info(
            f"[{account_name}] Order submitted | "
            f"ticket={ticket} symbol={resolved_symbol} symbolId={symbol_id} label=MT5_{ticket} fallback={is_fallback}"
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

    existing_order_id = _should_skip_duplicate_pending(account_name, client, ticket)
    if existing_order_id > 0:
        logger.info(
            f"[{account_name}] PENDING_OPEN skip for ticket {ticket} "
            f"(already mapped to active orderId={existing_order_id})"
        )
        notify_info(
            event="pending_order_skipped_existing_mapping",
            message="Pending order skipped because active cTrader order mapping already exists",
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
                order_id=existing_order_id,
                reason="existing_active_pending_mapping",
            ),
        )
        return None

    try:
        trade_side = _normalize_trade_side(side)
        ptype = _normalize_pending_type(pending_type)
        multiplier = float(getattr(config, "lot_multiplier", 1.0) or 1.0)
        adjusted_lots = _clamp_lots(config, multiplier * float(volume))
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
        sl_r = _round_price_or_none(client, symbol_id, sl)
        tp_r = _round_price_or_none(client, symbol_id, tp)
        stop_r = _round_price_or_none(client, symbol_id, stop_price)
        limit_r = _round_price_or_none(client, symbol_id, limit_price)
        expiration_ms_n = _normalize_expiration_ms(expiration_ms)

        if ptype == "limit" and limit_r is None:
            raise ValueError("Pending LIMIT order requires limit_price > 0")
        if ptype == "stop" and stop_r is None:
            raise ValueError("Pending STOP order requires stop_price > 0")
        if ptype == "stop_limit" and (stop_r is None or limit_r is None):
            raise ValueError("Pending STOP_LIMIT order requires both stop_price > 0 and limit_price > 0")

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
                pending_type=ptype if "ptype" in locals() else pending_type,
                sl=sl,
                tp=tp,
                stop_price=stop_price,
                limit_price=limit_price,
                expiration_ms=expiration_ms,
            ),
        )
        raise

    pending_label = f"MT5_PENDING_{ticket}"

    logger.info(
        f"[{account_name}] Creating pending {ptype.upper()} {trade_side} | "
        f"ticket={ticket} symbol={resolved_symbol} symbolId={symbol_id} mt5_symbol={mt5_symbol} | "
        f"Volume={volume_to_send} units ({adjusted_lots:.4f} lots before cTrader snap) | "
        f"stop={stop_r} limit={limit_r} SL={sl_r} TP={tp_r} | "
        f"Label={pending_label} | expiry_ms={expiration_ms_n}"
    )

    try:
        _clear_stale_position_mapping(account_name, ticket)

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
            label=pending_label,
            expiration_ms=expiration_ms_n,
        )

        order_id = _extract_pending_order_id(resp)
        if order_id > 0:
            _store_pending_mapping_immediately(account_name, ticket, order_id, ptype)

        logger.info(
            f"[{account_name}] Pending order submitted | "
            f"ticket={ticket} symbol={resolved_symbol} symbolId={symbol_id} "
            f"label={pending_label} orderId={order_id or 'unknown'}"
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
                expiration_ms=expiration_ms_n,
                magic=magic,
                order_id=order_id or None,
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
                expiration_ms=expiration_ms_n,
                magic=magic,
            ),
        )
        raise
