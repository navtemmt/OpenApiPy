"""
Account Manager for Multiple cTrader Connections

Manages multiple cTrader client connections for different accounts.
"""

import inspect
from typing import Dict, Optional, Tuple

import ctrader_client as ctr_mod
from ctrader_client import CTraderClient
from config_loader import AccountConfig
from ctrader_open_api import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthRes,
    ProtoOAExecutionEvent,
    ProtoOAReconcileReq,
    ProtoOAReconcileRes,
)
from trade_processor import _enforce_max_risk_on_fill, notify_position_update
from app_state import logger, notify_error, notify_warning, notify_info


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
        self.clients: Dict[str, CTraderClient] = {}
        self.configs: Dict[str, AccountConfig] = {}
        self.position_maps: Dict[str, Dict[int, int]] = {}
        self.position_volumes: Dict[str, Dict[int, int]] = {}
        self.order_maps: Dict[str, Dict[int, int]] = {}
        self.account_equity: Dict[str, float] = {}
        self.account_balance: Dict[str, float] = {}
        self.mt5_payloads: Dict[str, Dict[int, dict]] = {}
        self.reconcile_requested: Dict[str, bool] = {}
        self.auth_seen: Dict[str, bool] = {}
        self.route_magic_map: Dict[int, str] = {}
        self.shared_token_files: Dict[str, str] = {}

    @staticmethod
    def _to_int(value, default=0) -> int:
        try:
            return int(float(value))
        except Exception:
            return int(default)

    @staticmethod
    def _to_float(value, default=None):
        try:
            return float(value)
        except Exception:
            return default

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

    @staticmethod
    def _config_account_id(config: AccountConfig) -> Optional[int]:
        try:
            v = getattr(config, "account_id", None)
            if v is not None and str(v).strip() != "":
                return int(v)
        except Exception:
            pass
        try:
            v = getattr(config, "accountid", None)
            if v is not None and str(v).strip() != "":
                return int(v)
        except Exception:
            pass
        return None

    @staticmethod
    def _config_route_magic(config: AccountConfig) -> Optional[int]:
        try:
            v = getattr(config, "route_magic_number", None)
            if v is not None and str(v).strip() != "":
                return int(v)
        except Exception:
            pass
        try:
            v = getattr(config, "magic_number", None)
            if v is not None and str(v).strip() != "":
                return int(v)
        except Exception:
            pass
        return None

    @staticmethod
    def _safe_str(value) -> str:
        try:
            return str(value or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _token_preview(token: str) -> str:
        tok = str(token or "")
        if len(tok) <= 10:
            return tok or "<empty>"
        return f"{tok[:6]}...{tok[-4:]}"

    def _notify_ctx(self, account_name: Optional[str] = None, **extra):
        ctx = {"account_name": account_name}
        ctx.update(extra)
        return ctx

    def _build_token_group_key(self, account: AccountConfig) -> str:
        explicit_candidates = (
            getattr(account, "token_group", None),
            getattr(account, "shared_token_group", None),
            getattr(account, "ctid_key", None),
        )
        for value in explicit_candidates:
            value = self._safe_str(value)
            if value:
                return f"explicit:{value}"

        access_token = self._safe_str(getattr(account, "access_token", ""))
        refresh_token = self._safe_str(getattr(account, "refresh_token", ""))
        if access_token or refresh_token:
            return f"pair:{access_token}|{refresh_token}"

        state_file = self._safe_str(getattr(account, "token_state_file", ""))
        if state_file:
            return f"state:{state_file}"

        return f"account:{self._safe_str(getattr(account, 'name', 'unknown'))}"

    def _resolve_shared_token_state_file(self, account: AccountConfig) -> Optional[str]:
        token_key = self._build_token_group_key(account)
        configured_state_file = self._safe_str(getattr(account, "token_state_file", ""))

        existing = self.shared_token_files.get(token_key)
        if existing is not None:
            if configured_state_file and configured_state_file != existing:
                msg = (
                    f"Shared token group detected; overriding token_state_file "
                    f"{configured_state_file} -> {existing}"
                )
                logger.warning("[%s] %s", account.name, msg)
                notify_warning(
                    event="shared_token_state_override",
                    message=msg,
                    **self._notify_ctx(
                        account.name,
                        token_group=token_key,
                        configured_state_file=configured_state_file,
                        canonical_state_file=existing,
                    ),
                )
            return existing or None

        if configured_state_file:
            self.shared_token_files[token_key] = configured_state_file
            logger.info(
                "[%s] Registered shared token group %s with state file %s",
                account.name,
                token_key,
                configured_state_file,
            )
            return configured_state_file

        logger.info(
            "[%s] Shared token group %s has no token_state_file configured",
            account.name,
            token_key,
        )
        self.shared_token_files[token_key] = ""
        return None

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
        if acc_name not in self.auth_seen:
            self.auth_seen[acc_name] = False

    def _register_route_magic(self, account: AccountConfig):
        route_magic = self._config_route_magic(account)
        if route_magic is None:
            logger.info(
                "[%s] No route_magic_number configured; magic-based routing unavailable",
                account.name,
            )
            return

        existing = self.route_magic_map.get(int(route_magic))
        if existing and existing != account.name:
            raise ValueError(
                f"Duplicate route_magic_number={int(route_magic)} for accounts "
                f"{existing!r} and {account.name!r}"
            )

        self.route_magic_map[int(route_magic)] = account.name
        logger.info("[%s] Registered route magic %s", account.name, int(route_magic))

    def _unregister_route_magic(self, account_name: str):
        stale = [magic for magic, name in self.route_magic_map.items() if name == account_name]
        for magic in stale:
            self.route_magic_map.pop(magic, None)
            logger.info("[%s] Unregistered route magic %s", account_name, magic)

    def _cache_funds_from_reconcile(self, account_name: str, reconcile_res):
        eq, bal = self._extract_account_equity_balance(reconcile_res)
        if eq is not None:
            self.account_equity[account_name] = float(eq)
        if bal is not None:
            self.account_balance[account_name] = float(bal)

        if eq is not None or bal is not None:
            logger.info(
                "[%s] Funds cached: equity=%s, balance=%s",
                account_name,
                self.account_equity.get(account_name),
                self.account_balance.get(account_name),
            )

    def _store_order_mapping(self, account_name: str, mt5_ticket: int, order_id: int):
        self._ensure_account_maps(account_name)
        self.order_maps[account_name][int(mt5_ticket)] = int(order_id)
        logger.info(
            "[%s] MT5 ticket %s -> cTrader orderId %s",
            account_name,
            int(mt5_ticket),
            int(order_id),
        )

    def _remove_order_mapping(self, account_name: str, mt5_ticket: int):
        self._ensure_account_maps(account_name)
        removed = self.order_maps[account_name].pop(int(mt5_ticket), None)
        if removed:
            logger.info(
                "[%s] Removed MT5 ticket %s -> stale cTrader orderId %s mapping",
                account_name,
                int(mt5_ticket),
                int(removed),
            )

    def _store_position_mapping(self, account_name: str, mt5_ticket: int, position_id: int):
        self._ensure_account_maps(account_name)
        self.position_maps[account_name][int(mt5_ticket)] = int(position_id)
        notify_position_update(account_name, int(mt5_ticket), self)
        logger.info(
            "[%s] MT5 ticket %s -> cTrader positionId %s",
            account_name,
            int(mt5_ticket),
            int(position_id),
        )

    def _remove_position_mapping(self, account_name: str, mt5_ticket: int):
        self._ensure_account_maps(account_name)
        removed = self.position_maps[account_name].pop(int(mt5_ticket), None)
        if removed:
            self.position_volumes[account_name].pop(int(removed), None)
            logger.info(
                "[%s] Removed MT5 ticket %s -> stale cTrader positionId %s mapping",
                account_name,
                int(mt5_ticket),
                int(removed),
            )

    def _store_position_volume(self, account_name: str, position_id: int, volume: int):
        if int(position_id) <= 0:
            return

        self._ensure_account_maps(account_name)

        if int(volume or 0) > 0:
            self.position_volumes[account_name][int(position_id)] = int(volume)
            logger.info(
                "[%s] positionId %s volume=%s cached",
                account_name,
                int(position_id),
                int(volume),
            )
        else:
            self.position_volumes[account_name].pop(int(position_id), None)

    def _handle_execution_order(self, account_name: str, extracted):
        order = getattr(extracted, "order", None)
        if order is None:
            return

        order_id = int(getattr(order, "orderId", 0) or 0)
        label = self._extract_order_label(order)
        ticket = self._label_to_ticket(label)

        if order_id and ticket is not None:
            self._store_order_mapping(account_name, int(ticket), int(order_id))

    def _handle_execution_position(self, account_name: str, extracted):
        pos = getattr(extracted, "position", None)
        if pos is None:
            return
    
        exec_type = getattr(extracted, "executionType", None)
        position_id = int(getattr(pos, "positionId", 0) or 0)
        label = self._extract_position_label(pos)
        ticket = self._label_to_ticket(label)
        volume = self._extract_position_volume(pos)
    
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAExecutionType,
            ProtoOAPositionStatus,
        )
    
        pos_status = getattr(pos, "positionStatus", None)
        is_fill = exec_type == ProtoOAExecutionType.ORDER_FILLED
        is_open = pos_status == ProtoOAPositionStatus.POSITION_STATUS_OPEN
    
        if position_id and ticket is not None:
            self._store_position_mapping(account_name, int(ticket), int(position_id))
    
            # Keep the pending-order mapping for ORDER_ACCEPTED / pending-created events.
            # Remove it only when the order is truly filled into an open position.
            if is_fill and is_open:
                self._remove_order_mapping(account_name, int(ticket))
    
        if position_id:
            self._store_position_volume(account_name, int(position_id), int(volume))
    
        if position_id:
            logger.info(
                "[%s] (exec pos) positionId=%s ticket=%s volume=%s exec_type=%s pos_status=%s",
                account_name,
                position_id,
                ticket,
                volume,
                exec_type,
                pos_status,
            )
    
        self._try_enforce_max_risk_on_fill(account_name, extracted, pos, position_id, ticket)

    def _try_enforce_max_risk_on_fill(self, account_name: str, extracted, pos, position_id: int, ticket: Optional[int]):
        try:
            if not position_id or ticket is None:
                return

            from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                ProtoOAExecutionType,
                ProtoOAPositionStatus,
            )

            pos_status = getattr(pos, "positionStatus", None)
            exec_type = getattr(extracted, "executionType", None)
            is_open = pos_status == ProtoOAPositionStatus.POSITION_STATUS_OPEN
            is_fill = exec_type == ProtoOAExecutionType.ORDER_FILLED

            if not (is_open and is_fill):
                return

            config = self.get_config(account_name)
            client_obj = self.get_client(account_name)
            if not config or not client_obj:
                return

            symbol_id = int(
                getattr(
                    getattr(pos, "tradeData", None) or pos,
                    "symbolId",
                    0,
                ) or 0
            )
            if symbol_id <= 0:
                return

            symbol = None
            if hasattr(client_obj, "symbol_details"):
                symbol = client_obj.symbol_details.get(symbol_id)

            mt5_data = self.mt5_payloads.get(account_name, {}).get(int(ticket), None)

            if symbol is not None:
                _enforce_max_risk_on_fill(
                    account_name=account_name,
                    client=client_obj,
                    config=config,
                    account_manager=self,
                    position=pos,
                    symbol=symbol,
                    mt5_symbol=None,
                    mt5_data=mt5_data,
                )
        except Exception as e:
            logger.debug("[%s] over-risk enforce failed: %s", account_name, e, exc_info=True)

    def _handle_reconcile_positions(self, account_name: str, extracted) -> int:
        count = 0
        pos_list = list(getattr(extracted, "position", []) or [])

        active_position_ids = set()
        active_position_tickets = set()

        for pos in pos_list:
            position_id = int(getattr(pos, "positionId", 0) or 0)
            if not position_id:
                continue

            active_position_ids.add(int(position_id))

            label = self._extract_position_label(pos)
            ticket = self._label_to_ticket(label)
            vol = self._extract_position_volume(pos)

            self._store_position_volume(account_name, int(position_id), int(vol))

            if ticket is not None:
                active_position_tickets.add(int(ticket))
                self._store_position_mapping(account_name, int(ticket), int(position_id))
                logger.info(
                    "[%s] (reconcile pos) MT5 ticket %s -> cTrader positionId %s volume=%s",
                    account_name,
                    int(ticket),
                    int(position_id),
                    int(vol),
                )
                count += 1

        stale_tickets = [
            t for t in self.position_maps.get(account_name, {}).keys()
            if t not in active_position_tickets
        ]
        for t in stale_tickets:
            self._remove_position_mapping(account_name, int(t))

        stale_ids = [
            pid for pid in self.position_volumes.get(account_name, {}).keys()
            if pid not in active_position_ids
        ]
        for pid in stale_ids:
            self.position_volumes[account_name].pop(pid, None)
            logger.info("[%s] Removed stale cached volume for positionId %s", account_name, pid)

        return count

    def _handle_reconcile_orders(self, account_name: str, extracted) -> int:
        order_count = 0
        order_list = list(getattr(extracted, "order", []) or [])

        active_order_tickets = set()

        for order in order_list:
            order_id = int(getattr(order, "orderId", 0) or 0)
            label = self._extract_order_label(order)
            ticket = self._label_to_ticket(label)

            if order_id and ticket is not None:
                active_order_tickets.add(int(ticket))
                self._store_order_mapping(account_name, int(ticket), int(order_id))
                logger.info(
                    "[%s] (reconcile order) MT5 ticket %s -> cTrader orderId %s",
                    account_name,
                    int(ticket),
                    int(order_id),
                )
                order_count += 1

        stale_tickets = [
            t for t in self.order_maps.get(account_name, {}).keys()
            if t not in active_order_tickets
        ]
        for t in stale_tickets:
            self._remove_order_mapping(account_name, int(t))

        return order_count

    def _process_reconcile(self, account_name: str, extracted):
        self._cache_funds_from_reconcile(account_name, extracted)

        count = self._handle_reconcile_positions(account_name, extracted)

        try:
            order_count = self._handle_reconcile_orders(account_name, extracted)
        except Exception as e:
            logger.debug("[%s] Failed parsing reconcile orders", account_name, exc_info=True)
            notify_error(
                event="reconcile_parse_orders",
                message="Failed parsing reconcile orders",
                exc=e,
                **self._notify_ctx(account_name),
            )
            order_count = 0

        logger.info(
            "[%s] Reconcile complete: %s MT5 positions (%s positions with volume cached), %s orders mapped",
            account_name,
            count,
            len(self.position_volumes[account_name]),
            order_count,
        )

    def _process_message(self, account_name: str, message):
        self._ensure_account_maps(account_name)
        extracted = Protobuf.extract(message)

        if isinstance(extracted, ProtoOAAccountAuthRes):
            if not self.auth_seen.get(account_name, False):
                self.auth_seen[account_name] = True
                logger.info("✓ Account %s connected and authenticated", account_name)
                notify_info(
                    event="account_authenticated",
                    message="cTrader account authenticated",
                    **self._notify_ctx(account_name),
                )
            self._send_reconcile_request(account_name)
            return

        if isinstance(extracted, ProtoOAExecutionEvent):
            logger.info("[%s] RAW EXECUTION: %s", account_name, extracted)
            self._handle_execution_order(account_name, extracted)
            self._handle_execution_position(account_name, extracted)
            return

        if isinstance(extracted, ProtoOAReconcileRes):
            self.reconcile_requested[account_name] = False
            self._process_reconcile(account_name, extracted)
            return

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

        self._store_position_mapping(account_name, int(ticket), int(position_id))

        vol = self._extract_position_volume(pos)
        self._store_position_volume(account_name, int(position_id), int(vol))

        logger.info(
            "[%s] updated MT5 ticket %s -> cTrader positionId %s, volume=%s",
            account_name,
            int(ticket),
            int(position_id),
            int(vol),
        )

    def _send_reconcile_request(self, account_name: str):
        client = self.get_client(account_name)
        config = self.get_config(account_name)

        if not client or not config:
            msg = "Cannot send reconcile: missing client/config"
            logger.warning("[%s] %s", account_name, msg)
            notify_warning(
                event="reconcile_missing_context",
                message=msg,
                **self._notify_ctx(account_name),
            )
            return

        account_id = self._config_account_id(config)
        if not account_id:
            msg = "Cannot send reconcile: missing account_id"
            logger.warning("[%s] %s", account_name, msg)
            notify_warning(
                event="reconcile_missing_account_id",
                message=msg,
                **self._notify_ctx(account_name),
            )
            return

        if self.reconcile_requested.get(account_name, False):
            logger.info("[%s] Reconcile already requested for this connection", account_name)
            return

        try:
            req = ProtoOAReconcileReq()
            req.ctidTraderAccountId = int(account_id)

            logger.info("[%s] Sending reconcile request...", account_name)
            d = client.send(req)
            self.reconcile_requested[account_name] = True

            def _on_reconcile(result):
                try:
                    extracted = Protobuf.extract(result)
                    if isinstance(extracted, ProtoOAReconcileRes):
                        self.reconcile_requested[account_name] = False
                        logger.info("[%s] Reconcile response processed", account_name)
                    else:
                        logger.info(
                            "[%s] Reconcile callback received message type %s",
                            account_name,
                            type(extracted).__name__,
                        )
                except Exception as e:
                    self.reconcile_requested[account_name] = False
                    logger.warning("[%s] Failed to process reconcile response: %s", account_name, e)
                    notify_error(
                        event="reconcile_callback_parse",
                        message="Failed to process reconcile response",
                        exc=e,
                        **self._notify_ctx(account_name),
                    )

            def _on_reconcile_err(failure):
                self.reconcile_requested[account_name] = False
                notify_error(
                    event="reconcile_request_errback",
                    message="Reconcile request errback triggered",
                    exc=Exception(str(failure)),
                    **self._notify_ctx(account_name),
                )
                client._on_error(failure)

            d.addCallback(_on_reconcile)
            d.addErrback(_on_reconcile_err)
        except Exception as e:
            self.reconcile_requested[account_name] = False
            notify_error(
                event="send_reconcile_request",
                message="Failed to send reconcile request",
                exc=e,
                **self._notify_ctx(account_name),
            )

    def add_account(self, account: AccountConfig):
        if not account.enabled:
            logger.info("Skipping disabled account: %s", account.name)
            return

        if account.name in self.clients:
            msg = "Account already initialized; replacing existing client"
            logger.warning("[%s] %s", account.name, msg)
            notify_warning(
                event="account_reinitialized",
                message=msg,
                **self._notify_ctx(account.name),
            )
            self._unregister_route_magic(account.name)

        logger.info("Initializing account: %s", account.name)

        shared_state_file = self._resolve_shared_token_state_file(account)
        account_id = self._config_account_id(account)

        logger.info(
            "[%s] Token bootstrap: access=%s refresh_present=%s state_file=%s env=%s account_id=%s",
            account.name,
            self._token_preview(getattr(account, "access_token", "")),
            bool(self._safe_str(getattr(account, "refresh_token", ""))),
            shared_state_file or getattr(account, "token_state_file", None),
            getattr(account, "environment", None),
            account_id,
        )

        client = CTraderClient(
            env=account.environment,
            client_id=account.client_id,
            client_secret=account.client_secret,
        )

        client.set_account_credentials(
            account_id=account_id,
            access_token=account.access_token or "",
            refresh_token=account.refresh_token or "",
            token_state_file=shared_state_file,
            account_name=account.name,
        )

        self.clients[account.name] = client
        self.configs[account.name] = account
        self._ensure_account_maps(account.name)
        self._register_route_magic(account)

        def on_message(message, acc_name=account.name):
            try:
                self._process_message(acc_name, message)
            except Exception as e:
                notify_error(
                    event="account_message_callback",
                    message="Failed to parse/process account message",
                    exc=e,
                    **self._notify_ctx(acc_name),
                )

        client.set_message_callback(on_message)

        def on_connected():
            self._ensure_account_maps(account.name)
            self.reconcile_requested[account.name] = False
            self.auth_seen[account.name] = False
            logger.info(
                "✓ Account %s socket connected; waiting for app/account authorization",
                account.name,
            )

        client.connect(on_connect=on_connected)

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
        omap = self.order_maps.get(account_name) or {}
        return omap.get(int(mt5_ticket))

    def get_position_volume(self, account_name: str, position_id: int) -> Optional[int]:
        vol_map = self.position_volumes.get(account_name) or {}
        return vol_map.get(int(position_id))

    def get_ticket_volume(self, account_name: str, mt5_ticket: int) -> Optional[int]:
        pid = self.get_position_id(account_name, mt5_ticket)
        if not pid:
            return None
        return self.get_position_volume(account_name, pid)

    def get_account_name_by_magic(self, magic: int) -> Optional[str]:
        try:
            return self.route_magic_map.get(int(magic))
        except Exception:
            return None

    def get_account_context_by_magic(
        self, magic: int
    ) -> Tuple[Optional[str], Optional[CTraderClient], Optional[AccountConfig]]:
        account_name = self.get_account_name_by_magic(magic)
        if not account_name:
            return None, None, None
        return account_name, self.get_client(account_name), self.get_config(account_name)

    def store_mt5_payload(self, account_name: str, mt5_ticket: int, payload: dict):
        try:
            self._ensure_account_maps(account_name)
            self.mt5_payloads[account_name][int(mt5_ticket)] = dict(payload or {})
        except Exception:
            logger.debug(
                "[%s] Failed to store MT5 payload for ticket %s",
                account_name,
                mt5_ticket,
                exc_info=True,
            )

    def store_mt5_payload_by_magic(self, magic: int, mt5_ticket: int, payload: dict) -> bool:
        account_name = self.get_account_name_by_magic(magic)
        if not account_name:
            return False
        self.store_mt5_payload(account_name, mt5_ticket, payload)
        return True

    def remove_mapping(self, account_name: str, mt5_ticket: int):
        try:
            ticket = int(mt5_ticket)
            self._remove_order_mapping(account_name, ticket)
            self._remove_position_mapping(account_name, ticket)
            self.mt5_payloads.get(account_name, {}).pop(ticket, None)
        except Exception:
            pass

    def get_all_accounts(self) -> Dict[str, Tuple[CTraderClient, AccountConfig]]:
        return {
            name: (self.clients[name], self.configs[name])
            for name in self.clients.keys()
        }

    def getpositionid(self, account_name: str, mt5_ticket: int) -> Optional[int]:
        return self.get_position_id(account_name, mt5_ticket)

    def getorderid(self, account_name: str, mt5_ticket: int) -> Optional[int]:
        return self.get_order_id(account_name, mt5_ticket)

    def getpositionvolume(self, account_name: str, position_id: int) -> Optional[int]:
        return self.get_position_volume(account_name, position_id)

    def removemapping(self, account_name: str, mt5_ticket: int):
        self.remove_mapping(account_name, mt5_ticket)

    def getallaccounts(self) -> Dict[str, Tuple[CTraderClient, AccountConfig]]:
        return self.get_all_accounts()

    def getaccountnamebymagic(self, magic: int) -> Optional[str]:
        return self.get_account_name_by_magic(magic)

    def getaccountcontextbymagic(
        self, magic: int
    ) -> Tuple[Optional[str], Optional[CTraderClient], Optional[AccountConfig]]:
        return self.get_account_context_by_magic(magic)


_manager_instance = None


def get_account_manager() -> AccountManager:
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = AccountManager()
    return _manager_instance
