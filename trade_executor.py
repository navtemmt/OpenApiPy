"""Trade execution logic for copying MT5 orders to cTrader accounts.
Handles volume conversion and order placement.
"""
from config_loader import get_multi_account_config
from symbol_mapper import SymbolMapper
from volume_converter import convert_mt5_lots_to_ctrader_cents
from app_state import logger


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

    # Adjust lots based on account config
    adjusted_lots = getattr(config, "lot_multiplier", 1.0) * volume
    adjusted_lots = max(
        getattr(config, "min_lot_size", 0.01),
        min(adjusted_lots, getattr(config, "max_lot_size", 100.0))
    )

    # Get symbol details from client
    symbol = client.symbol_details.get(symbol_id) if hasattr(client, "symbol_details") else None

    if symbol is None:
        # Fallback: use lots_to_units
        logger.warning(
            f"[{account_name}] No symbol details for {mt5_symbol} "
            f"(id={symbol_id}), falling back to lots_to_units"
        )
        base_units = mapper.lots_to_units(adjusted_lots, mt5_symbol)
        sym_upper = (mt5_symbol or "").upper()

        if any(metal in sym_upper for metal in ["XAU", "XAG", "GOLD", "SILVER"]):
            min_units = 100
            units = int(base_units)
            if units < min_units:
                logger.warning(
                    f"[{account_name}] Volume {units} below minimum {min_units}, "
                    f"adjusting to {min_units} units"
                )
                units = min_units
            volume_to_send = units
        else:
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
        lot_size_cents = getattr(symbol, "lotSize", 0)
        min_volume_cents = getattr(symbol, "minVolume", 0)
        max_volume_cents = getattr(symbol, "maxVolume", 0)
        step_volume_cents = getattr(symbol, "stepVolume", 0)

        volume_to_send = convert_mt5_lots_to_ctrader_cents(
            mt5_lots=adjusted_lots,
            mt5_contract_size=1.0,  # Default
            mt5_volume_min=0.01,  # Default
            mt5_volume_step=0.01,  # Default
            lot_size_cents=lot_size_cents,
            min_volume_cents=min_volume_cents,
            max_volume_cents=max_volume_cents,
            step_volume_cents=step_volume_cents,
        )

        logger.info(
            f"[{account_name}] Volume conversion: symbol_id={symbol_id}, "
            f"mt5_lots={adjusted_lots:.4f}, lotSize={lot_size_cents}, "
            f"min={min_volume_cents}, max={max_volume_cents}, "
            f"step={step_volume_cents} -> volume_cents={volume_to_send}"
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
