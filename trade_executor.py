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
    # FIX: Create SymbolMapper instance with account-specific config
    mapper = SymbolMapper(
        prefix=config.get("symbol_prefix", ""),
        suffix=config.get("symbol_suffix", ""),
        custom_map=config.get("custom_symbols", {}),
        broker_symbol_map=client.symbol_name_to_id,
    )
    symbol_id = mapper.get_symbol_id(mt5_symbol)
    
    if symbol_id is None:
        logger.error(
            f"[{account_name}] Could not map MT5 symbol {mt5_symbol} to cTrader symbolId"
        )
        return

    # Convert MT5 lots to cTrader volume in cents
    volume_cents = convert_mt5_lots_to_ctrader_cents(
        mt5_lots=volume,
        multiplier=config.get("volume_multiplier", 1.0),
    )

    if volume_cents <= 0:
        logger.warning(
            f"[{account_name}] Skipping zero or negative volume for ticket {ticket}"
        )
        return

    # Determine trade side
    trade_side = "BUY" if side.upper() in ("BUY", "LONG") else "SELL"

    logger.info(
        f"[{account_name}] Opening {trade_side} {mt5_symbol} (symbolId={symbol_id}) | "
        f"Volume: {volume_cents} cents | SL: {sl} | TP: {tp} | "
        f"Label: MT5_{ticket}"
    )

    try:
        response = client.send_market_order(
            symbol_id=symbol_id,
            side=trade_side,
            volume=volume_cents,
            stop_loss=sl if sl > 0 else None,
            take_profit=tp if tp > 0 else None,
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
