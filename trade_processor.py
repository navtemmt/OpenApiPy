"""
Trade event processing and handling logic.
Processes incoming MT5 trade events and routes them to appropriate handlers.
"""

import time

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


def _now_ms() -> int:
    return int(time.time() * 1000)


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


def _risk_reference(config) -> str:
    """
    What to use as base for PERCENT_EQUITY:
      - EQUITY (default)
      - BALANCE
    """
    raw = str(getattr(config, "risk_reference", "EQUITY") or "EQUITY")
    raw = raw.split(";", 1)[0].split("#", 1)[0]
    return raw.strip().upper()


def _get_account_equity_or_balance(account_manager, account_name: str, config) -> float:
    ref = _risk_reference(config)

    try:
        if ref == "BALANCE" and hasattr(account_manager, "get_balance"):
            v = account_manager.get_balance(account_name)
        elif hasattr(account_manager, "get_equity"):
            v = account_manager.get_equity(account_name)
        else:
            v = None
    except Exception:
        v = None

    try:
        return float(v or 0.0)
    except Exception:
        return 0.0


def _get_symbol_details(client, symbol_id: int):
    try:
        return client.symbol_details.get(int(symbol_id)) if hasattr(client, "symbol_details") else None
    except Exception:
        return None


def _estimate_risk_ccy_per_1lot(symbol, entry_price: float, sl_price: float) -> float:
    """
    Estimate money risk (in deposit currency) for 1.0 lot given entry and SL.

    Requires:
      - tickValue (money per 1 lot per tick), and
      - tick size inferred from pipPosition or digits.

    Returns 0 if cannot compute.
    """
    try:
        entry = float(entry_price or 0.0)
        sl = float(sl_price or 0.0)
        if entry <= 0 or sl <= 0:
            return 0.0

        dist = abs(entry - sl)
        if dist <= 0:
            return 0.0

        pip_pos = getattr(symbol, "pipPosition", None)
        digits = int(getattr(symbol, "digits", 0) or 0)

        if pip_pos is not None:
            tick_size = 10 ** (-int(pip_pos))
        elif digits > 0:
            tick_size = 10 ** (-digits)
        else:
            return 0.0

        if tick_size <= 0:
            return 0.0

        ticks = dist / float(tick_size)
        if ticks <= 0:
            return 0.0

        tick_value = float(getattr(symbol, "tickValue", 0) or 0.0)
        if tick_value <= 0:
            return 0.0

        return float(ticks) * float(tick_value)
    except Exception:
        return 0.0


