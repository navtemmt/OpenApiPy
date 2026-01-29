"""Symbol Mapping Utilities for MT5 to cTrader

Handles symbol name transformation and mapping between platforms.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class SymbolMapper:
    """Maps MT5 symbols to cTrader symbols with support for prefixes, suffixes, and custom mappings."""
    
    # Hardcoded common forex symbol to cTrader symbol ID mapping
    # These are standard symbol IDs for most cTrader brokers
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
    
    def __init__(self, prefix: str = "", suffix: str = "", custom_map: dict = None):
        """Initialize symbol mapper.
        
        Args:
            prefix: Prefix to strip from MT5 symbols
            suffix: Suffix to strip from MT5 symbols
            custom_map: Custom symbol name mappings
        """
        self.prefix = prefix
        self.suffix = suffix
        self.custom_map = custom_map or {}
        
        logger.debug(f"Symbol mapper initialized: prefix='{self.prefix}', suffix='{self.suffix}', custom_map={self.custom_map}")
    
    def mt5_to_ctrader_name(self, mt5_symbol: str) -> str:
        """Convert MT5 symbol name to cTrader symbol name.
        
        Process:
        1. Check custom mapping first
        2. Strip prefix/suffix if configured
        3. Return normalized symbol name
        
        Args:
            mt5_symbol: Symbol name from MT5 (e.g., "mEURUSD.raw")
        
        Returns:
            cTrader symbol name (e.g., "EURUSD")
        """
        # Check custom mapping first
        if mt5_symbol.upper() in self.custom_map:
            ctrader_symbol = self.custom_map[mt5_symbol.upper()]
            logger.debug(f"Symbol mapped via custom map: {mt5_symbol} -> {ctrader_symbol}")
            return ctrader_symbol
        
        # Strip prefix and suffix
        ctrader_symbol = mt5_symbol
        
        if self.prefix and ctrader_symbol.startswith(self.prefix):
            ctrader_symbol = ctrader_symbol[len(self.prefix):]
        
        if self.suffix and ctrader_symbol.endswith(self.suffix):
            ctrader_symbol = ctrader_symbol[:-len(self.suffix)]
        
        logger.debug(f"Symbol normalized: {mt5_symbol} -> {ctrader_symbol}")
        return ctrader_symbol
    
    def get_symbol_id(self, mt5_symbol: str) -> Optional[int]:
        """Get cTrader symbol ID for an MT5 symbol.
        
        Args:
            mt5_symbol: Symbol name from MT5
        
        Returns:
            cTrader symbol ID, or None if not found
        """
        ctrader_symbol = self.mt5_to_ctrader_name(mt5_symbol)
        symbol_id = self.COMMON_SYMBOL_IDS.get(ctrader_symbol.upper())
        
        if symbol_id is None:
            logger.warning(f"No symbol ID mapping found for {mt5_symbol} (normalized: {ctrader_symbol})")
            logger.warning(f"Add to custom_symbols in accounts_config.ini or update COMMON_SYMBOL_IDS")
        else:
            logger.debug(f"Symbol ID resolved: {mt5_symbol} -> {ctrader_symbol} -> {symbol_id}")
        
        return symbol_id
    
    def lots_to_units(self, lots: float, symbol: str = None) -> int:
        """Convert MT5 lot size to cTrader volume units.
        
        For forex: 1 lot = 100,000 units
        For metals (gold/silver): 1 lot = 100 units (typically)
        
        Args:
            lots: MT5 lot size (e.g., 0.01)
            symbol: Optional symbol name for special handling
        
        Returns:
            Volume in units
        """
        # Check if it's a metal symbol (gold/silver typically use different unit sizes)
        if symbol and any(metal in symbol.upper() for metal in ["XAU", "XAG", "GOLD", "SILVER"]):
            units = int(lots * 100)  # Metals: 1 lot = 100 units
            logger.debug(f"Metal volume conversion: {lots} lots -> {units} units")
        else:
            units = int(lots * 100000)  # Forex: 1 lot = 100,000 units
            logger.debug(f"Forex volume conversion: {lots} lots -> {units} units")
        
        return units
