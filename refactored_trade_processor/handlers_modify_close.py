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
    _to_float_or_none,
    _MASTER_LOTS_LOCK,
)

from .risk import *

from .helpers import *

from .routing import *
from .routing import (
    _get_target_account_contexts,
)

from .sltp_repair import *

from .destination_recovery import (
    recover_missing_destination,
)


def handle_modify_event(
    data,
    account_manager,
):
    ticket = _to_int(
        data.get("ticket")
    )

    mt5_symbol = data.get("symbol")

    new_sl = _to_float(
        data.get("sl", 0),
        0.0,
    )

    new_tp = _to_float(
        data.get("tp", 0),
        0.0,
    )

    magic = _to_int(
        data.get("magic", 0),
        0,
    )

    logger.info(
        f"MODIFY event - "
        f"Ticket: {ticket}, "
        f"Symbol: {mt5_symbol}, "
        f"New SL: {new_sl}, "
        f"New TP: {new_tp}"
    )

    contexts = _get_target_account_contexts(
        data,
        account_manager,
    )

    if not contexts:
        msg = (
            f"MODIFY ignored for ticket {ticket}: "
            f"no target accounts for "
            f"magic={magic}"
        )

        logger.warning(msg)

        alert_trade_warning(
            account_name="router",
            action="modify_no_target_accounts",
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
            position_id = (
                account_manager.get_position_id(
                    account_name,
                    ticket,
                )
            )

            symbol_id = _safe_symbol_id_or_warn(
                account_name,
                client,
                config,
                ticket,
                mt5_symbol,
                "modify",
            )

            if (
                position_id
                and symbol_id is not None
            ):
                try:
                    client.amend_position(
                        account_id=config.account_id,
                        position_id=position_id,
                        symbol_id=symbol_id,
                        stop_loss=(
                            new_sl
                            if new_sl > 0
                            else None
                        ),
                        take_profit=(
                            new_tp
                            if new_tp > 0
                            else None
                        ),
                    )

                    logger.info(
                        f"[{account_name}] "
                        f"Modified position "
                        f"{position_id} "
                        f"for ticket {ticket}"
                    )

                    _clear_pending_sltp(
                        account_name,
                        ticket,
                    )

                except Exception as amend_error:
                    msg = (
                        f"Immediate modify failed "
                        f"for ticket {ticket}, "
                        f"queueing repair: "
                        f"{amend_error}"
                    )

                    logger.warning(
                        f"[{account_name}] {msg}"
                    )

                    alert_trade_warning(
                        account_name=account_name,
                        action=(
                            "modify_immediate_failed_"
                            "queue_repair"
                        ),
                        ticket=ticket,
                        message=msg,
                        mt5_symbol=mt5_symbol,
                        position_id=position_id,
                        symbol_id=symbol_id,
                        sl=new_sl,
                        tp=new_tp,
                    )

                    _set_pending_sltp(
                        account_name,
                        ticket,
                        mt5_symbol,
                        new_sl,
                        new_tp,
                    )

                    _touch_pending_sltp_retry(
                        account_name,
                        ticket,
                        error=str(
                            amend_error
                        ),
                        position_id=position_id,
                    )

            else:
                msg = (
                    f"Position not found for "
                    f"ticket {ticket}, "
                    f"storing pending SL/TP"
                )

                logger.warning(
                    f"[{account_name}] {msg}"
                )

                alert_trade_warning(
                    account_name=account_name,
                    action=(
                        "modify_position_missing_"
                        "store_pending_sltp"
                    ),
                    ticket=ticket,
                    message=msg,
                    mt5_symbol=mt5_symbol,
                    sl=new_sl,
                    tp=new_tp,
                )

                _set_pending_sltp(
                    account_name,
                    ticket,
                    mt5_symbol,
                    new_sl,
                    new_tp,
                )

        except Exception as e:
            alert_trade_failure(
                account_name=account_name,
                action="handle_modify_event",
                ticket=ticket,
                exc=e,
                mt5_symbol=mt5_symbol,
                sl=new_sl,
                tp=new_tp,
                magic=magic,
            )


# ---------------------------------------------------------------------------
# CLOSE
# ---------------------------------------------------------------------------

def handle_close_event(
    data,
    account_manager,
):
    ticket = _to_int(
        data.get("ticket")
    )

    mt5_symbol = data.get("symbol")

    close_lots = _to_float_or_none(
        data.get("volume", None)
    )

    mt5_contract_size = _to_float(
        data.get("mt5_contract_size", 0),
        0.0,
    )

    magic = _to_int(
        data.get("magic", 0),
        0,
    )

    logger.info(
        f"CLOSE event - "
        f"Ticket: {ticket}, "
        f"Symbol: {mt5_symbol}, "
        f"close_lots={close_lots}"
    )

    with _MASTER_LOTS_LOCK:
        master_open_lots = float(
            MASTER_OPEN_LOTS.get(
                int(ticket),
                0,
            )
            or 0
        )

        master_closed_lots = float(
            MASTER_CLOSED_LOTS.get(
                int(ticket),
                0,
            )
            or 0
        )

    master_remaining_lots = max(
        0.0,
        master_open_lots
        - master_closed_lots,
    )

    proportional_pct = None

    if (
        close_lots is not None
        and master_remaining_lots > 0
    ):
        proportional_pct = max(
            0.0,
            min(
                1.0,
                float(close_lots)
                / float(master_remaining_lots),
            ),
        )

    contexts = _get_target_account_contexts(
        data,
        account_manager,
    )

    if not contexts:
        msg = (
            f"CLOSE ignored for ticket {ticket}: "
            f"no target accounts for "
            f"magic={magic}"
        )

        logger.warning(msg)

        alert_trade_warning(
            account_name="router",
            action="close_no_target_accounts",
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
            position_id = (
                account_manager.get_position_id(
                    account_name,
                    ticket,
                )
            )

            if not position_id:
                logger.info(
                    f"[{account_name}] CLOSE ignored "
                    f"for ticket {ticket} "
                    f"(no mapping)"
                )

                _clear_pending_sltp(
                    account_name,
                    ticket,
                )

                continue

            symbol_id = _safe_symbol_id_or_warn(
                account_name,
                client,
                config,
                ticket,
                mt5_symbol,
                "close",
            )

            if symbol_id is None:
                _clear_pending_sltp(
                    account_name,
                    ticket,
                )

                continue

            rm = _risk_mode(config)

            follower_units = (
                account_manager.get_position_volume(
                    account_name,
                    position_id,
                )
            )

            close_units = None

            if (
                close_lots is not None
                and follower_units is not None
                and int(follower_units) > 0
            ):
                if (
                    rm != "SOURCE_VOLUME"
                    and proportional_pct is not None
                ):
                    close_units = int(
                        round(
                            proportional_pct
                            * float(follower_units)
                        )
                    )

                    logger.info(
                        f"[{account_name}] "
                        f"Proportional CLOSE: "
                        f"risk_mode={rm}, "
                        f"master_close_lots="
                        f"{float(close_lots):.4f}, "
                        f"master_remaining_lots="
                        f"{master_remaining_lots:.4f}, "
                        f"pct={proportional_pct:.4f}, "
                        f"follower_units="
                        f"{int(follower_units)} "
                        f"-> close_units="
                        f"{close_units}"
                    )

                else:
                    if mt5_contract_size > 0:
                        close_units = (
                            _lots_to_ctrader_cents(
                                float(close_lots),
                                mt5_contract_size,
                            )
                        )

                    logger.info(
                        f"[{account_name}] "
                        f"Absolute CLOSE: "
                        f"risk_mode={rm}, "
                        f"close_lots={close_lots}, "
                        f"mt5_contract_size="
                        f"{mt5_contract_size} "
                        f"-> close_units="
                        f"{close_units}"
                    )

            if (
                close_units is None
                or int(close_units) <= 0
            ):
                close_units = follower_units

            if (
                close_units is None
                or int(close_units) <= 0
            ):
                msg = (
                    f"Cannot close ticket {ticket} "
                    f"(positionId={position_id}) "
                    f"because close volume is "
                    f"unknown/invalid."
                )

                logger.warning(
                    f"[{account_name}] {msg}"
                )

                alert_trade_warning(
                    account_name=account_name,
                    action="close_invalid_volume",
                    ticket=ticket,
                    message=msg,
                    mt5_symbol=mt5_symbol,
                    position_id=position_id,
                    follower_units=follower_units,
                    close_lots=close_lots,
                )

                _clear_pending_sltp(
                    account_name,
                    ticket,
                )

                continue

            if (
                follower_units is not None
                and int(follower_units) > 0
            ):
                close_units = min(
                    int(close_units),
                    int(follower_units),
                )

            client.close_position(
                account_id=config.account_id,
                position_id=position_id,
                volume=int(close_units),
                symbol_id=symbol_id,
            )

            logger.info(
                f"[{account_name}] Close sent for "
                f"position {position_id} "
                f"(ticket {ticket}) "
                f"close_units={int(close_units)}"
            )

            if (
                follower_units is not None
                and int(close_units)
                >= int(follower_units)
            ):
                fallback_position_id = (
                    account_manager.get_fallback_position_id(
                        account_name,
                        int(ticket),
                    )
                )

                if (
                    fallback_position_id
                    and int(fallback_position_id)
                    != int(position_id)
                ):
                    account_manager._remove_position_mapping(
                        account_name,
                        int(ticket),
                        origin="pending",
                    )

                    logger.warning(
                        f"[{account_name}] "
                        f"Preserving market fallback "
                        f"positionId="
                        f"{fallback_position_id} "
                        f"for ticket={ticket} "
                        f"after canonical pending close"
                    )

                else:
                    account_manager.remove_mapping(
                        account_name,
                        ticket,
                    )

            _clear_pending_sltp(
                account_name,
                ticket,
            )

        except Exception as e:
            alert_trade_failure(
                account_name=account_name,
                action="handle_close_event",
                ticket=ticket,
                exc=e,
                mt5_symbol=mt5_symbol,
                position_id=(
                    position_id
                    if "position_id" in locals()
                    else None
                ),
                symbol_id=(
                    symbol_id
                    if "symbol_id" in locals()
                    else None
                ),
                close_lots=close_lots,
                follower_units=(
                    follower_units
                    if "follower_units" in locals()
                    else None
                ),
                mt5_contract_size=mt5_contract_size,
                magic=magic,
            )

    if close_lots is not None:
        with _MASTER_LOTS_LOCK:
            MASTER_CLOSED_LOTS[
                int(ticket)
            ] = (
                master_closed_lots
                + float(close_lots)
            )

    try:
        if close_lots is None:
            with _MASTER_LOTS_LOCK:
                MASTER_OPEN_LOTS.pop(
                    int(ticket),
                    None,
                )

                MASTER_CLOSED_LOTS.pop(
                    int(ticket),
                    None,
                )

    except Exception:
        pass
