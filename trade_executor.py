"""
Trade execution logic for copying MT5 orders to cTrader accounts.
Handles volume conversion and order placement.

Pending transition rules:

LIMIT:
- If destination LIMIT is still active:
1. submit fallback market order
2. request cancellation of destination LIMIT
3. if LIMIT activates during the race, the LIMIT-originated position
is canonical and the market position is temporary.
4. if LIMIT is successfully cancelled before activation, the market
position becomes canonical.

STOP:
- NEVER convert to market.
- If the destination STOP activates, adopt the activated destination
position.
- If it is still pending, wait for activation/reconciliation.

STOP_LIMIT:
- Treat conservatively like STOP: never create a market fallback.

Labels:
MT5_PENDING_<ticket> = destination pending-originated position/order
MT5_<ticket>         = direct/fallback market position/order
"""

from config_loader import get_multi_account_config
from symbol_mapper import SymbolMapper
from app_state import logger, notify_error, notify_warning, notify_info

def _base_context(
account_name=None,
ticket=None,
mt5_symbol=None,
resolved_symbol=None,
symbol_id=None,
side=None,
volume=None,
magic=None,
pending_type=None,
adjusted_lots=None,
volume_units=None,
sl=None,
tp=None,
stop_price=None,
limit_price=None,
expiration_ms=None,
reason=None,
order_id=None,
):
ctx = {}

```
if account_name is not None:
    ctx["account_name"] = str(account_name)

if ticket is not None:
    try:
        ctx["ticket"] = int(ticket)
    except Exception:
        ctx["ticket"] = ticket

if mt5_symbol is not None:
    ctx["mt5_symbol"] = str(mt5_symbol)

if resolved_symbol is not None:
    ctx["resolved_symbol"] = str(resolved_symbol)

if symbol_id is not None:
    try:
        ctx["symbol_id"] = int(symbol_id)
    except Exception:
        ctx["symbol_id"] = symbol_id

if side is not None:
    ctx["side"] = str(side)

if volume is not None:
    ctx["volume"] = volume

if magic is not None:
    ctx["magic"] = magic

if pending_type is not None:
    ctx["pending_type"] = str(pending_type)

if adjusted_lots is not None:
    ctx["adjusted_lots"] = float(adjusted_lots)

if volume_units is not None:
    try:
        ctx["volume_units"] = int(volume_units)
    except Exception:
        ctx["volume_units"] = volume_units

if sl is not None:
    ctx["sl"] = sl

if tp is not None:
    ctx["tp"] = tp

if stop_price is not None:
    ctx["stop_price"] = stop_price

if limit_price is not None:
    ctx["limit_price"] = limit_price

if expiration_ms is not None:
    try:
        ctx["expiration_ms"] = int(expiration_ms)
    except Exception:
        ctx["expiration_ms"] = expiration_ms

if reason is not None:
    ctx["reason"] = str(reason)

if order_id is not None:
    try:
        ctx["order_id"] = int(order_id)
    except Exception:
        ctx["order_id"] = order_id

return ctx
```

def _to_float(value, default=0.0) -> float:
try:
return float(value)
except Exception:
return float(default)

def _to_int(value, default=0) -> int:
try:
return int(float(value))
except Exception:
return int(default)

def _normalize_trade_side(side: str) -> str:
side_norm = str(side or "").strip().upper()

```
if side_norm in ("BUY", "LONG"):
    return "BUY"

if side_norm in ("SELL", "SHORT"):
    return "SELL"

raise ValueError(f"Unsupported trade side: {side}")
```

def *normalize_pending_type(pending_type: str) -> str:
ptype = (
str(pending_type or "")
.strip()
.lower()
.replace("-", "*")
.replace(" ", "_")
)

```
aliases = {
    "limit": "limit",
    "stop": "stop",
    "stop_limit": "stop_limit",
    "stoplimit": "stop_limit",

    "buy_limit": "limit",
    "sell_limit": "limit",
    "buylimit": "limit",
    "selllimit": "limit",

    "buy_stop": "stop",
    "sell_stop": "stop",
    "buystop": "stop",
    "sellstop": "stop",

    "buy_stop_limit": "stop_limit",
    "sell_stop_limit": "stop_limit",
    "buystoplimit": "stop_limit",
    "sellstoplimit": "stop_limit",
}

normalized = aliases.get(ptype, ptype)

if normalized not in ("limit", "stop", "stop_limit"):
    raise ValueError(f"Unsupported pending type: {pending_type}")

return normalized
```

def _clamp_lots(config, lots: float) -> float:
raw_lots = float(lots or 0.0)

```
min_lot = float(
    getattr(config, "min_lot_size", 0.01) or 0.01
)

max_lot = float(
    getattr(config, "max_lot_size", 100.0) or 100.0
)

if max_lot < min_lot:
    max_lot = min_lot

return max(min_lot, min(raw_lots, max_lot))
```

def _snap_volume_units(
volume_units: int,
min_units: int,
max_units: int,
step_units: int,
) -> int:
"""
Clamp and snap cTrader Open API volume (UNITS) to broker constraints.
"""
v = int(volume_units or 0)

