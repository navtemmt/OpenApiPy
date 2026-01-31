"""Account Manager for Multiple cTrader Connections

Manages multiple cTrader client connections for different accounts.
"""
import logging
from typing import Dict, Optional

from ctrader_client import CTraderClient
from config_loader import AccountConfig
from ctrader_open_api import Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAReconcileReq,
    ProtoOAReconcileRes,
    ProtoOAExecutionEvent,
)

logger = logging.getLogger(__name__)


class AccountManager:
    """Manages multiple cTrader client connections."""
    
    def __init__(self):
        """Initialize account manager."""
        self.clients: Dict[str, CTraderClient] = {}
        self.configs: Dict[str, AccountConfig] = {}
        # Per-account mapping: MT5 ticket -> cTrader positionId
        self.position_maps: Dict[str, Dict[int, int]] = {}
        # Per-account mapping: cTrader positionId -> volume (cents of units)
        self.position_volumes: Dict[str, Dict[int, int]] = {}
    
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
        self.position_maps[account.name] = {}     # init empty map
        self.position_volumes[account.name] = {}  # init empty volume map

        # Hook message callback (handles execution events + reconcile + position updates)
        def on_message(message, acc_name=account.name):
            try:
                extracted = Protobuf.extract(message)

                # DEBUG: log all execution events so we can see fills vs accepts
                if isinstance(extracted, ProtoOAExecutionEvent):
                    logger.info(f"[{acc_name}] RAW EXECUTION: {extracted}")

                # 1) Execution events: map ticket -> positionId and try to store volume
                if isinstance(extracted, ProtoOAExecutionEvent):
                    position = getattr(extracted, "position", None)
                    if position is not None:
                        position_id = getattr(position, "positionId", 0)
                        trade_data = getattr(position, "tradeData", None)
                        label = getattr(trade_data, "label", "") if trade_data else ""
                        volume = getattr(position, "volume", 0)

                        if position_id:
                            # Map MT5 ticket -> positionId as soon as we know it
                            if isinstance(label, str) and label.startswith("MT5_"):
                                mt5_ticket_str = label.split("_", 1)[1]
                                try:
                                    mt5_ticket = int(mt5_ticket_str)
                                except ValueError:
                                    mt5_ticket = None

                                if mt5_ticket is not None:
                                    self.position_maps[acc_name][mt5_ticket] = position_id

                            # Store positive volume when present
                            if volume > 0:
                                self.position_volumes[acc_name][position_id] = int(volume)
                                logger.info(
                                    f"[{acc_name}] (exec) positionId {position_id} "
                                    f"volume={volume}"
                                )

                    return  # done handling execution event

                # 2) Reconcile response: preload ALL positions
                if isinstance(extracted, ProtoOAReconcileRes):
                    count = 0
                    for pos in extracted.position:
                        position_id = getattr(pos, "positionId", 0)
                        trade_data = getattr(pos, "tradeData", None)
                        label = getattr(trade_data, "label", "") if trade_data else ""
                        volume = getattr(pos, "volume", 0)

                        if not position_id:
                            continue

                        # Always store positive volume for this position
                        if volume > 0:
                            self.position_volumes[acc_name][position_id] = int(volume)

                        # If label is MT5_..., also build ticket mapping
                        if isinstance(label, str) and label.startswith("MT5_"):
                            mt5_ticket_str = label.split("_", 1)[1]
                            try:
                                mt5_ticket = int(mt5_ticket_str)
                            except ValueError:
                                continue

                            self.position_maps[acc_name][mt5_ticket] = position_id
                            count += 1

                    logger.info(
                        f"[{acc_name}] Reconcile complete: {count} MT5 positions "
                        f"({len(self.position_volumes[acc_name])} with volume)"
                    )
                    return  # done handling reconcile

                # 3) Single-position updates with a .position field
                if not hasattr(extracted, "position"):
                    return

                position = extracted.position
                position_id = getattr(position, "positionId", 0)
                trade_data = getattr(position, "tradeData", None)
                if not (position_id and trade_data and hasattr(trade_data, "label")):
                    return

                label = trade_data.label  # e.g. "MT5_1441124621"
                if not (isinstance(label, str) and label.startswith("MT5_")):
                    return

                mt5_ticket_str = label.split("_", 1)[1]
                try:
                    mt5_ticket = int(mt5_ticket_str)
                except ValueError:
                    return

                # Update mapping: MT5 ticket -> cTrader positionId
                self.position_maps[acc_name][mt5_ticket] = position_id

                # Update current volume
                volume = getattr(position, "volume", 0)
                if volume > 0:
                    self.position_volumes[acc_name][position_id] = int(volume)

                logger.info(
                    f"[{acc_name}] updated MT5 ticket {mt5_ticket} -> "
                    f"cTrader positionId {position_id}, volume={volume}"
                )
            except Exception as e:
                logger.debug(f"[{acc_name}] Failed to parse message: {e}")

        # Register the callback so it actually runs
        client.set_message_callback(on_message)
        
        # Connect the client (will auto-authorize account)
        def on_connected():
            logger.info(f"✓ Account {account.name} connected and authenticated")
            
            # Immediately reconcile open positions once account is authorized
            try:
                req = ProtoOAReconcileReq()
                req.ctidTraderAccountId = account.account_id
                logger.info(f"[{account.name}] Sending reconcile request...")
                d = client.client.send(req)  # low-level client inside CTraderClient
                
                def _on_reconcile(result):
                    try:
                        Protobuf.extract(result)
                        logger.info(f"[{account.name}] Reconcile response processed")
                    except Exception as e:
                        logger.warning(
                            f"[{account.name}] Failed to process reconcile response: {e}"
                        )
                
                d.addCallback(_on_reconcile)
                d.addErrback(client._on_error)
            except Exception as e:
                logger.error(f"[{account.name}] Failed to send reconcile request: {e}")
        
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

    def get_position_volume(self, account_name: str, position_id: int) -> Optional[int]:
        """Get stored cTrader volume (cents of units) for a positionId."""
        vol_map = self.position_volumes.get(account_name) or {}
        return vol_map.get(int(position_id))
    
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
