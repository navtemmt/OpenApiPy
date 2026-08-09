#!/usr/bin/env python3
"""Configuration Loader for Multi-Account Trading

Loads configuration for multiple cTrader accounts.
- Credentials loaded from .env file (private, never commit)
- Trading settings loaded from accounts_config.ini (public, safe to commit)

Routing notes:
- `magic_numbers` is the primary routing/filtering field.
- `route_magic_number` is supported as a backward-compatible single-value alias.
- `receive_all_signals=true` means the account is eligible to receive any incoming
  signal, subject to the normal symbol/risk filters. If `magic_numbers` is also set,
  it still acts as a filter list for that account.
"""

import configparser
import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

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
    refresh_token: str
    token_state_file: str
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

    # Risk sizing
    risk_mode: str  # SOURCE_VOLUME | FIXED_LOT | PERCENT_EQUITY | FIXED_USD
    reject_if_no_sl: bool
    fixed_lot: float
    source_volume_fallback: bool
    fixed_usd_risk: float
    risk_percent: float
    risk_reference: str  # EQUITY | BALANCE

    # Startup sync for missed live market positions
    startup_sync_market_orders: bool
    startup_market_recovery_mode: str  # market | market_or_pending | skip
    startup_market_max_distance_pips: float
    startup_pending_expiration_ms: int

    # Risk management
    max_daily_trades: int
    max_concurrent_positions: int

    # Routing / filtering
    receive_all_signals: bool
    route_magic_number: Optional[int]
    magic_numbers: Optional[Set[int]]
    allowed_symbols: Optional[Set[str]]
    blocked_symbols: Set[str]

    # Runtime tracking
    daily_trade_count: int = 0
    current_positions: int = 0


