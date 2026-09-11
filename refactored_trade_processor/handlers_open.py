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
    _to_int,
    _to_float,
    _MASTER_LOTS_LOCK,
)

from .risk import *

from .helpers import *
from .helpers import (
    _extract_open_entry_price,
    _is_startup_market_recovery,
)

from .routing import *
from .routing import (
    _get_target_account_contexts,
)

from .sltp_repair import *

from .destination_recovery import (
    recover_missing_destination,
    is_destination_recovery_state,
)


def handle_open_event(
    data,
    account_manager,
):
    ticket = _to_int(
        data.get("ticket")
    )

    mt5_symbol = data.get("symbol")

    side = str(
        data.get("side")
        or data.get("type")
        or ""
    ).strip().upper()

    src_volume = _to_float(
        data.get("volume", 0),
        0.0,
    )

    sl = _to_float(
        data.get("sl", 0),
        0.0,
    )

    tp = _to_float(
        data.get("tp", 0),
        0.0,
    )

    magic = _to_int(
        data.get("magic", 0),
        0,
    )

    entry_price = _extract_open_entry_price(
        data
    )

    is_startup_recovery = (
        _is_startup_market_recovery(data)
    )

    logger.info(
        f"OPEN event - Ticket: {ticket}, "
        f"Symbol: {mt5_symbol}, "
        f"Side: {side}, "
        f"Volume: {src_volume}, "
        f"SL: {sl}, TP: {tp}, "
        f"EntryPrice: {entry_price}, "
        f"StartupRecovery: "
        f"{is_startup_recovery}"
    )

    if ticket <= 0:
        msg = "OPEN ignored: invalid ticket"

        logger.warning(msg)

        alert_trade_warning(
            account_name="router",
            action="open_invalid_ticket",
            ticket=ticket,
            message=msg,
            magic=magic,
            mt5_symbol=mt5_symbol,
        )

        return

    if src_volume > 0:
        with _MASTER_LOTS_LOCK:
            MASTER_OPEN_LOTS[
                int(ticket)
            ] = float(src_volume)

            MASTER_CLOSED_LOTS[
                int(ticket)
            ] = 0.0

    contexts = _get_target_account_contexts(
        data,
        account_manager,
    )

    if not contexts:
        msg = (
            f"OPEN ignored for ticket {ticket}: "
            f"no target accounts for "
            f"magic={magic}"
        )

        logger.warning(msg)

        alert_trade_warning(
            account_name="router",
            action="open_no_target_accounts",
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
            # CANONICAL LOOKUPS
            # ----------------------------------------------------------

            pending_position_id = (
                account_manager.get_pending_position_id(
                    account_name,
                    int(ticket),
                )
            )

            existing_position_id = (
                account_manager.get_position_id(
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

            pending_type = (
                account_manager.get_pending_type(
                    account_name,
                    int(ticket),
                )
            )

            pending_state = (
                account_manager.get_pending_state(
                    account_name,
                    int(ticket),
                )
            )

            # ----------------------------------------------------------
            # PENDING-ORIGIN POSITION IS ALWAYS CANONICAL.
            #
            # This protects the LIMIT activation race.
            # ----------------------------------------------------------

            if pending_position_id:
                logger.info(
                    f"[{account_name}] OPEN ticket "
                    f"{ticket} already has "
                    f"pending-origin positionId="
                    f"{pending_position_id}; "
                    f"keeping it canonical"
                )

                try_apply_pending_sltp(
                    account_name=account_name,
                    client=client,
                    config=config,
                    ticket=int(ticket),
                    account_manager=account_manager,
                    force=True,
                )

                continue

            # ----------------------------------------------------------
            # IMPORTANT CHANGE:
            #
            # A destination-side UNKNOWN / RECOVERY_REQUIRED state is
            # NOT treated as an ordinary pending order.
            #
            # Previously:
            #
            #     existing_order_id OR pending_type
            #         -> transition_pending_to_market()
            #
            # That could chase the market after the cTrader pending
            # order disappeared.
            #
            # Now destination-loss states use the safe recovery planner.
            # ----------------------------------------------------------

            if is_destination_recovery_state(
                pending_state
            ):
                logger.warning(
                    f"[{account_name}] OPEN ticket "
                    f"{ticket} has destination "
                    f"recovery state={pending_state}; "
                    f"using source-entry recovery instead "
                    f"of pending-to-market transition"
                )

                recovered = recover_missing_destination(
                    account_name=account_name,
                    ticket=int(ticket),
                    account_manager=account_manager,
                    data=data,
                    force=True,
                )

                if recovered:
                    try_apply_pending_sltp(
                        account_name=account_name,
                        client=client,
                        config=config,
                        ticket=int(ticket),
                        account_manager=account_manager,
                        force=True,
                    )

                continue

            # ----------------------------------------------------------
            # NORMAL PENDING -> MARKET TRANSITION
            #
            # This is only used when the pending order is still known
            # and its state is not an unresolved destination-loss state.
            #
            # trade_executor is responsible for:
            #   - LIMIT race
            #   - STOP / STOP_LIMIT no-market-fallback
            #   - cancellation confirmation
            #   - ORDER_NOT_FOUND reconciliation
            # ----------------------------------------------------------

            if existing_order_id or pending_type:
                logger.info(
                    f"[{account_name}] OPEN "
                    f"pending-to-market transition "
                    f"for ticket {ticket}: "
                    f"pending orderId="
                    f"{existing_order_id}, "
                    f"type={pending_type}, "
                    f"state={pending_state}"
                )

                transition_pending_to_market(
                    account_name=account_name,
                    client=client,
                    config=config,
                    ticket=int(ticket),
                    mt5_symbol=mt5_symbol,
                    side=side,
                    volume=src_volume,
                    sl=sl,
                    tp=tp,
                    magic=magic,
                    account_manager=account_manager,
                    pending_type=pending_type,
                )

                try_apply_pending_sltp(
                    account_name=account_name,
                    client=client,
                    config=config,
                    ticket=int(ticket),
                    account_manager=account_manager,
                    force=True,
                )

                continue

            # ----------------------------------------------------------
            # EXISTING POSITION
            # ----------------------------------------------------------

            if existing_position_id:
                logger.info(
                    f"[{account_name}] OPEN skip "
                    f"for ticket {ticket}: "
                    f"already mapped to "
                    f"positionId="
                    f"{existing_position_id}"
                )

                continue

            # ----------------------------------------------------------
            # NORMAL NEW MARKET OPEN
            # ----------------------------------------------------------

            lots, decision = (
                _resolve_open_volume_for_account(
                    data,
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
                    f"OPEN rejected for ticket "
                    f"{ticket}: {decision}"
                )

                logger.warning(
                    f"[{account_name}] {msg}"
                )

                alert_trade_warning(
                    account_name=account_name,
                    action="open_rejected",
                    ticket=ticket,
                    message=msg,
                    mt5_symbol=mt5_symbol,
                    side=side,
                    volume=src_volume,
                    decision=decision,
                )

                continue

            logger.info(
                f"[{account_name}] OPEN sizing: "
                f"{decision}, "
                f"lots={float(lots):.4f}"
            )

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

            # ----------------------------------------------------------
            # STARTUP RECOVERY
            # ----------------------------------------------------------

            if is_startup_recovery:
                if not _startup_sync_market_orders_enabled(
                    config
                ):
                    logger.info(
                        f"[{account_name}] Startup "
                        f"recovery skipped for ticket "
                        f"{ticket} "
                        f"(startup_sync_market_orders="
                        f"false)"
                    )

                    continue

                recovery_plan = (
                    _build_startup_recovery_plan(
                        client=client,
                        config=config,
                        mt5_symbol=mt5_symbol,
                        side=side,
                        entry_price=entry_price,
                        data=data,
                    )
                )

                logger.info(
                    f"[{account_name}] Startup "
                    f"recovery decision for ticket "
                    f"{ticket}: "
                    f"{recovery_plan.get('reason')} "
                    f"-> "
                    f"{recovery_plan.get('action')}"
                )

                if (
                    recovery_plan.get("action")
                    == "skip"
                ):
                    _clear_pending_sltp(
                        account_name,
                        ticket,
                    )

                    continue

                if (
                    recovery_plan.get("action")
                    == "pending"
                ):
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
                        pending_type=(
                            recovery_plan.get(
                                "pending_type",
                                "limit",
                            )
                        ),
                        stop_price=float(
                            recovery_plan.get(
                                "stop_price",
                                0.0,
                            )
                            or 0.0
                        ),
                        limit_price=float(
                            recovery_plan.get(
                                "limit_price",
                                0.0,
                            )
                            or 0.0
                        ),
                        expiration_ms=(
                            _startup_pending_expiration_ms(
                                config
                            )
                        ),
                    )

                    continue

            # ----------------------------------------------------------
            # NORMAL MARKET COPY
            # ----------------------------------------------------------

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

            try_apply_pending_sltp(
                account_name=account_name,
                client=client,
                config=config,
                ticket=int(ticket),
                account_manager=account_manager,
                force=False,
            )

        except Exception as e:
            alert_trade_failure(
                account_name=account_name,
                action="handle_open_event",
                ticket=ticket,
                exc=e,
                mt5_symbol=mt5_symbol,
                side=side,
                volume=src_volume,
                sl=sl,
                tp=tp,
                magic=magic,
            )


# ---------------------------------------------------------------------------
# PENDING OPEN
# ---------------------------------------------------------------------------
