"""Account Manager for Multiple cTrader Connections

Manages multiple cTrader client connections for different accounts.
"""
import logging
from typing import Dict
from ctrader_client import CTraderClient
from config_loader import AccountConfig

logger = logging.getLogger(__name__)


class AccountManager:
    """Manages multiple cTrader client connections."""
    
    def __init__(self):
        """Initialize account manager."""
        self.clients: Dict[str, CTraderClient] = {}
        self.configs: Dict[str, AccountConfig] = {}
    
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
        
        # Override client credentials with account-specific values
        client.client_id = account.client_id
        client.client_secret = account.client_secret
        
        # Set account credentials BEFORE connecting (prevents reconnection loop)
        client.set_account_credentials(
            account_id=account.account_id,
            access_token=account.access_token or ""
        )
        
        # Store references
        self.clients[account.name] = client
        self.configs[account.name] = account
        
        # Connect the client (will auto-authorize account)
        def on_connected():
            logger.info(f"✓ Account {account.name} connected and authenticated")
        
        client.connect(on_connect=on_connected)
    
    def get_client(self, account_name: str) -> CTraderClient:
        """Get cTrader client for an account.
        
        Args:
            account_name: Account name
        
        Returns:
            CTrader client instance
        """
        return self.clients.get(account_name)
    
    def get_config(self, account_name: str) -> AccountConfig:
        """Get account configuration.
        
        Args:
            account_name: Account name
        
        Returns:
            Account configuration
        """
        return self.configs.get(account_name)
    
    def get_all_accounts(self) -> Dict[str, tuple[CTraderClient, AccountConfig]]:
        """Get all active accounts.
        
        Returns:
            Dict mapping account name to (client, config) tuple
        """
        return {name: (self.clients[name], self.configs[name]) 
                for name in self.clients.keys()}


# Global instance
_manager_instance = None


def get_account_manager() -> AccountManager:
    """Get or create global account manager instance."""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = AccountManager()
    return _manager_instance
