"""Configuration Loader for Multi-Account Trading

Loads configuration for multiple cTrader accounts.
- Credentials loaded from .env file (private, never commit)
- Trading settings loaded from accounts_config.ini (public, safe to commit)
"""
import configparser
import json
import logging
import os
from typing import Dict, List, Optional, Set
from dataclasses import dataclass
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass
class AccountConfig:
    """Configuration for a single cTrader account."""
    name: str
    enabled: bool
    account_id: int
    client_id: str
    client_secret: str
    access_token: str
    environment: str  # "demo" or "live"

    # Symbol mapping
    symbol_prefix: str
    symbol_suffix: str
    custom_symbols: Dict[str, str]

    # Trading settings
    lot_multiplier: float
    min_lot_size: float
    max_lot_size: float
    copy_sl: bool
    copy_tp: bool

    # Risk sizing (NEW)
    risk_mode: str  # SOURCE_VOLUME | FIXED_LOT | PERCENT_EQUITY | FIXED_USD
    reject_if_no_sl: bool
    fixed_lot: float
    source_volume_fallback: bool
    fixed_usd_risk: float
    risk_percent: float
    risk_reference: str  # EQUITY | BALANCE

    # Risk management
    max_daily_trades: int
    max_concurrent_positions: int

    # Filtering
    magic_numbers: Optional[Set[int]]
    allowed_symbols: Optional[Set[str]]
    blocked_symbols: Set[str]

    # Runtime tracking
    daily_trade_count: int = 0
    current_positions: int = 0