```
if min_units and int(min_units) > 0:
    v = max(v, int(min_units))

if max_units and int(max_units) > 0:
    v = min(v, int(max_units))

if step_units and int(step_units) > 0:
    base = (
        int(min_units)
        if min_units and int(min_units) > 0
        else 0
    )

    steps = round(
        (v - base) / float(step_units)
    )

    v = base + int(steps) * int(step_units)

if min_units and int(min_units) > 0:
    v = max(v, int(min_units))

if max_units and int(max_units) > 0:
    v = min(v, int(max_units))

return int(v)
```

def _map_symbol_id(client, config, mt5_symbol: str):
mapper = SymbolMapper(
prefix=getattr(config, "symbol_prefix", ""),
suffix=getattr(config, "symbol_suffix", ""),
custom_map=getattr(config, "custom_symbols", {}),
broker_symbol_map=getattr(
client,
"symbol_name_to_id",
{},
) or {},
strict=True,
)

```
return mapper.get_symbol_id(mt5_symbol)
```

def _resolve_ctrader_symbol_name(
client,
symbol_id: int,
fallback: str = "",
) -> str:
"""
Best-effort reverse lookup: symbolId -> cTrader symbol name.
"""
try:
symbol = (
client.symbol_details.get(int(symbol_id))
if hasattr(client, "symbol_details")
else None
)

```
    name = (
        getattr(symbol, "symbolName", None)
        if symbol is not None
        else None
    )

    if name:
        return str(name)

except Exception:
    pass

try:
    broker_symbol_map = (
        getattr(client, "symbol_name_to_id", {})
        or {}
    )

    for name, sid in broker_symbol_map.items():
        if int(sid) == int(symbol_id):
            return str(name)

except Exception:
    pass

return str(fallback or f"symbolId={symbol_id}")
```

def _get_symbol_details(client, symbol_id: int):
try:
return (
client.symbol_details.get(int(symbol_id))
if hasattr(client, "symbol_details")
else None
)
except Exception:
return None

def _round_price_or_none(client, symbol_id: int, price):
price_f = _to_float(price, 0.0)

```
if price_f <= 0:
    return None

return client.round_price_for_symbol(
    int(symbol_id),
    float(price_f),
)
```

def _normalize_expiration_ms(expiration_ms) -> int:
value = _to_int(expiration_ms, 0)
return value if value > 0 else 0

def _extract_pending_order_id(response) -> int:
try:
if response is None:
return 0

```
    direct = getattr(response, "orderId", None)

    if direct is not None and int(direct) > 0:
        return int(direct)

    order = getattr(response, "order", None)

    if order is not None:
        nested = getattr(order, "orderId", None)

        if nested is not None and int(nested) > 0:
            return int(nested)

    extracted = None

    try:
        from ctrader_open_api import Protobuf

        extracted = Protobuf.extract(response)

    except Exception:
        extracted = None

    if extracted is not None:
        direct = getattr(
            extracted,
            "orderId",
            None,
        )

        if direct is not None and int(direct) > 0:
            return int(direct)

        order = getattr(
            extracted,
            "order",
            None,
        )

        if order is not None:
            nested = getattr(
                order,
                "orderId",
                None,
            )

            if nested is not None and int(nested) > 0:
                return int(nested)

except Exception:
    pass

return 0
```

def _get_account_manager():
try:
from account_manager import get_account_manager

```
    return get_account_manager()

except Exception:
    logger.debug(
        "Failed to obtain account manager",
        exc_info=True,
    )
    return None
```

def _store_pending_mapping_immediately(
account_name,
ticket,
order_id,
):
try:
if int(order_id or 0) <= 0:
return

```
    manager = _get_account_manager()

    if manager is None:
        return

    if hasattr(manager, "_store_order_mapping"):
        manager._store_order_mapping(
            account_name,
            int(ticket),
            int(order_id),
        )

except Exception:
    logger.debug(
        "[%s] Failed to immediately store pending mapping "
        "for ticket %s",
        account_name,
        ticket,
        exc_info=True,
    )
```

def _store_pending_type(
account_name,
ticket,
pending_type,
):
"""
Store pending type when the destination pending order is created.

```
The new AccountManager can use this to distinguish LIMIT-originated
positions from STOP-originated positions during an asynchronous race.

This remains optional so older AccountManager versions do not break.
"""
try:
    manager = _get_account_manager()

    if manager is None:
        return

    ptype = _normalize_pending_type(pending_type)

    if hasattr(manager, "set_pending_type"):
        manager.set_pending_type(
            account_name,
            int(ticket),
            ptype,
        )
        return

    if hasattr(manager, "_set_pending_type"):
        manager._set_pending_type(
            account_name,
            int(ticket),
            ptype,
        )
        return

    logger.debug(
        "[%s] AccountManager has no pending-type state method; "
        "pending type remains available from caller state | "
        "ticket=%s type=%s",
        account_name,
        ticket,
        ptype,
    )

except Exception:
    logger.debug(
        "[%s] Failed to store pending type | ticket=%s type=%s",
        account_name,
        ticket,
        pending_type,
        exc_info=True,
    )
