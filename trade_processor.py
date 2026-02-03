"""Trade event processing and handling logic.
Processes incoming MT5 trade events and routes them to appropriate handlers.
"""
from app_state import logger, PENDING_SLTP
from trade_executor import copy_open_to_account
from symbol_mapper import SymbolMapper
from volume_converter import convert_mt5_lots_to_ctrader_cents


def try_apply_pending_sltp(account_name, client, config, ticket, account_manager):
    """Try to apply pending SL/TP for a ticket if positionId is now known."""
    pending = PENDING_SLTP.get(int(ticket))
    if not pending:
        return

    position_id = account_manager.get_position_id(account_name, int(ticket))
    if not position_id:
        return  # not mapped yet

    mt5_symbol = pending.get("symbol")
    ctrader_symbol = SymbolMapper.map_symbol(mt5_symbol)
    new_sl = pending.get("sl", 0)
    new_tp = pending.get("tp", 0)

    logger.info(
        f"[{account_name}] Applying pending SL/TP for ticket {ticket} -> "
        f"positionId={position_id}, SL={new_sl}, TP={new_tp}"
    )

    try:
        client.amend_position(
            position_id=position_id,
            stop_loss=new_sl if new_sl > 0 else None,
            take_profit=new_tp if new_tp > 0 else None,
        )
        logger.info(
            f"[{account_name}] Successfully applied pending SL/TP for ticket {ticket}"
        )
        del PENDING_SLTP[int(ticket)]
    except Exception as e:
        logger.error(
            f"[{account_name}] Failed to apply pending SL/TP for ticket {ticket}: {e}"
        )


def process_trade_event(data, account_manager):
    """Process a single trade event from MT5 and route to appropriate handler."""
    try:
        event_type = data.get("event_type")
        ticket = int(data.get("ticket", 0))
        magic = int(data.get("magic", 0))

        logger.info(
            f"Processing event: {event_type} for ticket {ticket} (magic: {magic})"
        )

        if event_type == "OPEN":
            handle_open_event(data, account_manager)
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
    """Handle MT5 position OPEN event."""
    ticket = int(data.get("ticket"))
    mt5_symbol = data.get("symbol")
    side = data.get("side")
    volume = float(data.get("volume", 0))
    sl = float(data.get("sl", 0))
    tp = float(data.get("tp", 0))
    magic = int(data.get("magic", 0))

    logger.info(
        f"OPEN event - Ticket: {ticket}, Symbol: {mt5_symbol}, "
        f"Side: {side}, Volume: {volume}, SL: {sl}, TP: {tp}"
    )

    # FIX: Unpack the (client, config) tuple directly
    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            copy_open_to_account(
                account_name=account_name,
                client=client,
                config=config,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                side=side,
                volume=volume,
                sl=sl,
                tp=tp,
                magic=magic,
            )
        except Exception as e:
            logger.error(f"[{account_name}] Failed to copy OPEN event: {e}")


def handle_modify_event(data, account_manager):
    """Handle MT5 position MODIFY event (SL/TP changes)."""
    ticket = int(data.get("ticket"))
    mt5_symbol = data.get("symbol")
    new_sl = float(data.get("sl", 0))
    new_tp = float(data.get("tp", 0))

    logger.info(
        f"MODIFY event - Ticket: {ticket}, Symbol: {mt5_symbol}, "
        f"New SL: {new_sl}, New TP: {new_tp}"
    )

    # FIX: Unpack the (client, config) tuple directly
    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            position_id = account_manager.get_position_id(account_name, ticket)

            if position_id:
                client.amend_position(
                    position_id=position_id,
                    stop_loss=new_sl if new_sl > 0 else None,
                    take_profit=new_tp if new_tp > 0 else None,
                )
                logger.info(
                    f"[{account_name}] Modified position {position_id} for ticket {ticket}"
                )
            else:
                logger.warning(
                    f"[{account_name}] Position not found for ticket {ticket}, "
                    f"storing pending SL/TP"
                )
                PENDING_SLTP[ticket] = {
                    "symbol": mt5_symbol,
                    "sl": new_sl,
                    "tp": new_tp,
                }

        except Exception as e:
            logger.error(
                f"[{account_name}] Failed to modify position for ticket {ticket}: {e}"
            )


def handle_close_event(data, account_manager):
    """Handle MT5 position CLOSE event."""
    ticket = int(data.get("ticket"))
    mt5_symbol = data.get("symbol")

    logger.info(f"CLOSE event - Ticket: {ticket}, Symbol: {mt5_symbol}")

    # FIX: Unpack the (client, config) tuple directly
    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            position_id = account_manager.get_position_id(account_name, ticket)

            if position_id:
                client.close_position(position_id)
                account_manager.remove_mapping(account_name, ticket)
                logger.info(
                    f"[{account_name}] Closed position {position_id} for ticket {ticket}"
                )
            else:
                logger.warning(
                    f"[{account_name}] No position found for ticket {ticket} to close"
                )

        except Exception as e:
            logger.error(
                f"[{account_name}] Failed to close position for ticket {ticket}: {e}"
            )

    if ticket in PENDING_SLTP:
        del PENDING_SLTP[ticket]
