"""
Account Manager for Multiple cTrader Connections

Manages multiple cTrader client connections for different accounts.

Important mapping lifecycle:

    Pending accepted:
        MT5 ticket -> cTrader pending orderId

    Pending filled:
        MT5 ticket -> cTrader pending-originated positionId
        pending order mapping removed

    LIMIT race:
        MT5_PENDING_<ticket> -> canonical pending-originated positionId
        MT5_<ticket>         -> temporary fallback market positionId

    LIMIT cancellation wins:
        MT5 ticket -> canonical market positionId

    Closed:
        mappings removed

Important cTrader behavior:

    cTrader may send a position shell with volume=0 for ORDER_ACCEPTED.
    That shell is NOT a running position and must never be saved as a live
    position mapping.

Canonical mapping priority:

    1. MT5_PENDING_<ticket> position
       This is a destination position created by the follower pending order.

    2. MT5_<ticket> position
       This is either:
         - a normal/direct market-copy position, or
         - a temporary LIMIT fallback market position.

If both exist at the same time, the MT5_PENDING_<ticket> position ALWAYS
remains canonical. Event arrival order must not change this.
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
# project, which caused execution callbacks to fail.
#
# Values confirmed by cTrader execution logs:
#
# ORDER_ACCEPTED = 2
# ORDER_FILLED   = 3
# ORDER_CANCELLED = 5
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

        # ------------------------------------------------------------------
        # CANONICAL POSITION
        #
        # MT5 ticket -> canonical cTrader running positionId
        #
        # This is the position that trade_processor should normally use when
        # modifying/closing the copied trade.
        # ------------------------------------------------------------------
        self.position_maps: Dict[str, Dict[int, int]] = {}

        # cTrader positionId -> live cTrader volume in units.
        self.position_volumes: Dict[str, Dict[int, int]] = {}

        # ------------------------------------------------------------------
        # PENDING ORDER
        #
        # MT5 ticket -> cTrader pending orderId
        #
        # This ONLY represents the actual follower pending order.
        # A fallback market order must NOT overwrite this mapping.
        # ------------------------------------------------------------------
        self.order_maps: Dict[str, Dict[int, int]] = {}

        # ------------------------------------------------------------------
        # FALLBACK MARKET ORDER
        #
        # MT5 ticket -> cTrader market orderId
        #
        # Used for LIMIT pending -> market fallback race handling.
        # ------------------------------------------------------------------
        self.fallback_market_order_maps: Dict[str, Dict[int, int]] = {}

        # ------------------------------------------------------------------
        # PENDING-ORIGIN POSITION
        #
        # MT5 ticket -> cTrader positionId created by MT5_PENDING_<ticket>
        #
        # This is kept separately from position_maps so that a later
        # MT5_<ticket> market position cannot overwrite it.
        # ------------------------------------------------------------------
        self.pending_position_maps: Dict[str, Dict[int, int]] = {}

        # ------------------------------------------------------------------
        # FALLBACK MARKET POSITION
        #
        # MT5 ticket -> cTrader positionId created by MT5_<ticket>
        #
        # This is temporary when a LIMIT pending races with a market
        # fallback. It is NOT canonical while a pending-originated position
        # exists.
        # ------------------------------------------------------------------
        self.fallback_market_position_maps: Dict[str, Dict[int, int]] = {}

        # ------------------------------------------------------------------
        # PENDING TYPE
        #
        # MT5 ticket -> "limit" / "stop" / "stop_limit"
        #
        # Used by trade_processor/trade_executor when deciding how a source
        # pending order should transition.
        # ------------------------------------------------------------------
        self.pending_types: Dict[str, Dict[int, str]] = {}

        self.account_equity: Dict[str, float] = {}
        self.account_balance: Dict[str, float] = {}

        # Stores latest MT5 event/payload per ticket. Used by risk handler.
        self.mt5_payloads: Dict[str, Dict[int, dict]] = {}

        self.reconcile_requested: Dict[str, bool] = {}
        self.auth_seen: Dict[str, bool] = {}

        self.route_magic_map: Dict[int, str] = {}
        self.shared_token_files: Dict[str, str] = {}

    # ======================================================================
    # BASIC HELPERS
    # ======================================================================

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

            MT5_<ticket>
                canonical/direct market-copy label

            MT5_PENDING_<ticket>
                follower pending-order label

            MT5<ticket>
                legacy bridge label

        Examples:

            MT5_5854323
                -> 5854323

            MT5_PENDING_5854323
                -> 5854323

            MT55854323
                -> 5854323
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
    def _is_pending_position_label(label: str) -> bool:
        return isinstance(label, str) and label.strip().startswith("MT5_PENDING_")

    @staticmethod
    def _is_market_position_label(label: str) -> bool:
        if not isinstance(label, str):
            return False

        value = label.strip()

        return (
            value.startswith("MT5_")
            and not value.startswith("MT5_PENDING_")
        ) or (
            value.startswith("MT5")
            and not value.startswith("MT5_")
        )

    @staticmethod
    def _extract_position_volume(pos) -> int:
        """
        Return positive live volume.

        A zero value means no running volume.

        cTrader ORDER_ACCEPTED may contain a position object with a positionId
        but zero volume. That object is only an accepted-order shell and must
        NOT be stored as a live position.
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

    # ======================================================================
    # SHARED TOKEN STATE
    # ======================================================================

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

        access_token = self._safe_str(
            getattr(account, "access_token", ""),
        )
        refresh_token = self._safe_str(
            getattr(account, "refresh_token", ""),
        )

        if access_token or refresh_token:
            return f"pair:{access_token}|{refresh_token}"

        state_file = self._safe_str(
            getattr(account, "token_state_file", ""),
        )

        if state_file:
            return f"state:{state_file}"

        return (
            f"account:{self._safe_str(getattr(account, 'name', 'unknown'))}"
        )

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

                logger.warning(
                    "[%s] %s",
                    account.name,
                    message,
                )

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

    # ======================================================================
    # ACCOUNT MAP INITIALIZATION
    # ======================================================================

    def _ensure_account_maps(self, account_name: str):
        if account_name not in self.position_maps:
            self.position_maps[account_name] = {}

        if account_name not in self.position_volumes:
            self.position_volumes[account_name] = {}

        if account_name not in self.order_maps:
            self.order_maps[account_name] = {}

        if account_name not in self.fallback_market_order_maps:
            self.fallback_market_order_maps[account_name] = {}

        if account_name not in self.pending_position_maps:
            self.pending_position_maps[account_name] = {}

        if account_name not in self.fallback_market_position_maps:
            self.fallback_market_position_maps[account_name] = {}

        if account_name not in self.pending_types:
            self.pending_types[account_name] = {}

        if account_name not in self.mt5_payloads:
            self.mt5_payloads[account_name] = {}

        if account_name not in self.reconcile_requested:
            self.reconcile_requested[account_name] = False

        if account_name not in self.auth_seen:
            self.auth_seen[account_name] = False

    # ======================================================================
    # ROUTE MAGIC
    # ======================================================================

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

            logger.info(
                "[%s] Unregistered route magic %s",
                account_name,
                magic,
            )

    # ======================================================================
    # FUNDS
    # ======================================================================

    def _cache_funds_from_reconcile(
        self,
        account_name: str,
        reconcile_res,
    ):
        equity, balance = self._extract_account_equity_balance(
            reconcile_res,
        )

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

    # ======================================================================
    # PENDING TYPE
    # ======================================================================

    @staticmethod
    def _normalize_pending_type(value) -> Optional[str]:
        if value is None:
            return None

        value = str(value).strip().lower()

        aliases = {
            "limit": "limit",
            "stop": "stop",
            "stop_limit": "stop_limit",
            "stoplimit": "stop_limit",
            "limit_order": "limit",
            "stop_order": "stop",
            "stop_limit_order": "stop_limit",
        }

        return aliases.get(value)

    def store_pending_type(
        self,
        account_name: str,
        mt5_ticket: int,
        pending_type: str,
    ):
        """
        Store the source/destination pending type for a ticket.

        trade_executor should call this when a follower pending order is
        created or restored.

        This does not affect canonical position selection; the label itself
        determines whether a live position originated from the pending order.
        """
        normalized = self._normalize_pending_type(pending_type)

        if normalized is None:
            logger.warning(
                "[%s] Ignoring invalid pending type %r for ticket %s",
                account_name,
                pending_type,
                mt5_ticket,
            )
            return

        self._ensure_account_maps(account_name)

        ticket = int(mt5_ticket)
        previous = self.pending_types[account_name].get(ticket)

        self.pending_types[account_name][ticket] = normalized

        if previous != normalized:
            logger.info(
                "[%s] Pending type stored | ticket=%s type=%s",
                account_name,
                ticket,
                normalized,
            )

    def get_pending_type(
        self,
        account_name: str,
        mt5_ticket: int,
    ) -> Optional[str]:
        pending_map = self.pending_types.get(account_name) or {}
        return pending_map.get(int(mt5_ticket))

    def remove_pending_type(
        self,
        account_name: str,
        mt5_ticket: int,
    ):
        self._ensure_account_maps(account_name)

        removed = self.pending_types[account_name].pop(
            int(mt5_ticket),
            None,
        )

        if removed:
            logger.info(
                "[%s] Removed pending type | ticket=%s type=%s",
                account_name,
                int(mt5_ticket),
                removed,
            )

    # ======================================================================
    # PENDING ORDER MAPPING
    # ======================================================================

    def _store_order_mapping(
        self,
        account_name: str,
        mt5_ticket: int,
        order_id: int,
    ):
        """
        Store the actual follower pending orderId.

        IMPORTANT:
            This is deliberately separate from fallback_market_order_maps.
            A market fallback execution must never overwrite the pending
            order mapping.
        """
        if int(order_id or 0) <= 0:
            return

        self._ensure_account_maps(account_name)

        previous_order_id = self.order_maps[account_name].get(
            int(mt5_ticket),
        )

        self.order_maps[account_name][int(mt5_ticket)] = int(order_id)

        if previous_order_id != int(order_id):
            logger.info(
                "[%s] MT5 ticket %s -> cTrader PENDING orderId %s",
                account_name,
                int(mt5_ticket),
                int(order_id),
            )

    def _remove_order_mapping(
        self,
        account_name: str,
        mt5_ticket: int,
    ):
        self._ensure_account_maps(account_name)

        removed = self.order_maps[account_name].pop(
            int(mt5_ticket),
            None,
        )

        if removed:
            logger.info(
                "[%s] Removed MT5 ticket %s -> pending orderId %s mapping",
                account_name,
                int(mt5_ticket),
                int(removed),
            )

    # ======================================================================
    # FALLBACK MARKET ORDER MAPPING
    # ======================================================================

    def _store_fallback_market_order_mapping(
        self,
        account_name: str,
        mt5_ticket: int,
        order_id: int,
    ):
        """
        Store a market fallback order separately from the pending order.

        This is used only for LIMIT pending -> market fallback handling.
        """
        if int(order_id or 0) <= 0:
            return

        self._ensure_account_maps(account_name)

        previous = self.fallback_market_order_maps[account_name].get(
            int(mt5_ticket),
        )

        self.fallback_market_order_maps[account_name][int(mt5_ticket)] = int(
            order_id,
        )

        if previous != int(order_id):
            logger.info(
                "[%s] MT5 ticket %s -> FALLBACK market orderId %s",
                account_name,
                int(mt5_ticket),
                int(order_id),
            )

    def _remove_fallback_market_order_mapping(
        self,
        account_name: str,
        mt5_ticket: int,
    ):
        self._ensure_account_maps(account_name)

        removed = self.fallback_market_order_maps[account_name].pop(
            int(mt5_ticket),
            None,
        )

        if removed:
            logger.info(
                "[%s] Removed fallback market order mapping | "
                "ticket=%s orderId=%s",
                account_name,
                int(mt5_ticket),
                int(removed),
            )

    def get_fallback_market_order_id(
        self,
        account_name: str,
        mt5_ticket: int,
    ) -> Optional[int]:
        order_map = self.fallback_market_order_maps.get(
            account_name,
        ) or {}

        return order_map.get(int(mt5_ticket))

    # ======================================================================
    # POSITION MAPPING
    # ======================================================================

    def _store_position_mapping(
        self,
        account_name: str,
        mt5_ticket: int,
        position_id: int,
    ):
        """
        Store the canonical running cTrader position.

        This method is intentionally simple for compatibility. New event
        handling should normally use _register_live_position(), which applies
        pending-origin priority.
        """
        if int(position_id or 0) <= 0:
            return

        self._ensure_account_maps(account_name)

        previous_position_id = self.position_maps[account_name].get(
            int(mt5_ticket),
        )

        self.position_maps[account_name][int(mt5_ticket)] = int(position_id)

        if previous_position_id != int(position_id):
            logger.info(
                "[%s] MT5 ticket %s -> cTrader CANONICAL positionId %s",
                account_name,
                int(mt5_ticket),
                int(position_id),
            )

        notify_position_update(
            account_name,
            int(mt5_ticket),
            self,
        )

    def _remove_position_mapping(
        self,
        account_name: str,
        mt5_ticket: int,
    ):
        self._ensure_account_maps(account_name)

        removed = self.position_maps[account_name].pop(
            int(mt5_ticket),
            None,
        )

        if removed:
            self.position_volumes[account_name].pop(
                int(removed),
                None,
            )

            logger.info(
                "[%s] Removed MT5 ticket %s -> canonical positionId %s",
                account_name,
                int(mt5_ticket),
                int(removed),
            )

    # ======================================================================
    # PENDING-ORIGIN POSITION
    # ======================================================================

    def _store_pending_position_mapping(
        self,
        account_name: str,
        mt5_ticket: int,
        position_id: int,
    ):
        """
        Store a position created by the destination pending order.

        This position has canonical priority over a market fallback.
        """
        if int(position_id or 0) <= 0:
            return

        self._ensure_account_maps(account_name)

        ticket = int(mt5_ticket)
        position_id = int(position_id)

        previous = self.pending_position_maps[account_name].get(ticket)

        self.pending_position_maps[account_name][ticket] = position_id

        if previous != position_id:
            logger.info(
                "[%s] MT5 ticket %s -> PENDING-ORIGIN positionId %s",
                account_name,
                ticket,
                position_id,
            )

        # Pending-origin position ALWAYS wins canonical mapping.
        previous_canonical = self.position_maps[account_name].get(ticket)

        self.position_maps[account_name][ticket] = position_id

        if previous_canonical != position_id:
            logger.info(
                "[%s] MT5 ticket %s canonical mapping promoted to "
                "PENDING-ORIGIN positionId %s",
                account_name,
                ticket,
                position_id,
            )

        notify_position_update(
            account_name,
            ticket,
            self,
        )

    def _remove_pending_position_mapping(
        self,
        account_name: str,
        mt5_ticket: int,
    ):
        self._ensure_account_maps(account_name)

        ticket = int(mt5_ticket)

        removed = self.pending_position_maps[account_name].pop(
            ticket,
            None,
        )

        if removed:
            logger.info(
                "[%s] Removed pending-origin position | "
                "ticket=%s positionId=%s",
                account_name,
                ticket,
                int(removed),
            )

        # Only remove canonical mapping if it points to the pending-origin
        # position. Do not accidentally remove a newer valid market position.
        canonical = self.position_maps[account_name].get(ticket)

        if removed and canonical == int(removed):
            self.position_maps[account_name].pop(ticket, None)

            self.position_volumes[account_name].pop(
                int(removed),
                None,
            )

    # ======================================================================
    # FALLBACK MARKET POSITION
    # ======================================================================

    def _store_fallback_market_position_mapping(
        self,
        account_name: str,
        mt5_ticket: int,
        position_id: int,
    ):
        """
        Store a market fallback position separately.

        If a pending-origin position exists, this NEVER becomes canonical.
        """
        if int(position_id or 0) <= 0:
            return

        self._ensure_account_maps(account_name)

        ticket = int(mt5_ticket)
        position_id = int(position_id)

        previous = self.fallback_market_position_maps[account_name].get(
            ticket,
        )

        self.fallback_market_position_maps[account_name][ticket] = position_id

        if previous != position_id:
            logger.info(
                "[%s] MT5 ticket %s -> FALLBACK market positionId %s",
                account_name,
                ticket,
                position_id,
            )

        pending_position = self.pending_position_maps[account_name].get(
            ticket,
        )

        if pending_position:
            logger.info(
                "[%s] LIMIT race detected | ticket=%s "
                "pending-origin positionId=%s remains CANONICAL; "
                "market fallback positionId=%s tracked separately",
                account_name,
                ticket,
                pending_position,
                position_id,
            )
            return

        # No pending-origin position exists.
        #
        # Therefore this market position is allowed to be canonical.
        current_canonical = self.position_maps[account_name].get(ticket)

        if current_canonical != position_id:
            self.position_maps[account_name][ticket] = position_id

            logger.info(
                "[%s] MT5 ticket %s -> market positionId %s "
                "is CANONICAL because no pending-origin position exists",
                account_name,
                ticket,
                position_id,
            )

            notify_position_update(
                account_name,
                ticket,
                self,
            )

    def _remove_fallback_market_position_mapping(
        self,
        account_name: str,
        mt5_ticket: int,
    ):
        self._ensure_account_maps(account_name)

        ticket = int(mt5_ticket)

        removed = self.fallback_market_position_maps[account_name].pop(
            ticket,
            None,
        )

        if removed:
            logger.info(
                "[%s] Removed fallback market position | "
                "ticket=%s positionId=%s",
                account_name,
                ticket,
                int(removed),
            )

        # If the fallback happened to be canonical, remove it.
        #
        # Normally this will only be true when no pending-origin position
        # exists.
        canonical = self.position_maps[account_name].get(ticket)

        if removed and canonical == int(removed):
            self.position_maps[account_name].pop(ticket, None)
            self.position_volumes[account_name].pop(
                int(removed),
                None,
            )

    def get_pending_position_id(
        self,
        account_name: str,
        mt5_ticket: int,
    ) -> Optional[int]:
        mapping = self.pending_position_maps.get(account_name) or {}
        return mapping.get(int(mt5_ticket))

    def get_fallback_market_position_id(
        self,
        account_name: str,
        mt5_ticket: int,
    ) -> Optional[int]:
        mapping = self.fallback_market_position_maps.get(
            account_name,
        ) or {}

        return mapping.get(int(mt5_ticket))

    # ======================================================================
    # POSITION REGISTRATION
    # ======================================================================

    def _register_live_position(
        self,
        account_name: str,
        mt5_ticket: int,
        position_id: int,
        volume: int,
        label: str,
    ):
        """
        Register a live position while applying canonical priority.

        Rules:

            MT5_PENDING_<ticket>
                -> pending-origin position
                -> ALWAYS canonical

            MT5_<ticket>
                -> market position
                -> canonical only if no pending-origin position exists

        This is the central race-safe mapping function.
        """
        if int(position_id or 0) <= 0:
            return

        if int(volume or 0) <= 0:
            return

        ticket = int(mt5_ticket)
        position_id = int(position_id)
        volume = int(volume)

        self._ensure_account_maps(account_name)

        is_pending_origin = self._is_pending_position_label(label)

        if is_pending_origin:
            self._store_pending_position_mapping(
                account_name,
                ticket,
                position_id,
            )

            self._store_position_volume(
                account_name,
                position_id,
                volume,
            )

            # The pending order has now actually activated.
            #
            # The pending order mapping can be removed because its resulting
            # position is now represented by pending_position_maps.
            pending_order_id = self.get_order_id(
                account_name,
                ticket,
            )

            if pending_order_id:
                self._remove_order_mapping(
                    account_name,
                    ticket,
                )

            logger.info(
                "[%s] LIVE PENDING-ORIGIN POSITION | "
                "ticket=%s positionId=%s volume=%s | "
                "CANONICAL=PENDING",
                account_name,
                ticket,
                position_id,
                volume,
            )

            return

        # Any normal MT5_<ticket> live position is treated as a market
        # position.
        if self._is_market_position_label(label):
            self._store_fallback_market_position_mapping(
                account_name,
                ticket,
                position_id,
            )

            self._store_position_volume(
                account_name,
                position_id,
                volume,
            )

            fallback_order_id = self.get_fallback_market_order_id(
                account_name,
                ticket,
            )

            if fallback_order_id:
                self._remove_fallback_market_order_mapping(
                    account_name,
                    ticket,
                )

            pending_position_id = self.get_pending_position_id(
                account_name,
                ticket,
            )

            if pending_position_id:
                logger.info(
                    "[%s] LIVE MARKET FALLBACK POSITION | "
                    "ticket=%s positionId=%s volume=%s | "
                    "pending-origin positionId=%s remains CANONICAL",
                    account_name,
                    ticket,
                    position_id,
                    volume,
                    pending_position_id,
                )
            else:
                logger.info(
                    "[%s] LIVE MARKET POSITION | "
                    "ticket=%s positionId=%s volume=%s | "
                    "CANONICAL=MARKET",
                    account_name,
                    ticket,
                    position_id,
                    volume,
                )

            return

        # Unknown label format should not blindly overwrite a canonical
        # pending-origin position.
        pending_position_id = self.get_pending_position_id(
            account_name,
            ticket,
        )

        if pending_position_id:
            logger.warning(
                "[%s] Unknown live position label for ticket=%s; "
                "pending-origin positionId=%s already canonical; "
                "not overwriting it | label=%r positionId=%s",
                account_name,
                ticket,
                pending_position_id,
                label,
                position_id,
            )

            self._store_position_volume(
                account_name,
                position_id,
                volume,
            )

            return

        self._store_position_mapping(
            account_name,
            ticket,
            position_id,
        )

        self._store_position_volume(
            account_name,
            position_id,
            volume,
        )

        logger.info(
            "[%s] LIVE POSITION with unknown label classification | "
            "ticket=%s positionId=%s volume=%s label=%r",
            account_name,
            ticket,
            position_id,
            volume,
            label,
        )

    # ======================================================================
    # POSITION VOLUME
    # ======================================================================

    def _store_position_volume(
        self,
        account_name: str,
        position_id: int,
        volume: int,
    ):
        if int(position_id or 0) <= 0:
            return

        self._ensure_account_maps(account_name)

        if int(volume or 0) > 0:
            previous_volume = self.position_volumes[account_name].get(
                int(position_id),
            )

            self.position_volumes[account_name][int(position_id)] = int(
                volume,
            )

            if previous_volume != int(volume):
                logger.info(
                    "[%s] positionId %s volume=%s cached",
                    account_name,
                    int(position_id),
                    int(volume),
                )
        else:
            self.position_volumes[account_name].pop(
                int(position_id),
                None,
            )

    # ======================================================================
    # EXECUTION ORDER
    # ======================================================================

    def _handle_execution_order(
        self,
        account_name: str,
        extracted,
    ):
        order = getattr(extracted, "order", None)

        if order is None:
            return

        order_id = self._to_int(
            getattr(order, "orderId", 0),
            default=0,
        )

        label = self._extract_order_label(order)
        ticket = self._label_to_ticket(label)

        if order_id <= 0 or ticket is None:
            return

        # Pending follower order:
        #
        # MT5_PENDING_<ticket>
        #
        # This must stay in the pending order map.
        if label.startswith("MT5_PENDING_"):
            self._store_order_mapping(
                account_name,
                int(ticket),
                int(order_id),
            )
            return

        # Market order:
        #
        # MT5_<ticket>
        #
        # This may be a normal market-copy order OR a LIMIT fallback.
        # Keep it separate so it cannot overwrite the pending orderId.
        if self._is_market_position_label(label):
            self._store_fallback_market_order_mapping(
                account_name,
                int(ticket),
                int(order_id),
            )

            return

        # Legacy/unknown MT5 label.
        #
        # Preserve old behavior for compatibility.
        self._store_order_mapping(
            account_name,
            int(ticket),
            int(order_id),
        )

    # ======================================================================
    # EXECUTION POSITION
    # ======================================================================

    def _handle_execution_position(
        self,
        account_name: str,
        extracted,
    ):
        position = getattr(extracted, "position", None)

        if position is None:
            return

        execution_type = self._to_int(
            getattr(extracted, "executionType", 0),
            default=0,
        )

        position_status = self._to_int(
            getattr(position, "positionStatus", 0),
            default=0,
        )

        position_id = self._to_int(
            getattr(position, "positionId", 0),
            default=0,
        )

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

        logger.info(
            "[%s] Execution position | "
            "ticket=%s positionId=%s volume=%s "
            "executionType=%s positionStatus=%s "
            "accepted=%s filled=%s cancelled=%s "
            "live=%s shell=%s label=%r",
            account_name,
            ticket,
            position_id,
            volume,
            execution_type,
            position_status,
            is_order_accepted,
            is_order_filled,
            is_order_cancelled,
            is_live_position,
            is_zero_volume_shell,
            label,
        )

        # --------------------------------------------------------------
        # ACCEPTED ORDER SHELL
        # --------------------------------------------------------------
        if is_order_accepted and is_zero_volume_shell:
            logger.info(
                "[%s] Accepted order shell retained as order mapping only | "
                "ticket=%s positionId=%s positionStatus=%s label=%r",
                account_name,
                ticket,
                position_id,
                position_status,
                label,
            )

            return

        # --------------------------------------------------------------
        # LIVE POSITION
        # --------------------------------------------------------------
        if is_live_position:
            self._register_live_position(
                account_name=account_name,
                mt5_ticket=int(ticket),
                position_id=int(position_id),
                volume=int(volume),
                label=label,
            )

            self._try_enforce_max_risk_on_fill(
                account_name,
                extracted,
                position,
                int(position_id),
                int(ticket),
            )

            return

        # --------------------------------------------------------------
        # ORDER CANCELLED
        # --------------------------------------------------------------
        if is_order_cancelled:
            #
            # IMPORTANT:
            #
            # A cancelled pending order should remove ONLY the pending order
            # mapping. It must not remove a market fallback order/position.
            #
            # If this is the LIMIT race and the pending was actually
            # cancelled, the market position can remain canonical.
            #
            self._remove_order_mapping(
                account_name,
                int(ticket),
            )

            logger.info(
                "[%s] Cancelled PENDING order removed pending order mapping | "
                "ticket=%s positionId=%s",
                account_name,
                ticket,
                position_id,
            )

            return

        # --------------------------------------------------------------
        # FILLED BUT CLOSED / ZERO-VOLUME SHELL
        # --------------------------------------------------------------
        if (
            is_order_filled
            and is_zero_volume_shell
            and position_status == POSITION_STATUS_CLOSED
        ):
            #
            # Do NOT blindly call remove_mapping() here.
            #
            # A zero-volume event can correspond to one side of a race.
            # Only remove the corresponding order/position state.
            #
            self._remove_order_mapping(
                account_name,
                int(ticket),
            )

            self._remove_fallback_market_order_mapping(
                account_name,
                int(ticket),
            )

            # If this shell corresponds to the pending-origin position,
            # remove that pending-origin position.
            pending_position_id = self.get_pending_position_id(
                account_name,
                int(ticket),
            )

            if pending_position_id and pending_position_id == position_id:
                self._remove_pending_position_mapping(
                    account_name,
                    int(ticket),
                )

            fallback_position_id = self.get_fallback_market_position_id(
                account_name,
                int(ticket),
            )

            if fallback_position_id and fallback_position_id == position_id:
                self._remove_fallback_market_position_mapping(
                    account_name,
                    int(ticket),
                )

            logger.info(
                "[%s] Closed position shell processed | "
                "ticket=%s positionId=%s",
                account_name,
                ticket,
                position_id,
            )

            return

        logger.info(
            "[%s] Ignored non-live execution position update | "
            "ticket=%s positionId=%s volume=%s "
            "executionType=%s positionStatus=%s label=%r",
            account_name,
            ticket,
            position_id,
            volume,
            execution_type,
            position_status,
            label,
        )

    # ======================================================================
    # MAX RISK
    # ======================================================================

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

            execution_type = self._to_int(
                getattr(extracted, "executionType", 0),
                default=0,
            )

            if execution_type != ORDER_FILLED:
                return

            config = self.get_config(account_name)
            client = self.get_client(account_name)

            if not config or not client:
                return

            trade_data = getattr(position, "tradeData", None) or position

            symbol_id = self._to_int(
                getattr(trade_data, "symbolId", 0),
                default=0,
            )

            if symbol_id <= 0:
                return

            symbol = (
                client.symbol_details.get(int(symbol_id))
                if hasattr(client, "symbol_details")
                else None
            )

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

            mt5_data = self.mt5_payloads.get(
                account_name,
                {},
            ).get(
                int(ticket),
                None,
            )

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
                "[%s] Over-risk enforcement failed | "
                "ticket=%s positionId=%s error=%s",
                account_name,
                ticket,
                position_id,
                error,
                exc_info=True,
            )

    # ======================================================================
    # RECONCILIATION - POSITIONS
    # ======================================================================

    def _handle_reconcile_positions(
        self,
        account_name: str,
        extracted,
    ) -> int:
        """
        Rebuild live position mappings.

        IMPORTANT:

        Reconciliation must use exactly the same canonical priority as live
        execution events.

            MT5_PENDING_<ticket>
                wins over
            MT5_<ticket>

        Therefore a market fallback found first cannot overwrite a
        pending-origin position found later.
        """
        count = 0

        positions = list(
            getattr(extracted, "position", []) or [],
        )

        active_position_ids = set()
        active_position_tickets = set()

        # ------------------------------------------------------------------
        # First collect/store volumes.
        # ------------------------------------------------------------------
        for position in positions:
            position_id = self._to_int(
                getattr(position, "positionId", 0),
                default=0,
            )

            if position_id <= 0:
                continue

            volume = self._extract_position_volume(position)

            if volume <= 0:
                continue

            active_position_ids.add(int(position_id))

            label = self._extract_position_label(position)
            ticket = self._label_to_ticket(label)

            self._store_position_volume(
                account_name,
                int(position_id),
                int(volume),
            )

            if ticket is None:
                continue

            active_position_tickets.add(int(ticket))

            # --------------------------------------------------------------
            # CENTRAL PRIORITY LOGIC
            # --------------------------------------------------------------
            self._register_live_position(
                account_name=account_name,
                mt5_ticket=int(ticket),
                position_id=int(position_id),
                volume=int(volume),
                label=label,
            )

            logger.info(
                "[%s] (reconcile pos) ticket=%s -> positionId=%s "
                "volume=%s label=%r pending_origin=%s canonical=%s",
                account_name,
                int(ticket),
                int(position_id),
                int(volume),
                label,
                self._is_pending_position_label(label),
                self.get_position_id(
                    account_name,
                    int(ticket),
                ),
            )

            count += 1

        # ------------------------------------------------------------------
        # Remove stale canonical ticket mappings only when there is no active
        # position for that ticket.
        # ------------------------------------------------------------------
        stale_tickets = [
            ticket
            for ticket in self.position_maps.get(
                account_name,
                {},
            ).keys()
            if ticket not in active_position_tickets
        ]

        for ticket in stale_tickets:
            self._remove_position_mapping(
                account_name,
                int(ticket),
            )

        # ------------------------------------------------------------------
        # Remove stale pending-origin mappings.
        # ------------------------------------------------------------------
        stale_pending_tickets = [
            ticket
            for ticket in self.pending_position_maps.get(
                account_name,
                {},
            ).keys()
            if ticket not in active_position_tickets
            or self.pending_position_maps[account_name][ticket]
            not in active_position_ids
        ]

        for ticket in stale_pending_tickets:
            self._remove_pending_position_mapping(
                account_name,
                int(ticket),
            )

        # ------------------------------------------------------------------
        # Remove stale fallback market position mappings.
        # ------------------------------------------------------------------
        stale_fallback_tickets = [
            ticket
            for ticket in self.fallback_market_position_maps.get(
                account_name,
                {},
            ).keys()
            if ticket not in active_position_tickets
            or self.fallback_market_position_maps[account_name][ticket]
            not in active_position_ids
        ]

        for ticket in stale_fallback_tickets:
            self._remove_fallback_market_position_mapping(
                account_name,
                int(ticket),
            )

        # ------------------------------------------------------------------
        # Remove stale cached position volumes.
        # ------------------------------------------------------------------
        stale_position_ids = [
            position_id
            for position_id in self.position_volumes.get(
                account_name,
                {},
            ).keys()
            if position_id not in active_position_ids
        ]

        for position_id in stale_position_ids:
            self.position_volumes[account_name].pop(
                position_id,
                None,
            )

            logger.info(
                "[%s] Removed stale cached volume for positionId %s",
                account_name,
                position_id,
            )

        return count

    # ======================================================================
    # RECONCILIATION - ORDERS
    # ======================================================================

    def _handle_reconcile_orders(
        self,
        account_name: str,
        extracted,
    ) -> int:
        """
        Rebuild pending and fallback market order mappings.

        cTrader reconciliation returns active orders.

        MT5_PENDING_<ticket>:
            -> pending order map

        MT5_<ticket>:
            -> fallback market order map

        A live pending-origin position does not mean that an unrelated
        fallback market position/order should be discarded.
        """
        order_count = 0

        orders = list(
            getattr(extracted, "order", []) or [],
        )

        active_pending_order_tickets = set()
        active_fallback_order_tickets = set()

        for order in orders:
            order_id = self._to_int(
                getattr(order, "orderId", 0),
                default=0,
            )

            label = self._extract_order_label(order)
            ticket = self._label_to_ticket(label)

            if order_id <= 0 or ticket is None:
                continue

            ticket = int(ticket)

            # --------------------------------------------------------------
            # PENDING ORDER
            # --------------------------------------------------------------
            if label.startswith("MT5_PENDING_"):
                active_pending_order_tickets.add(ticket)

                self._store_order_mapping(
                    account_name,
                    ticket,
                    int(order_id),
                )

                logger.info(
                    "[%s] (reconcile order) PENDING "
                    "MT5 ticket %s -> cTrader orderId %s",
                    account_name,
                    ticket,
                    order_id,
                )

                order_count += 1
                continue

            # --------------------------------------------------------------
            # MARKET / FALLBACK ORDER
            # --------------------------------------------------------------
            if self._is_market_position_label(label):
                active_fallback_order_tickets.add(ticket)

                self._store_fallback_market_order_mapping(
                    account_name,
                    ticket,
                    int(order_id),
                )

                logger.info(
                    "[%s] (reconcile order) MARKET/FALLBACK "
                    "MT5 ticket %s -> cTrader orderId %s",
                    account_name,
                    ticket,
                    order_id,
                )

                order_count += 1
                continue

            # --------------------------------------------------------------
            # LEGACY ORDER LABEL
            # --------------------------------------------------------------
            active_pending_order_tickets.add(ticket)

            self._store_order_mapping(
                account_name,
                ticket,
                int(order_id),
            )

            logger.info(
                "[%s] (reconcile order) LEGACY "
                "MT5 ticket %s -> cTrader orderId %s",
                account_name,
                ticket,
                order_id,
            )

            order_count += 1

        # ------------------------------------------------------------------
        # Remove stale pending order mappings.
        #
        # A live pending-origin position means the pending order may already
        # have activated, so stale order mapping can safely be removed.
        # ------------------------------------------------------------------
        stale_pending_order_tickets = [
            ticket
            for ticket in self.order_maps.get(
                account_name,
                {},
            ).keys()
            if ticket not in active_pending_order_tickets
        ]

        for ticket in stale_pending_order_tickets:
            self._remove_order_mapping(
                account_name,
                int(ticket),
            )

        # ------------------------------------------------------------------
        # Remove stale fallback market order mappings.
        #
        # Do not remove fallback POSITION mappings here.
        # They are separate state and may still be running.
        # ------------------------------------------------------------------
        stale_fallback_order_tickets = [
            ticket
            for ticket in self.fallback_market_order_maps.get(
                account_name,
                {},
            ).keys()
            if ticket not in active_fallback_order_tickets
        ]

        for ticket in stale_fallback_order_tickets:
            self._remove_fallback_market_order_mapping(
                account_name,
                int(ticket),
            )

        return order_count

    # ======================================================================
    # RECONCILIATION
    # ======================================================================

    def _process_reconcile(
        self,
        account_name: str,
        extracted,
    ):
        self._cache_funds_from_reconcile(
            account_name,
            extracted,
        )

        position_count = self._handle_reconcile_positions(
            account_name,
            extracted,
        )

        try:
            order_count = self._handle_reconcile_orders(
                account_name,
                extracted,
            )

        except Exception as error:
            logger.debug(
                "[%s] Failed parsing reconcile orders",
                account_name,
                exc_info=True,
            )

            notify_error(
                event="reconcile_parse_orders",
                message="Failed parsing reconcile orders",
                exc=error,
                **self._notify_ctx(account_name),
            )

            order_count = 0

        logger.info(
            "[%s] Reconcile complete: %s MT5 positions "
            "(%s positions with volume cached), %s orders mapped | "
            "pending_positions=%s fallback_positions=%s "
            "pending_orders=%s fallback_orders=%s",
            account_name,
            position_count,
            len(self.position_volumes[account_name]),
            order_count,
            len(self.pending_position_maps.get(account_name, {})),
            len(
                self.fallback_market_position_maps.get(
                    account_name,
                    {},
                )
            ),
            len(self.order_maps.get(account_name, {})),
            len(
                self.fallback_market_order_maps.get(
                    account_name,
                    {},
                )
            ),
        )

    # ======================================================================
    # MESSAGE PROCESSING
    # ======================================================================

    def _process_message(
        self,
        account_name: str,
        message,
    ):
        self._ensure_account_maps(account_name)

        extracted = Protobuf.extract(message)

        # ------------------------------------------------------------------
        # ACCOUNT AUTH
        # ------------------------------------------------------------------
        if isinstance(extracted, ProtoOAAccountAuthRes):
            if not self.auth_seen.get(account_name, False):
                self.auth_seen[account_name] = True

                logger.info(
                    "✓ Account %s connected and authenticated",
                    account_name,
                )

                notify_info(
                    event="account_authenticated",
                    message="cTrader account authenticated",
                    **self._notify_ctx(account_name),
                )

            self._send_reconcile_request(account_name)

            return

        # ------------------------------------------------------------------
        # EXECUTION EVENT
        # ------------------------------------------------------------------
        if isinstance(extracted, ProtoOAExecutionEvent):
            logger.info(
                "[%s] RAW EXECUTION: %s",
                account_name,
                extracted,
            )

            # Order event must be processed before position event.
            self._handle_execution_order(
                account_name,
                extracted,
            )

            self._handle_execution_position(
                account_name,
                extracted,
            )

            return

        # ------------------------------------------------------------------
        # RECONCILIATION RESPONSE
        # ------------------------------------------------------------------
        if isinstance(extracted, ProtoOAReconcileRes):
            self.reconcile_requested[account_name] = False

            self._process_reconcile(
                account_name,
                extracted,
            )

            return

        # ------------------------------------------------------------------
        # GENERIC POSITION UPDATE
        # ------------------------------------------------------------------
        if not hasattr(extracted, "position"):
            return

        position = extracted.position

        position_id = self._to_int(
            getattr(position, "positionId", 0),
            default=0,
        )

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

        # Use exactly the same race-safe logic as execution and reconciliation.
        self._register_live_position(
            account_name=account_name,
            mt5_ticket=int(ticket),
            position_id=int(position_id),
            volume=int(volume),
            label=label,
        )

        logger.info(
            "[%s] Updated live position state | "
            "ticket=%s positionId=%s volume=%s label=%r canonical=%s",
            account_name,
            int(ticket),
            int(position_id),
            int(volume),
            label,
            self.get_position_id(
                account_name,
                int(ticket),
            ),
        )

    # ======================================================================
    # RECONCILE REQUEST
    # ======================================================================

    def _send_reconcile_request(
        self,
        account_name: str,
    ):
        client = self.get_client(account_name)
        config = self.get_config(account_name)

        if not client or not config:
            message = "Cannot send reconcile: missing client/config"

            logger.warning(
                "[%s] %s",
                account_name,
                message,
            )

            notify_warning(
                event="reconcile_missing_context",
                message=message,
                **self._notify_ctx(account_name),
            )

            return

        account_id = self._config_account_id(config)

        if not account_id:
            message = "Cannot send reconcile: missing account_id"

            logger.warning(
                "[%s] %s",
                account_name,
                message,
            )

            notify_warning(
                event="reconcile_missing_account_id",
                message=message,
                **self._notify_ctx(account_name),
            )

            return

        if self.reconcile_requested.get(account_name, False):
            logger.info(
                "[%s] Reconcile already requested for this connection",
                account_name,
            )

            return

        try:
            request = ProtoOAReconcileReq()
            request.ctidTraderAccountId = int(account_id)

            logger.info(
                "[%s] Sending reconcile request...",
                account_name,
            )

            deferred = client.send(request)

            self.reconcile_requested[account_name] = True

            def _on_reconcile(result):
                try:
                    response = Protobuf.extract(result)

                    if isinstance(response, ProtoOAReconcileRes):
                        self.reconcile_requested[account_name] = False

                        self._process_reconcile(
                            account_name,
                            response,
                        )

                        logger.info(
                            "[%s] Reconcile response processed",
                            account_name,
                        )

                    else:
                        logger.info(
                            "[%s] Reconcile callback received message type %s",
                            account_name,
                            type(response).__name__,
                        )

                except Exception as error:
                    self.reconcile_requested[account_name] = False

                    logger.warning(
                        "[%s] Failed to process reconcile response: %s",
                        account_name,
                        error,
                    )

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

    # ======================================================================
    # ACCOUNT MANAGEMENT
    # ======================================================================

    def add_account(
        self,
        account: AccountConfig,
    ):
        if not account.enabled:
            logger.info(
                "Skipping disabled account: %s",
                account.name,
            )

            return

        if account.name in self.clients:
            message = (
                "Account already initialized; replacing existing client"
            )

            logger.warning(
                "[%s] %s",
                account.name,
                message,
            )

            notify_warning(
                event="account_reinitialized",
                message=message,
                **self._notify_ctx(account.name),
            )

            self._unregister_route_magic(account.name)

        logger.info(
            "Initializing account: %s",
            account.name,
        )

        shared_state_file = self._resolve_shared_token_state_file(
            account,
        )

        account_id = self._config_account_id(account)

        logger.info(
            "[%s] Token bootstrap: access=%s refresh_present=%s "
            "state_file=%s env=%s account_id=%s",
            account.name,
            self._token_preview(
                getattr(account, "access_token", ""),
            ),
            bool(
                self._safe_str(
                    getattr(account, "refresh_token", ""),
                )
            ),
            shared_state_file
            or getattr(
                account,
                "token_state_file",
                None,
            ),
            getattr(
                account,
                "environment",
                None,
            ),
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

        self._ensure_account_maps(
            account.name,
        )

        self._register_route_magic(account)

        def on_message(
            message,
            account_name=account.name,
        ):
            try:
                self._process_message(
                    account_name,
                    message,
                )

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
                "✓ Account %s socket connected; "
                "waiting for app/account authorization",
                account.name,
            )

        client.connect(
            on_connect=on_connected,
        )

    # ======================================================================
    # PUBLIC GETTERS
    # ======================================================================

    def get_client(
        self,
        account_name: str,
    ) -> Optional[CTraderClient]:
        return self.clients.get(account_name)

    def get_config(
        self,
        account_name: str,
    ) -> Optional[AccountConfig]:
        return self.configs.get(account_name)

    def get_equity(
        self,
        account_name: str,
    ) -> Optional[float]:
        return self.account_equity.get(account_name)

    def get_balance(
        self,
        account_name: str,
    ) -> Optional[float]:
        return self.account_balance.get(account_name)

    def get_position_id(
        self,
        account_name: str,
        mt5_ticket: int,
    ) -> Optional[int]:
        """
        Return the CANONICAL cTrader positionId.

        Canonical priority is:

            pending-origin position
                >
            market position
        """
        position_map = self.position_maps.get(account_name) or {}
        return position_map.get(int(mt5_ticket))

    def get_order_id(
        self,
        account_name: str,
        mt5_ticket: int,
    ) -> Optional[int]:
        """
        Return the actual follower PENDING orderId.

        This deliberately does NOT return the fallback market orderId.
        """
        order_map = self.order_maps.get(account_name) or {}
        return order_map.get(int(mt5_ticket))

    def get_position_volume(
        self,
        account_name: str,
        position_id: int,
    ) -> Optional[int]:
        volume_map = self.position_volumes.get(account_name) or {}

        return volume_map.get(
            int(position_id),
        )

    def get_ticket_volume(
        self,
        account_name: str,
        mt5_ticket: int,
    ) -> Optional[int]:
        position_id = self.get_position_id(
            account_name,
            mt5_ticket,
        )

        if not position_id:
            return None

        return self.get_position_volume(
            account_name,
            position_id,
        )

    def get_account_name_by_magic(
        self,
        magic: int,
    ) -> Optional[str]:
        try:
            return self.route_magic_map.get(
                int(magic),
            )
        except Exception:
            return None

    def get_account_context_by_magic(
        self,
        magic: int,
    ) -> Tuple[
        Optional[str],
        Optional[CTraderClient],
        Optional[AccountConfig],
    ]:
        account_name = self.get_account_name_by_magic(magic)

        if not account_name:
            return None, None, None

        return (
            account_name,
            self.get_client(account_name),
            self.get_config(account_name),
        )

    # ======================================================================
    # MT5 PAYLOAD
    # ======================================================================

    def store_mt5_payload(
        self,
        account_name: str,
        mt5_ticket: int,
        payload: dict,
    ):
        try:
            self._ensure_account_maps(account_name)

            self.mt5_payloads[account_name][int(mt5_ticket)] = dict(
                payload or {},
            )

        except Exception:
            logger.debug(
                "[%s] Failed to store MT5 payload for ticket %s",
                account_name,
                mt5_ticket,
                exc_info=True,
            )

    def store_mt5_payload_by_magic(
        self,
        magic: int,
        mt5_ticket: int,
        payload: dict,
    ) -> bool:
        account_name = self.get_account_name_by_magic(magic)

        if not account_name:
            return False

        self.store_mt5_payload(
            account_name,
            mt5_ticket,
            payload,
        )

        return True

    # ======================================================================
    # REMOVE ALL MAPPING
    # ======================================================================

    def remove_mapping(
        self,
        account_name: str,
        mt5_ticket: int,
    ):
        """
        Remove all bridge state for a fully closed/cancelled MT5 ticket.

        This includes:

            canonical position
            pending-origin position
            fallback market position
            pending order
            fallback market order
            pending type
            MT5 payload
        """
        try:
            ticket = int(mt5_ticket)

            self._remove_order_mapping(
                account_name,
                ticket,
            )

            self._remove_fallback_market_order_mapping(
                account_name,
                ticket,
            )

            self._remove_position_mapping(
                account_name,
                ticket,
            )

            self._remove_pending_position_mapping(
                account_name,
                ticket,
            )

            self._remove_fallback_market_position_mapping(
                account_name,
                ticket,
            )

            self.remove_pending_type(
                account_name,
                ticket,
            )

            self.mt5_payloads.get(
                account_name,
                {},
            ).pop(
                ticket,
                None,
            )

            logger.info(
                "[%s] Removed ALL bridge mappings for MT5 ticket %s",
                account_name,
                ticket,
            )

        except Exception:
            logger.debug(
                "[%s] Failed removing mappings for ticket %s",
                account_name,
                mt5_ticket,
                exc_info=True,
            )

    # ======================================================================
    # ALL ACCOUNTS
    # ======================================================================

    def get_all_accounts(
        self,
    ) -> Dict[
        str,
        Tuple[CTraderClient, AccountConfig],
    ]:
        return {
            account_name: (
                self.clients[account_name],
                self.configs[account_name],
            )
            for account_name in self.clients.keys()
            if account_name in self.configs
        }

    # ======================================================================
    # BACKWARD-COMPATIBLE ALIASES
    # ======================================================================

    def getpositionid(
        self,
        account_name: str,
        mt5_ticket: int,
    ) -> Optional[int]:
        return self.get_position_id(
            account_name,
            mt5_ticket,
        )

    def getorderid(
        self,
        account_name: str,
        mt5_ticket: int,
    ) -> Optional[int]:
        return self.get_order_id(
            account_name,
            mt5_ticket,
        )

    def getpositionvolume(
        self,
        account_name: str,
        position_id: int,
    ) -> Optional[int]:
        return self.get_position_volume(
            account_name,
            position_id,
        )

    def removemapping(
        self,
        account_name: str,
        mt5_ticket: int,
    ):
        self.remove_mapping(
            account_name,
            mt5_ticket,
        )

    def getallaccounts(
        self,
    ) -> Dict[
        str,
        Tuple[CTraderClient, AccountConfig],
    ]:
        return self.get_all_accounts()

    def getaccountnamebymagic(
        self,
        magic: int,
    ) -> Optional[str]:
        return self.get_account_name_by_magic(magic)

    def getaccountcontextbymagic(
        self,
        magic: int,
    ) -> Tuple[
        Optional[str],
        Optional[CTraderClient],
        Optional[AccountConfig],
    ]:
        return self.get_account_context_by_magic(magic)


# ==========================================================================
# SINGLETON
# ==========================================================================

_manager_instance = None


def get_account_manager() -> AccountManager:
    global _manager_instance

    if _manager_instance is None:
        _manager_instance = AccountManager()

    return _manager_instance