```

def _clear_stale_position_mapping(
account_name,
ticket,
):
"""
Clear only position mapping before creating a pending order.

```
Do NOT clear an existing order mapping here. The existing order mapping
may represent a still-active cTrader pending order during startup replay.
"""
try:
    manager = _get_account_manager()

    if manager is None:
        return

    if hasattr(manager, "_remove_position_mapping"):
        manager._remove_position_mapping(
            account_name,
            int(ticket),
        )

except Exception:
    logger.debug(
        "[%s] Failed to clear stale position mapping "
        "for ticket %s",
        account_name,
        ticket,
        exc_info=True,
    )
```

def _get_existing_order_mapping(
account_name,
ticket,
) -> int:
try:
manager = _get_account_manager()

```
    if manager is None:
        return 0

    if hasattr(manager, "get_order_id"):
        return int(
            manager.get_order_id(
                account_name,
                int(ticket),
            ) or 0
        )

    if hasattr(manager, "getorderid"):
        return int(
            manager.getorderid(
                account_name,
                int(ticket),
            ) or 0
        )

    if hasattr(manager, "get_orderid"):
        return int(
            manager.get_orderid(
                account_name,
                int(ticket),
            ) or 0
        )

except Exception:
    logger.debug(
        "[%s] Failed to read existing order mapping "
        "for ticket %s",
        account_name,
        ticket,
        exc_info=True,
    )

return 0
```

def _get_existing_position_mapping(
account_name,
ticket,
) -> int:
try:
manager = _get_account_manager()

```
    if manager is None:
        return 0

    if hasattr(manager, "get_position_id"):
        return int(
            manager.get_position_id(
                account_name,
                int(ticket),
            ) or 0
        )

    if hasattr(manager, "getpositionid"):
        return int(
            manager.getpositionid(
                account_name,
                int(ticket),
            ) or 0
        )

except Exception:
    logger.debug(
        "[%s] Failed to read existing position mapping "
        "for ticket %s",
        account_name,
        ticket,
        exc_info=True,
    )

return 0
```

def _has_active_pending_order(
client,
order_id: int,
) -> bool:
"""
Best-effort check whether a cTrader pending order is still active.

```
IMPORTANT:
False here means "not observed as active", not necessarily that the
order was definitely cancelled. The execution/reconciliation handlers
remain authoritative for the final state.
"""
try:
    if int(order_id or 0) <= 0:
        return False

    containers = []

    for attr in (
        "pending_orders",
        "pendingOrders",
        "orders",
        "open_orders",
        "openOrders",
    ):
        try:
            obj = getattr(
                client,
                attr,
                None,
            )

            if obj is not None:
                containers.append(obj)

        except Exception:
            pass

    for container in containers:
        try:
            if isinstance(container, dict):

                if int(order_id) in [
                    int(k)
                    for k in container.keys()
                ]:
                    return True

                for val in container.values():
                    oid = getattr(
                        val,
                        "orderId",
                        None,
                    )

                    if (
                        oid is not None
                        and int(oid) == int(order_id)
                    ):
                        return True

            else:
                values = (
                    container.values()
                    if hasattr(container, "values")
                    else container
                )

                for val in values:
                    oid = getattr(
                        val,
                        "orderId",
                        None,
                    )

                    if (
                        oid is not None
                        and int(oid) == int(order_id)
                    ):
                        return True

        except Exception:
            continue

except Exception:
    logger.debug(
        "Failed active pending-order check for orderId=%s",
        order_id,
        exc_info=True,
    )

return False
```

def _should_skip_duplicate_pending(
account_name,
client,
ticket,
) -> int:
"""
Return existing orderId if this MT5 ticket is already mapped to a
live/known pending order.
"""
order_id = _get_existing_order_mapping(
account_name,
ticket,
)

```
if order_id <= 0:
    return 0

if _has_active_pending_order(
    client,
    order_id,
):
    return int(order_id)

return 0
```

def _should_copy(
account_name,
config,
mt5_symbol,
magic,
volume,
):
multi_config = get_multi_account_config()

```
should_copy, reason = multi_config.should_copy_trade(
    config,
    mt5_symbol,
    magic,
    volume,
)

if not should_copy:
    logger.info(
        f"[{account_name}] Skipping copy | "
        f"symbol={mt5_symbol} magic={magic} "
        f"volume={volume} reason={reason}"
    )

    notify_info(
        event="trade_copy_skipped",
        message=f"Trade copy skipped: {reason}",
        account_name=account_name,
        mt5_symbol=mt5_symbol,
        magic=magic,
        volume=volume,
        reason=reason,
    )

    return False

return True
```

def _calc_volume_units(
account_name,
client,
config,
symbol_id: int,
mt5_symbol: str,
mt5_lots: float,
) -> int:
"""
Convert MT5 lots -> cTrader volume UNITS using cTrader symbol lotSize,
then snap to broker constraints.
"""
symbol = _get_symbol_details(
client,
symbol_id,
)

