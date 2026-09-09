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

def _resolve_target_accounts(data, account_manager):
    magic = _to_int(
        data.get("magic", 0),
        0,
    )

    if magic <= 0:
        msg = (
            "No valid magic in event payload; "
            "cannot route event"
        )

        logger.warning(msg)

        alert_trade_warning(
            account_name="router",
            action="resolve_target_accounts_invalid_magic",
            ticket=_to_int(
                data.get("ticket", 0),
                0,
            ),
            message=msg,
            magic=magic,
        )

        return []

    matched = []
    seen = set()

    try:
        for (
            account_name,
            (_, config),
        ) in account_manager.get_all_accounts().items():

            if not config:
                continue

            route_magic = getattr(
                config,
                "route_magic_number",
                None,
            )

            magic_numbers = getattr(
                config,
                "magic_numbers",
                None,
            )

            matched_here = False

            try:
                if (
                    route_magic is not None
                    and int(route_magic)
                    == int(magic)
                ):
                    matched_here = True

            except Exception:
                pass

            if not matched_here and magic_numbers:
                try:
                    if int(magic) in {
                        int(x)
                        for x in magic_numbers
                    }:
                        matched_here = True

                except Exception:
                    pass

            if (
                matched_here
                and account_name not in seen
            ):
                matched.append(account_name)
                seen.add(account_name)

    except Exception as e:
        alert_trade_failure(
            account_name="router",
            action="resolve_target_accounts_exception",
            ticket=_to_int(
                data.get("ticket", 0),
                0,
            ),
            exc=e,
            magic=magic,
        )

        return []

    if not matched:
        msg = (
            f"No configured account routes "
            f"for magic={magic}"
        )

        logger.warning(msg)

        alert_trade_warning(
            account_name="router",
            action="resolve_target_accounts_no_match",
            ticket=_to_int(
                data.get("ticket", 0),
                0,
            ),
            message=msg,
            magic=magic,
        )

        return []

    logger.info(
        f"Resolved magic={magic} -> "
        f"target accounts: {matched}"
    )

    return matched


def _get_target_account_contexts(
    data,
    account_manager,
):
    account_names = _resolve_target_accounts(
        data,
        account_manager,
    )

    contexts = []

    for account_name in account_names:
        try:
            client = account_manager.get_client(
                account_name
            )

            config = account_manager.get_config(
                account_name
            )

        except Exception as e:
            alert_trade_failure(
                account_name=account_name,
                action="load_target_account_context",
                ticket=_to_int(
                    data.get("ticket", 0),
                    0,
                ),
                exc=e,
            )

            continue

        if not client or not config:
            msg = (
                f"Target account {account_name} "
                f"is unavailable or not initialized"
            )

            logger.warning(msg)

            alert_trade_warning(
                account_name=account_name,
                action="target_account_unavailable",
                ticket=_to_int(
                    data.get("ticket", 0),
                    0,
                ),
                message=msg,
            )

            continue

        contexts.append(
            (
                account_name,
                client,
                config,
            )
        )

    return contexts


