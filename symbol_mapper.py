"""Symbol Mapping Utilities for MT5 to cTrader

Handles symbol name transformation and mapping between platforms.
"""
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class SymbolMapper:
    """Maps MT5 symbols to cTrader symbols with support for prefixes, suffixes, and custom mappings."""

    # Hardcoded common forex symbol to cTrader symbol ID mapping
    # These are fallback IDs; dynamic broker_symbol_map should be preferred
    COMMON_SYMBOL_IDS = {
        "EURUSD": 1,
        "GBPUSD": 2,
        "USDJPY": 3,
        "USDCHF": 4,
        "AUDUSD": 5,
        "USDCAD": 6,
        "NZDUSD": 7,
        "EURGBP": 8,
        "EURJPY": 9,
        "GBPJPY": 10,
        "EURCHF": 11,
        "AUDJPY": 12,
        "EURAUD": 13,
        "EURCAD": 14,
        "GBPCHF": 15,
        "GBPAUD": 16,
        "GBPCAD": 17,
        "AUDCAD": 18,
        "AUDCHF": 19,
        "AUDNZD": 20,
        "CADCHF": 21,
        "CADJPY": 22,
        "CHFJPY": 23,
        "NZDCAD": 24,
        "NZDCHF": 25,
        "NZDJPY": 26,
        "XAUUSD": 27,  # Gold
        "XAGUSD": 28,  # Silver
        # Add more as needed
    }

    def __init__(
        self,
        prefix: str = "",
        suffix: str = "",
        custom_map: Optional[Dict[str, str]] = None,
        broker_symbol_map: Optional[Dict[str, int]] = None,
    ):
        """Initialize symbol mapper.

        Args:
            prefix: Prefix to strip from MT5 symbols (when normalizing).
            suffix: Suffix to strip from MT5 symbols (when normalizing).
            custom_map: Custom symbol name mappings, e.g. {"XAUUSD": "GOLD", "XAUUSD.m": "XAUUSD"}.
            broker_symbol_map: Dynamic cTrader symbol name -> symbolId map from CTraderClient.
        """
        self.prefix = prefix or ""
        self.suffix = suffix or ""
        # normalize custom_map keys/values to upper-case
        self.custom_map: Dict[str, str] = {
            k.upper(): v.upper() for k, v in (custom_map or {}).items()
        }
        # normalize broker symbol map to upper-case keys
        self.broker_symbol_map: Dict[str, int] = {
            k.upper(): v for k, v in (broker_symbol_map or {}).items()
        }

        logger.info(
            "Symbol mapper initialized: prefix='%s', suffix='%s', custom_map=%s, "
            "broker_symbol_map_size=%d",
            self.prefix,
            self.suffix,
            self.custom_map,
            len(self.broker_symbol_map),
        )

    def mt5_to_ctrader_name(self, mt5_symbol: str) -> str:
        """Convert MT5 symbol name to cTrader symbol name (name only).

        Process:
        1. Check custom mapping first (e.g. XAUUSD -> GOLD, XAUUSD.m -> XAUUSD).
        2. Strip prefix/suffix if configured.
        3. Return normalized symbol name (upper-case).
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
        """Get cTrader symbol ID for an MT5 symbol.

        Resolution order:
        1. Normalize name via mt5_to_ctrader_name().
        2. Look up in dynamic broker_symbol_map if available.
        3. Fallback to COMMON_SYMBOL_IDS if not found in broker_symbol_map.
        """
        ctrader_symbol = self.mt5_to_ctrader_name(mt5_symbol)
        key = ctrader_symbol.upper()

        symbol_id = None

        # 1) dynamic broker map first
        if self.broker_symbol_map:
            symbol_id = self.broker_symbol_map.get(key)
            if symbol_id is not None:
                logger.debug(
                    "Symbol ID resolved via broker map: %s -> %s -> %s",
                    mt5_symbol,
                    ctrader_symbol,
                    symbol_id,
                )
                return symbol_id

        # 2) fallback to hardcoded common map
        symbol_id = self.COMMON_SYMBOL_IDS.get(key)

        if symbol_id is None:
            logger.warning(
                "No symbol ID mapping found for %s (normalized: %s)",
                mt5_symbol,
                ctrader_symbol,
            )
            logger.warning(
                "Add to custom_symbols in accounts_config.ini, ensure broker_symbol_map "
                "is populated, or update COMMON_SYMBOL_IDS"
            )
        else:
            logger.debug(
                "Symbol ID resolved via COMMON_SYMBOL_IDS: %s -> %s -> %s",
                mt5_symbol,
                ctrader_symbol,
                symbol_id,
            )

        return symbol_id

    def lots_to_units(self, lots: float, symbol: str = None) -> int:
        """Convert MT5 lot size to cTrader volume units.

        For forex: 1 lot = 100,000 units.
        For metals (gold/silver): 1 lot = 100 units (typically).
        """
        if symbol and any(
            metal in symbol.upper() for metal in ["XAU", "XAG", "GOLD", "SILVER"]
        ):
            units = int(lots * 100)  # Metals: 1 lot = 100 units
            logger.debug("Metal volume conversion: %s lots -> %s units", lots, units)
        else:
            units = int(lots * 100000)  # Forex: 1 lot = 100,000 units
            logger.debug("Forex volume conversion: %s lots -> %s units", lots, units)

        return units
