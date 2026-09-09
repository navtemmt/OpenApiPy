import time
from threading import Lock

from app_state import (
    logger, PENDING_SLTP, MASTER_OPEN_LOTS, MASTER_CLOSED_LOTS,
    alert_trade_failure, alert_trade_warning, alert_trade_info,
)
from trade_executor import (copy_open_to_account, copy_pending_to_account, transition_pending_to_market)
from symbol_mapper import SymbolMapper

from .common import *
from .risk import *
from .helpers import *

# ---------------------------------------------------------------------------
# NEW: destination-loss recovery
# ---------------------------------------------------------------------------

def _is_destination_recovery_state(state) -> bool:
    """
    States that mean the cTrader destination is unresolved/missing.

    IMPORTANT:
    These are NOT source cancellation states.

    UNKNOWN is deliberately treated as recoverable here because an
    ORDER_NOT_FOUND or unexpected cTrader cancellation means we do not
    know whether the destination disappeared while MT5 is still live.
    """

    state = str(state or "").strip().upper()

    return state in (
        "UNKNOWN",
        "RECOVERY_REQUIRED",
        "DESTINATION_CANCELLED",
        "DESTINATION_LOST",
        "LOST",
    )


def _get_stored_mt5_payload(
    account_manager,
    account_name: str,
    ticket: int,
):
    """
    Retrieve the last MT5 payload stored by AccountManager.

    This intentionally uses the existing mt5_payloads store so the
    AccountManager API does not need to change just to perform recovery.
    """

    try:
        bucket = getattr(
            account_manager,
            "mt5_payloads",
            {},
        )

        account_bucket = bucket.get(
            str(account_name),
            {},
        )

        payload = account_bucket.get(
            int(ticket)
        )

        if isinstance(payload, dict):
            return dict(payload)

    except Exception:
        logger.debug(
            "[%s] Failed reading stored MT5 payload "
            "for recovery ticket=%s",
            account_name,
            ticket,
            exc_info=True,
        )

    return None


