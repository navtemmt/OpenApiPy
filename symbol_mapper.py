"""
Symbol Mapping Utilities for MT5 to cTrader

Handles symbol name transformation and mapping between platforms.
"""
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class SymbolMapper:
    """
    Maps MT5 symbols to cTrader symbols with support for prefixes, suffixes,
    and custom mappings.

    IMPORTANT:
    cTrader symbolId values are broker/account specific.
    Do not hardcode symbolId fallbacks for live trading.
    """

    def __init__(
        self,
        prefix: str = "",
        suffix: str = "",
        custom_map: Optional[Dict[str, str]] = None,
        broker_symbol_map: Optional[Dict[str, int]] = None,
        strict: bool = True,
    ):
        """
        Args:
            prefix: Prefix to strip from MT5 symbols (when normalizing).
            suffix: Suffix to strip from MT5 symbols (when normalizing).
            custom_map: Custom symbol name mappings, e.g. {"XAUUSD": "GOLD", "XAUUSD.m": "XAUUSD"}.
            broker_symbol_map: Dynamic cTrader symbol name -> symbolId map from CTraderClient.
            strict: If True, return None when symbol is missing in broker_symbol_map (recommended).
        """
        self.prefix = prefix or ""
        self.suffix = suffix or ""
        self.strict = bool(strict)

        # normalize custom_map keys/values to upper-case
        self.custom_map: Dict[str, str] = {
            k.upper(): v.upper() for k, v in (custom_map or {}).items()
        }

        # normalize broker symbol map to upper-case keys
        self.broker_symbol_map: Dict[str, int] = {
            k.upper(): int(v) for k, v in (broker_symbol_map or {}).items()
        }

        logger.info(
            "Symbol mapper initialized: prefix='%s', suffix='%s', custom_map=%s, broker_symbol_map_size=%d, strict=%s",
            self.prefix,
            self.suffix,
            self.custom_map,
            len(self.broker_symbol_map),
            self.strict,
        )

    def mt5_to_ctrader_name(self, mt5_symbol: str) -> str:
        """
        Convert MT5 symbol name to cTrader symbol name (name only).

        Process:
        1) Custom mapping override (e.g. XAUUSD -> GOLD, XAUUSD.m -> XAUUSD).
        2) Strip prefix/suffix if configured.
        3) Return normalized symbol name (upper-case).
        """
        raw = (mt5_symbol or "").upper()

        # 1) custom mapping override
        if raw in self.custom_map:
            mapped = self.custom_map[raw]
            logger.debug("Symbol mapped via custom map: %s -> %s", mt5_symbol, mapped)
            return mapped

        # 2) strip configured prefix/suffix
        ctrader_symbol = raw

        if self.prefix and ctrader_symbol.startswith(self.prefix.upper()):
            ctrader_symbol = ctrader_symbol[len(self.prefix) :]

        if self.suffix and ctrader_symbol.endswith(self.suffix.upper()):
            ctrader_symbol = ctrader_symbol[: -len(self.suffix)]

        logger.debug("Symbol normalized: %s -> %s", mt5_symbol, ctrader_symbol)
        return ctrader_symbol

    def get_symbol_id(self, mt5_symbol: str) -> Optional[int]:
        """
        Get cTrader symbol ID for an MT5 symbol.

        Resolution order:
        1) Normalize name via mt5_to_ctrader_name().
        2) Look up in dynamic broker_symbol_map.
        3) If missing: return None (strict mode) and log a warning.
        """
        ctrader_symbol = self.mt5_to_ctrader_name(mt5_symbol)
        key = ctrader_symbol.upper()

        if not self.broker_symbol_map:
            logger.warning(
                "broker_symbol_map is empty; cannot resolve symbolId for %s (normalized: %s). "
                "Ensure CTraderClient loaded symbols first.",
                mt5_symbol,
                ctrader_symbol,
            )
            return None

        symbol_id = self.broker_symbol_map.get(key)
        if symbol_id is not None:
            logger.debug(
                "Symbol ID resolved via broker map: %s -> %s -> %s",
                mt5_symbol,
                ctrader_symbol,
                symbol_id,
            )
            return int(symbol_id)

        logger.warning(
            "No symbolId found in broker map for %s (normalized: %s). "
            "Add mapping in custom_symbols (accounts_config.ini) or ensure the symbol exists on the cTrader account.",
            mt5_symbol,
            ctrader_symbol,
        )

        if self.strict:
            return None

        # Non-strict mode: still return None (no unsafe fallbacks).
        return None

    def lots_to_units(self, lots: float, symbol: str = None) -> int:
        """
        Convert MT5 lot size to cTrader volume units (simple heuristic).

        Note: This is not reliable for all instruments/brokers; prefer using
        symbol metadata and your dedicated cTrader volume conversion functions.
        """
        if symbol and any(metal in symbol.upper() for metal in ["XAU", "XAG", "GOLD", "SILVER"]):
            units = int(lots * 100)  # Metals: 1 lot = 100 units (common, but broker dependent)
            logger.debug("Metal volume conversion: %s lots -> %s units", lots, units)
        else:
            units = int(lots * 100000)  # Forex: 1 lot = 100,000 units (common)
            logger.debug("Forex volume conversion: %s lots -> %s units", lots, units)

        return units
