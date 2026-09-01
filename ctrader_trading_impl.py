#!/usr/bin/env python3
"""
Trading helpers extracted from ctrader_client.py.

Design goal:
- Reduce ctrader_client.py size without breaking API/attribute names.
- All functions operate on the CTraderClient instance `self`.
- Keep compatibility with:
  - self.is_account_authed
  - self.snap_volume_for_symbol, self.round_price_for_symbol
  - self.send
  - self._on_error

Close-volume policy:
- Default remains broker snapping / min-volume behavior for compatibility.
- Optional per-account override via:
    self.account_config["close_volume_policy"]
  or
    self.close_volume_policy

Allowed values:
- "min_volume"       -> current behavior; snap via broker rules
- "floor"            -> never increase requested close volume
- "full_if_below_min"-> allow snapped min-volume close when request is too small
"""

from typing import Optional, Any, Dict

from app_state import logger, notify_info, notify_warning, notify_error
from ctrader_open_api import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOANewOrderReq,
    ProtoOAAmendOrderReq,
    ProtoOAAmendPositionSLTPReq,
    ProtoOAClosePositionReq,
    ProtoOACancelOrderReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAOrderType,
    ProtoOATradeSide,
    ProtoOATimeInForce,
)


def parse_mt5_ticket_from_label(label: str) -> Optional[int]:
    """
    Parse bridge labels.

    Supported formats:
    - MT5_<ticket>          canonical market-copy label
    - MT5_PENDING_<ticket>  temporary follower-pending label
    - MT5<ticket>           legacy label
    """
    if not label:
        return None

    value = str(label).strip()

    if value.startswith("MT5_PENDING_"):
        suffix = value[len("MT5_PENDING_"):]
    elif value.startswith("MT5_"):
        suffix = value[len("MT5_"):]
    elif value.startswith("MT5"):
        suffix = value[len("MT5"):]
    else:
        return None

    try:
        return int(suffix) if suffix.isdigit() else None
    except Exception:
        return None


def _read_attr_or_key(obj, name: str, default=None):
    if obj is None:
        return default
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