```
resolved_symbol = _resolve_ctrader_symbol_name(
    client,
    symbol_id,
    fallback=mt5_symbol,
)

if symbol is None:
    msg = (
        f"Missing cTrader symbol_details for "
        f"mt5_symbol={mt5_symbol} "
        f"resolved_symbol={resolved_symbol} "
        f"symbolId={symbol_id}. "
        f"Wait for symbols to load before trading."
    )

    logger.error(
        f"[{account_name}] {msg}"
    )

    notify_warning(
        event="volume_conversion_missing_symbol_details",
        message=msg,
        **_base_context(
            account_name=account_name,
            mt5_symbol=mt5_symbol,
            resolved_symbol=resolved_symbol,
            symbol_id=symbol_id,
        ),
    )

    return 0

lot_size = int(
    getattr(symbol, "lotSize", 0) or 0
)

min_units = int(
    getattr(symbol, "minVolume", 0) or 0
)

max_units = int(
    getattr(symbol, "maxVolume", 0) or 0
)

step_units = int(
    getattr(symbol, "stepVolume", 0) or 0
)

if (
    lot_size <= 0
    or min_units <= 0
    or step_units <= 0
):
    msg = (
        f"Invalid cTrader symbol specs for "
        f"mt5_symbol={mt5_symbol} "
        f"resolved_symbol={resolved_symbol} "
        f"symbolId={symbol_id}: "
        f"lotSize={lot_size}, "
        f"minVolume={min_units}, "
        f"stepVolume={step_units}, "
        f"maxVolume={max_units}"
    )

    logger.error(
        f"[{account_name}] {msg}"
    )

    notify_warning(
        event="volume_conversion_invalid_symbol_specs",
        message=msg,
        **_base_context(
            account_name=account_name,
            mt5_symbol=mt5_symbol,
            resolved_symbol=resolved_symbol,
            symbol_id=symbol_id,
        ),
    )

    return 0

raw_units = int(
    round(
        float(mt5_lots)
        * float(lot_size)
    )
)

snapped = _snap_volume_units(
    raw_units,
    min_units,
    max_units,
    step_units,
)

logger.info(
    f"[{account_name}] Volume conversion "
    f"(cTrader specs): "
    f"symbol={resolved_symbol} "
    f"symbolId={symbol_id}, "
    f"mt5_symbol={mt5_symbol}, "
    f"mt5_lots={mt5_lots:.4f}, "
    f"lotSize={lot_size}, "
    f"min={min_units}, "
    f"max={max_units}, "
    f"step={step_units} "
    f"-> raw_units={raw_units}, "
    f"units={snapped}"
)

return int(snapped)
```

def transition_pending_to_market(
account_name,
client,
config,
ticket,
mt5_symbol,
side,
volume,
sl,
tp,
magic,
account_manager,
pending_type=None,
):
"""
Handle an MT5 pending-order trigger.

```
LIMIT:
    If the destination LIMIT has already activated, adopt that
    MT5_PENDING_<ticket> position.

    If it is still pending, create MT5_<ticket> market fallback and
    request cancellation of the pending order.

STOP:
    Never create a market fallback. The destination STOP itself must
    activate. If it is already live, adopt it.

STOP_LIMIT:
    Conservatively treated like STOP. Never create a market fallback.

IMPORTANT:
    cTrader is asynchronous. A LIMIT can activate between the state
    check and the cancellation request. That race is expected.

    In that race:
        MT5_PENDING_<ticket> = canonical position
        MT5_<ticket>         = temporary fallback position

    The AccountManager must preserve that canonical-origin priority.
"""

ticket = int(ticket)

try:
    ptype = (
        _normalize_pending_type(pending_type)
        if pending_type
        else None
    )
except Exception:
    ptype = None

pending_label = f"MT5_PENDING_{ticket}"
canonical_label = f"MT5_{ticket}"

logger.info(
    "[%s] Pending transition requested | "
    "ticket=%s pending_type=%s pending_label=%s canonical_label=%s",
    account_name,
    ticket,
    ptype or "unknown",
    pending_label,
    canonical_label,
)

# ------------------------------------------------------------
# STEP 1:
# Check whether the destination pending-originated position
# has already activated.
#
# This check MUST happen BEFORE cancellation.
# ------------------------------------------------------------
existing_position_id = _get_existing_position_mapping(
    account_name,
    ticket,
)

if existing_position_id > 0:
    logger.info(
        "[%s] Pending transition: destination already has "
        "live positionId=%s for ticket=%s. "
        "Adopting existing position; no market order and "
        "no pending cancellation.",
        account_name,
        existing_position_id,
        ticket,
    )

    try:
        client.amend_position(
            account_id=config.account_id,
            position_id=int(existing_position_id),
            sl=(
                sl
                if float(sl or 0) > 0
                else None
            ),
            tp=(
                tp
                if float(tp or 0) > 0
                else None
            ),
        )

        logger.info(
            "[%s] Pending transition: "
            "SL/TP applied to adopted positionId=%s",
            account_name,
            existing_position_id,
        )

    except Exception as exc:
        logger.warning(
            "[%s] Pending transition: "
            "SL/TP amend failed for adopted "
            "positionId=%s: %s",
            account_name,
            existing_position_id,
            exc,
        )

    return {
        "action": "adopt",
        "position_id": int(existing_position_id),
        "canonical": True,
        "pending_type": ptype,
    }

# ------------------------------------------------------------
# STEP 2:
# Obtain the currently mapped destination pending order.
# ------------------------------------------------------------
existing_order_id = _get_existing_order_mapping(
    account_name,
    ticket,
)

