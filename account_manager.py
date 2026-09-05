"""
Account Manager for Multiple cTrader Connections

Manages multiple cTrader client connections for different accounts.

Important mapping lifecycle:
    Pending accepted:
        MT5 ticket -> cTrader orderId

    Pending filled / market order filled:
        MT5 ticket -> cTrader positionId
        old orderId mapping removed

    Closed:
        mappings removed

cTrader sends a position shell with volume=0 for ORDER_ACCEPTED. That shell is
not a running position and must never be saved as the live position mapping.
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


# Do not import ProtoOAExecutionType / ProtoOAPositionStatus.
# They are not exported by the generated protobuf module installed in this
# project, which caused every execution callback to fail.
#
# Values below are confirmed by your cTrader execution logs:
# - ORDER_ACCEPTED = 2
# - ORDER_FILLED = 3
# - ORDER_CANCELLED = 5
#
# POSITION_STATUS_CREATED / OPEN / CLOSED are treated defensively below:
# accepted position shells always have volume=0; live positions have volume>0.
ORDER_ACCEPTED = 2
ORDER_FILLED = 3
ORDER_CANCELLED = 5

POSITION_STATUS_CREATED = 1
POSITION_STATUS_OPEN = 2
POSITION_STATUS_CLOSED = 3


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

        # MT5 ticket -> cTrader running positionId.
        self.position_maps: Dict[str, Dict[int, int]] = {}

        # cTrader positionId -> live cTrader volume in units.
        self.position_volumes: Dict[str, Dict[int, int]] = {}

        # MT5 ticket -> cTrader pending orderId.
        # Kept as the original public order map for backward compatibility.
        self.order_maps: Dict[str, Dict[int, int]] = {}

        # Origin-aware state used to keep a LIMIT-origin position canonical when
        # a temporary MT5_<ticket> market fallback also exists.
        self.pending_types: Dict[str, Dict[int, str]] = {}
        self.pending_states: Dict[str, Dict[int, str]] = {}
        self.pending_position_maps: Dict[str, Dict[int, int]] = {}
        self.market_position_maps: Dict[str, Dict[int, int]] = {}
        self.market_order_maps: Dict[str, Dict[int, int]] = {}
        self.market_fallback_submitted: Dict[str, Dict[int, bool]] = {}

        self.account_equity: Dict[str, float] = {}
        self.account_balance: Dict[str, float] = {}

        # Stores latest MT5 event/payload per ticket. Used by the risk handler.
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
            trade_data = getattr(pos, "tradeData", None)
            if trade_data is None:
                return ""
            label = getattr(trade_data, "label", "")
            return label if isinstance(label, str) else ""
        except Exception:
            return ""

    @staticmethod
    def _extract_order_label(order) -> str:
        try:
            trade_data = getattr(order, "tradeData", None)
            if trade_data is None:
                return ""
            label = getattr(trade_data, "label", "")
            return label if isinstance(label, str) else ""
        except Exception:
            return ""

    @staticmethod
    def _label_to_ticket(label: str) -> Optional[int]:
        """
        Parse bridge labels.

        Supported formats:
            MT5_<ticket>          canonical market-copy label
            MT5_PENDING_<ticket>  follower pending-order label
            MT5<ticket>           legacy bridge label

        Examples:
            MT5_5854323          -> 5854323
            MT5_PENDING_5854323  -> 5854323
            MT55854323           -> 5854323
        """
        if not isinstance(label, str):
            return None

        value = label.strip()

        if value.startswith("MT5_PENDING_"):
            suffix = value[len("MT5_PENDING_"):]
        elif value.startswith("MT5_"):
            suffix = value[len("MT5_"):]
        elif value.startswith("MT5"):
            suffix = value[len("MT5"):]
        else:
            return None

        try:
            return int(suffix) if suffix.isdigit() else None
        except Exception:
            return None

    @staticmethod
    def _extract_position_volume(pos) -> int:
        """
        Return positive live volume. A zero value means no running volume.

        cTrader's ORDER_ACCEPTED callback may include a position object with
        positionId but zero volume. That object is only a pending-order shell.
        """
        try:
            trade_data = getattr(pos, "tradeData", None)
            if trade_data is not None:
                volume = getattr(trade_data, "volume", 0)
                if int(volume or 0) > 0:
                    return int(volume)
        except Exception:
            pass

        try:
            volume = getattr(pos, "volume", 0)
            return int(volume) if int(volume or 0) > 0 else 0
        except Exception:
            return 0

    @staticmethod
    def _extract_account_equity_balance(
        reconcile_res,
    ) -> Tuple[Optional[float], Optional[float]]:
        try:
            account_obj = getattr(reconcile_res, "account", None)
            if account_obj is None:
                return None, None

            if hasattr(account_obj, "__iter__") and not isinstance(
                account_obj,
                (bytes, str),
            ):
                first_account = None
                for account in account_obj:
                    first_account = account
                    break
                account_obj = first_account

            if account_obj is None:
                return None, None

            equity = getattr(account_obj, "equity", None)
            balance = getattr(account_obj, "balance", None)

            equity_float = float(equity) if equity is not None else None
            balance_float = float(balance) if balance is not None else None

            return equity_float, balance_float
        except Exception:
            return None, None

    @staticmethod
    def _config_account_id(config: AccountConfig) -> Optional[int]:
        try:
            value = getattr(config, "account_id", None)
            if value is not None and str(value).strip() != "":
                return int(value)
        except Exception:
            pass

        try:
            value = getattr(config, "accountid", None)
            if value is not None and str(value).strip() != "":
                return int(value)
        except Exception:
            pass

        return None

    @staticmethod
    def _config_route_magic(config: AccountConfig) -> Optional[int]:
        try:
            value = getattr(config, "route_magic_number", None)
            if value is not None and str(value).strip() != "":
                return int(value)
        except Exception:
            pass

        try:
            value = getattr(config, "magic_number", None)
            if value is not None and str(value).strip() != "":
                return int(value)
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
        token = str(token or "")
        if len(token) <= 10:
            return token or "<empty>"
        return f"{token[:6]}...{token[-4:]}"

    def _notify_ctx(self, account_name: Optional[str] = None, **extra):
        context = {"account_name": account_name}
        context.update(extra)
        return context

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

    def _resolve_shared_token_state_file(
        self,
        account: AccountConfig,
    ) -> Optional[str]:
        token_key = self._build_token_group_key(account)
        configured_state_file = self._safe_str(
            getattr(account, "token_state_file", ""),
        )

        existing = self.shared_token_files.get(token_key)

        if existing is not None:
            if configured_state_file and configured_state_file != existing:
                message = (
                    "Shared token group detected; overriding token_state_file "
                    f"{configured_state_file} -> {existing}"
                )

                logger.warning("[%s] %s", account.name, message)

                notify_warning(
                    event="shared_token_state_override",
                    message=message,
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

    def _ensure_account_maps(self, account_name: str):
        if account_name not in self.position_maps:
            self.position_maps[account_name] = {}

        if account_name not in self.position_volumes:
            self.position_volumes[account_name] = {}

        if account_name not in self.order_maps:
            self.order_maps[account_name] = {}

        if account_name not in self.pending_types:
            self.pending_types[account_name] = {}

        if account_name not in self.pending_states:
            self.pending_states[account_name] = {}

        if account_name not in self.pending_position_maps:
            self.pending_position_maps[account_name] = {}

        if account_name not in self.market_position_maps:
            self.market_position_maps[account_name] = {}

        if account_name not in self.market_order_maps:
            self.market_order_maps[account_name] = {}

        if account_name not in self.market_fallback_submitted:
            self.market_fallback_submitted[account_name] = {}

        if account_name not in self.mt5_payloads:
            self.mt5_payloads[account_name] = {}

        if account_name not in self.reconcile_requested:
            self.reconcile_requested[account_name] = False

        if account_name not in self.auth_seen:
            self.auth_seen[account_name] = False

    def _register_route_magic(self, account: AccountConfig):
        route_magic = self._config_route_magic(account)

        if route_magic is None:
            logger.info(
                "[%s] No route_magic_number configured; "
                "magic-based routing unavailable",
                account.name,
            )
            return

        existing_account = self.route_magic_map.get(int(route_magic))
        if existing_account and existing_account != account.name:
            raise ValueError(
                f"Duplicate route_magic_number={int(route_magic)} for accounts "
                f"{existing_account!r} and {account.name!r}"
            )

        self.route_magic_map[int(route_magic)] = account.name

        logger.info(
            "[%s] Registered route magic %s",
            account.name,
            int(route_magic),
        )

    def _unregister_route_magic(self, account_name: str):
        stale_magics = [
            magic
            for magic, mapped_name in self.route_magic_map.items()
            if mapped_name == account_name
        ]

        for magic in stale_magics:
            self.route_magic_map.pop(magic, None)
            logger.info("[%s] Unregistered route magic %s", account_name, magic)

    def _cache_funds_from_reconcile(self, account_name: str, reconcile_res):
        equity, balance = self._extract_account_equity_balance(reconcile_res)

        if equity is not None:
            self.account_equity[account_name] = float(equity)

        if balance is not None:
            self.account_balance[account_name] = float(balance)

        if equity is not None or balance is not None:
            logger.info(
                "[%s] Funds cached: equity=%s, balance=%s",
                account_name,
                self.account_equity.get(account_name),
                self.account_balance.get(account_name),
            )

    @staticmethod
    def _normalize_pending_type_value(pending_type):
        value = str(pending_type or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "buy_limit": "limit",
            "sell_limit": "limit",
            "buylimit": "limit",
            "selllimit": "limit",
            "buy_stop": "stop",
            "sell_stop": "stop",
            "buystop": "stop",
            "sellstop": "stop",
            "buy_stop_limit": "stop_limit",
            "sell_stop_limit": "stop_limit",
            "buystoplimit": "stop_limit",
            "sellstoplimit": "stop_limit",
            "stoplimit": "stop_limit",
        }
        return aliases.get(value, value)

    @staticmethod
    def _extract_order_pending_type(order):
        """Best-effort textual pending-type extraction; never guess numeric enums."""
        for obj in (order, getattr(order, "tradeData", None)):
            if obj is None:
                continue
            for name in ("orderType", "order_type", "type", "pendingType", "pending_type"):
                try:
                    value = getattr(obj, name, None)
                except Exception:
                    value = None
                if value is None:
                    continue
                text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
                if text.isdigit():
                    continue
                if "stop_limit" in text or "stoplimit" in text:
                    return "stop_limit"
                if "limit" in text:
                    return "limit"
                if "stop" in text:
                    return "stop"
        return None

    @staticmethod
    def _position_origin_from_label(label):
        value = str(label or "").strip()
        if value.startswith("MT5_PENDING_"):
            return "pending"
        if value.startswith("MT5_") or value.startswith("MT5"):
            return "market"
        return None

    def _select_canonical_position(self, account_name: str, mt5_ticket: int, notify=True):
        """Select canonical position deterministically: pending-origin wins over market."""
        self._ensure_account_maps(account_name)
        ticket = int(mt5_ticket)
        pending_id = self.pending_position_maps[account_name].get(ticket)
        market_id = self.market_position_maps[account_name].get(ticket)
        previous = self.position_maps[account_name].get(ticket)

        canonical = pending_id or market_id
        if canonical:
            self.position_maps[account_name][ticket] = int(canonical)
            if previous != int(canonical):
                logger.info(
                    "[%s] Canonical mapping ticket=%s -> positionId=%s "
                    "origin=%s pending=%s market=%s state=%s",
                    account_name,
                    ticket,
                    int(canonical),
                    "pending" if pending_id else "market",
                    pending_id,
                    market_id,
                    self.pending_states[account_name].get(ticket),
                )
            if notify:
                notify_position_update(account_name, ticket, self)
            return int(canonical)

        self.position_maps[account_name].pop(ticket, None)
        if previous and notify:
            notify_position_update(account_name, ticket, self)
        return None

    def _store_order_mapping(
        self,
        account_name: str,
        mt5_ticket: int,
        order_id: int,
        pending_type=None,
        state="PENDING",
    ):
        """Store a pending cTrader orderId for an MT5 ticket."""
        if int(order_id or 0) <= 0:
            return

        self._ensure_account_maps(account_name)
        ticket = int(mt5_ticket)
        oid = int(order_id)
        previous_order_id = self.order_maps[account_name].get(ticket)
        self.order_maps[account_name][ticket] = oid

        if pending_type:
            self.pending_types[account_name][ticket] = self._normalize_pending_type_value(pending_type)
        if state:
            self.pending_states[account_name][ticket] = str(state).upper()

        if previous_order_id != oid:
            logger.info(
                "[%s] MT5 ticket %s -> cTrader pending orderId %s type=%s state=%s",
                account_name,
                ticket,
                oid,
                self.pending_types[account_name].get(ticket),
                self.pending_states[account_name].get(ticket),
            )

    def _remove_order_mapping(self, account_name: str, mt5_ticket: int, clear_state=False):
        self._ensure_account_maps(account_name)
        ticket = int(mt5_ticket)
        removed = self.order_maps[account_name].pop(ticket, None)
        if removed:
            logger.info(
                "[%s] Removed MT5 ticket %s -> stale cTrader orderId %s mapping",
                account_name,
                ticket,
                int(removed),
            )
        if clear_state:
            self.pending_types[account_name].pop(ticket, None)
            self.pending_states[account_name].pop(ticket, None)

    def register_pending(self, account_name, ticket, pending_type, order_id=0):
        self._ensure_account_maps(account_name)
        ticket = int(ticket)
        ptype = self._normalize_pending_type_value(pending_type)
        if ptype not in ("limit", "stop", "stop_limit"):
            raise ValueError(f"Unsupported pending type: {pending_type}")
        self.pending_types[account_name][ticket] = ptype
        self.pending_states[account_name][ticket] = "PENDING"
        if int(order_id or 0) > 0:
            self._store_order_mapping(account_name, ticket, int(order_id), ptype, "PENDING")

    def get_pending_type(self, account_name, ticket):
        return (self.pending_types.get(account_name) or {}).get(int(ticket))

    def set_pending_state(self, account_name, ticket, state):
        self._ensure_account_maps(account_name)
        value = str(state or "UNKNOWN").upper()
        self.pending_states[account_name][int(ticket)] = value
        logger.info(
            "[%s] Pending state ticket=%s -> %s",
            account_name,
            int(ticket),
            value,
        )

    def get_pending_state(self, account_name, ticket):
        return (self.pending_states.get(account_name) or {}).get(int(ticket))

    def get_pending_position_id(self, account_name, ticket):
        return (self.pending_position_maps.get(account_name) or {}).get(int(ticket))

    def get_market_position_id(self, account_name, ticket):
        return (self.market_position_maps.get(account_name) or {}).get(int(ticket))

    def get_fallback_position_id(self, account_name, ticket):
        pending_id = self.get_pending_position_id(account_name, ticket)
        market_id = self.get_market_position_id(account_name, ticket)
        if pending_id and market_id:
            return int(market_id)
        return None

    def is_market_fallback(self, account_name, ticket):
        return self.get_fallback_position_id(account_name, ticket) is not None

    def register_market_order(self, account_name, ticket, order_id=0, fallback=False):
        self._ensure_account_maps(account_name)
        ticket = int(ticket)
        if int(order_id or 0) > 0:
            self.market_order_maps[account_name][ticket] = int(order_id)
        if fallback:
            self.market_fallback_submitted[account_name][ticket] = True
        logger.info(
            "[%s] Market order registered ticket=%s orderId=%s fallback=%s",
            account_name,
            ticket,
            int(order_id) if int(order_id or 0) > 0 else None,
            fallback,
        )

    def get_market_order_id(self, account_name, ticket):
        return (self.market_order_maps.get(account_name) or {}).get(int(ticket))

    def has_market_fallback_submitted(self, account_name, ticket):
        return bool((self.market_fallback_submitted.get(account_name) or {}).get(int(ticket)))

    def clear_market_order(self, account_name, ticket):
        self._ensure_account_maps(account_name)
        self.market_order_maps[account_name].pop(int(ticket), None)

    def _store_position_mapping(
        self,
        account_name: str,
        mt5_ticket: int,
        position_id: int,
        origin=None,
    ):
        """Store a live position while preserving pending-vs-market origin."""
        if int(position_id or 0) <= 0:
            return

        self._ensure_account_maps(account_name)
        ticket = int(mt5_ticket)
        pid = int(position_id)

        if origin == "pending":
            self.pending_position_maps[account_name][ticket] = pid
            self.pending_states[account_name][ticket] = "ACTIVATED"
            self._remove_order_mapping(account_name, ticket)
        elif origin == "market":
            self.market_position_maps[account_name][ticket] = pid
            self.market_order_maps[account_name].pop(ticket, None)
        else:
            # Preserve legacy callers, but prefer an already-known pending origin.
            if self.pending_types[account_name].get(ticket):
                self.pending_position_maps[account_name][ticket] = pid
                self.pending_states[account_name][ticket] = "ACTIVATED"
                self._remove_order_mapping(account_name, ticket)
            else:
                self.market_position_maps[account_name][ticket] = pid
                self.market_order_maps[account_name].pop(ticket, None)

        self._select_canonical_position(account_name, ticket)

    def _remove_position_mapping(self, account_name: str, mt5_ticket: int, origin=None):
        self._ensure_account_maps(account_name)
        ticket = int(mt5_ticket)

        if origin == "pending":
            pid = self.pending_position_maps[account_name].pop(ticket, None)
            if pid:
                self.position_volumes[account_name].pop(int(pid), None)
        elif origin == "market":
            pid = self.market_position_maps[account_name].pop(ticket, None)
            if pid:
                self.position_volumes[account_name].pop(int(pid), None)
        else:
            for mapping in (
                self.pending_position_maps[account_name],
                self.market_position_maps[account_name],
            ):
                pid = mapping.pop(ticket, None)
                if pid:
                    self.position_volumes[account_name].pop(int(pid), None)

        self._select_canonical_position(account_name, ticket)

    def _store_position_volume(self, account_name: str, position_id: int, volume: int):
        if int(position_id or 0) <= 0:
            return

        self._ensure_account_maps(account_name)
        pid = int(position_id)
        if int(volume or 0) > 0:
            previous_volume = self.position_volumes[account_name].get(pid)
            self.position_volumes[account_name][pid] = int(volume)
            if previous_volume != int(volume):
                logger.info(
                    "[%s] positionId %s volume=%s cached",
                    account_name,
                    pid,
                    int(volume),
                )
        else:
            self.position_volumes[account_name].pop(pid, None)

    def _handle_execution_order(self, account_name: str, extracted):
        order = getattr(extracted, "order", None)
        if order is None:
            return

        order_id = self._to_int(getattr(order, "orderId", 0), default=0)
        label = self._extract_order_label(order)
        ticket = self._label_to_ticket(label)
        if order_id <= 0 or ticket is None:
            return

        origin = self._position_origin_from_label(label)
        if origin == "pending":
            if self.get_pending_position_id(account_name, int(ticket)):
                logger.info(
                    "[%s] Ignoring stale pending orderId=%s for already activated ticket=%s",
                    account_name,
                    order_id,
                    ticket,
                )
                return
            self._store_order_mapping(
                account_name,
                int(ticket),
                int(order_id),
                self.get_pending_type(account_name, int(ticket)),
                self.get_pending_state(account_name, int(ticket)) or "PENDING",
            )
        elif origin == "market":
            self.register_market_order(
                account_name,
                int(ticket),
                int(order_id),
                self.has_market_fallback_submitted(account_name, int(ticket)),
            )

    def _handle_execution_position(self, account_name: str, extracted):
        position = getattr(extracted, "position", None)
        if position is None:
            return

        execution_type = self._to_int(getattr(extracted, "executionType", 0), default=0)
        position_status = self._to_int(getattr(position, "positionStatus", 0), default=0)
        position_id = self._to_int(getattr(position, "positionId", 0), default=0)
        label = self._extract_position_label(position)
        ticket = self._label_to_ticket(label)
        volume = self._extract_position_volume(position)

        if ticket is None:
            return

        is_order_accepted = execution_type == ORDER_ACCEPTED
        is_order_filled = execution_type == ORDER_FILLED
        is_order_cancelled = execution_type == ORDER_CANCELLED
        is_live_position = position_id > 0 and volume > 0
        is_zero_volume_shell = position_id > 0 and volume <= 0
        origin = self._position_origin_from_label(label)

        logger.info(
            "[%s] Execution position | ticket=%s positionId=%s volume=%s "
            "executionType=%s positionStatus=%s origin=%s accepted=%s filled=%s "
            "cancelled=%s live=%s shell=%s",
            account_name,
            ticket,
            position_id,
            volume,
            execution_type,
            position_status,
            origin,
            is_order_accepted,
            is_order_filled,
            is_order_cancelled,
            is_live_position,
            is_zero_volume_shell,
        )

        if is_order_accepted and is_zero_volume_shell:
            logger.info(
                "[%s] Accepted order shell retained as order mapping only | "
                "ticket=%s positionId=%s positionStatus=%s",
                account_name,
                ticket,
                position_id,
                position_status,
            )
            return

        if is_live_position:
            self._store_position_mapping(
                account_name,
                int(ticket),
                int(position_id),
                origin=origin,
            )
            self._store_position_volume(account_name, int(position_id), int(volume))

            self._try_enforce_max_risk_on_fill(
                account_name,
                extracted,
                position,
                int(position_id),
                int(ticket),
            )
            return

        if is_order_cancelled:
            if origin == "pending" or self.get_order_id(account_name, int(ticket)):
                if self.get_pending_position_id(account_name, int(ticket)):
                    self.set_pending_state(account_name, int(ticket), "ACTIVATED")
                else:
                    self._remove_order_mapping(account_name, int(ticket))
                    self.set_pending_state(account_name, int(ticket), "CANCELLED")
            elif origin == "market":
                self.clear_market_order(account_name, int(ticket))
            return

        if is_order_filled and is_zero_volume_shell and position_status == POSITION_STATUS_CLOSED:
            if origin == "pending":
                self._remove_order_mapping(account_name, int(ticket))
                if not self.get_pending_position_id(account_name, int(ticket)):
                    self.set_pending_state(account_name, int(ticket), "CANCELLED")
            elif origin == "market":
                self.clear_market_order(account_name, int(ticket))
                self._remove_position_mapping(account_name, int(ticket), origin="market")
            return

        logger.info(
            "[%s] Ignored non-live execution position update | "
            "ticket=%s positionId=%s volume=%s executionType=%s positionStatus=%s",
            account_name,
            ticket,
            position_id,
            volume,
            execution_type,
            position_status,
        )

    def _try_enforce_max_risk_on_fill(
        self,
        account_name: str,
        extracted,
        position,
        position_id: int,
        ticket: Optional[int],
    ):
        try:
            if int(position_id or 0) <= 0 or ticket is None:
                return

            volume = self._extract_position_volume(position)
            if volume <= 0:
                return

            execution_type = self._to_int(getattr(extracted, "executionType", 0), default=0)
            if execution_type != ORDER_FILLED:
                return

            config = self.get_config(account_name)
            client = self.get_client(account_name)
            if not config or not client:
                return

            trade_data = getattr(position, "tradeData", None) or position
            symbol_id = self._to_int(getattr(trade_data, "symbolId", 0), default=0)
            if symbol_id <= 0:
                return

            symbol = client.symbol_details.get(int(symbol_id)) if hasattr(client, "symbol_details") else None
            if symbol is None:
                logger.warning(
                    "[%s] Over-risk check skipped: symbol details missing | "
                    "ticket=%s positionId=%s symbolId=%s",
                    account_name,
                    ticket,
                    position_id,
                    symbol_id,
                )
                return

            mt5_data = self.mt5_payloads.get(account_name, {}).get(int(ticket), None)
            _enforce_max_risk_on_fill(
                account_name=account_name,
                client=client,
                config=config,
                account_manager=self,
                position=position,
                symbol=symbol,
                mt5_symbol=None,
                mt5_data=mt5_data,
            )
        except Exception as error:
            logger.debug(
                "[%s] Over-risk enforcement failed | ticket=%s positionId=%s error=%s",
                account_name,
                ticket,
                position_id,
                error,
                exc_info=True,
            )

    def _handle_reconcile_positions(self, account_name: str, extracted) -> int:
        """Rebuild origin-aware position state before selecting canonical mappings."""
        self._ensure_account_maps(account_name)
        positions = list(getattr(extracted, "position", []) or [])
        active_position_ids = set()
        active_pending_tickets = set()
        active_market_tickets = set()
        count = 0

        # First collect every live position. Canonical selection happens only
        # after the complete snapshot is known, so iteration order cannot make
        # MT5_<ticket> overwrite MT5_PENDING_<ticket>.
        for position in positions:
            position_id = self._to_int(getattr(position, "positionId", 0), default=0)
            volume = self._extract_position_volume(position)
            if position_id <= 0 or volume <= 0:
                continue

            active_position_ids.add(int(position_id))
            self._store_position_volume(account_name, int(position_id), int(volume))

            label = self._extract_position_label(position)
            ticket = self._label_to_ticket(label)
            origin = self._position_origin_from_label(label)
            if ticket is None or origin is None:
                continue

            ticket = int(ticket)
            if origin == "pending":
                self.pending_position_maps[account_name][ticket] = int(position_id)
                self.pending_states[account_name][ticket] = "ACTIVATED"
                active_pending_tickets.add(ticket)
            else:
                self.market_position_maps[account_name][ticket] = int(position_id)
                self.market_order_maps[account_name].pop(ticket, None)
                active_market_tickets.add(ticket)

            count += 1

            logger.info(
                "[%s] (reconcile pos) ticket=%s positionId=%s volume=%s origin=%s",
                account_name,
                ticket,
                int(position_id),
                int(volume),
                origin,
            )

        # Remove origin-specific positions absent from the non-empty snapshot.
        for ticket, position_id in list(self.pending_position_maps[account_name].items()):
            if ticket not in active_pending_tickets:
                self.pending_position_maps[account_name].pop(ticket, None)
                self.position_volumes[account_name].pop(int(position_id), None)

        for ticket, position_id in list(self.market_position_maps[account_name].items()):
            if ticket not in active_market_tickets:
                self.market_position_maps[account_name].pop(ticket, None)
                self.position_volumes[account_name].pop(int(position_id), None)

        # Re-select canonical after both origins have been collected.
        all_tickets = (
            set(active_pending_tickets)
            | set(active_market_tickets)
            | set(self.position_maps.get(account_name, {}).keys())
        )
        for ticket in all_tickets:
            self._select_canonical_position(account_name, int(ticket))

        stale_position_ids = [
            position_id
            for position_id in self.position_volumes.get(account_name, {}).keys()
            if position_id not in active_position_ids
        ]
        for position_id in stale_position_ids:
            self.position_volumes[account_name].pop(position_id, None)
            logger.info(
                "[%s] Removed stale cached volume for positionId %s",
                account_name,
                position_id,
            )

        return count

    def _handle_reconcile_orders(self, account_name: str, extracted) -> int:
        self._ensure_account_maps(account_name)
        orders = list(getattr(extracted, "order", []) or [])
        active_pending_tickets = set()
        active_market_tickets = set()
        order_count = 0

        for order in orders:
            order_id = self._to_int(getattr(order, "orderId", 0), default=0)
            label = self._extract_order_label(order)
            ticket = self._label_to_ticket(label)
            if order_id <= 0 or ticket is None:
                continue

            ticket = int(ticket)
            origin = self._position_origin_from_label(label)

            if origin == "pending":
                # A pending-origin position is canonical; the old pending order
                # may still appear in a reconcile snapshot during the race.
                if self.get_pending_position_id(account_name, ticket):
                    continue

                active_pending_tickets.add(ticket)
                pending_type = (
                    self.get_pending_type(account_name, ticket)
                    or self._extract_order_pending_type(order)
                )
                self._store_order_mapping(
                    account_name,
                    ticket,
                    int(order_id),
                    pending_type,
                    "PENDING",
                )
                order_count += 1

                logger.info(
                    "[%s] (reconcile order) MT5 ticket %s -> cTrader pending orderId %s type=%s",
                    account_name,
                    ticket,
                    int(order_id),
                    self.get_pending_type(account_name, ticket),
                )

            elif origin == "market":
                active_market_tickets.add(ticket)
                self.register_market_order(
                    account_name,
                    ticket,
                    int(order_id),
                    self.has_market_fallback_submitted(account_name, ticket),
                )

        for ticket in list(self.order_maps.get(account_name, {})):
            if ticket not in active_pending_tickets and not self.get_pending_position_id(account_name, int(ticket)):
                self._remove_order_mapping(account_name, int(ticket))

        for ticket in list(self.market_order_maps.get(account_name, {})):
            if ticket not in active_market_tickets and not self.get_market_position_id(account_name, int(ticket)):
                self.market_order_maps[account_name].pop(ticket, None)

        return order_count

    def _process_reconcile(self, account_name: str, extracted):
        self._cache_funds_from_reconcile(account_name, extracted)
        position_count = self._handle_reconcile_positions(account_name, extracted)

        try:
            order_count = self._handle_reconcile_orders(account_name, extracted)
        except Exception as error:
            logger.debug("[%s] Failed parsing reconcile orders", account_name, exc_info=True)
            notify_error(
                event="reconcile_parse_orders",
                message="Failed parsing reconcile orders",
                exc=error,
                **self._notify_ctx(account_name),
            )
            order_count = 0

        logger.info(
            "[%s] Reconcile complete: %s MT5 positions "
            "(%s positions with volume cached), %s orders mapped",
            account_name,
            position_count,
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

        position = extracted.position
        position_id = self._to_int(getattr(position, "positionId", 0), default=0)
        if position_id <= 0:
            return

        label = self._extract_position_label(position)
        ticket = self._label_to_ticket(label)
        if ticket is None:
            return

        volume = self._extract_position_volume(position)
        if volume <= 0:
            logger.info(
                "[%s] Ignored non-execution zero-volume position update | "
                "ticket=%s positionId=%s",
                account_name,
                ticket,
                position_id,
            )
            return

        origin = self._position_origin_from_label(label)
        if origin is None:
            return

        self._store_position_mapping(
            account_name,
            int(ticket),
            int(position_id),
            origin=origin,
        )
        self._store_position_volume(account_name, int(position_id), int(volume))

        logger.info(
            "[%s] Updated MT5 ticket %s -> cTrader positionId %s, volume=%s",
            account_name,
            int(ticket),
            int(position_id),
            int(volume),
        )

    def _send_reconcile_request(self, account_name: str):
        client = self.get_client(account_name)
        config = self.get_config(account_name)

        if not client or not config:
            message = "Cannot send reconcile: missing client/config"
            logger.warning("[%s] %s", account_name, message)
            notify_warning(
                event="reconcile_missing_context",
                message=message,
                **self._notify_ctx(account_name),
            )
            return

        account_id = self._config_account_id(config)
        if not account_id:
            message = "Cannot send reconcile: missing account_id"
            logger.warning("[%s] %s", account_name, message)
            notify_warning(
                event="reconcile_missing_account_id",
                message=message,
                **self._notify_ctx(account_name),
            )
            return

        if self.reconcile_requested.get(account_name, False):
            logger.info("[%s] Reconcile already requested for this connection", account_name)
            return

        try:
            request = ProtoOAReconcileReq()
            request.ctidTraderAccountId = int(account_id)
            logger.info("[%s] Sending reconcile request...", account_name)

            deferred = client.send(request)
            self.reconcile_requested[account_name] = True

            def _on_reconcile(result):
                try:
                    response = Protobuf.extract(result)
                    if isinstance(response, ProtoOAReconcileRes):
                        self.reconcile_requested[account_name] = False
                        self._process_reconcile(account_name, response)
                        logger.info("[%s] Reconcile response processed", account_name)
                    else:
                        logger.info(
                            "[%s] Reconcile callback received message type %s",
                            account_name,
                            type(response).__name__,
                        )
                except Exception as error:
                    self.reconcile_requested[account_name] = False
                    logger.warning("[%s] Failed to process reconcile response: %s", account_name, error)
                    notify_error(
                        event="reconcile_callback_parse",
                        message="Failed to process reconcile response",
                        exc=error,
                        **self._notify_ctx(account_name),
                    )
                return result

            def _on_reconcile_error(failure):
                self.reconcile_requested[account_name] = False
                notify_error(
                    event="reconcile_request_errback",
                    message="Reconcile request errback triggered",
                    exc=Exception(str(failure)),
                    **self._notify_ctx(account_name),
                )
                try:
                    client._on_error(failure)
                except Exception:
                    logger.debug(
                        "[%s] Failed forwarding reconcile error to client",
                        account_name,
                        exc_info=True,
                    )
                return failure

            deferred.addCallback(_on_reconcile)
            deferred.addErrback(_on_reconcile_error)
        except Exception as error:
            self.reconcile_requested[account_name] = False
            notify_error(
                event="send_reconcile_request",
                message="Failed to send reconcile request",
                exc=error,
                **self._notify_ctx(account_name),
            )

    def add_account(self, account: AccountConfig):
        if not account.enabled:
            logger.info("Skipping disabled account: %s", account.name)
            return

        if account.name in self.clients:
            message = "Account already initialized; replacing existing client"
            logger.warning("[%s] %s", account.name, message)
            notify_warning(
                event="account_reinitialized",
                message=message,
                **self._notify_ctx(account.name),
            )
            self._unregister_route_magic(account.name)

        logger.info("Initializing account: %s", account.name)
        shared_state_file = self._resolve_shared_token_state_file(account)
        account_id = self._config_account_id(account)

        logger.info(
            "[%s] Token bootstrap: access=%s refresh_present=%s "
            "state_file=%s env=%s account_id=%s",
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

        def on_message(message, account_name=account.name):
            try:
                self._process_message(account_name, message)
            except Exception as error:
                notify_error(
                    event="account_message_callback",
                    message="Failed to parse/process account message",
                    exc=error,
                    **self._notify_ctx(account_name),
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

    def request_reconcile(self, account_name: str):
        """Request a fresh cTrader reconcile snapshot for an account."""
        self._send_reconcile_request(account_name)

    def get_position_id(self, account_name: str, mt5_ticket: int) -> Optional[int]:
        position_map = self.position_maps.get(account_name) or {}
        return position_map.get(int(mt5_ticket))

    def get_order_id(self, account_name: str, mt5_ticket: int) -> Optional[int]:
        order_map = self.order_maps.get(account_name) or {}
        return order_map.get(int(mt5_ticket))

    def get_position_volume(self, account_name: str, position_id: int) -> Optional[int]:
        volume_map = self.position_volumes.get(account_name) or {}
        return volume_map.get(int(position_id))

    def get_ticket_volume(self, account_name: str, mt5_ticket: int) -> Optional[int]:
        position_id = self.get_position_id(account_name, mt5_ticket)
        if not position_id:
            return None
        return self.get_position_volume(account_name, position_id)

    def get_account_name_by_magic(self, magic: int) -> Optional[str]:
        try:
            return self.route_magic_map.get(int(magic))
        except Exception:
            return None

    def get_account_context_by_magic(
        self,
        magic: int,
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
        """Remove all bridge state for a fully closed/cancelled MT5 ticket."""
        try:
            ticket = int(mt5_ticket)
            self._remove_order_mapping(account_name, ticket, clear_state=True)
            self._remove_position_mapping(account_name, ticket)
            self.market_order_maps.get(account_name, {}).pop(ticket, None)
            self.market_fallback_submitted.get(account_name, {}).pop(ticket, None)
            self.pending_types.get(account_name, {}).pop(ticket, None)
            self.pending_states.get(account_name, {}).pop(ticket, None)
            self.mt5_payloads.get(account_name, {}).pop(ticket, None)
        except Exception:
            logger.debug(
                "[%s] Failed removing mappings for ticket %s",
                account_name,
                mt5_ticket,
                exc_info=True,
            )

    def get_all_accounts(self) -> Dict[str, Tuple[CTraderClient, AccountConfig]]:
        return {
            account_name: (self.clients[account_name], self.configs[account_name])
            for account_name in self.clients.keys()
            if account_name in self.configs
        }

    # Backward-compatible aliases used by older trade_processor.py versions.
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
        self,
        magic: int,
    ) -> Tuple[Optional[str], Optional[CTraderClient], Optional[AccountConfig]]:
        return self.get_account_context_by_magic(magic)


_manager_instance = None


def get_account_manager() -> AccountManager:
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = AccountManager()
    return _manager_instance
