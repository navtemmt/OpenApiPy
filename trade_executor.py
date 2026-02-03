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
      ctrader_symbol = SymbolMapper.map_symbol(mt5_symbol)

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
    trade_side = "BUY" if side.upper() in ["BUY", "LONG"] else "SELL"

    logger.info(
              f"[{account_name}] Opening {trade_side} {ctrader_symbol} | "
              f"Volume: {volume_cents} cents | SL: {sl} | TP: {tp} | "
              f"Label: MT5_{ticket}"
    )

    try:
              # Send market order request to cTrader
              response = client.send_market_order(
                            symbol=ctrader_symbol,
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
