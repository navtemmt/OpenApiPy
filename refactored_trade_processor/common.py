import time
from threading import Lock

from app_state import (
    logger, PENDING_SLTP, MASTER_OPEN_LOTS, MASTER_CLOSED_LOTS,
    alert_trade_failure, alert_trade_warning, alert_trade_info,
)
from trade_executor import (copy_open_to_account, copy_pending_to_account, transition_pending_to_market)
from symbol_mapper import SymbolMapper

"""
Trade event processing and handling logic.
Processes incoming MT5 trade events and routes them to appropriate handlers.
Supports one MT5 magic -> multiple cTrader destination accounts.
"""

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


_PENDING_SLTP_MAX_AGE_MS = 5 * 60 * 1000
_PENDING_SLTP_BASE_RETRY_MS = 350
_PENDING_SLTP_MAX_RETRY_MS = 5000
_PENDING_SLTP_MAX_ATTEMPTS = 12

_PENDING_SLTP_LOCK = Lock()
_MASTER_LOTS_LOCK = Lock()


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

def build_account_symbol_mapper(client, config) -> SymbolMapper:
    return SymbolMapper(
        prefix=getattr(config, "symbol_prefix", ""),
        suffix=getattr(config, "symbol_suffix", ""),
        custom_map=getattr(config, "custom_symbols", {}),
        broker_symbol_map=getattr(client, "symbol_name_to_id", {}),
        strict=True,
    )


def get_symbol_id_for_account(client, config, mt5_symbol: str):
    try:
        mapper = build_account_symbol_mapper(client, config)
        return mapper.get_symbol_id(mt5_symbol)
    except Exception:
        return None


def now_ms() -> int:
    return int(time.time() * 1000)


