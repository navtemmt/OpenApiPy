"""
Trade execution logic for copying MT5 orders to cTrader accounts.
Handles volume conversion and order placement.
"""

from config_loader import get_multi_account_config
from symbol_mapper import SymbolMapper
from app_state import logger


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


def _should_copy(account_name, config, mt5_symbol, magic, volume):
    multi_config = get_multi_account_config()
    should_copy, reason = multi_config.should_copy_trade(config, mt5_symbol, magic, volume)
    if not should_copy:
        logger.info(f"[{account_name}] Skipping: {reason}")
        return False
    return True


def _calc_volume_units(account_name, client, config, symbol_id: int, mt5_symbol: str, mt5_lots: float) -> int:
    """
    Convert MT5 lots -> cTrader volume UNITS using cTrader symbol lotSize, then snap.
    """
    symbol = client.symbol_details.get(symbol_id) if hasattr(client, "symbol_details") else None
    if symbol is None:
        logger.error(
            f"[{account_name}] Missing cTrader symbol_details for {mt5_symbol} (symbolId={symbol_id}). "
            f"Wait for symbols to load before trading."
        )
        return 0

    lot_size = int(getattr(symbol, "lotSize", 0) or 0)
    min_units = int(getattr(symbol, "minVolume", 0) or 0)
    max_units = int(getattr(symbol, "maxVolume", 0) or 0)
    step_units = int(getattr(symbol, "stepVolume", 0) or 0)

    if lot_size <= 0 or min_units <= 0 or step_units <= 0:
        logger.error(
            f"[{account_name}] Invalid cTrader symbol specs for {mt5_symbol} (symbolId={symbol_id}): "
            f"lotSize={lot_size}, minVolume={min_units}, stepVolume={step_units}, maxVolume={max_units}"
        )
        return 0

    raw_units = int(round(float(mt5_lots) * float(lot_size)))
    snapped = _snap_volume_units(raw_units, min_units, max_units, step_units)

    logger.info(
        f"[{account_name}] Volume conversion (cTrader specs): symbolId={symbol_id}, "
        f"mt5_lots={mt5_lots:.4f}, lotSize={lot_size}, "
        f"min={min_units}, max={max_units}, step={step_units} -> units={snapped}"
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

    symbol_id = _map_symbol_id(client, config, mt5_symbol)
    if symbol_id is None:
        logger.error(f"[{account_name}] Could not map MT5 symbol {mt5_symbol} to cTrader symbolId")
        return

    if not _should_copy(account_name, config, mt5_symbol, magic, volume):
        return

    adjusted_lots = getattr(config, "lot_multiplier", 1.0) * float(volume)
    adjusted_lots = max(
        float(getattr(config, "min_lot_size", 0.01)),
        min(adjusted_lots, float(getattr(config, "max_lot_size", 100.0))),
    )

    volume_to_send = _calc_volume_units(
        account_name=account_name,
        client=client,
        config=config,
        symbol_id=symbol_id,
        mt5_symbol=mt5_symbol,
        mt5_lots=float(adjusted_lots),
    )

    if volume_to_send <= 0:
        logger.warning(f"[{account_name}] Skipping zero or negative volume for ticket {ticket}")
        return

    trade_side = "BUY" if side.upper() in ("BUY", "LONG") else "SELL"

    logger.info(
        f"[{account_name}] Opening {trade_side} {mt5_symbol} (symbolId={symbol_id}) | "
        f"Volume: {volume_to_send} units | SL: {sl} | TP: {tp} | "
        f"Label: MT5_{ticket}"
    )

    try:
        response = client.send_market_order(
            account_id=config.account_id,
            symbol_id=symbol_id,
            side=trade_side,
            volume=volume_to_send,  # UNITS (as cTrader expects)
            sl=None,  # SL/TP applied separately via pending mechanism (trade_processor.py)
            tp=None,
            label=f"MT5_{ticket}",
        )

        logger.info(f"[{account_name}] Order submitted for MT5 ticket {ticket}")
        return response

    except Exception as e:
        logger.error(f"[{account_name}] Failed to open position for ticket {ticket}: {e}")
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

    symbol_id = _map_symbol_id(client, config, mt5_symbol)
    if symbol_id is None:
        logger.error(f"[{account_name}] Could not map MT5 symbol {mt5_symbol} to cTrader symbolId")
        return

    if not _should_copy(account_name, config, mt5_symbol, magic, volume):
        return

    adjusted_lots = getattr(config, "lot_multiplier", 1.0) * float(volume)
    adjusted_lots = max(
        float(getattr(config, "min_lot_size", 0.01)),
        min(adjusted_lots, float(getattr(config, "max_lot_size", 100.0))),
    )

    volume_to_send = _calc_volume_units(
        account_name=account_name,
        client=client,
        config=config,
        symbol_id=symbol_id,
        mt5_symbol=mt5_symbol,
        mt5_lots=float(adjusted_lots),
    )

    if volume_to_send <= 0:
        logger.warning(f"[{account_name}] Skipping zero or negative pending volume for ticket {ticket}")
        return

    trade_side = "BUY" if side.upper() in ("BUY", "LONG") else "SELL"
    ptype = (pending_type or "").strip().lower()

    # Round all prices to symbol digits (prevents invalid precision rejects)
    sl_r = client.round_price_for_symbol(symbol_id, float(sl)) if sl and float(sl) > 0 else None
    tp_r = client.round_price_for_symbol(symbol_id, float(tp)) if tp and float(tp) > 0 else None
    stop_r = client.round_price_for_symbol(symbol_id, float(stop_price)) if stop_price and float(stop_price) > 0 else 0.0
    limit_r = client.round_price_for_symbol(symbol_id, float(limit_price)) if limit_price and float(limit_price) > 0 else 0.0

    logger.info(
        f"[{account_name}] Creating pending {ptype.upper()} {trade_side} {mt5_symbol} (symbolId={symbol_id}) | "
        f"Volume: {volume_to_send} units | stop={stop_r} limit={limit_r} SL={sl_r} TP={tp_r} | "
        f"Label: MT5_{ticket} | expiry_ms={int(expiration_ms or 0)}"
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
        logger.info(f"[{account_name}] Pending order submitted for MT5 ticket {ticket}")
        return resp

    except Exception as e:
        logger.error(f"[{account_name}] Failed to create pending order for ticket {ticket}: {e}")
        raise