def _first_nonempty(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _normalize_side(side: str) -> str:
    side_norm = str(side or "").strip().lower()
    if side_norm in ("buy", "long"):
        return "buy"
    if side_norm in ("sell", "short"):
        return "sell"
    raise ValueError(f"Unsupported side: {side}")


def _resolve_symbol_name_from_id(self, symbol_id: Optional[int]) -> Optional[str]:
    if symbol_id is None:
        return None
    try:
        symbol = self.symbol_details.get(int(symbol_id)) if hasattr(self, "symbol_details") else None
        name = getattr(symbol, "symbolName", None) if symbol is not None else None
        if name:
            return str(name)
    except Exception:
        pass
    try:
        broker_symbol_map = getattr(self, "symbol_name_to_id", {}) or {}
        for name, sid in broker_symbol_map.items():
            if int(sid) == int(symbol_id):
                return str(name)
    except Exception:
        pass
    return None


def _extract_response_context(
    self,
    extracted,
    fallback_account_id: Optional[int] = None,
    fallback_symbol_id: Optional[int] = None,
    fallback_label: Optional[str] = None,
):
    order = _read_attr_or_key(extracted, "order", None)
    position = _read_attr_or_key(extracted, "position", None)
    deal = _read_attr_or_key(extracted, "deal", None)

    order_trade_data = _read_attr_or_key(order, "tradeData", None)
    position_trade_data = _read_attr_or_key(position, "tradeData", None)

    symbol_id = _first_nonempty(
        _read_attr_or_key(order_trade_data, "symbolId", None),
        _read_attr_or_key(position_trade_data, "symbolId", None),
        _read_attr_or_key(deal, "symbolId", None),
        fallback_symbol_id,
    )
    label = _first_nonempty(
        _read_attr_or_key(order_trade_data, "label", None),
        _read_attr_or_key(position_trade_data, "label", None),
        fallback_label,
    )

    ticket = _parse_mt5_ticket_from_label(label) if label else None
    symbol_name = _resolve_symbol_name_from_id(self, symbol_id) if symbol_id is not None else None

    return {
        "account_id": _first_nonempty(
            _read_attr_or_key(extracted, "ctidTraderAccountId", None),
            fallback_account_id,
        ),
        "ticket": ticket,
        "symbol_id": symbol_id,
        "symbol_name": symbol_name,
        "label": label,
        "execution_type": _read_attr_or_key(extracted, "executionType", None),
        "order_id": _read_attr_or_key(order, "orderId", None),
        "position_id": _read_attr_or_key(position, "positionId", None),
        "deal_id": _read_attr_or_key(deal, "dealId", None),
    }


def _format_context(context: Dict[str, Any]) -> str:
    parts = []
    if context.get("account_id") is not None:
        parts.append(f"accountId={context['account_id']}")
    if context.get("ticket") is not None:
        parts.append(f"ticket={context['ticket']}")
    if context.get("symbol_name"):
        parts.append(f"symbol={context['symbol_name']}")
    if context.get("symbol_id") is not None:
        parts.append(f"symbolId={context['symbol_id']}")
    if context.get("position_id") is not None:
        parts.append(f"positionId={context['position_id']}")
    if context.get("order_id") is not None:
        parts.append(f"orderId={context['order_id']}")
    if context.get("deal_id") is not None:
        parts.append(f"dealId={context['deal_id']}")
    if context.get("execution_type"):
        parts.append(f"execType={context['execution_type']}")
    if context.get("label"):
        parts.append(f"label={context['label']}")
    return " ".join(parts)


def _normalize_pending_type(pending_type: str) -> str:
    ptype = str(pending_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "limit": "limit",
        "stop": "stop",
        "stoplimit": "stop_limit",
        "stop_limit": "stop_limit",
        "buylimit": "limit",
        "selllimit": "limit",
        "buy_limit": "limit",
        "sell_limit": "limit",
        "buystop": "stop",
        "sellstop": "stop",
        "buy_stop": "stop",
        "sell_stop": "stop",
        "buystoplimit": "stop_limit",
        "sellstoplimit": "stop_limit",
        "buy_stop_limit": "stop_limit",
        "sell_stop_limit": "stop_limit",
    }
    normalized = aliases.get(ptype, ptype)
    if normalized not in ("limit", "stop", "stop_limit"):
        raise ValueError(f"Unsupported pending type: {pending_type}")
    return normalized


def _base_event_context(
    account_id: Optional[int] = None,
    symbol_id: Optional[int] = None,
    symbol_name: Optional[str] = None,
    ticket: Optional[int] = None,
    order_id: Optional[int] = None,
    position_id: Optional[int] = None,
    side: Optional[str] = None,
    volume: Optional[int] = None,
    pending_type: Optional[str] = None,
    label: Optional[str] = None,
    stop_price: Optional[float] = None,
    limit_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    expiration_ms: Optional[int] = None,
):
    ctx = {}
    if account_id is not None:
        ctx["account_id"] = int(account_id)
    if symbol_id is not None:
        ctx["symbol_id"] = int(symbol_id)
    if symbol_name:
        ctx["symbol_name"] = str(symbol_name)
    if ticket is not None:
        ctx["ticket"] = int(ticket)
    if order_id is not None:
        ctx["order_id"] = int(order_id)
    if position_id is not None:
        ctx["position_id"] = int(position_id)
    if side:
        ctx["side"] = str(side)
    if volume is not None:
        ctx["volume"] = int(volume)
    if pending_type:
        ctx["pending_type"] = str(pending_type)
    if label:
        ctx["label"] = str(label)
    if stop_price is not None:
        ctx["stop_price"] = float(stop_price)
    if limit_price is not None:
        ctx["limit_price"] = float(limit_price)
    if stop_loss is not None:
        ctx["stop_loss"] = float(stop_loss)
    if take_profit is not None:
        ctx["take_profit"] = float(take_profit)
    if expiration_ms is not None:
        ctx["expiration_ms"] = int(expiration_ms)
    return ctx


def _notify_response_ok(event: str, message: str, context: Dict[str, Any]):
    notify_info(event=event, message=message, **context)


def _notify_response_warn(event: str, message: str, context: Dict[str, Any]):
    notify_warning(event=event, message=message, **context)


def _notify_response_error(
    event: str,
    message: str,
    exc: Optional[Exception] = None,
    context: Optional[Dict[str, Any]] = None,
):
    notify_error(event=event, message=message, exc=exc, **(context or {}))


def _auth_guard(event: str, context: Dict[str, Any]):
    self_ref = context.get("self_ref")
    if not getattr(self_ref, "is_account_authed", False):
        exc = RuntimeError("Account not authenticated yet")
        notify_error(
            event=event,
            message=str(exc),
            exc=exc,
            **{k: v for k, v in context.items() if k != "self_ref"},
        )
        raise exc


def _get_close_volume_policy(self) -> str:
    """
    Per-account close sizing policy.

    Supported values:
      - min_volume
      - floor
      - full_if_below_min
    """
    config = getattr(self, "account_config", {}) or {}
    policy = (
        config.get("close_volume_policy")
        or getattr(self, "close_volume_policy", None)
        or "min_volume"
    )
    policy = str(policy).strip().lower()
    allowed = {"min_volume", "floor", "full_if_below_min"}
    if policy not in allowed:
        logger.warning(
            "Unknown close_volume_policy=%r; falling back to min_volume",
            policy,
        )
        return "min_volume"
    return policy


def amend_position(
    self,
    account_id: int,
    position_id: int,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    symbol_id: Optional[int] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
):
    if stop_loss is not None:
        sl = stop_loss
    if take_profit is not None:
        tp = take_profit
    return modify_position(
        self,
        account_id=account_id,
        position_id=position_id,
        sl=sl,
        tp=tp,
        symbol_id=symbol_id,
    )


def send_market_order(
    self,
    account_id: int,
    symbol_id: int,
    side: str,
    volume: int,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    label: str = "MT5_Copy",
):
    ticket = _parse_mt5_ticket_from_label(label)
    symbol_name = _resolve_symbol_name_from_id(self, symbol_id) or "UNKNOWN"

    _auth_guard(
        "ctrader_market_order_not_authed",
        {
            "self_ref": self,
            **_base_event_context(
                account_id=account_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                ticket=ticket,
                side=side,
                volume=volume,
                label=label,
            ),
        },
    )

    try:
        side_norm = _normalize_side(side)
        volume = self.snap_volume_for_symbol(symbol_id, int(volume))
        if sl is not None and float(sl) > 0:
            sl = self.round_price_for_symbol(symbol_id, float(sl))
        if tp is not None and float(tp) > 0:
            tp = self.round_price_for_symbol(symbol_id, float(tp))
    except Exception as e:
        _notify_response_error(
            "ctrader_market_order_prepare_failed",
            str(e),
            exc=e,
            context=_base_event_context(
                account_id=account_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                ticket=ticket,
                side=side,
                volume=volume,
                label=label,
                stop_loss=sl,
                take_profit=tp,
            ),
        )
        raise

    req = ProtoOANewOrderReq()
    req.ctidTraderAccountId = int(account_id)
    req.symbolId = int(symbol_id)
    req.orderType = ProtoOAOrderType.MARKET
    req.tradeSide = ProtoOATradeSide.BUY if side_norm == "buy" else ProtoOATradeSide.SELL
    req.volume = int(volume)
    if sl is not None and float(sl) > 0.0:
        req.stopLoss = float(sl)
    if tp is not None and float(tp) > 0.0:
        req.takeProfit = float(tp)
    req.label = label

    logger.info(
        "Sending market order accountId=%s ticket=%s symbol=%s symbolId=%s side=%s volume=%s sl=%s tp=%s label=%s",
        account_id, ticket, symbol_name, symbol_id, side_norm, volume, sl, tp, label
    )

    try:
        d = self.send(req)
    except Exception as e:
        _notify_response_error(
            "ctrader_market_order_send_failed",
            str(e),
            exc=e,
            context=_base_event_context(
                account_id=account_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                ticket=ticket,
                side=side_norm,
                volume=volume,
                label=label,
                stop_loss=sl,
                take_profit=tp,
            ),
        )
        raise

    def on_resp(result):
        try:
            extracted = Protobuf.extract(result)
            context = _extract_response_context(
                self,
                extracted,
                fallback_account_id=account_id,
                fallback_symbol_id=symbol_id,
                fallback_label=label,
            )
            logger.info("Order response %s %s", _format_context(context), extracted)
            _notify_response_ok(
                "ctrader_market_order_response",
                "Market order response received",
                context,
            )
        except Exception as e:
            _notify_response_warn(
                "ctrader_market_order_response_parse_warning",
                "Failed to parse market order response; raw response logged",
                {
                    **_base_event_context(
                        account_id=account_id,
                        symbol_id=symbol_id,
                        symbol_name=symbol_name,
                        ticket=ticket,
                        side=side_norm,
                        volume=volume,
                        label=label,
                        stop_loss=sl,
                        take_profit=tp,
                    ),
                    "error": str(e),
                    "raw_type": type(result).__name__,
                },
            )
            logger.warning(
                "Order response raw accountId=%s ticket=%s symbol=%s symbolId=%s label=%s raw=%r",
                account_id, ticket, symbol_name, symbol_id, label, result
            )

    def on_err(failure):
        _notify_response_error(
            "ctrader_market_order_errback",
            str(failure),
            exc=Exception(str(failure)),
            context=_base_event_context(
                account_id=account_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                ticket=ticket,
                side=side_norm,
                volume=volume,
                label=label,
                stop_loss=sl,
                take_profit=tp,
            ),
        )
        return self._on_error(failure)

    d.addCallback(on_resp)
    d.addErrback(on_err)
    return d


def send_pending_order(
    self,
    account_id: int,
    symbol_id: int,
    side: str,
    volume: int,
    pending_type: str,
    stop_price: float = 0.0,
    limit_price: float = 0.0,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    label: str = "MT5_Pending",
    expiration_ms: int = 0,
):
    ticket = _parse_mt5_ticket_from_label(label)
    symbol_name = _resolve_symbol_name_from_id(self, symbol_id) or "UNKNOWN"

    _auth_guard(
        "ctrader_pending_order_not_authed",
        {
            "self_ref": self,
            **_base_event_context(
                account_id=account_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                ticket=ticket,
                side=side,
                volume=volume,
                pending_type=pending_type,
                label=label,
            ),
        },
    )

    try:
        side_norm = _normalize_side(side)
        ptype = _normalize_pending_type(pending_type)
        volume = self.snap_volume_for_symbol(symbol_id, int(volume))

        stop_price = float(stop_price or 0.0)
        limit_price = float(limit_price or 0.0)

        if stop_price > 0:
            stop_price = self.round_price_for_symbol(symbol_id, stop_price)
        if limit_price > 0:
            limit_price = self.round_price_for_symbol(symbol_id, limit_price)
        if sl is not None and float(sl) > 0:
            sl = self.round_price_for_symbol(symbol_id, float(sl))
        if tp is not None and float(tp) > 0:
            tp = self.round_price_for_symbol(symbol_id, float(tp))
    except Exception as e:
        _notify_response_error(
            "ctrader_pending_order_prepare_failed",
            str(e),
            exc=e,
            context=_base_event_context(
                account_id=account_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                ticket=ticket,
                side=side,
                volume=volume,
                pending_type=pending_type,
                label=label,
                stop_price=stop_price or None,
                limit_price=limit_price or None,
                stop_loss=sl,
                take_profit=tp,
                expiration_ms=expiration_ms,
            ),
        )
        raise

    req = ProtoOANewOrderReq()
    req.ctidTraderAccountId = int(account_id)
    req.symbolId = int(symbol_id)
    req.tradeSide = ProtoOATradeSide.BUY if side_norm == "buy" else ProtoOATradeSide.SELL
    req.volume = int(volume)
    req.label = str(label)

    if ptype == "limit":
        if not limit_price and float(limit_price) <= 0.0:
            raise ValueError("LIMIT pending order requires limit_price > 0")
        req.orderType = ProtoOAOrderType.LIMIT
        req.limitPrice = float(limit_price)
    elif ptype == "stop":
        if not stop_price and float(stop_price) <= 0.0:
            raise ValueError("STOP pending order requires stop_price > 0")
        req.orderType = ProtoOAOrderType.STOP
        req.stopPrice = float(stop_price)
    else:
        if not stop_price and float(stop_price) <= 0.0:
            raise ValueError("STOP_LIMIT pending order requires stop_price > 0")
        if not limit_price and float(limit_price) <= 0.0:
            raise ValueError("STOP_LIMIT pending order requires limit_price > 0")
        req.orderType = ProtoOAOrderType.STOPLIMIT
        req.stopPrice = float(stop_price)
        req.limitPrice = float(limit_price)

    if sl is not None and float(sl) > 0.0:
        req.stopLoss = float(sl)
    if tp is not None and float(tp) > 0.0:
        req.takeProfit = float(tp)
    if expiration_ms and int(expiration_ms) > 0:
        req.timeInForce = ProtoOATimeInForce.GOOD_TILL_DATE
        req.expirationTimestamp = int(expiration_ms)

    logger.info(
        "Sending pending order accountId=%s ticket=%s symbol=%s symbolId=%s type=%s side=%s vol=%s stop=%s limit=%s SL=%s TP=%s exp=%s label=%s",
        account_id, ticket, symbol_name, symbol_id, ptype, side_norm, volume,
        stop_price, limit_price, sl, tp, int(expiration_ms or 0), label
    )

    try:
        d = self.send(req)
    except Exception as e:
        _notify_response_error(
            "ctrader_pending_order_send_failed",
            str(e),
            exc=e,
            context=_base_event_context(
                account_id=account_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                ticket=ticket,
                side=side_norm,
                volume=volume,
                pending_type=ptype,
                label=label,
                stop_price=stop_price or None,
                limit_price=limit_price or None,
                stop_loss=sl,
                take_profit=tp,
                expiration_ms=expiration_ms,
            ),
        )
        raise

    def on_resp(result):
        try:
            extracted = Protobuf.extract(result)
            context = _extract_response_context(
                self,
                extracted,
                fallback_account_id=account_id,
                fallback_symbol_id=symbol_id,
                fallback_label=label,
            )
            logger.info("Pending order response %s %s", _format_context(context), extracted)
            _notify_response_ok(
                "ctrader_pending_order_response",
                "Pending order response received",
                context,
            )
        except Exception as e:
            _notify_response_warn(
                "ctrader_pending_order_response_parse_warning",
                "Failed to parse pending order response; raw response logged",
                {
                    **_base_event_context(
                        account_id=account_id,
                        symbol_id=symbol_id,
                        symbol_name=symbol_name,
                        ticket=ticket,
                        side=side_norm,
                        volume=volume,
                        pending_type=ptype,
                        label=label,
                        stop_price=stop_price or None,
                        limit_price=limit_price or None,
                        stop_loss=sl,
                        take_profit=tp,
                        expiration_ms=expiration_ms,
                    ),
                    "error": str(e),
                    "raw_type": type(result).__name__,
                },
            )
            logger.warning(
                "Pending order response raw accountId=%s ticket=%s symbol=%s symbolId=%s label=%s raw=%r",
                account_id, ticket, symbol_name, symbol_id, label, result
            )

    def on_err(failure):
        _notify_response_error(
            "ctrader_pending_order_errback",
            str(failure),
            exc=Exception(str(failure)),
            context=_base_event_context(
                account_id=account_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                ticket=ticket,
                side=side_norm,
                volume=volume,
                pending_type=ptype,
                label=label,
                stop_price=stop_price or None,
                limit_price=limit_price or None,
                stop_loss=sl,
                take_profit=tp,
                expiration_ms=expiration_ms,
            ),
        )
        return self._on_error(failure)

    d.addCallback(on_resp)
    d.addErrback(on_err)
    return d


def amend_pending_order(
    self,
    account_id: int,
    order_id: int,
    symbol_id: int,
    side: str,
    volume: int,
    pending_type: str,
    stop_price: Optional[float] = None,
    limit_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    expiration_ms: Optional[int] = None,
):
    symbol_name = _resolve_symbol_name_from_id(self, symbol_id) or "UNKNOWN"

    _auth_guard(
        "ctrader_amend_pending_not_authed",
        {
            "self_ref": self,
            **_base_event_context(
                account_id=account_id,
                order_id=order_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                side=side,
                volume=volume,
                pending_type=pending_type,
            ),
        },
    )

    try:
        side_norm = _normalize_side(side)
        ptype = _normalize_pending_type(pending_type)

        account_id = int(account_id)
        order_id = int(order_id)
        symbol_id = int(symbol_id)
        volume = self.snap_volume_for_symbol(symbol_id, int(volume))

        stop_price = float(stop_price or 0.0)
        limit_price = float(limit_price or 0.0)

        if stop_price > 0:
            stop_price = self.round_price_for_symbol(symbol_id, stop_price)
        else:
            stop_price = None

        if limit_price > 0:
            limit_price = self.round_price_for_symbol(symbol_id, limit_price)
        else:
            limit_price = None

        if stop_loss is not None and float(stop_loss) > 0:
            stop_loss = self.round_price_for_symbol(symbol_id, float(stop_loss))
        else:
            stop_loss = None

        if take_profit is not None and float(take_profit) > 0:
            take_profit = self.round_price_for_symbol(symbol_id, float(take_profit))
        else:
            take_profit = None
    except Exception as e:
        _notify_response_error(
            "ctrader_amend_pending_prepare_failed",
            str(e),
            exc=e,
            context=_base_event_context(
                account_id=account_id,
                order_id=order_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                side=side,
                volume=volume,
                pending_type=pending_type,
                stop_price=stop_price,
                limit_price=limit_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                expiration_ms=expiration_ms,
            ),
        )
        raise

    req = ProtoOAAmendOrderReq()
    req.ctidTraderAccountId = account_id
    req.orderId = order_id
    req.symbolId = symbol_id
    req.tradeSide = ProtoOATradeSide.BUY if side_norm == "buy" else ProtoOATradeSide.SELL
    req.volume = int(volume)

    if ptype == "limit":
        if limit_price is None:
            raise ValueError("LIMIT amend requires limit_price > 0")
        req.orderType = ProtoOAOrderType.LIMIT
        req.limitPrice = float(limit_price)
    elif ptype == "stop":
        if stop_price is None:
            raise ValueError("STOP amend requires stop_price > 0")
        req.orderType = ProtoOAOrderType.STOP
        req.stopPrice = float(stop_price)
    else:
        if stop_price is None:
            raise ValueError("STOP_LIMIT amend requires stop_price > 0")
        if limit_price is None:
            raise ValueError("STOP_LIMIT amend requires limit_price > 0")
        req.orderType = ProtoOAOrderType.STOPLIMIT
        req.stopPrice = float(stop_price)
        req.limitPrice = float(limit_price)

    if stop_loss is not None:
        req.stopLoss = float(stop_loss)
    if take_profit is not None:
        req.takeProfit = float(take_profit)

    if expiration_ms is not None and int(expiration_ms) > 0:
        req.timeInForce = ProtoOATimeInForce.GOOD_TILL_DATE
        req.expirationTimestamp = int(expiration_ms)

    logger.info(
        "Amending pending order accountId=%s orderId=%s symbol=%s symbolId=%s type=%s side=%s vol=%s stop=%s limit=%s SL=%s TP=%s exp=%s",
        account_id, order_id, symbol_name, symbol_id, ptype, side_norm, volume,
        stop_price, limit_price, stop_loss, take_profit,
        int(expiration_ms or 0) if expiration_ms is not None else 0
    )

    try:
        d = self.send(req)
    except Exception as e:
        _notify_response_error(
            "ctrader_amend_pending_send_failed",
            str(e),
            exc=e,
            context=_base_event_context(
                account_id=account_id,
                order_id=order_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                side=side_norm,
                volume=volume,
                pending_type=ptype,
                stop_price=stop_price,
                limit_price=limit_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                expiration_ms=expiration_ms,
            ),
        )
        raise

    def on_resp(result):
        try:
            extracted = Protobuf.extract(result)
            context = _extract_response_context(
                self,
                extracted,
                fallback_account_id=account_id,
                fallback_symbol_id=symbol_id,
            )
            if context.get("order_id") is None:
                context["order_id"] = int(order_id)
            logger.info("Amend pending order response %s %s", _format_context(context), extracted)
            _notify_response_ok(
                "ctrader_amend_pending_response",
                "Amend pending order response received",
                context,
            )
        except Exception as e:
            _notify_response_warn(
                "ctrader_amend_pending_response_parse_warning",
                "Failed to parse amend pending response; raw response logged",
                {
                    **_base_event_context(
                        account_id=account_id,
                        order_id=order_id,
                        symbol_id=symbol_id,
                        symbol_name=symbol_name,
                        side=side_norm,
                        volume=volume,
                        pending_type=ptype,
                        stop_price=stop_price,
                        limit_price=limit_price,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        expiration_ms=expiration_ms,
                    ),
                    "error": str(e),
                    "raw_type": type(result).__name__,
                },
            )
            logger.warning(
                "Amend pending order response raw accountId=%s orderId=%s symbol=%s symbolId=%s raw=%r",
                account_id, order_id, symbol_name, symbol_id, result
            )

    def on_err(failure):
        _notify_response_error(
            "ctrader_amend_pending_errback",
            str(failure),
            exc=Exception(str(failure)),
            context=_base_event_context(
                account_id=account_id,
                order_id=order_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                side=side_norm,
                volume=volume,
                pending_type=ptype,
                stop_price=stop_price,
                limit_price=limit_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                expiration_ms=expiration_ms,
            ),
        )
        return self._on_error(failure)

    d.addCallback(on_resp)
    d.addErrback(on_err)
    return d


def cancel_pending_order(self, account_id: int, order_id: int):
    _auth_guard(
        "ctrader_cancel_pending_not_authed",
        {"self_ref": self, **_base_event_context(account_id=account_id, order_id=order_id)},
    )

    req = ProtoOACancelOrderReq()
    req.ctidTraderAccountId = int(account_id)
    req.orderId = int(order_id)

    logger.info("Cancelling pending order accountId=%s orderId=%s", account_id, order_id)

    try:
        d = self.send(req)
    except Exception as e:
        _notify_response_error(
            "ctrader_cancel_pending_send_failed",
            str(e),
            exc=e,
            context=_base_event_context(account_id=account_id, order_id=order_id),
        )
        raise

    def on_resp(result):
        try:
            extracted = Protobuf.extract(result)
            context = _extract_response_context(self, extracted, fallback_account_id=account_id)
            if context.get("order_id") is None:
                context["order_id"] = int(order_id)
            logger.info("Cancel order response %s %s", _format_context(context), extracted)
            _notify_response_ok(
                "ctrader_cancel_pending_response",
                "Cancel pending order response received",
                context,
            )
        except Exception as e:
            _notify_response_warn(
                "ctrader_cancel_pending_response_parse_warning",
                "Failed to parse cancel pending response; raw response logged",
                {
                    **_base_event_context(account_id=account_id, order_id=order_id),
                    "error": str(e),
                    "raw_type": type(result).__name__,
                },
            )
            logger.warning(
                "Cancel order response raw accountId=%s orderId=%s raw=%r",
                account_id, order_id, result
            )

    def on_err(failure):
        _notify_response_error(
            "ctrader_cancel_pending_errback",
            str(failure),
            exc=Exception(str(failure)),
            context=_base_event_context(account_id=account_id, order_id=order_id),
        )
        return self._on_error(failure)

    d.addCallback(on_resp)
    d.addErrback(on_err)
    return d


def modify_position(
    self,
    account_id: int,
    position_id: int,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    symbol_id: Optional[int] = None,
):
    symbol_name = _resolve_symbol_name_from_id(self, symbol_id if symbol_id is not None else None)

    _auth_guard(
        "ctrader_modify_position_not_authed",
        {
            "self_ref": self,
            **_base_event_context(
                account_id=account_id,
                position_id=position_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                stop_loss=sl,
                take_profit=tp,
            ),
        },
    )

    orig_sl, orig_tp = sl, tp

    try:
        if sl is not None and float(sl) <= 0.0:
            sl = None
        if tp is not None and float(tp) <= 0.0:
            tp = None

        if symbol_id is not None:
            if sl is not None:
                sl = self.round_price_for_symbol(symbol_id, sl)
            if tp is not None:
                tp = self.round_price_for_symbol(symbol_id, tp)
    except Exception as e:
        _notify_response_error(
            "ctrader_modify_position_prepare_failed",
            str(e),
            exc=e,
            context=_base_event_context(
                account_id=account_id,
                position_id=position_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                stop_loss=orig_sl,
                take_profit=orig_tp,
            ),
        )
        raise

    req = ProtoOAAmendPositionSLTPReq()
    req.ctidTraderAccountId = int(account_id)
    req.positionId = int(position_id)
    if sl is not None:
        req.stopLoss = float(sl)
    if tp is not None:
        req.takeProfit = float(tp)

    logger.info(
        "Modifying position accountId=%s positionId=%s symbol=%s symbolId=%s SL=%s->%s TP=%s->%s",
        account_id, position_id, symbol_name, symbol_id, orig_sl, sl, orig_tp, tp
    )

    try:
        d = self.send(req)
    except Exception as e:
        _notify_response_error(
            "ctrader_modify_position_send_failed",
            str(e),
            exc=e,
            context=_base_event_context(
                account_id=account_id,
                position_id=position_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                stop_loss=sl,
                take_profit=tp,
            ),
        )
        raise

    def on_resp(result):
        try:
            extracted = Protobuf.extract(result)
            context = _extract_response_context(
                self,
                extracted,
                fallback_account_id=account_id,
                fallback_symbol_id=symbol_id,
            )
            if context.get("position_id") is None:
                context["position_id"] = int(position_id)
            logger.info("Amend response %s %s", _format_context(context), extracted)
            _notify_response_ok(
                "ctrader_modify_position_response",
                "Modify position response received",
                context,
            )
        except Exception as e:
            _notify_response_warn(
                "ctrader_modify_position_response_parse_warning",
                "Failed to parse modify position response; raw response logged",
                {
                    **_base_event_context(
                        account_id=account_id,
                        position_id=position_id,
                        symbol_id=symbol_id,
                        symbol_name=symbol_name,
                        stop_loss=sl,
                        take_profit=tp,
                    ),
                    "error": str(e),
                    "raw_type": type(result).__name__,
                },
            )
            logger.warning(
                "Amend response raw accountId=%s positionId=%s symbol=%s symbolId=%s raw=%r",
                account_id, position_id, symbol_name, symbol_id, result
            )

    def on_err(failure):
        _notify_response_error(
            "ctrader_modify_position_errback",
            str(failure),
            exc=Exception(str(failure)),
            context=_base_event_context(
                account_id=account_id,
                position_id=position_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                stop_loss=sl,
                take_profit=tp,
            ),
        )
        return self._on_error(failure)

    d.addCallback(on_resp)
    d.addErrback(on_err)
    return d


def close_position(self, *args: Any, **kwargs: Any):
    account_id = kwargs.get("account_id")
    position_id = kwargs.get("position_id", kwargs.get("pos_id"), kwargs.get("position"))
    volume = kwargs.get("volume", kwargs.get("qty"), kwargs.get("volume_cents"))
    symbol_id = kwargs.get("symbol_id")

    if account_id is None and len(args) >= 1:
        account_id = args[0]
    if position_id is None and len(args) >= 2:
        position_id = args[1]
    if volume is None and len(args) >= 3:
        volume = args[2]
    if symbol_id is None and len(args) >= 4:
        symbol_id = args[3]

    if account_id is None or position_id is None or volume is None:
        exc = TypeError("close_position requires account_id, position_id, volume, symbol_id")
        _notify_response_error(
            "ctrader_close_position_invalid_args",
            str(exc),
            exc=exc,
            context=_base_event_context(
                account_id=account_id,
                position_id=position_id,
                symbol_id=symbol_id,
                volume=volume,
            ),
        )
        raise exc

    symbol_name = None
    if symbol_id is not None:
        try:
            symbol_name = _resolve_symbol_name_from_id(self, int(symbol_id))
        except Exception:
            symbol_name = None

    _auth_guard(
        "ctrader_close_position_not_authed",
        {
            "self_ref": self,
            **_base_event_context(
                account_id=account_id,
                position_id=position_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                volume=volume,
            ),
        },
    )

    try:
        account_id = int(account_id)
        position_id = int(position_id)
        volume = int(volume)

        if symbol_id is not None:
            symbol_id = int(symbol_id)
            symbol_name = _resolve_symbol_name_from_id(self, symbol_id)

            requested_volume = int(volume)
            policy = _get_close_volume_policy(self)
            snapped_volume = int(self.snap_volume_for_symbol(symbol_id, requested_volume))

            if policy == "floor" and snapped_volume > requested_volume:
                raise ValueError(
                    f"Close volume would be increased by broker snapping: "
                    f"requested={requested_volume}, snapped={snapped_volume}, "
                    f"symbolId={symbol_id}, policy={policy}"
                )

            if policy == "full_if_below_min" and snapped_volume > requested_volume:
                logger.warning(
                    "Close size below broker minimum; min-volume close permitted by policy. "
                    "requested=%s snapped=%s symbolId=%s",
                    requested_volume,
                    snapped_volume,
                    symbol_id,
                )

            volume = snapped_volume

    except Exception as e:
        _notify_response_error(
            "ctrader_close_position_prepare_failed",
            str(e),
            exc=e,
            context=_base_event_context(
                account_id=account_id,
                position_id=position_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                volume=volume,
            ),
        )
        raise

    req = ProtoOAClosePositionReq()
    req.ctidTraderAccountId = account_id
    req.positionId = position_id
    req.volume = volume

    logger.info(
        "Closing position accountId=%s positionId=%s symbol=%s symbolId=%s volume=%s",
        account_id, position_id, symbol_name, symbol_id, volume
    )

    try:
        d = self.send(req)
    except Exception as e:
        _notify_response_error(
            "ctrader_close_position_send_failed",
            str(e),
            exc=e,
            context=_base_event_context(
                account_id=account_id,
                position_id=position_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                volume=volume,
            ),
        )
        raise

    def on_resp(result):
        try:
            extracted = Protobuf.extract(result)
            context = _extract_response_context(
                self,
                extracted,
                fallback_account_id=account_id,
                fallback_symbol_id=symbol_id,
            )
            if context.get("position_id") is None:
                context["position_id"] = int(position_id)
            logger.info("Close response %s %s", _format_context(context), extracted)
            _notify_response_ok(
                "ctrader_close_position_response",
                "Close position response received",
                context,
            )
        except Exception as e:
            _notify_response_warn(
                "ctrader_close_position_response_parse_warning",
                "Failed to parse close position response; raw response logged",
                {
                    **_base_event_context(
                        account_id=account_id,
                        position_id=position_id,
                        symbol_id=symbol_id,
                        symbol_name=symbol_name,
                        volume=volume,
                    ),
                    "error": str(e),
                    "raw_type": type(result).__name__,
                },
            )
            logger.warning(
                "Close response raw accountId=%s positionId=%s symbol=%s symbolId=%s volume=%s raw=%r",
                account_id, position_id, symbol_name, symbol_id, volume, result
            )

    def on_err(failure):
        _notify_response_error(
            "ctrader_close_position_errback",
            str(failure),
            exc=Exception(str(failure)),
            context=_base_event_context(
                account_id=account_id,
                position_id=position_id,
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                volume=volume,
            ),
        )
        return self._on_error(failure)

    d.addCallback(on_resp)
    d.addErrback(on_err)
    return d
