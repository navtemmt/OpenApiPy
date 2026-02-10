#!/usr/bin/env python3
"""
Symbol-related helpers extracted from ctrader_client.py.

Key point (your confirmed debug dump):
- ProtoOASymbolsListReq returns light symbols (limited fields).
- Full trading specs like lotSize/minVolume/maxVolume/stepVolume are obtained via ProtoOASymbolByIdReq.

This module:
- Builds self.symbol_name_to_id from SymbolsList
- Stores basic symbol objects in self.symbol_details
- Then upgrades self.symbol_details entries with full ProtoOASymbol objects from SymbolById
"""

import logging
from typing import Optional, Iterable, List

from ctrader_open_api import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOASymbolsListReq,
    ProtoOASymbolByIdReq,
)

logger = logging.getLogger(__name__)


def load_symbol_map(self, debug_dump: bool = False) -> None:
    """Request SymbolsList then request full specs by id."""
    if not getattr(self, "account_id", None):
        return

    logger.info("Loading symbols for account %s...", self.account_id)
    req = ProtoOASymbolsListReq()
    req.ctidTraderAccountId = int(self.account_id)

    d = self.client.send(req)
    d.addCallback(lambda result: on_symbols_list(self, result, debug_dump=debug_dump))
    d.addErrback(self._on_error)


def on_symbols_list(self, result, debug_dump: bool = False) -> None:
    """Parse SymbolsList (light symbols) then fetch full specs via SymbolById."""
    try:
        msg = Protobuf.extract(result)
        symbols = getattr(msg, "symbol", None)
        if not symbols:
            logger.error("SymbolsList response has no symbols field: %r", msg)
            return

        self.symbol_name_to_id.clear()
        self.symbol_details.clear()

        ids: List[int] = []

        for s in symbols:
            try:
                name = (getattr(s, "symbolName", "") or "").upper()
                sid = int(getattr(s, "symbolId", 0) or 0)
                if not name or not sid:
                    continue

                self.symbol_name_to_id[name] = sid
                self.symbol_details[sid] = s
                ids.append(sid)

                if debug_dump and name in ("EURAUD", "XAUUSD", "BTCUSD", "US500"):
                    try:
                        if hasattr(s, "ListFields"):
                            fields = [(f.name, v) for f, v in s.ListFields()]
                            logger.info("DBG LIGHT SYMBOL %s id=%s fields=%s", name, sid, fields)
                        else:
                            logger.info("DBG LIGHT SYMBOL %s id=%s repr=%r", name, sid, s)
                    except Exception as e:
                        logger.info("DBG LIGHT SYMBOL dump failed for %s: %s", name, e)

            except Exception:
                continue

        logger.info("Loaded %d symbols (light)", len(self.symbol_name_to_id))

        # Now request full symbol specs (lotSize/minVolume/stepVolume/etc.)
        # Batch it to avoid overly large protobuf messages.
        request_symbol_specs(self, ids, batch_size=200, debug_dump=debug_dump)

    except Exception:
        logger.exception("Failed parsing symbols list")


def _chunked(items: Iterable[int], n: int) -> Iterable[List[int]]:
    chunk: List[int] = []
    for x in items:
        chunk.append(int(x))
        if len(chunk) >= n:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def request_symbol_specs(self, symbol_ids: List[int], batch_size: int = 200, debug_dump: bool = False) -> None:
    """
    Request full ProtoOASymbol entities for symbol_ids and merge into self.symbol_details.

    Some OpenApiPy builds model req.symbolId as a repeated field; we use append/extend safely.
    """
    if not getattr(self, "account_id", None):
        return
    if not symbol_ids:
        return

    sent = 0
    for batch in _chunked(symbol_ids, int(batch_size or 200)):
        req = ProtoOASymbolByIdReq()
        req.ctidTraderAccountId = int(self.account_id)

        # Common Python protobuf API: repeated field supports extend()
        try:
            req.symbolId.extend([int(s) for s in batch])
        except Exception:
            # Fallback: append one-by-one
            for sid in batch:
                try:
                    req.symbolId.append(int(sid))
                except Exception:
                    pass

        d = self.client.send(req)
        d.addCallback(lambda result, dd=debug_dump: on_symbol_specs(self, result, debug_dump=dd))
        d.addErrback(self._on_error)
        sent += len(batch)

    logger.info("Requested full specs for %d symbols (batched)", sent)


def on_symbol_specs(self, result, debug_dump: bool = False) -> None:
    """Merge full ProtoOASymbol entities into symbol_details."""
    try:
        msg = Protobuf.extract(result)
        symbols = getattr(msg, "symbol", None)
        if not symbols:
            logger.warning("SymbolById response has no symbol field: %r", msg)
            return

        updated = 0
        for s in symbols:
            try:
                sid = int(getattr(s, "symbolId", 0) or 0)
                if not sid:
                    continue

                # Replace light symbol with full symbol
                self.symbol_details[sid] = s
                updated += 1

                if debug_dump and sid in (self.symbol_name_to_id.get("EURAUD", -1),
                                         self.symbol_name_to_id.get("XAUUSD", -1),
                                         self.symbol_name_to_id.get("BTCUSD", -1),
                                         self.symbol_name_to_id.get("US500", -1)):
                    try:
                        if hasattr(s, "ListFields"):
                            fields = [(f.name, v) for f, v in s.ListFields()]
                            logger.info("DBG FULL SYMBOL id=%s fields=%s", sid, fields)
                        else:
                            logger.info("DBG FULL SYMBOL id=%s repr=%r", sid, s)
                    except Exception as e:
                        logger.info("DBG FULL SYMBOL dump failed for %s: %s", sid, e)

            except Exception:
                continue

        logger.info("Loaded full specs for %d symbols", updated)

    except Exception:
        logger.exception("Failed parsing symbol specs response")


def get_symbol_id_by_name(self, name: str) -> Optional[int]:
    """Lookup symbolId by symbol name (case-insensitive)."""
    return self.symbol_name_to_id.get((name or "").upper())


def round_price_for_symbol(self, symbol_id: int, price: float) -> float:
    """
    Round price using symbol digits if available on FULL symbol;
    if not available, return as-is.
    """
    symbol = self.symbol_details.get(int(symbol_id))
    if symbol is None:
        return float(price)

    digits = getattr(symbol, "digits", None)
    if digits is None:
        return float(price)

    try:
        digits = int(digits)
        factor = 10 ** digits
        return round(float(price) * factor) / factor
    except Exception:
        return float(price)


def snap_volume_for_symbol(self, symbol_id: int, volume_units: int) -> int:
    """
    Clamp volume using FULL symbol specs (minVolume/maxVolume/stepVolume) if present.
    If specs are missing/zero, returns the input volume_units unchanged.
    """
    v = int(volume_units or 0)
    symbol = self.symbol_details.get(int(symbol_id))
    if symbol is None:
        return v

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
