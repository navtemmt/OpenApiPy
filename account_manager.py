"""Account Manager for Multiple cTrader Connections

Manages multiple cTrader client connections for different accounts.
"""
import logging
from typing import Dict, Optional

from ctrader_client import CTraderClient
from config_loader import AccountConfig
from ctrader_open_api import Protobuf

logger = logging.getLogger(__name__)


class AccountManager:
    """Manages multiple cTrader client connections."""
    
    def __init__(self):
        """Initialize account manager."""
        self.clients: Dict[str, CTraderClient] = {}
        self.configs: Dict[str, AccountConfig] = {}
        # Per-account mapping: MT5 ticket -> cTrader positionId
        self.position_maps: Dict[str, Dict[int, int]] = {}
    
    def add_account(self, account: AccountConfig):
        """Add and connect a cTrader account.
        
        Args:
            account: Account configuration
        """
        if not account.enabled:
            logger.info(f"Skipping disabled account: {account.name}")
            return
        
        logger.info(f"Initializing account: {account.name}")
        
        # Create cTrader client for this environment
        client = CTraderClient(env=account.environment)
        
        # Override client credentials with account-specific values FIRST
        # (before setting account credentials to avoid clearing them)
        client.client_id = account.client_id
        client.client_secret = account.client_secret
        
        # Now set account credentials (account_id and access_token)
        client.set_account_credentials(
            account_id=account.account_id,
            access_token=account.access_token or ""
        )
        
        # Store references
        self.clients[account.name] = client
        self.configs[account.name] = account
        self.position_maps[account.name] = {}  # init empty map

        # Hook message callback (for future position tracking)
        def on_message(message, acc_name=account.name):
            logger.debug(f"[{acc_name}] raw cTrader message: {message!r}")
            # later: parse Protobuf.extract(message) and update position_maps

        client.set_message_callback(on_message)
        
        # Connect the client (will auto-authorize account)
        def on_connected():
            logger.info(f"✓ Account {account.name} connected and authenticated")
        
        client.connect(on_connect=on_connected)
    
    def get_client(self, account_name: str) -> CTraderClient:
        """Get cTrader client for an account."""
        return self.clients.get(account_name)
    
    def get_config(self, account_name: str) -> AccountConfig:
        """Get account configuration."""
        return self.configs.get(account_name)

    def get_position_id(self, account_name: str, mt5_ticket: int) -> Optional[int]:
        """Get cTrader positionId for an MT5 ticket on a given account."""
        pos_map = self.position_maps.get(account_name) or {}
        return pos_map.get(int(mt5_ticket))
    
    def get_all_accounts(self) -> Dict[str, tuple[CTraderClient, AccountConfig]]:
        """Get all active accounts."""
        return {
            name: (self.clients[name], self.configs[name])
            for name in self.clients.keys()
        }


# Global instance
_manager_instance = None


def get_account_manager() -> AccountManager:
    """Get or create global account manager instance."""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = AccountManager()
    return _manager_instance
