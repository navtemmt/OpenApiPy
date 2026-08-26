"""Global application state and logging configuration.
Shared state for the MT5 to cTrader bridge server.
"""
import logging
import sys

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

if not root_logger.handlers:
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root_logger.addHandler(console_handler)
else:
    for handler in root_logger.handlers:
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))

logger = logging.getLogger("mt5_ctrader_bridge")
logger.setLevel(logging.INFO)
logger.propagate = True


def log_trade_critical(account_name: str, action: str, ticket: int, exc: Exception, **context):
    ctx = " ".join(f"{k}={v}" for k, v in context.items() if v is not None)
    logger.error(
        f"[{account_name}] CRITICAL trade sync failure | action={action} ticket={ticket} {ctx} error={exc}"
    )
    logger.exception(
        f"[{account_name}] TRACEBACK | action={action} ticket={ticket} {ctx}"
    )


# Global pending SL/TP map:
# account_name -> {mt5_ticket -> dict(symbol, sl, tp, created_ms, attempts, next_retry_ms, last_error)}
PENDING_SLTP = {}

# --- PATCH: pending lifecycle support ---
# Track live pending mapping to allow cancellation on PENDING_CLOSE.
# mt5_ticket -> dict(symbol, side, pending_type, volume, label, ctrader_order_id, created_ts)
PENDING_MAP = {}

# Simple dedupe to avoid double-processing when MT5 sends both TT-DELETE and polling close.
# (mt5_ticket, event_type) -> last_seen_epoch_ms
EVENT_DEDUPE = {}
DEDUPE_WINDOW_MS = 1500

# --- PATCH: close-proportional support (for FIXED_LOT / FIXED_USD / PERCENT_EQUITY) ---
# Store the master (MT5) original OPEN lots so we can compute partial-close percent later:
# pct = close_lots / master_open_lots
# mt5_ticket -> float lots
MASTER_OPEN_LOTS = {}

# Track cumulative lots closed on the master side so we can base each proportional
# follower close on the *remaining* master size instead of the original size.
# mt5_ticket -> float lots_closed_so_far
MASTER_CLOSED_LOTS = {}