class MultiAccountConfig:
    """Multi-account configuration manager."""

    def __init__(self, config_file: str = "accounts_config.ini"):
        load_dotenv()

        self.accounts: Dict[str, AccountConfig] = {}
        self.config = configparser.ConfigParser(
            inline_comment_prefixes=(";", "#")
        )

        if not os.path.exists(config_file):
            logger.error(f"Config file not found: {config_file}")
            raise FileNotFoundError(f"Please create {config_file}")

        self.config.read(config_file)
        logger.info(f"Loaded configuration from {config_file}")

        self._load_accounts()
        self._validate_route_magic_numbers()

        enabled_count = sum(1 for acc in self.accounts.values() if acc.enabled)
        logger.info(f"Loaded {len(self.accounts)} accounts, {enabled_count} enabled")

    def _default_token_state_file(self, account_name: str) -> str:
        token_dir = os.getenv("CTRADER_TOKEN_STATE_DIR", "runtime_tokens")
        return os.path.join(token_dir, f"{account_name}.json")

    def _parse_int_set(self, raw: str, *, section: str, field_name: str) -> Optional[Set[int]]:
        value = (raw or "").strip()
        if not value:
            return None

        try:
            parsed = {int(part.strip()) for part in value.split(",") if part.strip()}
            return parsed if parsed else None
        except ValueError:
            logger.warning(f"{section}: Invalid {field_name} format")
            return None

    def _parse_symbol_set(self, raw: str) -> Optional[Set[str]]:
        value = (raw or "").strip()
        if not value:
            return None
        parsed = {s.strip().upper() for s in value.split(",") if s.strip()}
        return parsed if parsed else None

    def _load_accounts(self) -> None:
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
                        f"(ID: {account.account_id}, {account.environment}, "
                        f"receive_all_signals={account.receive_all_signals}, "
                        f"route_magic_number={account.route_magic_number}, "
                        f"magic_numbers={sorted(account.magic_numbers) if account.magic_numbers else None})"
                    )
                else:
                    logger.info(f"○ Loaded account: {account.name} (DISABLED)")

            except Exception as e:
                logger.error(f"Failed to load account {section}: {e}", exc_info=True)

    def _load_account(self, section: str) -> AccountConfig:
        account_name_upper = section.replace("Account_", "").upper()
        account_name = section.replace("Account_", "")

        account_id_key = f"ACCOUNT_{account_name_upper}_ACCOUNT_ID"
        client_id_key = f"ACCOUNT_{account_name_upper}_CLIENT_ID"
        client_secret_key = f"ACCOUNT_{account_name_upper}_CLIENT_SECRET"
        access_token_key = f"ACCOUNT_{account_name_upper}_ACCESS_TOKEN"
        refresh_token_key = f"ACCOUNT_{account_name_upper}_REFRESH_TOKEN"
        token_state_file_key = f"ACCOUNT_{account_name_upper}_TOKEN_STATE_FILE"

        account_id = int(os.getenv(account_id_key, "0"))
        client_id = os.getenv(client_id_key, "")
        client_secret = os.getenv(client_secret_key, "")
        access_token = os.getenv(access_token_key, "")
        refresh_token = os.getenv(refresh_token_key, "")
        token_state_file = (
            os.getenv(token_state_file_key, "").strip()
            or self._default_token_state_file(account_name)
        )

        if account_id == 0 or not client_id or not client_secret:
            logger.warning(
                f"{section}: Missing credentials in .env file "
                f"(keys: {account_id_key}, {client_id_key}, {client_secret_key})"
            )

        custom_symbols_str = self.config.get(section, "custom_symbols", fallback="{}")
        try:
            custom_symbols = json.loads(custom_symbols_str)
            if not isinstance(custom_symbols, dict):
                logger.warning(f"{section}: custom_symbols must be a JSON object, using empty")
                custom_symbols = {}
        except json.JSONDecodeError:
            logger.warning(f"{section}: Invalid custom_symbols JSON, using empty")
            custom_symbols = {}

        receive_all_signals = self.config.getboolean(
            section, "receive_all_signals", fallback=False
        )

        route_magic_number: Optional[int] = None
        route_magic_str = self.config.get(
            section, "route_magic_number", fallback=""
        ).strip()
        if route_magic_str:
            try:
                route_magic_number = int(route_magic_str)
            except ValueError:
                logger.warning(
                    f"{section}: Invalid route_magic_number={route_magic_str}, ignoring"
                )

        magic_numbers = self._parse_int_set(
            self.config.get(section, "magic_numbers", fallback=""),
            section=section,
            field_name="magic_numbers",
        )

        if route_magic_number is not None:
            if magic_numbers is None:
                magic_numbers = {route_magic_number}
            else:
                magic_numbers.add(route_magic_number)

        allowed_symbols = self._parse_symbol_set(
            self.config.get(section, "allowed_symbols", fallback="")
        )

        blocked_symbols = self._parse_symbol_set(
            self.config.get(section, "blocked_symbols", fallback="")
        ) or set()

        risk_mode = self.config.get(
            section, "risk_mode", fallback="SOURCE_VOLUME"
        ).strip().upper()
        reject_if_no_sl = self.config.getboolean(
            section, "reject_if_no_sl", fallback=False
        )
        fixed_lot = self.config.getfloat(section, "fixed_lot", fallback=0.0)
        source_volume_fallback = self.config.getboolean(
            section, "source_volume_fallback", fallback=True
        )
        fixed_usd_risk = self.config.getfloat(
            section, "fixed_usd_risk", fallback=0.0
        )
        risk_percent = self.config.getfloat(section, "risk_percent", fallback=0.0)
        risk_reference = self.config.get(
            section, "risk_reference", fallback="EQUITY"
        ).strip().upper()

        startup_sync_market_orders = self.config.getboolean(
            section, "startup_sync_market_orders", fallback=False
        )
        startup_market_recovery_mode = self.config.get(
            section, "startup_market_recovery_mode", fallback="skip"
        ).strip().lower()
        startup_market_max_distance_pips = self.config.getfloat(
            section, "startup_market_max_distance_pips", fallback=10.0
        )
        startup_pending_expiration_ms = self.config.getint(
            section, "startup_pending_expiration_ms", fallback=0
        )

        if risk_reference not in ("EQUITY", "BALANCE"):
            logger.warning(
                f"{section}: Invalid risk_reference={risk_reference}, defaulting to EQUITY"
            )
            risk_reference = "EQUITY"

        if risk_mode not in ("SOURCE_VOLUME", "FIXED_LOT", "PERCENT_EQUITY", "FIXED_USD"):
            logger.warning(
                f"{section}: Invalid risk_mode={risk_mode}, defaulting to SOURCE_VOLUME"
            )
            risk_mode = "SOURCE_VOLUME"

        if startup_market_recovery_mode not in ("market", "market_or_pending", "skip"):
            logger.warning(
                f"{section}: Invalid startup_market_recovery_mode="
                f"{startup_market_recovery_mode}, defaulting to skip"
            )
            startup_market_recovery_mode = "skip"

        if startup_market_max_distance_pips < 0:
            logger.warning(
                f"{section}: Invalid startup_market_max_distance_pips="
                f"{startup_market_max_distance_pips}, defaulting to 10.0"
            )
            startup_market_max_distance_pips = 10.0

        if startup_pending_expiration_ms < 0:
            logger.warning(
                f"{section}: Invalid startup_pending_expiration_ms="
                f"{startup_pending_expiration_ms}, defaulting to 0"
            )
            startup_pending_expiration_ms = 0

        environment = self.config.get(section, "environment", fallback="demo").strip().lower()
        if environment not in ("demo", "live"):
            logger.warning(f"{section}: Invalid environment={environment}, defaulting to demo")
            environment = "demo"

        logger.info(
            "%s: token config loaded access_present=%s refresh_present=%s token_state_file=%s",
            section,
            bool(access_token),
            bool(refresh_token),
            token_state_file,
        )

        return AccountConfig(
            name=account_name,
            enabled=self.config.getboolean(section, "enabled", fallback=True),
            account_id=account_id,
            client_id=client_id,
            client_secret=client_secret,
            access_token=access_token,
            refresh_token=refresh_token,
            token_state_file=token_state_file,
            environment=environment,
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
            startup_sync_market_orders=startup_sync_market_orders,
            startup_market_recovery_mode=startup_market_recovery_mode,
            startup_market_max_distance_pips=startup_market_max_distance_pips,
            startup_pending_expiration_ms=startup_pending_expiration_ms,
            max_daily_trades=self.config.getint(section, "max_daily_trades", fallback=1000),
            max_concurrent_positions=self.config.getint(
                section, "max_concurrent_positions", fallback=100
            ),
            receive_all_signals=receive_all_signals,
            route_magic_number=route_magic_number,
            magic_numbers=magic_numbers,
            allowed_symbols=allowed_symbols,
            blocked_symbols=blocked_symbols,
        )

    def _validate_route_magic_numbers(self) -> None:
        seen: Dict[int, List[str]] = {}
        for account in self.get_enabled_accounts():
            if account.route_magic_number is None:
                continue
            seen.setdefault(account.route_magic_number, []).append(account.name)

        for route_magic, names in seen.items():
            if len(names) > 1:
                logger.info(
                    "route_magic_number=%s is shared by multiple accounts (fan-out): %s",
                    route_magic,
                    names,
                )

    def get_enabled_accounts(self) -> List[AccountConfig]:
        return [acc for acc in self.accounts.values() if acc.enabled]

    def get_accounts_by_magic(self, magic: int) -> List[AccountConfig]:
        """
        Fan-out routing:
        - Accounts with receive_all_signals=True are included, subject to magic filters.
        - Accounts with magic_numbers containing the magic are included.
        - route_magic_number remains a backward-compatible alias.
        - Result order follows config file load order.
        """
        matches: List[AccountConfig] = []

        for account in self.get_enabled_accounts():
            include = False

            if account.receive_all_signals:
                if account.magic_numbers is None or magic in account.magic_numbers:
                    include = True

            if account.magic_numbers is not None and magic in account.magic_numbers:
                include = True

            if (
                not include
                and account.route_magic_number is not None
                and account.route_magic_number == magic
            ):
                include = True

            if include:
                matches.append(account)

        return matches

    def get_account_by_magic(self, magic: int) -> Optional[AccountConfig]:
        """
        Backward-compatible single-match helper.
        Returns the first matching account according to get_accounts_by_magic().
        """
        matches = self.get_accounts_by_magic(magic)
        return matches[0] if matches else None

    def should_copy_trade(
        self,
        account: AccountConfig,
        symbol: str,
        magic: int,
        lots: float,
    ) -> tuple[bool, str]:
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

        if lots > account.max_lot_size:
            return False, f"Lot size {lots} above maximum {account.max_lot_size}"

        return True, "OK"


_config_instance: Optional["MultiAccountConfig"] = None


def get_multi_account_config() -> MultiAccountConfig:
    global _config_instance
    if _config_instance is None:
        _config_instance = MultiAccountConfig()
    return _config_instance