def _resolve_open_volume_for_account(data: dict, config, *, account_name=None, client=None, account_manager=None):
    """
    Decide which lots to use for OPEN based on per-account risk settings.

    risk_mode:
      - SOURCE_VOLUME
      - FIXED_LOT
      - FIXED_USD
      - PERCENT_EQUITY

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

    if risk_mode in ("FIXED_USD", "PERCENT_EQUITY"):
        # money-risk modes: NEVER fallback to source volume if missing inputs
        if not (account_manager and client and account_name):
            return None, f"REJECT_{risk_mode}_MISSING_CONTEXT"

        mt5_symbol = data.get("symbol")
        side = data.get("side") or data.get("type")

        symbol_id = _get_symbol_id_for_account(client, config, mt5_symbol)
        if symbol_id is None:
            return None, f"REJECT_{risk_mode}_NO_SYMBOL_ID"

        symbol = _get_symbol_details(client, int(symbol_id))
        if symbol is None:
            return None, f"REJECT_{risk_mode}_NO_SYMBOL_DETAILS"

        # Determine entry price:
        # For both market and pending orders we now rely on MT5 to provide entry_price.
        # For pendings this is set in handle_pending_open_event; for markets the MT5 EA
        # should populate data["entry_price"] with the current MT5 price.
        entry_price = float(data.get("entry_price", 0) or 0.0)
        if entry_price <= 0:
            return None, f"REJECT_{risk_mode}_NO_ENTRY_PRICE_FROM_MT5"

        risk_per_1lot = _estimate_risk_ccy_per_1lot(symbol, float(entry_price), float(sl))
        if risk_per_1lot <= 0:
            return None, f"REJECT_{risk_mode}_CANNOT_PRICE_RISK"

        if risk_mode == "FIXED_USD":
            usd_risk = float(getattr(config, "fixed_usd_risk", 0) or 0)
            if usd_risk <= 0:
                return None, "REJECT_FIXED_USD_INVALID"
        else:
            pct = float(getattr(config, "risk_percent", 0) or 0)
            ref_amt = _get_account_equity_or_balance(account_manager, account_name, config)
            if pct <= 0:
                return None, "REJECT_PERCENT_EQUITY_INVALID_PCT"
            if ref_amt <= 0:
                return None, "REJECT_NO_EQUITY"
            usd_risk = (pct / 100.0) * float(ref_amt)

        lots = float(usd_risk) / float(risk_per_1lot)
        if lots <= 0:
            return None, f"REJECT_{risk_mode}_LOTS_NONPOSITIVE"

        return lots, f"{risk_mode} usd={usd_risk:.2f} perLot={risk_per_1lot:.2f} entry={float(entry_price):.5f}"

    # default behavior
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

        # accept MT5 "PENDING_CLOSE" as alias of "PENDING_CANCEL"
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
            lots, decision = _resolve_open_volume_for_account(
                data,
                config,
                account_name=account_name,
                client=client,
                account_manager=account_manager,
            )
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
    """
    Pending order open (LIMIT / STOP / STOP_LIMIT).

    Expected MT5 payload keys (recommended):
      pending_type: 'limit' | 'stop' | 'stop_limit'
      For LIMIT: entry_price (or limit_price)
      For STOP: entry_price (or stop_price)
      For STOP_LIMIT: stop_price + limit_price (preferred)

    Also uses:
      ticket, symbol, side/type, volume, sl, tp, magic
      expiration_ms (optional): ms since epoch
    """
    ticket = int(data.get("ticket"))
    mt5_symbol = data.get("symbol")
    side = data.get("side") or data.get("type")
    volume = float(data.get("volume", 0))
    sl = float(data.get("sl", 0))
    tp = float(data.get("tp", 0))
    magic = int(data.get("magic", 0))

    pending_type = (data.get("pending_type") or data.get("order_type") or "").strip().lower()

    # Accept either "entry_price" or explicit stop/limit prices
    entry_price = float(data.get("entry_price", 0) or 0)
    stop_price = float(data.get("stop_price", 0) or 0)
    limit_price = float(data.get("limit_price", 0) or 0)

    expiration_ms = int(data.get("expiration_ms", 0) or 0)

    # Backward-compatible defaults:
    # - For LIMIT/STOP, if explicit field not provided, use entry_price
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

    # Determine pending order entry price for risk sizing
    pending_entry_price = 0.0
    if pending_type == "limit":
        pending_entry_price = float(limit_price or 0.0)
    elif pending_type == "stop":
        pending_entry_price = float(stop_price or 0.0)
    elif pending_type == "stop_limit":
        # Prefer limit price as the actual fill target; if missing, use stop.
        pending_entry_price = float(limit_price or 0.0) if float(limit_price or 0.0) > 0 else float(stop_price or 0.0)

    for account_name, (client, config) in account_manager.get_all_accounts().items():
        try:
            # If money-risk modes are enabled, size volume here (override the source volume)
            rm = _risk_mode(config)
            sizing_volume = float(volume)

            if rm in ("FIXED_USD", "PERCENT_EQUITY"):
                # Provide entry_price so resolver doesn't use quotes for pending orders
                sizing_data = dict(data)
                sizing_data["entry_price"] = float(pending_entry_price or 0.0)

                lots, decision = _resolve_open_volume_for_account(
                    sizing_data,
                    config,
                    account_name=account_name,
                    client=client,
                    account_manager=account_manager,
                )
                if lots is None or float(lots) <= 0:
                    logger.warning(f"[{account_name}] PENDING_OPEN rejected for ticket {ticket}: {decision}")
                    continue
                sizing_volume = float(lots)
                logger.info(f"[{account_name}] PENDING_OPEN sizing: {decision}, lots={float(lots):.4f}")

            copy_pending_to_account(
                account_name=account_name,
                client=client,
                config=config,
                ticket=ticket,
                mt5_symbol=mt5_symbol,
                side=side,
                volume=float(sizing_volume),
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

    Uses AccountManager mapping: per-account ticket -> cTrader orderId.
    (orderId is learned from ProtoOAExecutionEvent.order where label == MT5_<ticket>.)
    """
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
