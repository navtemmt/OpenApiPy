"""
Account Manager for Multiple cTrader Connections

Manages multiple cTrader client connections for different accounts.
"""
import logging
from typing import Dict, Optional, Tuple

from ctrader_client import CTraderClient
from config_loader import AccountConfig
from ctrader_open_api import Protobuf
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_position_label(pos) -> str:
        try:
            td = getattr(pos, "tradeData", None)
            if td is None:
                return ""
            lbl = getattr(td, "label", "")
            return lbl if isinstance(lbl, str) else ""
        except Exception:
            return ""

    @staticmethod
    def _label_to_ticket(label: str) -> Optional[int]:
        if not (isinstance(label, str) and label.startswith("MT5_")):
            return None
        try:
            return int(label.split("_", 1)[1])
        except Exception:
            return None

    @staticmethod
    def _extract_position_volume(pos) -> int:
        """
        Best-effort volume extractor.

        In execution events and many position updates:
          pos.tradeData.volume

        In reconcile:
          pos.volume may be present too.
        """
        try:
            td = getattr(pos, "tradeData", None)
            if td is not None:
                v = getattr(td, "volume", 0)
                if int(v) > 0:
                    return int(v)
        except Exception:
            pass

        try:
            v = getattr(pos, "volume", 0)
            return int(v) if int(v) > 0 else 0
        except Exception:
            return 0

    def _ensure_account_maps(self, acc_name: str):
        if acc_name not in self.position_maps:
            self.position_maps[acc_name] = {}
        if acc_name not in self.position_volumes:
            self.position_volumes[acc_name] = {}

    # ------------------------------------------------------------------
    # Account lifecycle
    # ------------------------------------------------------------------

    def add_account(self, account: AccountConfig):
        """Add and connect a cTrader account."""
        if not account.enabled:
            logger.info("Skipping disabled account: %s", account.name)
            return

        logger.info("Initializing account: %s", account.name)

        # Create cTrader client for this environment
        client = CTraderClient(env=account.environment)

        # Override client credentials with account-specific values FIRST
        client.client_id = account.client_id
        client.client_secret = account.client_secret

        # Now set account credentials (account_id and access_token)
        client.set_account_credentials(
            account_id=account.account_id,
            access_token=account.access_token or "",
        )

        # Store references
        self.clients[account.name] = client
        self.configs[account.name] = account
        self._ensure_account_maps(account.name)

        # Hook message callback (handles execution events + reconcile + position updates)
        def on_message(message, acc_name=account.name):
            try:
                extracted = Protobuf.extract(message)

                # 1) Execution events: fills / partial fills / accepts etc.
                if isinstance(extracted, ProtoOAExecutionEvent):
                    logger.info(f"[{acc_name}] RAW EXECUTION: {extracted}")

                    exec_type = getattr(extracted, "executionType", None)
                    pos = getattr(extracted, "position", None)

                    if pos is not None:
                        position_id = int(getattr(pos, "positionId", 0) or 0)
                        label = self._extract_position_label(pos)
                        ticket = self._label_to_ticket(label)

                        if position_id and ticket is not None:
                            self.position_maps[acc_name][int(ticket)] = position_id

                        # Only trust volume on FILLED / PARTIALLY_FILLED events
                        # ORDER_FILLED = 4, ORDER_PARTIALLY_FILLED = 5 (as observed in your logs)
                        vol = self._extract_position_volume(pos)
                        if position_id and vol > 0 and exec_type in (4, 5):
                            self.position_volumes[acc_name][position_id] = int(vol)
                            logger.info(
                                f"[{acc_name}] (exec fill) positionId {position_id} volume={vol}"
                            )

                # 2) Reconcile response: preload ALL positions
                if isinstance(extracted, ProtoOAReconcileRes):
                    count = 0
                    for pos in extracted.position:
                        position_id = int(getattr(pos, "positionId", 0) or 0)
                        if not position_id:
                            continue

                        label = self._extract_position_label(pos)
                        ticket = self._label_to_ticket(label)
                        vol = self._extract_position_volume(pos)

                        if vol > 0:
                            self.position_volumes[acc_name][position_id] = int(vol)

                        if ticket is not None:
                            self.position_maps[acc_name][int(ticket)] = position_id
                            count += 1

                    logger.info(
                        f"[{acc_name}] Reconcile complete: {count} MT5 positions "
                        f"({len(self.position_volumes[acc_name])} with volume)"
                    )
                    return  # done handling reconcile

                # 3) Single-position updates with a .position field
                if not hasattr(extracted, "position"):
                    return

                pos = extracted.position
                position_id = int(getattr(pos, "positionId", 0) or 0)
                if not position_id:
                    return

                label = self._extract_position_label(pos)
                ticket = self._label_to_ticket(label)
                if ticket is None:
                    return

                # Update mapping: MT5 ticket -> cTrader positionId
                self.position_maps[acc_name][int(ticket)] = position_id

                # Update current volume
                vol = self._extract_position_volume(pos)
                if vol > 0:
                    self.position_volumes[acc_name][position_id] = int(vol)

                logger.info(
                    f"[{acc_name}] updated MT5 ticket {int(ticket)} -> "
                    f"cTrader positionId {position_id}, volume={vol}"
                )

            except Exception as e:
                logger.debug(f"[{acc_name}] Failed to parse message: {e}")

        client.set_message_callback(on_message)

        # Connect the client (will auto-authorize account)
        def on_connected():
            logger.info("✓ Account %s connected and authenticated", account.name)

            # Immediately reconcile open positions once account is authorized
            try:
                req = ProtoOAReconcileReq()
                req.ctidTraderAccountId = int(account.account_id)
                logger.info("[%s] Sending reconcile request...", account.name)
                d = client.send(req)  # low-level client inside CTraderClient

                def _on_reconcile(result):
                    try:
                        Protobuf.extract(result)
                        logger.info("[%s] Reconcile response processed", account.name)
                    except Exception as e:
                        logger.warning(
                            "[%s] Failed to process reconcile response: %s", account.name, e
                        )

                d.addCallback(_on_reconcile)
                d.addErrback(client._on_error)
            except Exception as e:
                logger.error("[%s] Failed to send reconcile request: %s", account.name, e)

        client.connect(on_connect=on_connected)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_client(self, account_name: str) -> Optional[CTraderClient]:
        return self.clients.get(account_name)

    def get_config(self, account_name: str) -> Optional[AccountConfig]:
        return self.configs.get(account_name)

    def get_position_id(self, account_name: str, mt5_ticket: int) -> Optional[int]:
        pos_map = self.position_maps.get(account_name) or {}
        return pos_map.get(int(mt5_ticket))

    def get_position_volume(self, account_name: str, position_id: int) -> Optional[int]:
        vol_map = self.position_volumes.get(account_name) or {}
        return vol_map.get(int(position_id))

    def get_ticket_volume(self, account_name: str, mt5_ticket: int) -> Optional[int]:
        """Convenience: get volume by MT5 ticket (via positionId mapping)."""
        pid = self.get_position_id(account_name, mt5_ticket)
        if not pid:
            return None
        return self.get_position_volume(account_name, pid)

    def remove_mapping(self, account_name: str, mt5_ticket: int):
        """Remove ticket->positionId mapping."""
        try:
            self.position_maps.get(account_name, {}).pop(int(mt5_ticket), None)
        except Exception:
            pass

    def get_all_accounts(self) -> Dict[str, Tuple[CTraderClient, AccountConfig]]:
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
