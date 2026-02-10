#!/usr/bin/env python3
"""
Symbol-related helpers extracted from ctrader_client.py.

Design goal: reduce ctrader_client.py size without breaking anything.
So all functions operate on the CTraderClient instance ("self") and keep
the same attribute names:
 - self.account_id
 - self.client
 - self.symbol_name_to_id
 - self.symbol_details
 - self._on_error
"""

import logging
from typing import Optional

from ctrader_open_api import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASymbolsListReq

logger = logging.getLogger(__name__)


def load_symbol_map(self) -> None:
    """Request and populate symbol_name_to_id and symbol_details."""
    if not getattr(self, "account_id", None):
        return

    logger.info("Loading symbols for account %s...", self.account_id)
    req = ProtoOASymbolsListReq()
    req.ctidTraderAccountId = int(self.account_id)

    d = self.client.send(req)
    d.addCallback(lambda result: on_symbols_list(self, result))
    d.addErrback(self._on_error)


def on_symbols_list(self, result) -> None:
    """Parse symbols list response and build maps."""
    try:
        msg = Protobuf.extract(result)
        symbols = getattr(msg, "symbol", None)
        if not symbols:
            logger.error("SymbolsList response has no symbols field: %r", msg)
            return

        self.symbol_name_to_id.clear()
        self.symbol_details.clear()

        for s in symbols:
            try:
                name = (getattr(s, "symbolName", "") or "").upper()
                sid = int(getattr(s, "symbolId", 0) or 0)
                if not name or not sid:
                    continue

                self.symbol_name_to_id[name] = sid
                self.symbol_details[sid] = s

                # DEBUG: dump the actual available fields for specific symbols.
                # Leave this in until we confirm correct extraction; then you can remove it.
                if name in ("EURAUD", "XAUUSD"):
                    try:
                        logger.info("DBG SYMBOL %s id=%s repr=%r", name, sid, s)
                        if hasattr(s, "ListFields"):
                            fields = [(f.name, v) for f, v in s.ListFields()]
                            logger.info("DBG SYMBOL %s fields=%s", name, fields)
                    except Exception as e:
                        logger.info("DBG SYMBOL dump failed for %s: %s", name, e)

            except Exception:
                continue

        logger.info("Loaded %d symbols", len(self.symbol_name_to_id))
    except Exception:
        logger.exception("Failed parsing symbols list")


def get_symbol_id_by_name(self, name: str) -> Optional[int]:
    """Lookup symbolId by symbol name (case-insensitive)."""
    return self.symbol_name_to_id.get((name or "").upper())


def round_price_for_symbol(self, symbol_id: int, price: float) -> float:
    """Round price to symbol precision (digits) if available."""
    symbol = self.symbol_details.get(int(symbol_id))
    digits = 4
    if symbol is not None and hasattr(symbol, "digits"):
        try:
            digits = int(getattr(symbol, "digits", digits))
        except Exception:
            pass
    factor = 10 ** int(digits)
    return round(float(price) * factor) / factor


def snap_volume_for_symbol(self, symbol_id: int, volume_units: int) -> int:
    """
    Clamp volume to symbol min/max/step.

    Assumes volume_units is in the same unit that the Open API expects for orders
    (i.e., ProtoOANewOrderReq.volume).
    """
    v = int(volume_units or 0)
    symbol = self.symbol_details.get(int(symbol_id))
    if symbol is None:
        return v

    # Common field names (may be zero / absent depending on broker/API build)
    min_v = int(getattr(symbol, "minVolume", 0) or 0)
    max_v = int(getattr(symbol, "maxVolume", 0) or 0)
    step_v = int(getattr(symbol, "stepVolume", 0) or 0)

    if min_v > 0:
        v = max(v, min_v)
    if max_v > 0:
        v = min(v, max_v)

    if step_v > 0:
        base = min_v if min_v > 0 else 0
        steps = round((v - base) / float(step_v))
        v = base + int(steps) * step_v
        if min_v > 0:
            v = max(v, min_v)

    return int(v)


def mt5_lots_to_ctrader_volume(self, symbol_id: int, mt5_lots: float) -> int:
    """
    Convert MT5 lots to cTrader volume "units", using cTrader symbol metadata when possible,
    then clamp with snap_volume_for_symbol().

    NOTE: If lotSize is missing/zero, this falls back to 100000 units/lot (FX convention).
    """
    sid = int(symbol_id)
    lots = float(mt5_lots or 0.0)
    symbol = self.symbol_details.get(sid)

    lot_size = 0
    if symbol is not None:
        try:
            lot_size = int(getattr(symbol, "lotSize", 0) or 0)
        except Exception:
            lot_size = 0

    if lot_size <= 0:
        lot_size = 100000  # FX default fallback

    raw_units = int(round(lots * float(lot_size)))
    return snap_volume_for_symbol(self, sid, raw_units)
