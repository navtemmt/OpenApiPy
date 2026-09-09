import time
from threading import Lock

from app_state import (
    logger, PENDING_SLTP, MASTER_OPEN_LOTS, MASTER_CLOSED_LOTS,
    alert_trade_failure, alert_trade_warning, alert_trade_info,
)
from trade_executor import (copy_open_to_account, copy_pending_to_account, transition_pending_to_market)
from symbol_mapper import SymbolMapper

from .sltp_repair import try_apply_pending_sltp

def notify_position_update(
    account_name,
    ticket,
    account_manager,
):
    try:
        client = account_manager.get_client(
            account_name
        )

        config = account_manager.get_config(
            account_name
        )

        if not client or not config:
            return

        try_apply_pending_sltp(
            account_name=account_name,
            client=client,
            config=config,
            ticket=int(ticket),
            account_manager=account_manager,
            force=True,
        )

    except Exception as e:
        logger.debug(
            f"[{account_name}] "
            f"notify_position_update failed: {e}"
        )


# ---------------------------------------------------------------------------
# Main event processor
# ---------------------------------------------------------------------------