def to_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def to_float_or_none(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default

    raw = str(value).strip().lower()

    if raw in ("1", "true", "yes", "y", "on"):
        return True

    if raw in ("0", "false", "no", "n", "off"):
        return False

    return default


# ---------------------------------------------------------------------------
# Pending SL/TP repair
# ---------------------------------------------------------------------------

def pending_sltp_bucket(account_name: str) -> dict:
    with _PENDING_SLTP_LOCK:
        return PENDING_SLTP.setdefault(str(account_name), {})


def next_pending_retry_delay_ms(attempts: int) -> int:
    attempts = max(0, int(attempts))
    delay = _PENDING_SLTP_BASE_RETRY_MS * (2 ** attempts)
    return min(delay, _PENDING_SLTP_MAX_RETRY_MS)


def set_pending_sltp(
    account_name: str,
    ticket: int,
    symbol: str,
    sl: float,
    tp: float,
):
    ticket = int(ticket)
    now_ms_value = now_ms()

    with _PENDING_SLTP_LOCK:
        bucket = PENDING_SLTP.setdefault(str(account_name), {})
        existing = bucket.get(ticket, {})

        bucket[ticket] = {
            "symbol": symbol,
            "sl": float(sl or 0.0),
            "tp": float(tp or 0.0),
            "created_ms": existing.get("created_ms", now_ms_value),
            "updated_ms": now_ms_value,
            "attempts": 0,
            "next_retry_ms": now_ms_value,
            "last_error": None,
            "last_position_id": existing.get("last_position_id"),
        }


def get_pending_sltp(account_name: str, ticket: int):
    with _PENDING_SLTP_LOCK:
        return PENDING_SLTP.setdefault(
            str(account_name), {}
        ).get(int(ticket))


def clear_pending_sltp(account_name: str, ticket: int):
    with _PENDING_SLTP_LOCK:
        PENDING_SLTP.setdefault(
            str(account_name), {}
        ).pop(int(ticket), None)


def touch_pending_sltp_retry(
    account_name: str,
    ticket: int,
    error: str = None,
    position_id=None,
):
    with _PENDING_SLTP_LOCK:
        pending = PENDING_SLTP.setdefault(
            str(account_name), {}
        ).get(int(ticket))

        if not pending:
            return

        attempts = int(pending.get("attempts", 0) or 0) + 1
        delay_ms = next_pending_retry_delay_ms(attempts - 1)

        pending["attempts"] = attempts
        pending["next_retry_ms"] = now_ms() + delay_ms
        pending["last_error"] = error
        pending["updated_ms"] = now_ms()

        if position_id:
            pending["last_position_id"] = int(position_id)


def pending_sltp_expired(pending: dict) -> bool:
    created_ms = to_int(
        pending.get("created_ms", 0),
        0,
    )

    if created_ms <= 0:
        return False

    return (now_ms() - created_ms) > _PENDING_SLTP_MAX_AGE_MS


def pending_sltp_due(pending: dict) -> bool:
    return now_ms() >= to_int(
        pending.get("next_retry_ms", 0),
        0,
    )


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------

def canonical_event_type(data: dict) -> str:
    raw = str(
        data.get("event_type")
        or data.get("action")
        or data.get("event")
        or ""
    ).strip().upper().replace("-", "_").replace(" ", "_")

    aliases = {
        "PENDING_CLOSE": "PENDING_CANCEL",
        "PENDINGCLOSE": "PENDING_CANCEL",
        "PENDING_CANCEL": "PENDING_CANCEL",
        "PENDINGCANCEL": "PENDING_CANCEL",
        "PENDING_DELETE": "PENDING_CANCEL",
        "PENDINGDELETE": "PENDING_CANCEL",
        "ORDER_DELETE": "PENDING_CANCEL",
        "ORDERDELETE": "PENDING_CANCEL",

        "PENDING_OPEN": "PENDING_OPEN",
        "PENDINGOPEN": "PENDING_OPEN",

        "PENDING_MODIFY": "PENDING_MODIFY",
        "PENDINGMODIFY": "PENDING_MODIFY",
        "PENDING_UPDATE": "PENDING_MODIFY",
        "PENDINGUPDATE": "PENDING_MODIFY",
    }

    return aliases.get(raw, raw)


def canonical_pending_type(data: dict) -> str:
    raw = str(
        data.get("pending_type")
        or data.get("order_type")
        or data.get("pending_order_type")
        or ""
    ).strip().lower().replace("-", "_").replace(" ", "_")

    aliases = {
        "limit": "limit",
        "stop": "stop",
        "stop_limit": "stop_limit",
        "stoplimit": "stop_limit",

        "buy_limit": "limit",
        "sell_limit": "limit",
        "buylimit": "limit",
        "selllimit": "limit",
        "op_buy_limit": "limit",
        "op_sell_limit": "limit",

        "buy_stop": "stop",
        "sell_stop": "stop",
        "buystop": "stop",
        "sellstop": "stop",
        "op_buy_stop": "stop",
        "op_sell_stop": "stop",

        "buy_stop_limit": "stop_limit",
        "sell_stop_limit": "stop_limit",
        "buystoplimit": "stop_limit",
        "sellstoplimit": "stop_limit",
    }

    return aliases.get(raw, raw)


# ---------------------------------------------------------------------------
# Volume / risk helpers
# ---------------------------------------------------------------------------

def lots_to_ctrader_cents(
    lots: float,
    mt5_contract_size: float,
) -> int:
    units = float(lots) * float(mt5_contract_size or 0.0)
    return int(round(units * 100.0))


def has_valid_sl(sl_value) -> bool:
    try:
        return float(sl_value or 0) > 0
    except Exception:
        return False


def risk_mode(config) -> str:
    raw = str(
        getattr(config, "risk_mode", "SOURCE_VOLUME")
        or "SOURCE_VOLUME"
    )

    raw = raw.split(";", 1)[0].split("#", 1)[0]

    return raw.strip().upper()


def risk_reference(config) -> str:
    raw = str(
        getattr(config, "risk_reference", "EQUITY")
        or "EQUITY"
    )

    raw = raw.split(";", 1)[0].split("#", 1)[0]

    return raw.strip().upper()


# ---------------------------------------------------------------------------
# Recovery configuration
# ---------------------------------------------------------------------------

def startup_market_recovery_mode(config) -> str:
    raw = str(
        getattr(
            config,
            "startup_market_recovery_mode",
            "skip",
        )
        or "skip"
    )

    raw = raw.split(";", 1)[0].split("#", 1)[0].strip().lower()

    if raw not in (
        "market",
        "market_or_pending",
        "skip",
    ):
        return "skip"

    return raw


def startup_sync_market_orders_enabled(config) -> bool:
    return bool(
        getattr(
            config,
            "startup_sync_market_orders",
            False,
        )
    )


def startup_market_max_distance_pips(config) -> float:
    try:
        v = float(
            getattr(
                config,
                "startup_market_max_distance_pips",
                10.0,
            )
            or 10.0
        )

        return v if v > 0 else 10.0

    except Exception:
        return 10.0


def startup_pending_expiration_ms(config) -> int:
    try:
        v = int(
            float(
                getattr(
                    config,
                    "startup_pending_expiration_ms",
                    0,
                )
                or 0
            )
        )

        return v if v > 0 else 0

    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Account / symbol helpers
# ---------------------------------------------------------------------------

def get_account_equity_or_balance(
    account_manager,
    account_name: str,
    config,
) -> float:
    ref = risk_reference(config)

    try:
        if (
            ref == "BALANCE"
            and hasattr(account_manager, "get_balance")
        ):
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


def get_symbol_details(client, symbol_id: int):
    try:
        return (
            client.symbol_details.get(int(symbol_id))
            if hasattr(client, "symbol_details")
            else None
        )
    except Exception:
        return None


def read_attr_or_key(obj, name, default=None):
    if obj is None:
        return default

    try:
        if isinstance(obj, dict):
            return obj.get(name, default)

        return getattr(obj, name, default)

    except Exception:
        return default


def first_positive_float(*values):
    for value in values:
        try:
            f = float(value)

            if f > 0:
                return f

        except Exception:
            pass

    return None


def symbol_pip_size(symbol) -> float:
    try:
        pip_pos = read_attr_or_key(
            symbol,
            "pipPosition",
            None,
        )

        digits = to_int(
            read_attr_or_key(
                symbol,
                "digits",
                0,
            ),
            0,
        )

        if pip_pos is not None:
            return float(
                10 ** (-int(pip_pos))
            )

        if digits > 0:
            return float(
                10 ** (-digits)
            )

    except Exception:
        pass

    return 0.0


# ---------------------------------------------------------------------------
# Risk calculation
# ---------------------------------------------------------------------------