if existing_order_id <= 0:
    logger.warning(
        "[%s] Pending transition: no destination "
        "pending order mapping found for ticket=%s.",
        account_name,
        ticket,
    )

    # STOP/STOP_LIMIT must NEVER fall back to market.
    if ptype in ("stop", "stop_limit"):
        logger.info(
            "[%s] Pending transition: %s ticket=%s has "
            "no pending mapping but market fallback is "
            "forbidden. Waiting for reconciliation.",
            account_name,
            ptype.upper(),
            ticket,
        )

        return {
            "action": "wait",
            "pending_type": ptype,
        }

    # LIMIT with no order mapping is ambiguous. Do not blindly
    # create a second order if the AccountManager already has a
    # live position. Re-check one last time.
    existing_position_id = _get_existing_position_mapping(
        account_name,
        ticket,
    )

    if existing_position_id > 0:
        return {
            "action": "adopt",
            "position_id": int(existing_position_id),
            "canonical": True,
            "pending_type": ptype,
        }

    logger.info(
        "[%s] Pending transition: LIMIT ticket=%s has "
        "no pending order and no live position; "
        "sending canonical market order.",
        account_name,
        ticket,
    )

    response = copy_open_to_account(
        account_name=account_name,
        client=client,
        config=config,
        ticket=ticket,
        mt5_symbol=mt5_symbol,
        side=side,
        volume=float(volume),
        sl=sl,
        tp=tp,
        magic=magic,
    )

    return {
        "action": "market",
        "canonical": True,
        "pending_type": ptype,
        "response": response,
    }

# ------------------------------------------------------------
# STEP 3:
# Check whether the pending order is STILL active.
# ------------------------------------------------------------
pending_is_active = _has_active_pending_order(
    client,
    existing_order_id,
)

if not pending_is_active:
    # The order is no longer visible as active.
    #
    # It may have:
    #   - activated
    #   - been cancelled
    #   - disappeared from the local cache
    #
    # Check position mapping again before doing anything.
    existing_position_id = _get_existing_position_mapping(
        account_name,
        ticket,
    )

    if existing_position_id > 0:
        logger.info(
            "[%s] Pending transition: orderId=%s no longer "
            "appears active, but positionId=%s exists. "
            "Adopting position.",
            account_name,
            existing_order_id,
            existing_position_id,
        )

        try:
            client.amend_position(
                account_id=config.account_id,
                position_id=int(existing_position_id),
                sl=(
                    sl
                    if float(sl or 0) > 0
                    else None
                ),
                tp=(
                    tp
                    if float(tp or 0) > 0
                    else None
                ),
            )
        except Exception as exc:
            logger.warning(
                "[%s] Failed applying SL/TP to adopted "
                "positionId=%s: %s",
                account_name,
                existing_position_id,
                exc,
            )

        return {
            "action": "adopt",
            "position_id": int(existing_position_id),
            "canonical": True,
            "pending_type": ptype,
        }

    if ptype in ("stop", "stop_limit"):
        logger.info(
            "[%s] Pending transition: %s orderId=%s "
            "not currently visible and no position exists. "
            "NO market fallback will be created.",
            account_name,
            ptype.upper(),
            existing_order_id,
        )

        return {
            "action": "wait",
            "pending_type": ptype,
            "order_id": existing_order_id,
        }

    # LIMIT:
    # If it disappeared and no position exists, it is most likely
    # already cancelled. Market order can therefore become canonical.
    logger.info(
        "[%s] Pending transition: LIMIT orderId=%s is no "
        "longer active and no destination position exists. "
        "Creating canonical market copy.",
        account_name,
        existing_order_id,
    )

    response = copy_open_to_account(
        account_name=account_name,
        client=client,
        config=config,
        ticket=ticket,
        mt5_symbol=mt5_symbol,
        side=side,
        volume=float(volume),
        sl=sl,
        tp=tp,
        magic=magic,
    )

    return {
        "action": "market",
        "canonical": True,
        "pending_type": ptype,
        "response": response,
    }

# ------------------------------------------------------------
# STEP 4:
# Pending is confirmed active.
#
# STOP:
#     Never send market.
#
# LIMIT:
#     Send market fallback, then cancel pending.
# ------------------------------------------------------------
if ptype in ("stop", "stop_limit"):
    logger.info(
        "[%s] Pending transition: %s orderId=%s is still "
        "active for ticket=%s. Waiting for destination "
        "pending order to activate. MARKET FALLBACK FORBIDDEN.",
        account_name,
        ptype.upper(),
        existing_order_id,
        ticket,
    )

    return {
        "action": "wait",
        "pending_type": ptype,
        "order_id": existing_order_id,
    }

# ------------------------------------------------------------
# LIMIT ONLY
#
# The pending LIMIT is definitely still active at this point.
# Submit the fallback market order FIRST.
#
# Why first?
# If we cancel first, MT5's OPEN event can leave the source
# without a corresponding destination position while cTrader's
# cancellation is asynchronous.
#
# The expected race is:
#
#   LIMIT still active
#        |
#        +--> market MT5_<ticket>
#        |
#        +--> cancel LIMIT
#
# If LIMIT activates during this operation:
#
#   MT5_PENDING_<ticket> = canonical
#   MT5_<ticket>         = temporary fallback
# ------------------------------------------------------------
logger.info(
    "[%s] LIMIT transition: destination pending orderId=%s "
    "is still active. Creating market fallback before "
    "requesting cancellation. ticket=%s",
    account_name,
    existing_order_id,
    ticket,
)

