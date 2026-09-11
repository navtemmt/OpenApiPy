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
    to_bool,
    to_float,
    first_positive_float,
    read_attr_or_key,
    symbol_pip_size,
    get_symbol_id_for_account,
    get_symbol_details,
    startup_market_recovery_mode,
    startup_market_max_distance_pips,
)


# ---------------------------------------------------------------------------
# Entry / price helpers
# ---------------------------------------------------------------------------

def extract_open_entry_price(data: dict) -> float:
    for key in (
        "entry_price",
        "open_price",
        "price",
        "entry",
        "openPrice",
    ):
        v = to_float(
            data.get(key, 0),
            0.0,
        )

        if v > 0:
            return v

    return 0.0


def is_startup_market_recovery(data: dict) -> bool:
    if to_bool(
        data.get("startup_sync", False),
        False,
    ):
        return True

    if to_bool(
        data.get("startup_recovery", False),
        False,
    ):
        return True

    if to_bool(
        data.get("is_startup_sync", False),
        False,
    ):
        return True

    if to_bool(
        data.get("recovery", False),
        False,
    ):
        return True

    sync_origin = str(
        data.get("sync_origin")
        or data.get("origin")
        or data.get("source")
        or data.get("reason")
        or ""
    ).strip().lower()

    return sync_origin in (
        "startup",
        "startup_sync",
        "startup_recovery",
        "recovery",
    )


def quote_value_from_obj(obj, names):
    if obj is None:
        return None

    for name in names:
        v = read_attr_or_key(
            obj,
            name,
            None,
        )

        pv = first_positive_float(v)

        if pv is not None:
            return pv

    nested = read_attr_or_key(
        obj,
        "quote",
        None,
    )

    if nested is not None:
        for name in names:
            v = read_attr_or_key(
                nested,
                name,
                None,
            )

            pv = first_positive_float(v)

            if pv is not None:
                return pv

    return None


def get_current_market_price(
    client,
    symbol_id: int,
    side: str,
):
    side = str(
        side or ""
    ).strip().upper()

    symbol = get_symbol_details(
        client,
        symbol_id,
    )

    quote_obj = None

    try:
        if hasattr(client, "spot_quotes"):
            quote_obj = client.spot_quotes.get(
                int(symbol_id)
            )
    except Exception:
        quote_obj = None

    if quote_obj is None:
        try:
            if hasattr(client, "symbol_quotes"):
                quote_obj = client.symbol_quotes.get(
                    int(symbol_id)
                )
        except Exception:
            quote_obj = None

    ask = first_positive_float(
        quote_value_from_obj(
            quote_obj,
            (
                "ask",
                "askPrice",
                "bestAsk",
            ),
        ),
        quote_value_from_obj(
            symbol,
            (
                "ask",
                "askPrice",
                "bestAsk",
            ),
        ),
    )

    bid = first_positive_float(
        quote_value_from_obj(
            quote_obj,
            (
                "bid",
                "bidPrice",
                "bestBid",
            ),
        ),
        quote_value_from_obj(
            symbol,
            (
                "bid",
                "bidPrice",
                "bestBid",
            ),
        ),
    )

    if side == "BUY":
        return (
            ask
            if ask is not None
            else bid
        )

    if side == "SELL":
        return (
            bid
            if bid is not None
            else ask
        )

    return (
        ask
        if ask is not None
        else bid
    )


def extract_mt_current_market_price(
    data: dict,
    side: str,
):
    side = str(
        side or ""
    ).strip().upper()

    ask = first_positive_float(
        data.get("current_ask"),
        data.get("ask"),
        data.get("ask_price"),
        data.get("mt5_ask"),
        data.get("symbol_ask"),
    )

    bid = first_positive_float(
        data.get("current_bid"),
        data.get("bid"),
        data.get("bid_price"),
        data.get("mt5_bid"),
        data.get("symbol_bid"),
    )

    last = first_positive_float(
        data.get("current_price"),
        data.get("price_current"),
        data.get("market_price"),
        data.get("last_price"),
        data.get("last"),
    )

    if side == "BUY":
        return (
            ask
            if ask is not None
            else (
                last
                if last is not None
                else bid
            )
        )

    if side == "SELL":
        return (
            bid
            if bid is not None
            else (
                last
                if last is not None
                else ask
            )
        )

    return (
        last
        if last is not None
        else (
            ask
            if ask is not None
            else bid
        )
    )


def extract_mt_pip_size(data: dict) -> float:
    direct = first_positive_float(
        data.get("pip_size"),
        data.get("pipSize"),
        data.get("point"),
        data.get("Point"),
        data.get("tick_size"),
        data.get("tickSize"),
    )

    if direct is not None and direct > 0:
        return float(direct)

    pip_pos = data.get(
        "pip_position",
        data.get(
            "pipPosition",
            None,
        ),
    )

    try:
        if (
            pip_pos is not None
            and str(pip_pos) != ""
        ):
            return float(
                10 ** -int(float(pip_pos))
            )

    except Exception:
        pass

    digits = data.get(
        "digits",
        data.get(
            "mt5_digits",
            data.get(
                "symbol_digits",
                None,
            ),
        ),
    )

    try:
        if (
            digits is not None
            and str(digits) != ""
        ):
            d = int(float(digits))

            if d > 0:
                return float(
                    10 ** -d
                )

    except Exception:
        pass

    return 0.0


