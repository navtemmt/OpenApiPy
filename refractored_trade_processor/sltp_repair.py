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

def _safe_symbol_id_or_warn(
    account_name,
    client,
    config,
    ticket,
    mt5_symbol,
    action_name,
):
    symbol_id = _get_symbol_id_for_account(
        client,
        config,
        mt5_symbol,
    )

    if symbol_id is None:
        msg = (
            f"{action_name} ignored for ticket "
            f"{ticket} "
            f"(symbol mapping failed for "
            f"{mt5_symbol})"
        )

        logger.warning(
            f"[{account_name}] {msg}"
        )

        alert_trade_warning(
            account_name=account_name,
            action=(
                f"{action_name.lower()}_"
                f"symbol_mapping_failed"
            ),
            ticket=int(ticket),
            message=msg,
            mt5_symbol=mt5_symbol,
        )

        return None

    return int(symbol_id)


# ---------------------------------------------------------------------------
# Pending SL/TP application
# ---------------------------------------------------------------------------

def try_apply_pending_sltp(
    account_name,
    client,
    config,
    ticket,
    account_manager,
    force=False,
):
    pending = _get_pending_sltp(
        account_name,
        int(ticket),
    )

    if not pending:
        return False

    if _pending_sltp_expired(pending):
        msg = (
            f"Pending SL/TP expired for ticket "
            f"{ticket}, dropping repair item"
        )

        logger.warning(
            f"[{account_name}] {msg}"
        )

        alert_trade_warning(
            account_name=account_name,
            action="pending_sltp_expired",
            ticket=int(ticket),
            message=msg,
            last_error=pending.get(
                "last_error"
            ),
        )

        _clear_pending_sltp(
            account_name,
            int(ticket),
        )

        return False

    attempts = _to_int(
        pending.get("attempts", 0),
        0,
    )

    if attempts >= _PENDING_SLTP_MAX_ATTEMPTS:
        msg = (
            f"Pending SL/TP exceeded retry "
            f"limit for ticket {ticket}"
        )

        logger.error(
            f"[{account_name}] {msg}, "
            f"last_error={pending.get('last_error')}"
        )

        alert_trade_failure(
            account_name=account_name,
            action="pending_sltp_retry_limit_exceeded",
            ticket=int(ticket),
            exc=Exception(
                pending.get("last_error")
                or "retry limit exceeded"
            ),
            attempts=attempts,
            mt5_symbol=pending.get(
                "symbol"
            ),
        )

        _clear_pending_sltp(
            account_name,
            int(ticket),
        )

        return False

    if (
        not force
        and not _pending_sltp_due(pending)
    ):
        return False

    position_id = account_manager.get_position_id(
        account_name,
        int(ticket),
    )

    if not position_id:
        _touch_pending_sltp_retry(
            account_name,
            int(ticket),
            error="position_mapping_not_ready",
        )

        return False

    mt5_symbol = pending.get("symbol")

    new_sl = float(
        pending.get("sl", 0)
        or 0
    )

    new_tp = float(
        pending.get("tp", 0)
        or 0
    )

    symbol_id = _get_symbol_id_for_account(
        client,
        config,
        mt5_symbol,
    )

    logger.info(
        f"[{account_name}] Applying pending SL/TP "
        f"for ticket {ticket} -> "
        f"positionId={position_id}, "
        f"symbolId={symbol_id}, "
        f"SL={new_sl}, TP={new_tp}, "
        f"attempt={attempts + 1}"
    )

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
            f"[{account_name}] Successfully "
            f"applied pending SL/TP for "
            f"ticket {ticket}"
        )

        _clear_pending_sltp(
            account_name,
            int(ticket),
        )

        return True

    except Exception as e:
        _touch_pending_sltp_retry(
            account_name,
            int(ticket),
            error=str(e),
            position_id=position_id,
        )

        alert_trade_failure(
            account_name=account_name,
            action="apply_pending_sltp",
            ticket=int(ticket),
            exc=e,
            mt5_symbol=mt5_symbol,
            position_id=position_id,
            symbol_id=symbol_id,
            sl=new_sl,
            tp=new_tp,
            attempt=attempts + 1,
        )

        return False


def drain_pending_sltp_repairs(
    account_manager,
    account_name=None,
    force=False,
):
    repaired = 0
    scanned_accounts = []

    try:
        if account_name is not None:
            scanned_accounts = [
                str(account_name)
            ]

        else:
            with _PENDING_SLTP_LOCK:
                scanned_accounts = list(
                    PENDING_SLTP.keys()
                )

    except Exception:
        scanned_accounts = []

    for name in scanned_accounts:
        try:
            client = account_manager.get_client(
                name
            )

            config = account_manager.get_config(
                name
            )

            if not client or not config:
                continue

            with _PENDING_SLTP_LOCK:
                pending_bucket = dict(
                    PENDING_SLTP.setdefault(
                        name,
                        {},
                    )
                )

            for ticket in list(
                pending_bucket.keys()
            ):
                if try_apply_pending_sltp(
                    account_name=name,
                    client=client,
                    config=config,
                    ticket=int(ticket),
                    account_manager=account_manager,
                    force=force,
                ):
                    repaired += 1

        except Exception as e:
            alert_trade_failure(
                account_name=name,
                action="drain_pending_sltp_repairs",
                ticket=0,
                exc=e,
            )

    return repaired