market_response = None

try:
    market_response = copy_open_to_account(
        account_name=account_name,
        client=client,
        config=config,
        ticket=ticket,
        mt5_symbol=mt5_symbol,
        side=side,
        volume=float(volume),
        sl=sl,
        tp=tp,
        magic=magic,
    )

    logger.info(
        "[%s] LIMIT transition: fallback market submitted "
        "for ticket=%s label=%s. Pending orderId=%s will "
        "now be cancelled.",
        account_name,
        ticket,
        canonical_label,
        existing_order_id,
    )

except Exception as exc:
    logger.error(
        "[%s] LIMIT transition: fallback market FAILED "
        "for ticket=%s. Pending orderId=%s will NOT be "
        "cancelled because there is no fallback position.",
        account_name,
        ticket,
        existing_order_id,
    )

    notify_error(
        event="limit_transition_market_failed",
        message=str(exc),
        exc=exc,
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            side=side,
            volume=volume,
            magic=magic,
            pending_type="limit",
            order_id=existing_order_id,
            reason="market_fallback_failed_pending_preserved",
        ),
    )

    raise

# ------------------------------------------------------------
# STEP 5:
# Request cancellation AFTER fallback market submission.
#
# ORDER_NOT_FOUND is NOT treated as successful cancellation.
# It usually means the pending activated during the race.
# The AccountManager/reconciliation must determine the actual
# broker state.
# ------------------------------------------------------------
cancel_result = None

try:
    cancel_result = client.cancel_pending_order(
        account_id=config.account_id,
        order_id=int(existing_order_id),
    )

    logger.info(
        "[%s] LIMIT transition: cancel requested for "
        "pending orderId=%s ticket=%s after fallback market "
        "submission.",
        account_name,
        existing_order_id,
        ticket,
    )

except Exception as exc:
    logger.warning(
        "[%s] LIMIT transition: cancellation request failed "
        "for pending orderId=%s ticket=%s: %s. "
        "Fallback market remains submitted; reconciliation "
        "must determine whether LIMIT activated.",
        account_name,
        existing_order_id,
        ticket,
        exc,
    )

    notify_warning(
        event="limit_transition_cancel_failed",
        message=str(exc),
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            side=side,
            volume=volume,
            magic=magic,
            pending_type="limit",
            order_id=existing_order_id,
            reason="cancel_failed_after_market_fallback",
        ),
    )

# ------------------------------------------------------------
# IMPORTANT:
#
# Do NOT immediately remove the pending mapping here.
#
# The cancellation response is asynchronous and may be:
#
#     ORDER_CANCELLED
#
# OR:
#
#     ORDER_NOT_FOUND
#
# because the LIMIT activated milliseconds before cancellation.
#
# AccountManager execution/reconciliation must resolve that.
# ------------------------------------------------------------
return {
    "action": "market_fallback",
    "canonical": False,
    "pending_type": "limit",
    "pending_order_id": int(existing_order_id),
    "market_label": canonical_label,
    "market_response": market_response,
    "cancel_result": cancel_result,
}
```

def copy_open_to_account(
account_name,
client,
config,
ticket,
mt5_symbol,
side,
volume,
sl,
tp,
magic,
):
"""Execute a new market order on cTrader for a given account."""

```
try:
    symbol_id = _map_symbol_id(
        client,
        config,
        mt5_symbol,
    )

except Exception as e:
    notify_error(
        event="map_symbol_market_open",
        message=str(e),
        exc=e,
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            side=side,
            volume=volume,
            magic=magic,
        ),
    )

    raise

if symbol_id is None:
    msg = (
        f"Could not map MT5 symbol to cTrader symbolId | "
        f"ticket={ticket} mt5_symbol={mt5_symbol}"
    )

    logger.error(
        f"[{account_name}] {msg}"
    )

    notify_warning(
        event="map_symbol_market_open_none",
        message=msg,
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            side=side,
            volume=volume,
            magic=magic,
        ),
    )

    return

resolved_symbol = _resolve_ctrader_symbol_name(
    client,
    symbol_id,
    fallback=mt5_symbol,
)

if not _should_copy(
    account_name,
    config,
    mt5_symbol,
    magic,
    volume,
):
    return

try:
    trade_side = _normalize_trade_side(side)

    multiplier = float(
        getattr(
            config,
            "lot_multiplier",
            1.0,
        ) or 1.0
    )

    adjusted_lots = _clamp_lots(
        config,
        multiplier * float(volume),
    )

except Exception as e:
    notify_error(
        event="prepare_market_open",
        message=str(e),
        exc=e,
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            resolved_symbol=resolved_symbol,
            symbol_id=symbol_id,
            side=side,
            volume=volume,
            magic=magic,
        ),
    )

    raise

volume_to_send = _calc_volume_units(
    account_name=account_name,
    client=client,
    config=config,
    symbol_id=symbol_id,
    mt5_symbol=mt5_symbol,
    mt5_lots=float(adjusted_lots),
)

