"""
Trade event processing and handling logic.
Processes incoming MT5 trade events and routes them to appropriate handlers.
"""

from app_state import logger, PENDING_SLTP, MASTER_OPEN_LOTS
from trade_executor import copy_open_to_account, copy_pending_to_account
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


def _has_valid_sl(sl_value) -> bool:
    try:
        return float(sl_value or 0) > 0
    except Exception:
        return False


def _risk_mode(config) -> str:
    """
    Read risk_mode robustly even if config value accidentally includes inline comment fragments.
    """
    raw = str(getattr(config, "risk_mode", "SOURCE_VOLUME") or "SOURCE_VOLUME")
    raw = raw.split(";", 1)[0].split("#", 1)[0]
    return raw.strip().upper()


def _resolve_open_volume_for_account(data: dict, config):
    """
    Decide which lots to use for OPEN based on per-account risk settings.

    Priority rules:
      1) FIXED_LOT: always take with fixed_lot (ignores reject_if_no_sl)
      2) If no SL:
         - reject_if_no_sl True -> reject
         - else -> fallback to source volume (if source_volume_fallback True), otherwise reject
      3) If SL exists:
         - for now: SOURCE_VOLUME behavior (you can later implement FIXED_USD / PERCENT_EQUITY sizing here)

    Returns:
      (lots: float | None, decision: str)
    """
    src_lots = float(data.get("volume", 0) or 0)
    sl = float(data.get("sl", 0) or 0)

    risk_mode = _risk_mode(config)
    reject_if_no_sl = bool(getattr(config, "reject_if_no_sl", False))
    source_volume_fallback = bool(getattr(config, "source_volume_fallback", True))

    if risk_mode == "FIXED_LOT":
        fixed_lot = float(getattr(config, "fixed_lot", 0) or 0)
        if fixed_lot > 0:
            return fixed_lot, "FIXED_LOT"
        return src_lots, "FIXED_LOT invalid -> SOURCE_VOLUME"

    if not _has_valid_sl(sl):
        if reject_if_no_sl:
            return None, "REJECT_NO_SL"
        if not source_volume_fallback:
            return None, "REJECT_NO_SL_FALLBACK_DISABLED"
        return src_lots, "NO_SL_FALLBACK_SOURCE_VOLUME"

    return src_lots, f"{risk_mode}_USING_SOURCE_VOLUME_FOR_NOW"


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

        elif event_type in ("PENDING_CANCEL", "PENDING_CLOSE"):
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
    src_volume = float(data.get("volume", 0))
    sl = float(data.get("sl", 0))
    tp = float(data.get("tp", 0))
    magic = int(data.get("magic", 0))

    logger.info(
        f"OPEN event - Ticket: {ticket}, Symbol: {mt5_symbol}, "
        f"Side: {side}, Volume: {src_volume}, SL: {sl}, TP: {tp}"
    )

    # Store master open lots for proportional close later (FIXED_LOT / FIXED_USD / PERCENT_EQUITY)
    if src_volume and float(src_volume) > 0:
        MASTER_OPEN_LOTS[int(ticket)] = float(src_volume)

    # Store pending SL/TP immediately so it can be applied as soon as positionId is known
    if (sl and sl > 0) or (tp and tp > 0):
        PENDING_SLTP[int(ticket)] = {"symbol": mt5_symbol, "sl": float(sl), "tp": float(tp)}

    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            lots, decision = _resolve_open_volume_for_account(data, config)
            if lots is None or float(lots) <= 0:
                logger.warning(f"[{account_name}] OPEN rejected for ticket {ticket}: {decision}")
                continue

            logger.info(f"[{account_name}] OPEN sizing: {decision}, lots={float(lots):.4f}")

            copy_open_to_account(
                account_name=account_name,
                client=client,
                config=config,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                side=side,
                volume=float(lots),
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
    ticket = int(data.get("ticket", 0))
    mt5_symbol = data.get("symbol")

    logger.info(f"PENDING_CANCEL event - Ticket: {ticket}, Symbol: {mt5_symbol}")

    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            order_id = account_manager.get_order_id(account_name, int(ticket))
            if not order_id:
                logger.warning(
                    f"[{account_name}] PENDING_CANCEL ignored for ticket {ticket} (no orderId mapping yet)"
                )
                continue

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

    master_open_lots = float(MASTER_OPEN_LOTS.get(int(ticket), 0) or 0)

    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            position_id = account_manager.get_position_id(account_name, ticket)
            if not position_id:
                logger.info(f"[{account_name}] CLOSE ignored for ticket {ticket} (no mapping)")
                continue

            symbol_id = _get_symbol_id_for_account(client, config, mt5_symbol)

            rm = _risk_mode(config)

            follower_units = account_manager.get_position_volume(account_name, position_id)

            close_units = None

            # If master sent a partial close volume and follower is not SOURCE_VOLUME,
            # close the same percentage of follower position.
            if close_lots is not None and follower_units is not None and int(follower_units) > 0:
                if rm != "SOURCE_VOLUME" and master_open_lots > 0:
                    pct = float(close_lots) / float(master_open_lots)
                    pct = max(0.0, min(1.0, pct))
                    close_units = int(round(pct * float(follower_units)))
                    logger.info(
                        f"[{account_name}] Proportional CLOSE: risk_mode={rm}, "
                        f"master_close_lots={float(close_lots):.4f}, master_open_lots={master_open_lots:.4f}, "
                        f"pct={pct:.4f}, follower_units={int(follower_units)} -> close_units={close_units}"
                    )
                else:
                    # Legacy behavior: treat MT5 close_lots as absolute lots-to-close
                    if mt5_contract_size > 0:
                        close_units = _lots_to_ctrader_cents(float(close_lots), mt5_contract_size)
                    logger.info(
                        f"[{account_name}] Absolute CLOSE: risk_mode={rm}, close_lots={close_lots}, "
                        f"mt5_contract_size={mt5_contract_size} -> close_units={close_units}"
                    )

            # If close volume unknown/invalid, close full follower position
            if close_units is None or int(close_units) <= 0:
                close_units = follower_units

            if close_units is None or int(close_units) <= 0:
                logger.warning(
                    f"[{account_name}] Cannot close ticket {ticket} (positionId={position_id}) "
                    f"because close volume is unknown/invalid."
                )
                continue

            # Never try to close more than current follower volume
            if follower_units is not None and int(follower_units) > 0:
                close_units = min(int(close_units), int(follower_units))

            client.close_position(
                account_id=config.account_id,
                position_id=position_id,
                volume=int(close_units),
                symbol_id=symbol_id,
            )

            logger.info(
                f"[{account_name}] Close sent for position {position_id} "
                f"(ticket {ticket}) close_units={int(close_units)}"
            )

            # If this was a full close on follower, remove mappings
            if follower_units is not None and int(close_units) >= int(follower_units):
                account_manager.remove_mapping(account_name, ticket)

        except Exception as e:
            logger.error(f"[{account_name}] Failed to close position for ticket {ticket}: {e}")

    if int(ticket) in PENDING_SLTP:
        del PENDING_SLTP[int(ticket)]

    # Best-effort cleanup
    try:
        if close_lots is None:
            MASTER_OPEN_LOTS.pop(int(ticket), None)
    except Exception:
        pass
