"""Configuration Loader for Trading Settings

Loads trading configuration from trading_config.ini file.
"""
import configparser
import logging
import os
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class TradingConfig:
    """Trading configuration manager."""
    
    def __init__(self, config_file: str = "trading_config.ini"):
        """Load configuration from INI file.
        
        Args:
            config_file: Path to configuration file
        """
        self.config = configparser.ConfigParser()
        
        if not os.path.exists(config_file):
            logger.warning(f"Config file not found: {config_file}. Using defaults.")
            self._set_defaults()
        else:
            self.config.read(config_file)
            logger.info(f"Loaded configuration from {config_file}")
        
        # Parse configuration sections
        self._load_symbol_mapping()
        self._load_trading_settings()
        self._load_filtering()
    
    def _set_defaults(self):
        """Set default configuration values."""
        self.config['SymbolMapping'] = {
            'prefix': '',
            'suffix': ''
        }
        self.config['CustomSymbols'] = {}
        self.config['Trading'] = {
            'lot_multiplier': '1.0',
            'min_lot_size': '0.01',
            'max_lot_size': '100.0',
            'copy_sl': 'true',
            'copy_tp': 'true'
        }
        self.config['Filtering'] = {
            'magic_numbers': '',
            'allowed_symbols': '',
            'blocked_symbols': ''
        }
    
    def _load_symbol_mapping(self):
        """Load symbol mapping settings."""
        self.symbol_prefix = self.config.get('SymbolMapping', 'prefix', fallback='')
        self.symbol_suffix = self.config.get('SymbolMapping', 'suffix', fallback='')
        
        # Load custom symbol mappings
        self.custom_symbols: Dict[str, str] = {}
        if 'CustomSymbols' in self.config:
            for mt5_symbol, ctrader_symbol in self.config['CustomSymbols'].items():
                self.custom_symbols[mt5_symbol.upper()] = ctrader_symbol.strip()
        
        logger.info(f"Symbol mapping: prefix='{self.symbol_prefix}', suffix='{self.symbol_suffix}'")
        if self.custom_symbols:
            logger.info(f"Custom symbol mappings: {self.custom_symbols}")
    
    def _load_trading_settings(self):
        """Load trading behavior settings."""
        self.lot_multiplier = self.config.getfloat('Trading', 'lot_multiplier', fallback=1.0)
        self.min_lot_size = self.config.getfloat('Trading', 'min_lot_size', fallback=0.01)
        self.max_lot_size = self.config.getfloat('Trading', 'max_lot_size', fallback=100.0)
        self.copy_sl = self.config.getboolean('Trading', 'copy_sl', fallback=True)
        self.copy_tp = self.config.getboolean('Trading', 'copy_tp', fallback=True)
        
        logger.info(f"Trading settings: multiplier={self.lot_multiplier}, min={self.min_lot_size}, max={self.max_lot_size}")
        logger.info(f"Copy SL={self.copy_sl}, Copy TP={self.copy_tp}")
    
    def _load_filtering(self):
        """Load trade filtering settings."""
        # Parse magic numbers
        magic_str = self.config.get('Filtering', 'magic_numbers', fallback='')
        self.magic_numbers: Optional[Set[int]] = None
        if magic_str.strip():
            try:
                self.magic_numbers = {int(m.strip()) for m in magic_str.split(',') if m.strip()}
                logger.info(f"Magic number filter: {self.magic_numbers}")
            except ValueError:
                logger.error(f"Invalid magic_numbers format: {magic_str}")
        
        # Parse allowed symbols
        allowed_str = self.config.get('Filtering', 'allowed_symbols', fallback='')
        self.allowed_symbols: Optional[Set[str]] = None
        if allowed_str.strip():
            self.allowed_symbols = {s.strip().upper() for s in allowed_str.split(',') if s.strip()}
            logger.info(f"Allowed symbols: {self.allowed_symbols}")
        
        # Parse blocked symbols
        blocked_str = self.config.get('Filtering', 'blocked_symbols', fallback='')
        self.blocked_symbols: Set[str] = set()
        if blocked_str.strip():
            self.blocked_symbols = {s.strip().upper() for s in blocked_str.split(',') if s.strip()}
            logger.info(f"Blocked symbols: {self.blocked_symbols}")
    
    def should_copy_trade(self, symbol: str, magic: int, lots: float) -> tuple[bool, str]:
        """Check if a trade should be copied based on filters.
        
        Args:
            symbol: MT5 symbol name
            magic: Magic number
            lots: Lot size
        
        Returns:
            (should_copy, reason) tuple
        """
        symbol_upper = symbol.upper()
        
        # Check magic number filter
        if self.magic_numbers is not None and magic not in self.magic_numbers:
            return False, f"Magic number {magic} not in allowed list"
        
        # Check blocked symbols
        if symbol_upper in self.blocked_symbols:
            return False, f"Symbol {symbol} is blocked"
        
        # Check allowed symbols
        if self.allowed_symbols is not None and symbol_upper not in self.allowed_symbols:
            return False, f"Symbol {symbol} not in allowed list"
        
        # Check lot size limits
        if lots < self.min_lot_size:
            return False, f"Lot size {lots} below minimum {self.min_lot_size}"
        
        return True, "OK"
    
    def adjust_lot_size(self, lots: float) -> float:
        """Apply lot size multiplier and limits.
        
        Args:
            lots: Original lot size
        
        Returns:
            Adjusted lot size
        """
        adjusted = lots * self.lot_multiplier
        adjusted = max(self.min_lot_size, min(adjusted, self.max_lot_size))
        return adjusted
    
    def get_custom_symbol(self, mt5_symbol: str) -> Optional[str]:
        """Get custom symbol mapping if exists.
        
        Args:
            mt5_symbol: MT5 symbol name
        
        Returns:
            Custom cTrader symbol name, or None if not mapped
        """
        return self.custom_symbols.get(mt5_symbol.upper())


# Global instance
_config_instance: Optional[TradingConfig] = None


def get_trading_config() -> TradingConfig:
    """Get or create global trading config instance."""
    global _config_instance
    if _config_instance is None:
        _config_instance = TradingConfig()
    return _config_instance
