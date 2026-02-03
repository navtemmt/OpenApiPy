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