if volume_to_send <= 0:
    msg = (
        f"Skipping zero or negative volume | "
        f"ticket={ticket} symbol={resolved_symbol} "
        f"symbolId={symbol_id}"
    )

    logger.warning(
        f"[{account_name}] {msg}"
    )

    notify_warning(
        event="market_open_zero_volume",
        message=msg,
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            resolved_symbol=resolved_symbol,
            symbol_id=symbol_id,
            adjusted_lots=adjusted_lots,
        ),
    )

    return

logger.info(
    f"[{account_name}] Opening {trade_side} | "
    f"ticket={ticket} "
    f"symbol={resolved_symbol} "
    f"symbolId={symbol_id} "
    f"mt5_symbol={mt5_symbol} | "
    f"Volume={volume_to_send} units "
    f"({adjusted_lots:.4f} lots before cTrader snap) | "
    f"SL={sl} TP={tp} | "
    f"Label=MT5_{ticket}"
)

try:
    response = client.send_market_order(
        account_id=config.account_id,
        symbol_id=symbol_id,
        side=trade_side,
        volume=volume_to_send,
        sl=None,
        tp=None,
        label=f"MT5_{ticket}",
    )

    logger.info(
        f"[{account_name}] Order submitted | "
        f"ticket={ticket} "
        f"symbol={resolved_symbol} "
        f"symbolId={symbol_id} "
        f"label=MT5_{ticket}"
    )

    notify_info(
        event="market_order_submitted",
        message="Market order submitted to cTrader",
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            resolved_symbol=resolved_symbol,
            symbol_id=symbol_id,
            side=trade_side,
            volume_units=volume_to_send,
            adjusted_lots=adjusted_lots,
            sl=sl,
            tp=tp,
            magic=magic,
        ),
    )

    return response

except Exception as e:
    notify_error(
        event="send_market_order",
        message=str(e),
        exc=e,
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            resolved_symbol=resolved_symbol,
            symbol_id=symbol_id,
            side=trade_side,
            volume_units=volume_to_send,
            adjusted_lots=adjusted_lots,
            sl=sl,
            tp=tp,
            magic=magic,
        ),
    )

    raise
```

def copy_pending_to_account(
account_name,
client,
config,
ticket,
mt5_symbol,
side,
volume,
sl,
tp,
magic,
pending_type: str,
stop_price: float = 0.0,
limit_price: float = 0.0,
expiration_ms: int = 0,
):
"""
Create a pending order on cTrader.

```
pending_type:
    limit
    stop
    stop_limit
