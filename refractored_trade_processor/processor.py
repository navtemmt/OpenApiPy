"""Trade event dispatcher facade."""
from app_state import logger, alert_trade_failure, alert_trade_warning
from .helpers import _canonical_event_type, _to_int
from .sltp_repair import drain_pending_sltp_repairs
from .handlers_open import handle_open_event
from .handlers_pending import (handle_pending_open_event, handle_pending_modify_event, handle_pending_cancel_event)
from .handlers_modify_close import handle_modify_event, handle_close_event

def process_trade_event(
    data,
    account_manager,
):
    try:
        event_type = _canonical_event_type(
            data
        )

        ticket = _to_int(
            data.get("ticket", 0),
            0,
        )

        magic = _to_int(
            data.get("magic", 0),
            0,
        )

        logger.info(
            f"Processing event: {event_type} "
            f"for ticket {ticket} "
            f"(magic: {magic})"
        )

        if event_type == "OPEN":
            handle_open_event(
                data,
                account_manager,
            )

        elif event_type == "PENDING_OPEN":
            handle_pending_open_event(
                data,
                account_manager,
            )

        elif event_type == "PENDING_MODIFY":
            handle_pending_modify_event(
                data,
                account_manager,
            )

        elif event_type == "PENDING_CANCEL":
            handle_pending_cancel_event(
                data,
                account_manager,
            )

        elif event_type == "MODIFY":
            handle_modify_event(
                data,
                account_manager,
            )

        elif event_type == "CLOSE":
            handle_close_event(
                data,
                account_manager,
            )

        else:
            msg = (
                f"Unknown event type: "
                f"{event_type}"
            )

            logger.warning(msg)

            alert_trade_warning(
                account_name="router",
                action="unknown_event_type",
                ticket=ticket,
                message=msg,
                magic=magic,
            )

        drain_pending_sltp_repairs(
            account_manager
        )

    except Exception as e:
        alert_trade_failure(
            account_name="router",
            action="process_trade_event",
            ticket=_to_int(
                data.get("ticket", 0),
                0,
            ),
            exc=e,
            event_type=(
                data.get("event_type")
                or data.get("action")
                or data.get("event")
            ),
            magic=_to_int(
                data.get("magic", 0),
                0,
            ),
            symbol=data.get("symbol"),
        )

        raise


# ---------------------------------------------------------------------------
# OPEN
# ---------------------------------------------------------------------------

