```python
import time
from threading import Lock

from app_state import (
    logger,
    PENDING_SLTP,
    MASTER_OPEN_LOTS,
    MASTER_CLOSED_LOTS,
    alert_trade_failure,
    alert_trade_warning,
    alert_trade_info,
)

from trade_executor import (
    copy_open_to_account,
    copy_pending_to_account,
    transition_pending_to_market,
)

from symbol_mapper import SymbolMapper

from .common import *
from .common import (
    to_int,
    to_float,
    canonical_pending_type,
    risk_mode,
)

from .risk import *
from .risk import resolve_open_volume_for_account

from .helpers import *

from .routing import *
from .routing import (
    get_target_account_contexts,
)

from .sltp_repair import *
from .sltp_repair import safe_symbol_id_or_warn

from .destination_recovery import recover_missing_destination


def handle_pending_open_event(
    data,
    account_manager,
):
    ticket = to_int(
        data.get("ticket")
    )

    mt5_symbol = data.get("symbol")

    side = str(
        data.get("side")
        or data.get("type")
        or ""
    ).strip().upper()

    volume = to_float(
        data.get("volume", 0),
        0.0,
    )

    sl = to_float(
        data.get("sl", 0),
        0.0,
    )

    tp = to_float(
        data.get("tp", 0),
        0.0,
    )

    magic = to_int(
        data.get("magic", 0),
        0,
    )

    pending_type = canonical_pending_type(
        data
    )

    entry_price = to_float(
        data.get("entry_price", 0),
        0.0,
    )

    stop_price = to_float(
        data.get("stop_price", 0),
        0.0,
    )

    limit_price = to_float(
        data.get("limit_price", 0),
        0.0,
    )

    expiration_ms = to_int(
        data.get("expiration_ms", 0),
        0,
    )

    if pending_type not in (
        "limit",
        "stop",
        "stop_limit",
    ):
        msg = (
            f"PENDING_OPEN ignored for ticket "
            f"{ticket}: unsupported "
            f"pending_type={pending_type!r}"
        )

        logger.warning(msg)

        alert_trade_warning(
            account_name="router",
            action="pending_open_unsupported_type",
            ticket=ticket,
            message=msg,
            pending_type=pending_type,
            mt5_symbol=mt5_symbol,
        )

        return

    if (
        pending_type == "limit"
        and limit_price <= 0
    ):
        limit_price = entry_price

    if (
        pending_type == "stop"
        and stop_price <= 0
    ):
        stop_price = entry_price

    if pending_type == "stop_limit":
        if stop_price <= 0:
            stop_price = entry_price

        if limit_price <= 0:
            limit_price = entry_price

    logger.info(
        f"PENDING_OPEN event - "
        f"Ticket: {ticket}, "
        f"Symbol: {mt5_symbol}, "
        f"Side: {side}, "
        f"Volume: {volume}, "
        f"pending_type={pending_type}, "
        f"stop_price={stop_price}, "
        f"limit_price={limit_price}, "
        f"SL={sl}, TP={tp}, "
        f"expiration_ms={expiration_ms}"
    )

    pending_entry_price = 0.0

    if pending_type == "limit":
        pending_entry_price = float(
            limit_price or 0.0
        )

    elif pending_type == "stop":
        pending_entry_price = float(
            stop_price or 0.0
        )

    elif pending_type == "stop_limit":
        pending_entry_price = (
            float(limit_price or 0.0)
            if float(limit_price or 0.0) > 0
            else float(stop_price or 0.0)
        )

    contexts = get_target_account_contexts(
        data,
        account_manager,
    )

    if not contexts:
        msg = (
            f"PENDING_OPEN ignored for ticket "
            f"{ticket}: no target accounts for "
            f"magic={magic}"
        )

        logger.warning(msg)

        alert_trade_warning(
            account_name="router",
            action="pending_open_no_target_accounts",
            ticket=ticket,
            message=msg,
            magic=magic,
            mt5_symbol=mt5_symbol,
        )

        return

    for (
        account_name,
        client,
        config,
    ) in contexts:

        try:
            existing_pending_position_id = (
                account_manager.get_pending_position_id(
                    account_name,
                    int(ticket),
                )
            )

            existing_order_id = (
                account_manager.get_order_id(
                    account_name,
                    int(ticket),
                )
            )

            existing_pending_type = (
                account_manager.get_pending_type(
                    account_name,
                    int(ticket),
                )
            )

            existing_pending_state = (
                account_manager.get_pending_state(
                    account_name,
                    int(ticket),
                )
            )

            if existing_pending_position_id:
                logger.info(
                    f"[{account_name}] "
                    f"PENDING_OPEN skip for ticket "
                    f"{ticket}: pending-origin "
                    f"positionId="
                    f"{existing_pending_position_id} "
                    f"already active"
                )

                continue

            if existing_order_id:
                if (
                    existing_pending_type
                    and existing_pending_type
                    != pending_type
                ):
                    logger.warning(
                        f"[{account_name}] "
                        f"PENDING_OPEN type mismatch "
                        f"for ticket {ticket}: "
                        f"existing="
                        f"{existing_pending_type}, "
                        f"incoming={pending_type}; "
                        f"keeping existing type"
                    )

                logger.info(
                    f"[{account_name}] "
                    f"PENDING_OPEN skip for ticket "
                    f"{ticket} "
                    f"(already mapped to "
                    f"orderId={existing_order_id}, "
                    f"type={existing_pending_type or pending_type}, "
                    f"state={existing_pending_state})"
                )

                continue

            rm = risk_mode(config)

            sizing_volume = float(volume)

            if rm in (
                "FIXED_USD",
                "PERCENT_EQUITY",
            ):
                sizing_data = dict(data)

                sizing_data[
                    "entry_price"
                ] = float(
                    pending_entry_price or 0.0
                )

                lots, decision = (
                    resolve_open_volume_for_account(
                        sizing_data,
                        config,
                        account_name=account_name,
                        client=client,
                        account_manager=account_manager,
                    )
                )

                if (
                    lots is None
                    or float(lots) <= 0
                ):
                    msg = (
                        f"PENDING_OPEN rejected "
                        f"for ticket {ticket}: "
                        f"{decision}"
                    )

                    logger.warning(
                        f"[{account_name}] {msg}"
                    )

                    alert_trade_warning(
                        account_name=account_name,
                        action="pending_open_rejected",
                        ticket=ticket,
                        message=msg,
                        mt5_symbol=mt5_symbol,
                        side=side,
                        volume=volume,
                        decision=decision,
                        pending_type=pending_type,
                    )

                    continue

                sizing_volume = float(lots)

                logger.info(
                    f"[{account_name}] "
                    f"PENDING_OPEN sizing: "
                    f"{decision}, "
                    f"lots={float(lots):.4f}"
                )

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
            alert_trade_failure(
                account_name=account_name,
                action="handle_pending_open_event",
                ticket=ticket,
                exc=e,
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


# ---------------------------------------------------------------------------
# PENDING MODIFY
# ---------------------------------------------------------------------------

def handle_pending_modify_event(
    data,
    account_manager,
):
    ticket = to_int(
        data.get("ticket"),
        0,
    )

    mt5_symbol = data.get("symbol")

    side = str(
        data.get("side")
        or data.get("type")
        or ""
    ).strip().upper()

    volume = to_float(
        data.get("volume", 0),
        0.0,
    )

    sl = to_float(
        data.get("sl", 0),
        0.0,
    )

    tp = to_float(
        data.get("tp", 0),
        0.0,
    )

    magic = to_int(
        data.get("magic", 0),
        0,
    )

    pending_type = canonical_pending_type(
        data
    )

    entry_price = to_float(
        data.get("entry_price", 0),
        0.0,
    )

    stop_price = to_float(
        data.get("stop_price", 0),
        0.0,
    )

    limit_price = to_float(
        data.get("limit_price", 0),
        0.0,
    )

    expiration_ms = to_int(
        data.get("expiration_ms", 0),
        0,
    )

    if pending_type not in (
        "limit",
        "stop",
        "stop_limit",
    ):
        msg = (
            f"PENDING_MODIFY ignored for ticket "
            f"{ticket}: unsupported "
            f"pending_type={pending_type!r}"
        )

        logger.warning(msg)

        alert_trade_warning(
            account_name="router",
            action="pending_modify_unsupported_type",
            ticket=ticket,
            message=msg,
            pending_type=pending_type,
            mt5_symbol=mt5_symbol,
        )

        return

    if (
        pending_type == "limit"
        and limit_price <= 0
    ):
        limit_price = entry_price

    if (
        pending_type == "stop"
        and stop_price <= 0
    ):
        stop_price = entry_price

    if pending_type == "stop_limit":
        if stop_price <= 0:
            stop_price = entry_price

        if limit_price <= 0:
            limit_price = entry_price

    logger.info(
        f"PENDING_MODIFY event - "
        f"Ticket: {ticket}, "
        f"Symbol: {mt5_symbol}, "
        f"Side: {side}, "
        f"Volume: {volume}, "
        f"pending_type={pending_type}, "
        f"stop_price={stop_price}, "
        f"limit_price={limit_price}, "
        f"SL={sl}, TP={tp}, "
        f"expiration_ms={expiration_ms}"
    )

    contexts = get_target_account_contexts(
        data,
        account_manager,
    )

    if not contexts:
        msg = (
            f"PENDING_MODIFY ignored for ticket "
            f"{ticket}: no target accounts for "
            f"magic={magic}"
        )

        logger.warning(msg)

        alert_trade_warning(
            account_name="router",
            action="pending_modify_no_target_accounts",
            ticket=ticket,
            message=msg,
            magic=magic,
            mt5_symbol=mt5_symbol,
        )

        return

    for (
        account_name,
        client,
        config,
    ) in contexts:

        try:
            order_id = (
                account_manager.get_order_id(
                    account_name,
                    int(ticket),
                )
            )

            if not order_id:
                msg = (
                    f"PENDING_MODIFY ignored for "
                    f"ticket {ticket} "
                    f"(no orderId mapping yet)"
                )

                logger.warning(
                    f"[{account_name}] {msg}"
                )

                alert_trade_warning(
                    account_name=account_name,
                    action=(
                        "pending_modify_missing_"
                        "order_mapping"
                    ),
                    ticket=ticket,
                    message=msg,
                    mt5_symbol=mt5_symbol,
                )

                continue

            symbol_id = safe_symbol_id_or_warn(
                account_name,
                client,
                config,
                ticket,
                mt5_symbol,
                "pending_modify",
            )

            if symbol_id is None:
                continue

            client.amend_pending_order(
                account_id=config.account_id,
                order_id=int(order_id),
                symbol_id=int(symbol_id),
                side=side,
                volume=float(volume),
                pending_type=pending_type,
                stop_price=(
                    float(stop_price)
                    if float(stop_price or 0) > 0
                    else None
                ),
                limit_price=(
                    float(limit_price)
                    if float(limit_price or 0) > 0
                    else None
                ),
                stop_loss=(
                    float(sl)
                    if float(sl or 0) > 0
                    else None
                ),
                take_profit=(
                    float(tp)
                    if float(tp or 0) > 0
                    else None
                ),
                expiration_ms=(
                    int(expiration_ms)
                    if int(expiration_ms or 0) > 0
                    else None
                ),
            )

            logger.info(
                f"[{account_name}] Modified pending "
                f"order {int(order_id)} "
                f"for ticket {ticket}"
            )

        except Exception as e:
            alert_trade_failure(
                account_name=account_name,
                action="handle_pending_modify_event",
                ticket=ticket,
                exc=e,
                mt5_symbol=mt5_symbol,
                order_id=(
                    order_id
                    if "order_id" in locals()
                    else None
                ),
                side=side,
                volume=volume,
                sl=sl,
                tp=tp,
                pending_type=pending_type,
                stop_price=stop_price,
                limit_price=limit_price,
                expiration_ms=expiration_ms,
            )


# ---------------------------------------------------------------------------
# PENDING CANCEL
# ---------------------------------------------------------------------------

def handle_pending_cancel_event(
    data,
    account_manager,
):
    ticket = to_int(
        data.get("ticket", 0),
        0,
    )

    mt5_symbol = data.get("symbol")

    magic = to_int(
        data.get("magic", 0),
        0,
    )

    logger.info(
        f"PENDING_CANCEL event - "
        f"Ticket: {ticket}, "
        f"Symbol: {mt5_symbol}"
    )

    contexts = get_target_account_contexts(
        data,
        account_manager,
    )

    if not contexts:
        msg = (
            f"PENDING_CANCEL ignored for ticket "
            f"{ticket}: no target accounts for "
            f"magic={magic}"
        )

        logger.warning(msg)

        alert_trade_warning(
            account_name="router",
            action="pending_cancel_no_target_accounts",
            ticket=ticket,
            message=msg,
            magic=magic,
            mt5_symbol=mt5_symbol,
        )

        return

    for (
        account_name,
        client,
        config,
    ) in contexts:

        try:
            # ----------------------------------------------------------
            # If pending already activated, there is nothing to cancel.
            # ----------------------------------------------------------

            if account_manager.get_pending_position_id(
                account_name,
                int(ticket),
            ):
                logger.info(
                    f"[{account_name}] "
                    f"PENDING_CANCEL ignored for "
                    f"ticket {ticket}: "
                    f"pending-origin position is "
                    f"already active"
                )

                continue

            order_id = (
                account_manager.get_order_id(
                    account_name,
                    int(ticket),
                )
            )

            if not order_id:
                current_state = (
                    account_manager.get_pending_state(
                        account_name,
                        int(ticket),
                    )
                )

                if current_state != "CANCELLED":
                    account_manager.set_pending_state(
                        account_name,
                        int(ticket),
                        "UNKNOWN",
                    )

                    account_manager.request_reconcile(
                        account_name
                    )

                msg = (
                    f"PENDING_CANCEL uncertain for "
                    f"ticket {ticket}: "
                    f"no orderId mapping"
                )

                logger.warning(
                    f"[{account_name}] {msg}"
                )

                alert_trade_warning(
                    account_name=account_name,
                    action=(
                        "pending_cancel_missing_"
                        "order_mapping"
                    ),
                    ticket=ticket,
                    message=msg,
                    mt5_symbol=mt5_symbol,
                )

                continue

            client.cancel_pending_order(
                account_id=config.account_id,
                order_id=int(order_id),
            )

            # Sending cancel is NOT confirmation.
            account_manager.set_pending_state(
                account_name,
                int(ticket),
                "CANCEL_REQUESTED",
            )

            logger.info(
                f"[{account_name}] "
                f"Cancel requested: "
                f"ticket {ticket} -> "
                f"orderId {int(order_id)}"
            )

        except Exception as e:
            text = str(e).upper()

            account_manager.set_pending_state(
                account_name,
                int(ticket),
                "UNKNOWN",
            )

            account_manager.request_reconcile(
                account_name
            )

            if (
                "ORDER_NOT_FOUND" in text
                or "ORDER NOT FOUND" in text
            ):
                logger.warning(
                    f"[{account_name}] "
                    f"PENDING_CANCEL "
                    f"ORDER_NOT_FOUND for ticket "
                    f"{ticket} "
                    f"orderId="
                    f"{order_id if 'order_id' in locals() else None}; "
                    f"treating cancellation as "
                    f"UNKNOWN and reconciling"
                )

            else:
                logger.warning(
                    f"[{account_name}] "
                    f"PENDING_CANCEL failed for "
                    f"ticket {ticket} "
                    f"orderId="
                    f"{order_id if 'order_id' in locals() else None}: "
                    f"{e}"
                )

            alert_trade_failure(
                account_name=account_name,
                action="handle_pending_cancel_event",
                ticket=ticket,
                exc=e,
                mt5_symbol=mt5_symbol,
                order_id=(
                    order_id
                    if "order_id" in locals()
                    else None
                ),
            )


# ---------------------------------------------------------------------------
# MODIFY
# ---------------------------------------------------------------------------