"""

try:
    symbol_id = _map_symbol_id(
        client,
        config,
        mt5_symbol,
    )

except Exception as e:
    notify_error(
        event="map_symbol_pending_open",
        message=str(e),
        exc=e,
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            side=side,
            volume=volume,
            magic=magic,
            pending_type=pending_type,
        ),
    )

    raise

if symbol_id is None:
    msg = (
        f"Could not map MT5 symbol to cTrader symbolId | "
        f"ticket={ticket} mt5_symbol={mt5_symbol}"
    )

    logger.error(
        f"[{account_name}] {msg}"
    )

    notify_warning(
        event="map_symbol_pending_open_none",
        message=msg,
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            side=side,
            volume=volume,
            magic=magic,
            pending_type=pending_type,
        ),
    )

    return

resolved_symbol = _resolve_ctrader_symbol_name(
    client,
    symbol_id,
    fallback=mt5_symbol,
)

if not _should_copy(
    account_name,
    config,
    mt5_symbol,
    magic,
    volume,
):
    return

existing_order_id = _should_skip_duplicate_pending(
    account_name,
    client,
    ticket,
)

if existing_order_id > 0:
    logger.info(
        f"[{account_name}] PENDING_OPEN skip for ticket "
        f"{ticket} (already mapped to active "
        f"orderId={existing_order_id})"
    )

    notify_info(
        event="pending_order_skipped_existing_mapping",
        message=(
            "Pending order skipped because active cTrader "
            "order mapping already exists"
        ),
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            resolved_symbol=resolved_symbol,
            symbol_id=symbol_id,
            side=side,
            volume=volume,
            magic=magic,
            pending_type=pending_type,
            order_id=existing_order_id,
            reason="existing_active_pending_mapping",
        ),
    )

    return None

try:
    trade_side = _normalize_trade_side(side)
    ptype = _normalize_pending_type(pending_type)

    multiplier = float(
        getattr(
            config,
            "lot_multiplier",
            1.0,
        ) or 1.0
    )

    adjusted_lots = _clamp_lots(
        config,
        multiplier * float(volume),
    )

except Exception as e:
    notify_error(
        event="prepare_pending_open",
        message=str(e),
        exc=e,
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            resolved_symbol=resolved_symbol,
            symbol_id=symbol_id,
            side=side,
            volume=volume,
            magic=magic,
            pending_type=pending_type,
        ),
    )

    raise

volume_to_send = _calc_volume_units(
    account_name=account_name,
    client=client,
    config=config,
    symbol_id=symbol_id,
    mt5_symbol=mt5_symbol,
    mt5_lots=float(adjusted_lots),
)

if volume_to_send <= 0:
    msg = (
        f"Skipping zero or negative pending volume | "
        f"ticket={ticket} symbol={resolved_symbol} "
        f"symbolId={symbol_id}"
    )

    logger.warning(
        f"[{account_name}] {msg}"
    )

    notify_warning(
        event="pending_open_zero_volume",
        message=msg,
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            resolved_symbol=resolved_symbol,
            symbol_id=symbol_id,
            adjusted_lots=adjusted_lots,
            pending_type=ptype,
        ),
    )

    return

try:
    sl_r = _round_price_or_none(
        client,
        symbol_id,
        sl,
    )

    tp_r = _round_price_or_none(
        client,
        symbol_id,
        tp,
    )

    stop_r = _round_price_or_none(
        client,
        symbol_id,
        stop_price,
    )

    limit_r = _round_price_or_none(
        client,
        symbol_id,
        limit_price,
    )

    expiration_ms_n = _normalize_expiration_ms(
        expiration_ms
    )

    if ptype == "limit" and limit_r is None:
        raise ValueError(
            "Pending LIMIT order requires "
            "limit_price > 0"
        )

    if ptype == "stop" and stop_r is None:
        raise ValueError(
            "Pending STOP order requires "
            "stop_price > 0"
        )

    if (
        ptype == "stop_limit"
        and (
            stop_r is None
            or limit_r is None
        )
    ):
        raise ValueError(
            "Pending STOP_LIMIT order requires "
            "both stop_price > 0 and limit_price > 0"
        )

except Exception as e:
    notify_error(
        event="round_pending_prices",
        message=str(e),
        exc=e,
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            resolved_symbol=resolved_symbol,
            symbol_id=symbol_id,
            side=trade_side,
            pending_type=(
                ptype
                if "ptype" in locals()
                else pending_type
            ),
            sl=sl,
            tp=tp,
            stop_price=stop_price,
            limit_price=limit_price,
            expiration_ms=expiration_ms,
        ),
    )

    raise

pending_label = f"MT5_PENDING_{ticket}"

logger.info(
    f"[{account_name}] Creating pending "
    f"{ptype.upper()} {trade_side} | "
    f"ticket={ticket} "
    f"symbol={resolved_symbol} "
    f"symbolId={symbol_id} "
    f"mt5_symbol={mt5_symbol} | "
    f"Volume={volume_to_send} units "
    f"({adjusted_lots:.4f} lots before cTrader snap) | "
    f"stop={stop_r} limit={limit_r} "
    f"SL={sl_r} TP={tp_r} | "
    f"Label={pending_label} | "
    f"expiry_ms={expiration_ms_n}"
)

try:
    _clear_stale_position_mapping(
        account_name,
        ticket,
    )

    # Remember the pending type before sending the order.
    # This is useful if cTrader sends an execution callback
    # immediately after acceptance/fill.
    _store_pending_type(
        account_name,
        ticket,
        ptype,
    )

    resp = client.send_pending_order(
        account_id=config.account_id,
        symbol_id=symbol_id,
        side=trade_side,
        volume=volume_to_send,
        pending_type=ptype,
        stop_price=stop_r,
        limit_price=limit_r,
        sl=sl_r,
        tp=tp_r,
        label=pending_label,
        expiration_ms=expiration_ms_n,
    )

    order_id = _extract_pending_order_id(
        resp
    )

    if order_id > 0:
        _store_pending_mapping_immediately(
            account_name,
            ticket,
            order_id,
        )

    logger.info(
        f"[{account_name}] Pending order submitted | "
        f"ticket={ticket} "
        f"symbol={resolved_symbol} "
        f"symbolId={symbol_id} "
        f"label={pending_label} "
        f"orderId={order_id or 'unknown'}"
    )

    notify_info(
        event="pending_order_submitted",
        message="Pending order submitted to cTrader",
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            resolved_symbol=resolved_symbol,
            symbol_id=symbol_id,
            side=trade_side,
            volume_units=volume_to_send,
            adjusted_lots=adjusted_lots,
            pending_type=ptype,
            stop_price=stop_r,
            limit_price=limit_r,
            sl=sl_r,
            tp=tp_r,
            expiration_ms=expiration_ms_n,
            magic=magic,
            order_id=order_id or None,
        ),
    )

    return resp

except Exception as e:
    notify_error(
        event="send_pending_order",
        message=str(e),
        exc=e,
        **_base_context(
            account_name=account_name,
            ticket=ticket,
            mt5_symbol=mt5_symbol,
            resolved_symbol=resolved_symbol,
            symbol_id=symbol_id,
            side=trade_side,
            volume_units=volume_to_send,
            adjusted_lots=adjusted_lots,
            pending_type=ptype,
            stop_price=stop_r,
            limit_price=limit_r,
            sl=sl_r,
            tp=tp_r,
            expiration_ms=expiration_ms_n,
            magic=magic,
        ),
    )

    raise
```

"""

The important change here is that **LIMIT no longer cancels first**. It first verifies the destination pending is active, submits `MT5_<ticket>` as the fallback, and only then requests cancellation.

One important point: **this executor change alone is not enough to finish the race fix.** Your `account_manager.py` must also be changed so that when both positions exist, it recognizes:

* `MT5_PENDING_<ticket>` → **canonical**
* `MT5_<ticket>` → **temporary fallback**

and does **not let the later market-position event overwrite the LIMIT-originated position mapping**.

That AccountManager change is the part that makes the mapping deterministic even when cTrader sends the events in either order.
