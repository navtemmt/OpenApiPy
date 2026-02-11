"""Global application state and logging configuration.
Shared state for the MT5 to cTrader bridge server.
"""
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Global pending SL/TP map: ticket -> dict(symbol, sl, tp)
PENDING_SLTP = {}

# --- PATCH: pending lifecycle support ---
# Track live pending mapping to allow cancellation on PENDING_CLOSE.
# mt5_ticket -> dict(symbol, side, pending_type, volume, label, ctrader_order_id, created_ts)
PENDING_MAP = {}

# Simple dedupe to avoid double-processing when MT5 sends both TT-DELETE and polling close.
# (mt5_ticket, event_type) -> last_seen_epoch_ms
EVENT_DEDUPE = {}
DEDUPE_WINDOW_MS = 1500