def recover_missing_destination(
    account_name,
    ticket,
    account_manager,
    data=None,
    force=False,
):
    """
    Recover a cTrader destination that disappeared while the MT5 source
    trade is still believed to be live.

    This function is intentionally independent from the normal OPEN path.

    It MUST NOT treat a missing cTrader order as proof that MT5 cancelled
    the trade.

    Recovery order:

        1. pending-origin position
           -> authoritative, do nothing

        2. existing destination position
           -> do nothing

        3. existing pending order
           -> do nothing

        4. existing market fallback order
           -> do nothing

        5. source payload is recovered

        6. compare current market price to original MT5 entry

        7. close enough -> market

        8. too far -> recreate pending order at original entry

    The caller can provide `data`.  If not, the last payload cached by
    AccountManager is used.
    """

    ticket = _to_int(ticket, 0)

    if ticket <= 0:
        return False

    try:
        client = account_manager.get_client(
            account_name
        )

        config = account_manager.get_config(
            account_name
        )

    except Exception as e:
        logger.warning(
            "[%s] Recovery unable to load account "
            "context for ticket=%s: %s",
            account_name,
            ticket,
            e,
        )
        return False

    if not client or not config:
        logger.warning(
            "[%s] Recovery skipped for ticket=%s: "
            "account unavailable",
            account_name,
            ticket,
        )
        return False

    # ------------------------------------------------------------------
    # Canonical pending-origin position ALWAYS wins.
    # ------------------------------------------------------------------

    try:
        pending_position_id = (
            account_manager.get_pending_position_id(
                account_name,
                int(ticket),
            )
        )
    except Exception:
        pending_position_id = None

    if pending_position_id:
        logger.info(
            "[%s] Recovery skipped for ticket=%s: "
            "pending-origin positionId=%s is canonical",
            account_name,
            ticket,
            pending_position_id,
        )

        return True

    # ------------------------------------------------------------------
    # Existing canonical position means there is nothing to recover.
    # ------------------------------------------------------------------

    try:
        existing_position_id = (
            account_manager.get_position_id(
                account_name,
                int(ticket),
            )
        )
    except Exception:
        existing_position_id = None

    if existing_position_id:
        logger.info(
            "[%s] Recovery skipped for ticket=%s: "
            "positionId=%s already mapped",
            account_name,
            ticket,
            existing_position_id,
        )

        return True

    # ------------------------------------------------------------------
    # Existing pending order means destination is not actually missing.
    # ------------------------------------------------------------------

    try:
        existing_order_id = (
            account_manager.get_order_id(
                account_name,
                int(ticket),
            )
        )
    except Exception:
        existing_order_id = None

    try:
        pending_type = (
            account_manager.get_pending_type(
                account_name,
                int(ticket),
            )
        )
    except Exception:
        pending_type = None

    try:
        pending_state = (
            account_manager.get_pending_state(
                account_name,
                int(ticket),
            )
        )
    except Exception:
        pending_state = None

    # If the order is mapped and this is not a destination-loss state,
    # leave it alone.
    if existing_order_id and not (
        force
        and _is_destination_recovery_state(
            pending_state
        )
    ):
        logger.info(
            "[%s] Recovery skipped for ticket=%s: "
            "orderId=%s still mapped, state=%s",
            account_name,
            ticket,
            existing_order_id,
            pending_state,
        )
        return True

    # If a pending-origin order is still mapped, it is authoritative.
    if existing_order_id and pending_type:
        logger.info(
            "[%s] Recovery skipped for ticket=%s: "
            "pending orderId=%s type=%s state=%s "
            "still exists",
            account_name,
            ticket,
            existing_order_id,
            pending_type,
            pending_state,
        )
        return True

    # ------------------------------------------------------------------
    # If the AccountManager has a separate market fallback order, do not
    # duplicate it.
    # ------------------------------------------------------------------

    try:
        market_order_id = (
            account_manager.get_market_order_id(
                account_name,
                int(ticket),
            )
        )
    except Exception:
        market_order_id = None

    if market_order_id:
        logger.info(
            "[%s] Recovery skipped for ticket=%s: "
            "market fallback orderId=%s still mapped",
            account_name,
            ticket,
            market_order_id,
        )
        return True

    try:
        market_position_id = (
            account_manager.get_market_position_id(
                account_name,
                int(ticket),
            )
        )
    except Exception:
        market_position_id = None

    if market_position_id:
        logger.info(
            "[%s] Recovery skipped for ticket=%s: "
            "market fallback positionId=%s already mapped",
            account_name,
            ticket,
            market_position_id,
        )
        return True

    # ------------------------------------------------------------------
    # Obtain source-of-truth MT5 payload.
    # ------------------------------------------------------------------

    if not isinstance(data, dict):
        data = None

    payload = (
        dict(data)
        if isinstance(data, dict)
        else _get_stored_mt5_payload(
            account_manager,
            account_name,
            ticket,
        )
    )

    if not payload:
        logger.warning(
            "[%s] Recovery skipped for ticket=%s: "
            "no cached MT5 source payload",
            account_name,
            ticket,
        )

        alert_trade_warning(
            account_name=account_name,
            action="destination_recovery_missing_mt5_payload",
            ticket=int(ticket),
            message=(
                "Cannot recover destination because "
                "the authoritative MT5 payload is unavailable"
            ),
        )

        return False

    # ------------------------------------------------------------------
    # Extract source information.
    # ------------------------------------------------------------------

    mt5_symbol = payload.get("symbol")

    side = str(
        payload.get("side")
        or payload.get("type")
        or ""
    ).strip().upper()

    src_volume = _to_float(
        payload.get("volume", 0),
        0.0,
    )

    sl = _to_float(
        payload.get("sl", 0),
        0.0,
    )

    tp = _to_float(
        payload.get("tp", 0),
        0.0,
    )

    magic = _to_int(
        payload.get("magic", 0),
        0,
    )

    entry_price = _extract_open_entry_price(
        payload
    )

    if not mt5_symbol:
        logger.warning(
            "[%s] Recovery skipped for ticket=%s: "
            "MT5 symbol missing",
            account_name,
            ticket,
        )
        return False

    if side not in ("BUY", "SELL"):
        logger.warning(
            "[%s] Recovery skipped for ticket=%s: "
            "invalid source side=%r",
            account_name,
            ticket,
            side,
        )
        return False

    if src_volume <= 0:
        logger.warning(
            "[%s] Recovery skipped for ticket=%s: "
            "invalid source volume=%s",
            account_name,
            ticket,
            src_volume,
        )
        return False

    if entry_price <= 0:
        logger.warning(
            "[%s] Recovery skipped for ticket=%s: "
            "original MT5 entry unavailable",
            account_name,
            ticket,
        )
        return False

    # ------------------------------------------------------------------
    # Resolve follower sizing using the original MT5 source data.
    # ------------------------------------------------------------------

    lots, decision = _resolve_open_volume_for_account(
        payload,
        config,
        account_name=account_name,
        client=client,
        account_manager=account_manager,
    )

    if lots is None or float(lots) <= 0:
        logger.warning(
            "[%s] Recovery rejected for ticket=%s: %s",
            account_name,
            ticket,
            decision,
        )

        alert_trade_warning(
            account_name=account_name,
            action="destination_recovery_rejected",
            ticket=int(ticket),
            message=decision,
            mt5_symbol=mt5_symbol,
            side=side,
            volume=src_volume,
        )

        return False

    # ------------------------------------------------------------------
    # Preserve SL/TP while destination is rebuilt.
    # ------------------------------------------------------------------

    if sl > 0 or tp > 0:
        _set_pending_sltp(
            account_name,
            ticket,
            mt5_symbol,
            sl,
            tp,
        )
    else:
        _clear_pending_sltp(
            account_name,
            ticket,
        )

    # ------------------------------------------------------------------
    # Recovery decision.
    #
    # Do NOT blindly use transition_pending_to_market().
    #
    # A destination-side cancellation is not a source-side cancellation.
    # We need to evaluate the original MT5 entry again.
    # ------------------------------------------------------------------

    recovery_config = config

    # Recovery should use market-or-pending behavior even if this was not
    # a startup event.  The configured maximum distance remains the guard.
    #
    # If startup_market_recovery_mode is "skip", that must not disable
    # emergency destination-loss recovery.
    #
    # Therefore we construct the plan directly using the same price/
    # distance logic below rather than honoring "skip".
    symbol_id = _get_symbol_id_for_account(
        client,
        config,
        mt5_symbol,
    )

    if symbol_id is None:
        logger.warning(
            "[%s] Recovery skipped for ticket=%s: "
            "symbol mapping failed for %s",
            account_name,
            ticket,
            mt5_symbol,
        )

        _clear_pending_sltp(
            account_name,
            ticket,
        )

        return False

    symbol = _get_symbol_details(
        client,
        int(symbol_id),
    )

    current_price = _extract_mt_current_market_price(
        payload,
        side,
    )

    current_price_source = "mt5"

    if (
        current_price is None
        or float(current_price) <= 0
    ):
        current_price = _get_current_market_price(
            client,
            int(symbol_id),
            side,
        )

        current_price_source = "ctrader"

    if (
        current_price is None
        or float(current_price) <= 0
    ):
        logger.warning(
            "[%s] Recovery skipped for ticket=%s: "
            "current market price unavailable",
            account_name,
            ticket,
        )

        return False

    pip_size = (
        _symbol_pip_size(symbol)
        if symbol is not None
        else 0.0
    )

    if pip_size <= 0:
        pip_size = _extract_mt_pip_size(
            payload
        )

    if pip_size <= 0:
        logger.warning(
            "[%s] Recovery skipped for ticket=%s: "
            "invalid pip size",
            account_name,
            ticket,
        )

        return False

    distance_pips = (
        abs(
            float(current_price)
            - float(entry_price)
        )
        / float(pip_size)
    )

    max_distance_pips = (
        _startup_market_max_distance_pips(
            recovery_config
        )
    )

    logger.info(
        "[%s] Destination recovery evaluation "
        "ticket=%s side=%s entry=%.5f current=%.5f "
        "source=%s distance=%.2f pips max=%.2f pips",
        account_name,
        ticket,
        side,
        float(entry_price),
        float(current_price),
        current_price_source,
        distance_pips,
        max_distance_pips,
    )

    # ------------------------------------------------------------------
    # CASE 1: Close enough -> market.
    # ------------------------------------------------------------------

    if distance_pips <= max_distance_pips:
        logger.warning(
            "[%s] Destination recovery ticket=%s -> "
            "MARKET; distance %.2f <= %.2f pips",
            account_name,
            ticket,
            distance_pips,
            max_distance_pips,
        )

        try:
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

            logger.info(
                "[%s] Destination recovery MARKET "
                "submitted for ticket=%s",
                account_name,
                ticket,
            )

            return True

        except Exception as e:
            logger.error(
                "[%s] Destination recovery MARKET "
                "failed for ticket=%s: %s",
                account_name,
                ticket,
                e,
            )

            alert_trade_failure(
                account_name=account_name,
                action="destination_recovery_market",
                ticket=int(ticket),
                exc=e,
                mt5_symbol=mt5_symbol,
                side=side,
                volume=src_volume,
                entry_price=entry_price,
                current_price=current_price,
                distance_pips=distance_pips,
            )

            return False

    # ------------------------------------------------------------------
    # CASE 2: Too far -> recreate at original MT5 entry.
    #
    # Correct pending type is determined strictly by current price
    # relative to original source entry.
    # ------------------------------------------------------------------

    pending_type = None
    stop_price = 0.0
    limit_price = 0.0

    if side == "BUY":
        if float(current_price) <= float(entry_price):
            pending_type = "limit"
            limit_price = float(entry_price)

            reason = (
                "BUY current <= entry -> BUY LIMIT"
            )

        else:
            pending_type = "stop"
            stop_price = float(entry_price)

            reason = (
                "BUY current > entry -> BUY STOP"
            )

    elif side == "SELL":
        if float(current_price) >= float(entry_price):
            pending_type = "limit"
            limit_price = float(entry_price)

            reason = (
                "SELL current >= entry -> SELL LIMIT"
            )

        else:
            pending_type = "stop"
            stop_price = float(entry_price)

            reason = (
                "SELL current < entry -> SELL STOP"
            )

    else:
        logger.warning(
            "[%s] Destination recovery skipped "
            "ticket=%s: unsupported side=%s",
            account_name,
            ticket,
            side,
        )

        return False

    logger.warning(
        "[%s] Destination recovery ticket=%s -> "
        "%s at original entry %.5f; "
        "current=%.5f distance=%.2f pips; %s",
        account_name,
        ticket,
        pending_type.upper(),
        float(entry_price),
        float(current_price),
        distance_pips,
        reason,
    )

    try:
        copy_pending_to_account(
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
            pending_type=pending_type,
            stop_price=stop_price,
            limit_price=limit_price,
            expiration_ms=0,
        )

        logger.info(
            "[%s] Destination recovery pending "
            "submitted for ticket=%s type=%s "
            "entry=%.5f",
            account_name,
            ticket,
            pending_type,
            float(entry_price),
        )

        return True

    except Exception as e:
        logger.error(
            "[%s] Destination recovery pending "
            "failed for ticket=%s type=%s: %s",
            account_name,
            ticket,
            pending_type,
            e,
        )

        alert_trade_failure(
            account_name=account_name,
            action="destination_recovery_pending",
            ticket=int(ticket),
            exc=e,
            mt5_symbol=mt5_symbol,
            side=side,
            volume=src_volume,
            entry_price=entry_price,
            current_price=current_price,
            distance_pips=distance_pips,
            pending_type=pending_type,
        )

        return False


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

