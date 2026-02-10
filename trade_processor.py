"""
Trade event processing and handling logic.
Processes incoming MT5 trade events and routes them to appropriate handlers.
"""

from app_state import logger, PENDING_SLTP
from trade_executor import copy_open_to_account, copy_pending_to_account
from symbol_mapper import SymbolMapper

# NEW: ticket(int) -> cTrader orderId(int) for pending orders
PENDING_ORDER_IDS = {}


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


def _parse_mt5_ticket_from_label(label: str):
    """
    Expected: 'MT5_<ticket>' (e.g., 'MT5_1468550799')
    """
    if not label:
        return None
    s = str(label).strip()
    if not s.startswith("MT5_"):
        return None
    tail = s[4:]
    if not tail.isdigit():
        return None
    try:
        return int(tail)
    except Exception:
        return None


def register_pending_order_id_from_execution(extracted_execution_event):
    """
    Call this from your Open API message handler when you receive ORDER_ACCEPTED
    for a pending order. Your extracted event already contains:
      extracted.order.orderId
      extracted.order.tradeData.label  (MT5_<ticket>)
    """
    try:
        order = getattr(extracted_execution_event, "order", None)
        if not order:
            return
        order_id = getattr(order, "orderId", None)
        label = getattr(getattr(order, "tradeData", None), "label", None)
        ticket = _parse_mt5_ticket_from_label(label or "")
        if ticket and order_id:
            PENDING_ORDER_IDS[int(ticket)] = int(order_id)
            logger.info(f"Registered pending mapping: ticket {ticket} -> orderId {int(order_id)}")
    except Exception as e:
        logger.debug(f"register_pending_order_id_from_execution failed: {e}")


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


def handle_pending_open_event(data, account_manager):
    ticket = int(data.get("ticket"))
    mt5_symbol = data.get("symbol")
    side = data.get("side") or data.get("type")
    volume = float(data.get("volume", 0))
    sl = float(data.get("sl", 0))
    tp = float(data.get("tp", 0))
    magic = int(data.get("magic", 0))

    pending_type = (data.get("pending_type") or data.get("order_type") or "").strip().lower()

    entry_price = float(data.get("entry_price", 0) or 0)
    stop_price = float(data.get("stop_price", 0) or 0)
    limit_price = float(data.get("limit_price", 0) or 0)

    expiration_ms = int(data.get("expiration_ms", 0) or 0)

    if pending_type in ("limit", "stop"):
        if pending_type == "limit" and limit_price <= 0:
            limit_price = entry_price
        if pending_type == "stop" and stop_price <= 0:
            stop_price = entry_price

    logger.info(
        f"PENDING_OPEN event - Ticket: {ticket}, Symbol: {mt5_symbol}, Side: {side}, "
        f"Volume: {volume}, pending_type={pending_type}, "
        f"stop_price={stop_price}, limit_price={limit_price}, SL={sl}, TP={tp}, "
        f"expiration_ms={expiration_ms}"
    )

    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            copy_pending_to_account(
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
                pending_type=pending_type,
                stop_price=stop_price,
                limit_price=limit_price,
                expiration_ms=expiration_ms,
            )
        except Exception as e:
            logger.error(f"[{account_name}] Failed to copy PENDING_OPEN event: {e}")


def handle_pending_cancel_event(data, account_manager):
    """
    Cancel pending order by MT5 ticket.
    Requires ticket->orderId mapping (PENDING_ORDER_IDS).
    """
    ticket = int(data.get("ticket"))
    mt5_symbol = data.get("symbol")

    logger.info(f"PENDING_CANCEL event - Ticket: {ticket}, Symbol: {mt5_symbol}")

    order_id = PENDING_ORDER_IDS.get(int(ticket))
    if not order_id:
        logger.warning(f"PENDING_CANCEL: No orderId mapping for ticket {ticket}. Cannot cancel.")
        return

    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            client.cancel_pending_order(account_id=config.account_id, order_id=int(order_id))
            logger.info(f"[{account_name}] Cancel sent: ticket {ticket} -> orderId {int(order_id)}")
        except Exception as e:
            logger.error(f"[{account_name}] Failed to cancel pending for ticket {ticket}: {e}")


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