class MultiAccountConfig:
    """Multi-account configuration manager."""

    def __init__(self, config_file: str = "accounts_config.ini"):
        """Load configuration from INI file.

        Args:
            config_file: Path to configuration file
        """
        load_dotenv()

        self.accounts: Dict[str, AccountConfig] = {}
        self.config = configparser.ConfigParser()

        if not os.path.exists(config_file):
            logger.error(f"Config file not found: {config_file}")
            raise FileNotFoundError(f"Please create {config_file}")

        self.config.read(config_file)
        logger.info(f"Loaded configuration from {config_file}")

        self._load_accounts()

        enabled_count = sum(1 for acc in self.accounts.values() if acc.enabled)
        logger.info(f"Loaded {len(self.accounts)} accounts, {enabled_count} enabled")

    def _load_accounts(self):
        """Load all account configurations."""
        for section in self.config.sections():
            if not section.startswith("Account_"):
                logger.warning(f"Skipping non-account section: {section}")
                continue

            try:
                account = self._load_account(section)
                self.accounts[account.name] = account

                if account.enabled:
                    logger.info(
                        f"✓ Loaded account: {account.name} "
                        f"(ID: {account.account_id}, {account.environment})"
                    )
                else:
                    logger.info(f"○ Loaded account: {account.name} (DISABLED)")

            except Exception as e:
                logger.error(f"Failed to load account {section}: {e}", exc_info=True)

    def _load_account(self, section: str) -> AccountConfig:
        """Load a single account configuration.

        Credentials are loaded from environment variables (.env file).
        Trading settings are loaded from accounts_config.ini.
        """
        account_name = section.replace("Account_", "").upper()

        # Load credentials from environment variables
        account_id_key = f"ACCOUNT_{account_name}_ACCOUNT_ID"
        client_id_key = f"ACCOUNT_{account_name}_CLIENT_ID"
        client_secret_key = f"ACCOUNT_{account_name}_CLIENT_SECRET"
        access_token_key = f"ACCOUNT_{account_name}_ACCESS_TOKEN"

        account_id = int(os.getenv(account_id_key, "0"))
        client_id = os.getenv(client_id_key, "")
        client_secret = os.getenv(client_secret_key, "")
        access_token = os.getenv(access_token_key, "")

        if account_id == 0 or not client_id or not client_secret:
            logger.warning(
                f"{section}: Missing credentials in .env file "
                f"(keys: {account_id_key}, {client_id_key}, {client_secret_key})"
            )

        # Parse custom symbols JSON
        custom_symbols_str = self.config.get(section, "custom_symbols", fallback="{}")
        try:
            custom_symbols = json.loads(custom_symbols_str)
        except json.JSONDecodeError:
            logger.warning(f"{section}: Invalid custom_symbols JSON, using empty")
            custom_symbols = {}

        # Parse magic numbers
        magic_str = self.config.get(section, "magic_numbers", fallback="")
        magic_numbers = None
        if magic_str.strip():
            try:
                magic_numbers = {int(m.strip()) for m in magic_str.split(",") if m.strip()}
            except ValueError:
                logger.warning(f"{section}: Invalid magic_numbers format")

        # Parse allowed symbols
        allowed_str = self.config.get(section, "allowed_symbols", fallback="")
        allowed_symbols = None
        if allowed_str.strip():
            allowed_symbols = {s.strip().upper() for s in allowed_str.split(",") if s.strip()}

        # Parse blocked symbols
        blocked_str = self.config.get(section, "blocked_symbols", fallback="")
        blocked_symbols = set()
        if blocked_str.strip():
            blocked_symbols = {s.strip().upper() for s in blocked_str.split(",") if s.strip()}

        # NEW: risk sizing config
        risk_mode = self.config.get(section, "risk_mode", fallback="SOURCE_VOLUME").strip().upper()
        reject_if_no_sl = self.config.getboolean(section, "reject_if_no_sl", fallback=False)
        fixed_lot = self.config.getfloat(section, "fixed_lot", fallback=0.0)
        source_volume_fallback = self.config.getboolean(section, "source_volume_fallback", fallback=True)
        fixed_usd_risk = self.config.getfloat(section, "fixed_usd_risk", fallback=0.0)
        risk_percent = self.config.getfloat(section, "risk_percent", fallback=0.0)
        risk_reference = self.config.get(section, "risk_reference", fallback="EQUITY").strip().upper()

        # sanitize
        if risk_reference not in ("EQUITY", "BALANCE"):
            logger.warning(f"{section}: Invalid risk_reference={risk_reference}, defaulting to EQUITY")
            risk_reference = "EQUITY"

        if risk_mode not in ("SOURCE_VOLUME", "FIXED_LOT", "PERCENT_EQUITY", "FIXED_USD"):
            logger.warning(f"{section}: Invalid risk_mode={risk_mode}, defaulting to SOURCE_VOLUME")
            risk_mode = "SOURCE_VOLUME"

        return AccountConfig(
            name=section.replace("Account_", ""),
            enabled=self.config.getboolean(section, "enabled", fallback=True),
            account_id=account_id,
            client_id=client_id,
            client_secret=client_secret,
            access_token=access_token,
            environment=self.config.get(section, "environment", fallback="demo"),
            symbol_prefix=self.config.get(section, "symbol_prefix", fallback=""),
            symbol_suffix=self.config.get(section, "symbol_suffix", fallback=""),
            custom_symbols=custom_symbols,
            lot_multiplier=self.config.getfloat(section, "lot_multiplier", fallback=1.0),
            min_lot_size=self.config.getfloat(section, "min_lot_size", fallback=0.01),
            max_lot_size=self.config.getfloat(section, "max_lot_size", fallback=100.0),
            copy_sl=self.config.getboolean(section, "copy_sl", fallback=True),
            copy_tp=self.config.getboolean(section, "copy_tp", fallback=True),
            risk_mode=risk_mode,
            reject_if_no_sl=reject_if_no_sl,
            fixed_lot=fixed_lot,
            source_volume_fallback=source_volume_fallback,
            fixed_usd_risk=fixed_usd_risk,
            risk_percent=risk_percent,
            risk_reference=risk_reference,
            max_daily_trades=self.config.getint(section, "max_daily_trades", fallback=1000),
            max_concurrent_positions=self.config.getint(section, "max_concurrent_positions", fallback=100),
            magic_numbers=magic_numbers,
            allowed_symbols=allowed_symbols,
            blocked_symbols=blocked_symbols,
        )

    def get_enabled_accounts(self) -> List[AccountConfig]:
        """Get list of enabled accounts."""
        return [acc for acc in self.accounts.values() if acc.enabled]

    def should_copy_trade(self, account: AccountConfig, symbol: str, magic: int, lots: float) -> tuple[bool, str]:
        """Check if a trade should be copied to this account.

        Args:
            account: Account configuration
            symbol: MT5 symbol name
            magic: Magic number
            lots: Lot size

        Returns:
            (should_copy, reason) tuple
        """
        symbol_upper = symbol.upper()

        if account.daily_trade_count >= account.max_daily_trades:
            return False, f"Daily trade limit reached ({account.max_daily_trades})"

        if account.current_positions >= account.max_concurrent_positions:
            return False, f"Max concurrent positions reached ({account.max_concurrent_positions})"

        if account.magic_numbers is not None and magic not in account.magic_numbers:
            return False, f"Magic number {magic} not in allowed list"

        if symbol_upper in account.blocked_symbols:
            return False, f"Symbol {symbol} is blocked"

        if account.allowed_symbols is not None and symbol_upper not in account.allowed_symbols:
            return False, f"Symbol {symbol} not in allowed list"

        if lots < account.min_lot_size:
            return False, f"Lot size {lots} below minimum {account.min_lot_size}"

        return True, "OK"


_config_instance: Optional[MultiAccountConfig] = None


def get_multi_account_config() -> MultiAccountConfig:
    """Get or create global multi-account config instance."""
    global _config_instance
    if _config_instance is None:
        _config_instance = MultiAccountConfig()
    return _config_instance
