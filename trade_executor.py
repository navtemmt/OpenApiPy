"""Trade execution logic for copying MT5 orders to cTrader accounts.
Handles volume conversion and order placement.
"""
from config_loader import get_multi_account_config
from symbol_mapper import SymbolMapper
from volume_converter import convert_mt5_lots_to_ctrader_cents
from app_state import logger


def _is_metal_symbol(symbol: str) -> bool:
    s = (symbol or "").upper()
    return any(m in s for m in ["XAU", "XAG", "GOLD", "SILVER"])


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

    # Create SymbolMapper instance with account-specific config
    mapper = SymbolMapper(
        prefix=getattr(config, "symbol_prefix", ""),
        suffix=getattr(config, "symbol_suffix", ""),
        custom_map=getattr(config, "custom_symbols", {}),
        broker_symbol_map=client.symbol_name_to_id,
    )
    symbol_id = mapper.get_symbol_id(mt5_symbol)

    if symbol_id is None:
        logger.error(
            f"[{account_name}] Could not map MT5 symbol {mt5_symbol} to cTrader symbolId"
        )
        return

    # Check risk/mirroring rules
    multi_config = get_multi_account_config()
    should_copy, reason = multi_config.should_copy_trade(
        config, mt5_symbol, magic, volume
    )
    if not should_copy:
        logger.info(f"[{account_name}] Skipping: {reason}")
        return

    # Adjust lots based on account config
    adjusted_lots = getattr(config, "lot_multiplier", 1.0) * volume
    adjusted_lots = max(
        getattr(config, "min_lot_size", 0.01),
        min(adjusted_lots, getattr(config, "max_lot_size", 100.0)),
    )

    # Get symbol details from client (cTrader side)
    symbol = client.symbol_details.get(symbol_id) if hasattr(
        client, "symbol_details"
    ) else None

    # MT5-side contract info if available (otherwise defaults)
    mt5_contract_size = getattr(client, "mt5_contract_size", 1.0)
    mt5_volume_min = getattr(client, "mt5_volume_min", 0.01)
    mt5_volume_step = getattr(client, "mt5_volume_step", 0.01)

    # Decide whether to use legacy lots_to_units fallback
    use_legacy = False
    lot_size_cents = 0
    min_volume_cents = 0
    max_volume_cents = 0
    step_volume_cents = 0

    if symbol is None:
        use_legacy = True
    else:
        lot_size_cents = getattr(symbol, "lotSize", 0)
        min_volume_cents = getattr(symbol, "minVolume", 0)
        max_volume_cents = getattr(symbol, "maxVolume", 0)
        step_volume_cents = getattr(symbol, "stepVolume", 0)

        # If broker does not provide meaningful specs, fall back to old logic
        if lot_size_cents <= 0 or min_volume_cents <= 0 or step_volume_cents <= 0:
            use_legacy = True

    if use_legacy:
        logger.warning(
            f"[{account_name}] Using legacy lots_to_units volume calc for {mt5_symbol} "
            f"(symbol_details missing or invalid: lotSize={lot_size_cents}, "
            f"min={min_volume_cents}, step={step_volume_cents})"
        )

        base_units = mapper.lots_to_units(adjusted_lots, mt5_symbol)

        if _is_metal_symbol(mt5_symbol):
            # Metals: enforce minimum units and send units directly
            # Metals: send units directly (1 lot = 100 units in lots_to_units)
            volume_to_send = int(base_units)
        else::
            # Everything else (forex / indices / crypto): units -> cents with min clamp
            units = int(base_units)
            cents = units * 100
            min_units = 1000
            min_cents = min_units * 100
            if cents < min_cents:
                logger.warning(
                    f"[{account_name}] Volume {cents} below minimum {min_cents}, "
                    f"adjusting to {min_cents}"
                )
                cents = min_cents
            volume_to_send = cents
    else:
        # Use proper volume conversion with symbol details
        volume_to_send = convert_mt5_lots_to_ctrader_cents(
            mt5_lots=adjusted_lots,
            mt5_contract_size=mt5_contract_size,
            mt5_volume_min=mt5_volume_min,
            mt5_volume_step=mt5_volume_step,
            lot_size_cents=lot_size_cents,
            min_volume_cents=min_volume_cents,
            max_volume_cents=max_volume_cents,
            step_volume_cents=step_volume_cents,
        )

        logger.info(
            f"[{account_name}] Volume conversion: symbol_id={symbol_id}, "
            f"mt5_lots={adjusted_lots:.4f}, mt5_contract_size={mt5_contract_size}, "
            f"lotSize={lot_size_cents}, min={min_volume_cents}, "
            f"max={max_volume_cents}, step={step_volume_cents} -> "
            f"volume_cents={volume_to_send}"
        )

    if volume_to_send <= 0:
        logger.warning(
            f"[{account_name}] Skipping zero or negative volume for ticket {ticket}"
        )
        return

    # Determine trade side
    trade_side = "BUY" if side.upper() in ("BUY", "LONG") else "SELL"

    logger.info(
        f"[{account_name}] Opening {trade_side} {mt5_symbol} (symbolId={symbol_id}) | "
        f"Volume: {volume_to_send} cents | SL: {sl} | TP: {tp} | "
        f"Label: MT5_{ticket}"
    )

    try:
        response = client.send_market_order(
            account_id=config.account_id,
            symbol_id=symbol_id,
            side=trade_side,
            volume=volume_to_send,
            sl=None,  # SL/TP applied separately via pending mechanism
            tp=None,
            label=f"MT5_{ticket}",
        )

        logger.info(
            f"[{account_name}] Successfully opened position for MT5 ticket {ticket}"
        )
        return response

    except Exception as e:
        logger.error(
            f"[{account_name}] Failed to open position for ticket {ticket}: {e}"
        )
        raise
