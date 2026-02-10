"""
Trade event processing and handling logic.
Processes incoming MT5 trade events and routes them to appropriate handlers.
"""

from app_state import logger, PENDING_SLTP
from trade_executor import copy_open_to_account
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


def _lots_to_ctrader_cents(lots: float, mt5_contract_size: float) -> int:
    """
    MT5 lots -> underlying units -> cTrader cents-of-units.
    units = lots * contract_size
    cents = units * 100
    """
    units = float(lots) * float(mt5_contract_size or 0.0)
    return int(round(units * 100.0))


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
        event_type = data.get("event_type") or data.get("action")
        ticket = int(data.get("ticket", 0))
        magic = int(data.get("magic", 0))

        logger.info(f"Processing event: {event_type} for ticket {ticket} (magic: {magic})")

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
    ticket = int(data.get("ticket"))
    mt5_symbol = data.get("symbol")
    side = data.get("side") or data.get("type")
    volume = float(data.get("volume", 0))
    sl = float(data.get("sl", 0))
    tp = float(data.get("tp", 0))
    magic = int(data.get("magic", 0))

    logger.info(
        f"OPEN event - Ticket: {ticket}, Symbol: {mt5_symbol}, "
        f"Side: {side}, Volume: {volume}, SL: {sl}, TP: {tp}"
    )

    # Store pending SL/TP immediately so it can be applied as soon as positionId is known
    if (sl and sl > 0) or (tp and tp > 0):
        PENDING_SLTP[int(ticket)] = {"symbol": mt5_symbol, "sl": float(sl), "tp": float(tp)}

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
    ticket = int(data.get("ticket"))
    mt5_symbol = data.get("symbol")
    new_sl = float(data.get("sl", 0))
    new_tp = float(data.get("tp", 0))

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
    ticket = int(data.get("ticket"))
    mt5_symbol = data.get("symbol")

    close_lots = data.get("volume", None)
    mt5_contract_size = float(data.get("mt5_contract_size", 0) or 0)

    logger.info(f"CLOSE event - Ticket: {ticket}, Symbol: {mt5_symbol}, close_lots={close_lots}")

    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            position_id = account_manager.get_position_id(account_name, ticket)
            if not position_id:
                logger.info(f"[{account_name}] CLOSE ignored for ticket {ticket} (no mapping)")
                continue

            symbol_id = _get_symbol_id_for_account(client, config, mt5_symbol)

            close_volume_cents = None
            if close_lots is not None and mt5_contract_size > 0:
                close_volume_cents = _lots_to_ctrader_cents(float(close_lots), mt5_contract_size)

            if close_volume_cents is None or close_volume_cents <= 0:
                close_volume_cents = account_manager.get_position_volume(account_name, position_id)

            if close_volume_cents is None or int(close_volume_cents) <= 0:
                logger.warning(
                    f"[{account_name}] Cannot close ticket {ticket} (positionId={position_id}) "
                    f"because close volume is unknown/invalid."
                )
                continue

            client.close_position(
                account_id=config.account_id,
                position_id=position_id,
                volume=int(close_volume_cents),
                symbol_id=symbol_id,
            )

            logger.info(
                f"[{account_name}] Close sent for position {position_id} "
                f"(ticket {ticket}) volume_cents={int(close_volume_cents)}"
            )

            if close_lots is None:
                account_manager.remove_mapping(account_name, ticket)

        except Exception as e:
            logger.error(f"[{account_name}] Failed to close position for ticket {ticket}: {e}")

    if int(ticket) in PENDING_SLTP:
        del PENDING_SLTP[int(ticket)]