# ---------------------------------------------------------------------------
# Recovery planner
# ---------------------------------------------------------------------------

def build_startup_recovery_plan(
    client,
    config,
    mt5_symbol: str,
    side: str,
    entry_price: float,
    data: dict = None,
):
    """
    Build a safe recovery plan.

    The important rule is:

        close to original MT5 entry -> market

        too far from original MT5 entry:
            BUY:
                current <= entry -> BUY LIMIT
                current >  entry -> BUY STOP

            SELL:
                current >= entry -> SELL LIMIT
                current <  entry -> SELL STOP

    This prevents the copier from chasing a market that has moved too far.
    """

    data = data or {}

    mode = startup_market_recovery_mode(config)

    if mode == "skip":
        return {
            "action": "skip",
            "reason": (
                "startup_market_recovery_mode=skip"
            ),
        }

    if mode == "market":
        return {
            "action": "market",
            "reason": (
                "startup_market_recovery_mode=market"
            ),
        }

    if float(entry_price or 0.0) <= 0:
        return {
            "action": "skip",
            "reason": (
                "startup recovery missing entry_price"
            ),
        }

    symbol_id = get_symbol_id_for_account(
        client,
        config,
        mt5_symbol,
    )

    if symbol_id is None:
        return {
            "action": "skip",
            "reason": (
                "startup recovery no symbol_id"
            ),
        }

    symbol = get_symbol_details(
        client,
        int(symbol_id),
    )

    current_price = (
        extract_mt_current_market_price(
            data,
            side,
        )
    )

    current_price_source = "mt5"

    if (
        current_price is None
        or float(current_price) <= 0
    ):
        current_price = get_current_market_price(
            client,
            int(symbol_id),
            side,
        )

        current_price_source = "ctrader"

    if (
        current_price is None
        or float(current_price) <= 0
    ):
        return {
            "action": "skip",
            "reason": (
                "startup recovery current market "
                "price unavailable from MT5/cTrader"
            ),
        }

    pip_size = (
        symbol_pip_size(symbol)
        if symbol is not None
        else 0.0
    )

    if pip_size <= 0:
        pip_size = extract_mt_pip_size(data)

    if pip_size <= 0:
        return {
            "action": "skip",
            "reason": (
                "startup recovery invalid pip size"
            ),
        }

    distance_pips = (
        abs(
            float(current_price)
            - float(entry_price)
        )
        / float(pip_size)
    )

    max_distance_pips = (
        startup_market_max_distance_pips(config)
    )

    if distance_pips <= max_distance_pips:
        return {
            "action": "market",
            "reason": (
                f"startup recovery "
                f"{current_price_source} quote "
                f"current={float(current_price):.5f}, "
                f"entry={float(entry_price):.5f}, "
                f"distance_pips={distance_pips:.2f} "
                f"<= max_distance_pips="
                f"{max_distance_pips:.2f}"
            ),
        }

    side = str(
        side or ""
    ).strip().upper()

    if side == "BUY":
        if float(current_price) <= float(entry_price):
            return {
                "action": "pending",
                "pending_type": "limit",
                "limit_price": float(entry_price),
                "stop_price": 0.0,
                "reason": (
                    f"startup recovery BUY -> LIMIT "
                    f"at entry; "
                    f"{current_price_source} quote "
                    f"current={float(current_price):.5f}, "
                    f"entry={float(entry_price):.5f}, "
                    f"distance_pips="
                    f"{distance_pips:.2f}"
                ),
            }

        return {
            "action": "pending",
            "pending_type": "stop",
            "stop_price": float(entry_price),
            "limit_price": 0.0,
            "reason": (
                f"startup recovery BUY -> STOP "
                f"at entry; "
                f"{current_price_source} quote "
                f"current={float(current_price):.5f}, "
                f"entry={float(entry_price):.5f}, "
                f"distance_pips="
                f"{distance_pips:.2f}"
            ),
        }

    if side == "SELL":
        if float(current_price) >= float(entry_price):
            return {
                "action": "pending",
                "pending_type": "limit",
                "limit_price": float(entry_price),
                "stop_price": 0.0,
                "reason": (
                    f"startup recovery SELL -> LIMIT "
                    f"at entry; "
                    f"{current_price_source} quote "
                    f"current={float(current_price):.5f}, "
                    f"entry={float(entry_price):.5f}, "
                    f"distance_pips="
                    f"{distance_pips:.2f}"
                ),
            }

        return {
            "action": "pending",
            "pending_type": "stop",
            "stop_price": float(entry_price),
            "limit_price": 0.0,
            "reason": (
                f"startup recovery SELL -> STOP "
                f"at entry; "
                f"{current_price_source} quote "
                f"current={float(current_price):.5f}, "
                f"entry={float(entry_price):.5f}, "
                f"distance_pips="
                f"{distance_pips:.2f}"
            ),
        }

    return {
        "action": "skip",
        "reason": (
            f"startup recovery unsupported "
            f"side={side!r}"
        ),
    }
