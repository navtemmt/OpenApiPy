"""
Account Manager for Multiple cTrader Connections

Manages multiple cTrader client connections for different accounts.
"""

import inspect
import logging
from typing import Dict, Optional, Tuple

import ctrader_client as ctr_mod
from ctrader_client import CTraderClient
from config_loader import AccountConfig
from ctrader_open_api import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAExecutionEvent,
    ProtoOAReconcileReq,
    ProtoOAReconcileRes,
)
from trade_processor import _enforce_max_risk_on_fill, notify_position_update

logger = logging.getLogger(__name__)

# One-shot debug: confirm which _on_spot_event implementation is live
try:
    logger.info(
        "DEBUG CTraderClient._on_spot_event SOURCE:\n%s",
        inspect.getsource(ctr_mod.CTraderClient._on_spot_event),
    )
except Exception:
    logger.info("DEBUG unable to inspect CTraderClient._on_spot_event source")


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
        # Per-account mapping: MT5 ticket -> cTrader orderId (pending orders)
        self.order_maps: Dict[str, Dict[int, int]] = {}

        # Per-account cached funds (deposit currency)
        self.account_equity: Dict[str, float] = {}
        self.account_balance: Dict[str, float] = {}

        # Per-account mapping: MT5 ticket -> last MT5 payload (for risk checks)
        self.mt5_payloads: Dict[str, Dict[int, dict]] = {}

        # Per-account reconcile guard
        self.reconcile_requested: Dict[str, bool] = {}

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
    def _extract_order_label(order) -> str:
        try:
            td = getattr(order, "tradeData", None)
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

    @staticmethod
    def _extract_account_equity_balance(
        reconcile_res,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Best-effort extraction from ProtoOAReconcileRes.

        Different Open API versions/wrappers may expose:
          - reconcile_res.account (single) OR reconcile_res.account[] (list)
          - fields like equity, balance
        """
        try:
            acc_obj = getattr(reconcile_res, "account", None)
            if acc_obj is None:
                return None, None

            if hasattr(acc_obj, "__iter__") and not isinstance(acc_obj, (bytes, str)):
                acc0 = None
                for a in acc_obj:
                    acc0 = a
                    break
                acc_obj = acc0

            if acc_obj is None:
                return None, None

            eq = getattr(acc_obj, "equity", None)
            bal = getattr(acc_obj, "balance", None)

            eq_f = float(eq) if eq is not None else None
            bal_f = float(bal) if bal is not None else None
            return eq_f, bal_f
        except Exception:
            return None, None

    def _ensure_account_maps(self, acc_name: str):
        if acc_name not in self.position_maps:
            self.position_maps[acc_name] = {}
        if acc_name not in self.position_volumes:
            self.position_volumes[acc_name] = {}
        if acc_name not in self.order_maps:
            self.order_maps[acc_name] = {}
        if acc_name not in self.mt5_payloads:
            self.mt5_payloads[acc_name] = {}
        if acc_name not in self.reconcile_requested:
            self.reconcile_requested[acc_name] = False

    def _send_reconcile_request(self, account_name: str):
        client = self.get_client(account_name)
        config = self.get_config(account_name)

        if not client or not config:
            logger.warning("[%s] Cannot send reconcile: missing client/config", account_name)
            return

        if self.reconcile_requested.get(account_name, False):
            logger.info("[%s] Reconcile already requested for this connection", account_name)
            return

        try:
            req = ProtoOAReconcileReq()
            req.ctidTraderAccountId = int(config.account_id)

            logger.info("[%s] Sending reconcile request...", account_name)
            d = client.send(req)
            self.reconcile_requested[account_name] = True

            def _on_reconcile(result):
                try:
                    extracted = Protobuf.extract(result)
                    if isinstance(extracted, ProtoOAReconcileRes):
                        logger.info("[%s] Reconcile response processed", account_name)
                    else:
                        logger.info(
                            "[%s] Reconcile callback received message type %s",
                            account_name,
                            type(extracted).__name__,
                        )
                except Exception as e:
                    logger.warning(
                        "[%s] Failed to process reconcile response: %s",
                        account_name,
                        e,
                    )

            def _on_reconcile_err(failure):
                self.reconcile_requested[account_name] = False
                client._on_error(failure)

            d.addCallback(_on_reconcile)
            d.addErrback(_on_reconcile_err)
        except Exception as e:
            self.reconcile_requested[account_name] = False
            logger.error("[%s] Failed to send reconcile request: %s", account_name, e)

    def _attach_post_auth_reconcile(self, client: CTraderClient, account_name: str):
        original = client._on_account_auth_success

        def wrapped(result, _orig=original, _acc_name=account_name):
            rv = _orig(result)
            try:
                logger.info("✓ Account %s connected and authenticated", _acc_name)
                self._send_reconcile_request(_acc_name)
            except Exception as e:
                logger.error("[%s] Post-auth reconcile hook failed: %s", _acc_name, e)
            return rv

        client._on_account_auth_success = wrapped

    # ------------------------------------------------------------------
    # Account lifecycle
    # ------------------------------------------------------------------

    def add_account(self, account: AccountConfig):
        """Add and connect a cTrader account."""
        if not account.enabled:
            logger.info("Skipping disabled account: %s", account.name)
            return

        logger.info("Initializing account: %s", account.name)

        client = CTraderClient(env=account.environment)

        # Override client credentials with account-specific values FIRST
        client.client_id = account.client_id
        client.client_secret = account.client_secret

        # Now set account credentials (account_id and access_token)
        client.set_account_credentials(
            account_id=account.account_id,
            access_token=account.access_token or "",
        )

        self.clients[account.name] = client
        self.configs[account.name] = account
        self._ensure_account_maps(account.name)

        # Ensure reconcile is sent only after account auth succeeds
        self._attach_post_auth_reconcile(client, account.name)

        def on_message(message, acc_name=account.name):
            try:
                self._ensure_account_maps(acc_name)
                extracted = Protobuf.extract(message)

                # 1) Execution events: fills / partial fills / accepts etc.
                if isinstance(extracted, ProtoOAExecutionEvent):
                    logger.info(f"[{acc_name}] RAW EXECUTION: {extracted}")

                    exec_type = getattr(extracted, "executionType", None)

                    order = getattr(extracted, "order", None)
                    if order is not None:
                        order_id = int(getattr(order, "orderId", 0) or 0)
                        olabel = self._extract_order_label(order)
                        oticket = self._label_to_ticket(olabel)
                        if order_id and oticket is not None:
                            self.order_maps[acc_name][int(oticket)] = int(order_id)
                            logger.info(
                                f"[{acc_name}] (exec order) MT5 ticket {int(oticket)} -> "
                                f"cTrader orderId {int(order_id)}"
                            )

                    pos = getattr(extracted, "position", None)
                    if pos is not None:
                        position_id = int(getattr(pos, "positionId", 0) or 0)
                        label = self._extract_position_label(pos)
                        ticket = self._label_to_ticket(label)

                        if position_id and ticket is not None:
                            self.position_maps[acc_name][int(ticket)] = position_id
                            notify_position_update(acc_name, int(ticket), self)

                        vol = self._extract_position_volume(pos)
                        if position_id and vol > 0:
                            self.position_volumes[acc_name][position_id] = int(vol)
                            logger.info(
                                f"[{acc_name}] (exec vol) positionId {position_id} "
                                f"volume={vol} (exec_type={exec_type})"
                            )

                        try:
                            if position_id and ticket is not None:
                                from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                                    ProtoOAExecutionType,
                                    ProtoOAPositionStatus,
                                )

                                pos_status = getattr(pos, "positionStatus", None)
                                is_open = pos_status == ProtoOAPositionStatus.POSITION_STATUS_OPEN
                                is_fill = exec_type == ProtoOAExecutionType.ORDER_FILLED

                                if is_open and is_fill:
                                    config = self.get_config(acc_name)
                                    if config:
                                        symbol_id = int(
                                            getattr(
                                                getattr(pos, "tradeData", None) or pos,
                                                "symbolId",
                                                0,
                                            )
                                            or 0
                                        )
                                        symbol = None
                                        client_obj = self.get_client(acc_name)
                                        if client_obj and hasattr(client_obj, "symbol_details"):
                                            symbol = client_obj.symbol_details.get(symbol_id)

                                        mt5_data = self.mt5_payloads.get(acc_name, {}).get(
                                            int(ticket), None
                                        )

                                        if symbol is not None:
                                            _enforce_max_risk_on_fill(
                                                account_name=acc_name,
                                                client=client_obj,
                                                config=config,
                                                account_manager=self,
                                                position=pos,
                                                symbol=symbol,
                                            )
                        except Exception as e:
                            logger.debug(f"[{acc_name}] over-risk enforce failed: {e}")

                    return

                # 2) Reconcile response: preload ALL positions + cache equity/balance if present
                if isinstance(extracted, ProtoOAReconcileRes):
                    eq, bal = self._extract_account_equity_balance(extracted)
                    if eq is not None:
                        self.account_equity[acc_name] = float(eq)
                    if bal is not None:
                        self.account_balance[acc_name] = float(bal)

                    if eq is not None or bal is not None:
                        logger.info(
                            f"[{acc_name}] Funds cached: equity={self.account_equity.get(acc_name)}, "
                            f"balance={self.account_balance.get(acc_name)}"
                        )

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
                            notify_position_update(acc_name, int(ticket), self)
                            count += 1

                    try:
                        for o in getattr(extracted, "order", []):
                            order_id = int(getattr(o, "orderId", 0) or 0)
                            olabel = self._extract_order_label(o)
                            oticket = self._label_to_ticket(olabel)
                            if order_id and oticket is not None:
                                self.order_maps[acc_name][int(oticket)] = int(order_id)
                    except Exception:
                        pass

                    logger.info(
                        f"[{acc_name}] Reconcile complete: {count} MT5 positions "
                        f"({len(self.position_volumes[acc_name])} with volume)"
                    )
                    return

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

                self.position_maps[acc_name][int(ticket)] = position_id
                notify_position_update(acc_name, int(ticket), self)

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

        def on_connected():
            self.reconcile_requested[account.name] = False
            logger.info(
                "✓ Account %s socket connected; waiting for app/account authorization",
                account.name,
            )

        client.connect(on_connect=on_connected)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_client(self, account_name: str) -> Optional[CTraderClient]:
        return self.clients.get(account_name)

    def get_config(self, account_name: str) -> Optional[AccountConfig]:
        return self.configs.get(account_name)

    def get_equity(self, account_name: str) -> Optional[float]:
        return self.account_equity.get(account_name)

    def get_balance(self, account_name: str) -> Optional[float]:
        return self.account_balance.get(account_name)

    def get_position_id(self, account_name: str, mt5_ticket: int) -> Optional[int]:
        pos_map = self.position_maps.get(account_name) or {}
        return pos_map.get(int(mt5_ticket))

    def get_order_id(self, account_name: str, mt5_ticket: int) -> Optional[int]:
        """Get cTrader orderId for a pending order by MT5 ticket."""
        omap = self.order_maps.get(account_name) or {}
        return omap.get(int(mt5_ticket))

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
            self.order_maps.get(account_name, {}).pop(int(mt5_ticket), None)
            self.mt5_payloads.get(account_name, {}).pop(int(mt5_ticket), None)
        except Exception:
            pass

    def get_all_accounts(self) -> Dict[str, Tuple[CTraderClient, AccountConfig]]:
        return {name: (self.clients[name], self.configs[name]) for name in self.clients.keys()}


# Global instance
_manager_instance = None


def get_account_manager() -> AccountManager:
    """Get or create global account manager instance."""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = AccountManager()
    return _manager_instance
